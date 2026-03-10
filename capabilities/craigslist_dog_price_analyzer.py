"""
Craigslist Dog Price Analyzer.

Periodically scans Craigslist RSS for dog and puppy listings matching keywords,
parses prices and attributes, stores in SQLite DB, computes price statistics over
30-day and 7-day windows, identifies potential deals, and notifies via Discord.
Self-registers as a 6-hour periodic task.
"""

import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Any, List

from capabilities.discord_notifier import notify

# Constants
DATA_DIR = Path('data')
DB_PATH = DATA_DIR / 'craigslist_dogs.db'
RSS_URL = 'https://seattle.craigslist.org/search/sss?format=rss&query=dog+puppy'
KEYWORDS = [
    'puppy', 'dog', 'labradoodle', 'goldendoodle', 'bernedoodle',
    'cavapoo', 'cockapoo', 'lab', 'golden', 'poodle'
]
PROFILE: Dict[str, Any] = {
    'max_price': 2500,
    'min_age_weeks': 6,
    'target_breeds': ['labradoodle', 'goldendoodle', 'bernedoodle']
}

def get_db_path() -> Path:
    """Return the SQLite database path, creating data dir if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DB_PATH

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create listings table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            link TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            price INTEGER,
            age_weeks INTEGER,
            breed TEXT,
            pubdate_str TEXT,
            fetch_time REAL NOT NULL
        )
    """)
    conn.commit()

def parse_listing(title: str, desc: str, link: str, pubdate_str: str) -> Dict[str, Any]:
    """Parse price, age, breed from title/desc."""
    text = f"{title} {desc}".lower()
    # Price
    price_match = re.search(r'\$?([\d,]+(?:\.\d{2})?)', text)
    price = None
    if price_match:
        price_str = price_match.group(1).replace(',', '')
        try:
            price = int(float(price_str))
        except ValueError:
            pass
    # Age weeks
    age_weeks = None
    age_match = re.search(r'(\d+(?:\.\d+)?)\s*(week|wk|month|mo|mos|day|days?)\b', text)
    if age_match:
        num = float(age_match.group(1))
        unit = age_match.group(2).lower()[:2]
        if unit.startswith('we'):
            age_weeks = round(num)
        elif unit.startswith('mo'):
            age_weeks = round(num * 4.33)
        elif unit.startswith('da'):
            age_weeks = round(num / 7)
    # Breed
    breed = 'unknown'
    for b in PROFILE['target_breeds']:
        if b.lower() in text:
            breed = b
            break
    return {
        'link': link,
        'title': title,
        'description': desc,
        'price': price,
        'age_weeks': age_weeks,
        'breed': breed,
        'pubdate_str': pubdate_str,
    }

def fetch_and_parse_rss(url: str) -> List[Dict[str, str]]:
    """Fetch and parse Craigslist RSS feed into list of item dicts."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            tree = ET.parse(resp)
        root = tree.getroot()
        items = []
        for item_elem in root.findall('.//item'):
            title_elem = item_elem.find('title')
            link_elem = item_elem.find('link')
            desc_elem = item_elem.find('description')
            pubdate_elem = item_elem.find('pubDate')
            title = title_elem.text or ''
            link = link_elem.text or ''
            description = (desc_elem.text or '') if desc_elem is not None else ''
            pubdate_str = pubdate_elem.text or ''
            items.append({
                'title': title,
                'description': description,
                'link': link,
                'pubdate_str': pubdate_str,
            })
        return items
    except Exception:
        return []

def filter_and_extract(
    items: List[Dict[str, str]], keywords: List[str], fetch_time: float
) -> List[Dict[str, Any]]:
    """Filter items matching keywords, parse, and apply profile filters."""
    listings = []
    for item in items:
        if any(kw.lower() in item['title'].lower() for kw in keywords):
            parsed = parse_listing(
                item['title'], item['description'], item['link'], item['pubdate_str']
            )
            if (
                parsed['price'] is not None
                and parsed['price'] <= PROFILE['max_price']
                and parsed['age_weeks'] is not None
                and parsed['age_weeks'] >= PROFILE['min_age_weeks']
            ):
                parsed['fetch_time'] = fetch_time
                listings.append(parsed)
    return listings

def upsert_listings(conn: sqlite3.Connection, listings: List[Dict[str, Any]]) -> None:
    """Upsert parsed listings into DB."""
    for listing in listings:
        conn.execute("""
            INSERT OR REPLACE INTO listings
            (link, title, description, price, age_weeks, breed, pubdate_str, fetch_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            listing['link'],
            listing['title'],
            listing['description'],
            listing['price'],
            listing['age_weeks'],
            listing['breed'],
            listing['pubdate_str'],
            listing['fetch_time'],
        ))
    conn.commit()

