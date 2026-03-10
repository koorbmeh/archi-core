"""Craigslist Dog Price Analyzer.

Fetches dog listings from Craigslist RSS feeds in major cities, filters for specific breeds/keywords,
stores parsed data (including prices) in SQLite DB, computes rolling statistics (30d/7d avg, min, count),
identifies new listings, and sends periodic Discord notifications on updates and potential deals.
Self-registers as a 6-hour periodic task.
"""

import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

from capabilities.discord_notifier import notify_async

_initialized: bool = False
KEYWORDS: List[str] = [
    'labrador', 'golden retriever', 'poodle', 'beagle', 'bulldog',
    'german shepherd', 'pomeranian', 'chihuahua', 'husky', 'dachshund'
]
RSS_URLS: List[str] = [
    'https://sfbay.craigslist.org/search/pet?format=rss&query=dog',
    'https://losangeles.craigslist.org/search/pet?format=rss&query=dog',
    'https://newyork.craigslist.org/search/pet?format=rss&query=dog',
]
MAX_PRICE: int = 2500


def get_db_path() -> Path:
    data_dir = Path('data')
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / 'craigslist_dogs.db'


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            link TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            price INTEGER,
            pubdate REAL,
            fetch_time REAL
        )
    """)
    conn.commit()


def parse_listing(title: str, desc: str, link: str, pubdate_str: str) -> Dict[str, Any]:
    price_match = re.search(r'\$(\d+(?:,\d{3})*(?:\.\d{2})?)', f"{title} {desc}", re.IGNORECASE)
    price_str = price_match.group(1).replace(',', '') if price_match else ''
    price = int(float(price_str)) if price_str else None

    pubdate = 0.0
    if pubdate_str:
        try:
            dt = datetime.strptime(pubdate_str.rstrip(), '%a, %d %b %Y %H:%M:%S %z')
            pubdate = dt.timestamp()
        except ValueError:
            pass

    return {
        'link': link,
        'title': title,
        'description': desc,
        'price': price,
        'pubdate': pubdate,
    }


def fetch_and_parse_rss(url: str) -> List[Dict[str, str]]:
    req = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tree = ET.parse(BytesIO(resp.read()))
        items = []
        for item in tree.findall('.//item'):
            title = item.find('title').text or ''
            link = item.find('link').text or ''
            pubdate_str = item.find('pubDate').text or ''
            desc = item.find('description').text or ''
            items.append({'title': title, 'link': link, 'pubdate': pubdate_str, 'description': desc})
        return items
    except Exception:
        return []


def filter_and_extract(items: List[Dict[str, str]], keywords: List[str], fetch_time: float) -> List[Dict[str, Any]]:
    filtered = []
    for item in items:
        if any(kw.lower() in item['title'].lower() for kw in keywords):
            parsed = parse_listing(
                item['title'], item['description'], item['link'], item['pubdate']
            )
            parsed['fetch_time'] = fetch_time
            filtered.append(parsed)
    return filtered


def upsert_listings(conn: sqlite3.Connection, listings: List[Dict[str, Any]]) -> None:
    for listing in listings:
        conn.execute("""
            INSERT OR REPLACE INTO listings
            (link, title, description, price, pubdate, fetch_time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            listing['link'], listing['title'], listing['description'],
            listing['price'], listing['pubdate'], listing['fetch_time']
        ))
    conn.commit()


def compute_stats(
    conn: sqlite3.Connection, cutoff_30d: float, cutoff_7d: float, profile: Dict[str, Any]
) -> Dict[str, Any]:
    max_price = profile.get('max_price', MAX_PRICE)
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM listings
        WHERE pubdate > ? AND price IS NOT NULL AND price <= ?
    """, (cutoff_30d, max_price))
    num_30d = cur.fetchone()[0]

    cur.execute("""
        SELECT AVG(price), MIN(price), MAX(price)
        FROM listings
        WHERE pubdate > ? AND price IS NOT NULL AND price <= ?
    """, (cutoff_30d, max_price))
    row = cur.fetchone()
    avg_price_30d = round(row[0]) if row[0] else None
    min_price_30d = row[1]
    max_price_30d = row[2]

    cur.execute("""
        SELECT COUNT(*)
        FROM listings
        WHERE pubdate > ?
    """, (cutoff_7d,))
    num_7d = cur.fetchone()[0]

    return {
        'num_30d': num_30d,
        'avg_price_30d': avg_price_30d,
        'min_price_30d': min_price_30d,
        'max_price_30d': max_price_30d,
        'num_7d': num_7d,
    }


def build_message(
    stats: Dict[str, Any], new_listings: List[Dict[str, Any]], keywords: List[str]
) -> str:
    msg = "🐕 **Craigslist Dog Price Update** 🐕\n\n"
    msg += f"**30-Day Stats:** {stats['num_30d']} listings\n"
    msg += f"Avg: ${stats['avg_price_30d'] or 'N/A'} | Low: ${stats['min_price_30d'] or 'N/A'}\n"
    msg += f"**New in 7d:** {stats['num_7d']}\n\n"
    msg += "**Recent Matches:**\n"
    for lst in new_listings[:15]:
        price = f"${lst['price']}" if lst['price'] else "Price N/A"
        msg += f"• [{lst['title'][:60]}...]({lst['link']}) {price}\n"
    msg += f"\nKeywords: {', '.join(keywords[:5])}..."
    return msg


async def analyze_and_notify() -> None:
    fetch_time = time.time()
    all_listings: List[Dict[str, Any]] = []
    for url in RSS_URLS:
        items = fetch_and_parse_rss(url)
        filtered = filter_and_extract(items, KEYWORDS, fetch_time)
        all_listings.extend(filtered)

    profile = {'max_price': MAX_PRICE}
    cutoff_30d = fetch_time - 30 * 86400
    cutoff_7d = fetch_time - 7 * 86400

    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        upsert_listings(conn, all_listings)
        stats = compute_stats(conn, cutoff_30d, cutoff_7d, profile)

        cur = conn.cursor()
        cur.execute("""
            SELECT title, price, link
            FROM listings
            WHERE fetch_time > ? AND price <= ?
            ORDER BY fetch_time DESC, price ASC
            LIMIT 20
        """, (cutoff_7d, profile['max_price']))
        new_listings = [
            {'title': row[0], 'price': row[1], 'link': row[2]} for row in cur.fetchall()
        ]

    if new_listings or stats['num_7d'] > 0:
        message = build_message(stats, new_listings, KEYWORDS)
        await notify_async(message)


def initialize() -> None:
    global _initialized
    if _initialized:
        return
    from src.kernel.periodic_registry import periodic_registry
    periodic_registry.register(
        'craigslist_dog_price_analyzer',
        'capabilities.craigslist_dog_price_analyzer',
        'analyze_and_notify',
        21600
    )
    _initialized = True


initialize()