def compute_stats(
    conn: sqlite3.Connection, cutoff_30d: float, cutoff_7d: float, profile: Dict[str, Any]
) -> Dict[str, Any]:
    """Compute price stats for 30d window and recent activity."""
    min_age = profile.get('min_age_weeks', 0)
    # 30d prices
    cur = conn.execute("""
        SELECT price FROM listings
        WHERE fetch_time >= ? AND price IS NOT NULL AND age_weeks >= ?
    """, (cutoff_30d, min_age))
    prices_30d = [row[0] for row in cur.fetchall()]
    stats: Dict[str, Any] = {'count_30d': len(prices_30d), 'deals_7d': []}
    if prices_30d:
        prices_30d.sort()
        stats.update({
            'avg_price_30d': sum(prices_30d) / len(prices_30d),
            'median_price_30d': prices_30d[len(prices_30d) // 2],
            'min_price_30d': prices_30d[0],
            'max_price_30d': prices_30d[-1],
        })
        # 7d deals <80% avg
        avg = stats['avg_price_30d']
        cur = conn.execute("""
            SELECT title, price, link FROM listings
            WHERE fetch_time >= ? AND price < ? AND price IS NOT NULL
            ORDER BY price ASC
        """, (cutoff_7d, avg * 0.8))
        stats['deals_7d'] = cur.fetchall()
    else:
        for key in ['avg_price_30d', 'median_price_30d', 'min_price_30d', 'max_price_30d']:
            stats[key] = 0
    # 7d new count
    cur = conn.execute("SELECT COUNT(*) FROM listings WHERE fetch_time >= ?", (cutoff_7d,))
    stats['new_count_7d'] = cur.fetchone()[0]
    return stats

def build_message(
    stats: Dict[str, Any],
    new_listings: List[Dict[str, Any]],
    keywords: List[str],
) -> str:
    """Build formatted Discord notification message."""
    lines = ["**🐕 Craigslist Dog Price Analyzer Update**"]
    lines.append(f"• Analyzed (30d): {stats['count_30d']} listings")
    avg = stats['avg_price_30d']
    if avg > 0:
        lines.append(
            f"• Avg/Median 30d: ${avg:.0f} / ${stats['median_price_30d']:.0f}"
        )
        lines.append(f"• Range 30d: ${stats['min_price_30d']:.0f}–${stats['max_price_30d']:.0f}")
    lines.append(f"• New listings (7d): {stats['new_count_7d']}")
    deals = stats['deals_7d']
    if deals:
        lines.append(f"**💰 Deals (<80% avg, 7d: {len(deals)})**")
        for title, price, link in deals[:5]:  # Top 5
            lines.append(f"  ${price} | {title[:50]}…\n  {link}")
    if new_listings:
        lines.append(f"**🆕 New matches ({len(new_listings)})**")
        for l in new_listings[:5]:
            age = f"{l['age_weeks']}w" if l['age_weeks'] else "?"
            lines.append(
                f"  ${l['price']} {age} {l['breed']} | {l['title'][:50]}…"
            )
    lines.append("\nKeywords: " + ", ".join(keywords))
    return "\n".join(lines)

async def analyze_and_notify() -> None:
    """Main coroutine: fetch, parse, store, analyze, notify."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        items = fetch_and_parse_rss(RSS_URL)
        fetch_time = time.time()
        new_listings = filter_and_extract(items, KEYWORDS, fetch_time)
        upsert_listings(conn, new_listings)
        cutoff_30d = fetch_time - 30 * 86400
        cutoff_7d = fetch_time - 7 * 86400
        stats = compute_stats(conn, cutoff_30d, cutoff_7d, PROFILE)
        message = build_message(stats, new_listings, KEYWORDS)
        notify(message)
    except Exception as exc:
        print(f"Craigslist analyzer error: {exc}")
    finally:
        conn.close()

def initialize() -> None:
    """Register as periodic task on module load."""
    try:
        from src.kernel.periodic_registry import periodic_registry
        periodic_registry.register(
            'craigslist_dog_price_analyzer_scan',
            'capabilities.craigslist_dog_price_analyzer',
            'analyze_and_notify',
            21600  # 6 hours
        )
    except Exception as exc:
        print(f"Periodic registration failed: {exc}")

# Self-initialize on import
initialize()