"""
Parses Madison Craigslist pets/dogs RSS feeds for Border Collie or specified breed keywords from Jesse's
profile, extracts prices/ages/conditions via regex, computes median price/listing volume/trends stored
in local SQLite DB, and sends periodic Discord DMs with summaries and pricing recommendations.
"""

import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from email import utils
from pathlib import Path
from typing import Dict, List, Any

from capabilities.discord_notifier import notify_async
from capabilities.personal_profile_manager import get_manager


def get_db_path() -> Path:
    db_path = Path('data') / 'craigslist_dogs.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS listings (
            guid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            description TEXT,
            pubdate REAL,
            fetch_time REAL NOT NULL,
            price INTEGER,
            age_months REAL,
            condition TEXT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_fetch_time ON listings(fetch_time)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_age ON listings(age_months)')
    conn.commit()


def parse_listing(title: str, desc: str, link: str, pubdate_str: str) -> Dict[str, Any]:
    full_text = f"{title} {desc}".lower()
    price_match = re.search(r'\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', full_text)
    price = int(price_match.group(1).replace(',', '')) if price_match else None

    age_match = re.search(r'(\d+(?:\.\d+)?)\s*(weeks?|months?|years?|mos?)\b', full_text, re.I)
    age_months = None
    if age_match:
        num = float(age_match.group(1))
        unit = age_match.group(2).lower()
        if 'week' in unit:
            age_months = num / 4.345
        elif 'month' in unit or 'mo' in unit:
            age_months = num
        elif 'year' in unit:
            age_months = num * 12

    cond_keywords = ['purebred', 'akc', 'registered', 'health guarantee', 'up to date', 'shots', 'vet checked']
    conditions = [kw for kw in cond_keywords if kw in full_text]
    condition = ', '.join(conditions) if conditions else None

    pub_ts = utils.mktime_tz(utils.parsedate_tz(pubdate_str)) if pubdate_str else None
    return {
        'price': price,
        'age_months': age_months,
        'condition': condition,
        'pubdate': pub_ts
    }


def fetch_and_parse_rss(url: str) -> List[Dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            tree = ET.parse(resp)
        root = tree.getroot()
        items = []
        for item in root.iterfind('.//item'):
            title = item.findtext('title', '')
            link = item.findtext('link', '')
            desc = item.findtext('description', '')
            pubdate_str = item.findtext('pubDate', '')
            guid = item.findtext('guid', link)
            items.append({'title': title, 'link': link, 'desc': desc, 'pubdate_str': pubdate_str, 'guid': guid})
        return items
    except Exception:
        return []


def filter_and_extract(items: List[Dict[str, str]], keywords: List[str], fetch_time: float) -> List[Dict[str, Any]]:
    filtered = []
    for item in items:
        full_lower = (item['title'] + ' ' + item['desc']).lower()
        if any(kw in full_lower for kw in keywords):
            parsed = parse_listing(item['title'], item['desc'], item['link'], item['pubdate_str'])
            listing = {
                'guid': item['guid'],
                'title': item['title'],
                'link': item['link'],
                'desc': item['desc'],
                'fetch_time': fetch_time,
                **parsed
            }
            filtered.append(listing)
    return filtered


def upsert_listings(conn: sqlite3.Connection, listings: List[Dict[str, Any]]) -> None:
    cur = conn.cursor()
    for l in listings:
        cur.execute('''
            INSERT OR REPLACE INTO listings
            (guid, title, link, description, pubdate, fetch_time, price, age_months, condition)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            l['guid'], l['title'], l['link'], l['desc'], l['pubdate'], l['fetch_time'],
            l['price'], l['age_months'], l['condition']
        ))
    conn.commit()


def compute_stats(conn: sqlite3.Connection, cutoff_30d: float, cutoff_7d: float, profile: Dict[str, Any]) -> Dict[str, Any]:
    stats = {}
    prices_30d = [row[0] for row in conn.execute(
        'SELECT price FROM listings WHERE fetch_time > ? AND price > 0', (cutoff_30d,)
    )]
    stats['count_30d'] = len(prices_30d)
    if prices_30d:
        prices_30d.sort()
        stats['median_30d'] = prices_30d[len(prices_30d) // 2]

    prices_7d = [row[0] for row in conn.execute(
        'SELECT price FROM listings WHERE fetch_time > ? AND price > 0', (cutoff_7d,)
    )]
    stats['count_7d'] = len(prices_7d)

    puppy_age_months = profile.get('puppy_age_months') or (profile.get('puppy_age_weeks', 0) / 4.345)
    if puppy_age_months and puppy_age_months > 0:
        low = max(0, puppy_age_months * 0.5)
        high = puppy_age_months * 1.5
        similar_prices = [row[0] for row in conn.execute(
            'SELECT price FROM listings WHERE age_months BETWEEN ? AND ? AND price > 0 AND fetch_time > ?',
            (low, high, cutoff_30d)
        )]
        stats['similar_count'] = len(similar_prices)
        if similar_prices:
            similar_prices.sort()
            stats['median_similar'] = similar_prices[len(similar_prices) // 2]

    stats['puppy_age_months'] = puppy_age_months
    stats['puppy_breed'] = profile.get('puppy_breed', 'puppy')
    return stats


def build_message(stats: Dict[str, Any], new_listings: List[Dict[str, Any]], keywords: List[str]) -> str:
    msg = "🚨 Craigslist Dog Price Analyzer - Madison\n\n"
    msg += f"Keywords: {', '.join(keywords[:5])}\n\n"
    msg += f"📊 Last 30d: {stats['count_30d']} listings"
    if med := stats.get('median_30d'):
        msg += f", median ${med:,}"
    msg += "\n"
    msg += f"📈 Last 7d: {stats['count_7d']} listings\n"

    if 'median_similar' in stats:
        msg += f"🐕 Similar age (~{stats['puppy_age_months']:.1f}mo): {stats['similar_count']} listings, median ${stats['median_similar']:,}\n"

    msg += f"\n💡 Rec for {stats['puppy_breed']}: "
    if med_sim := stats.get('median_similar'):
        rec_price = int(med_sim * 0.9)
        msg += f"List ~${rec_price:,} (10% below similar median)"
    else:
        msg += "Gather more data or price conservatively"

    if new_listings:
        msg += f"\n\n🔥 New matches ({len(new_listings)}):\n"
        for l in new_listings[:5]:
            price_str = f"${l['price']:,}" if l['price'] else "?"
            age_str = f"{l['age_months']:.1f}mo" if l['age_months'] else "?"
            msg += f"• {l['title'][:55]}... | {price_str} | {age_str}\n  {l['link']}\n"

    return msg


async def analyze_and_notify() -> None:
    url = 'https://madison.craigslist.org/search/pet?format=rss'
    manager = get_manager()
    profile = manager.get_profile()
    keywords = [k.lower() for k in profile.get('desired_dog_breeds', ['border collie', 'puppy'])]
    fetch_time = time.time()

    items = fetch_and_parse_rss(url)
    filtered = filter_and_extract(items, keywords, fetch_time)

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        if filtered:
            upsert_listings(conn, filtered)
        cutoff_30d = fetch_time - 30 * 86400
        cutoff_7d = fetch_time - 7 * 86400
        stats = compute_stats(conn, cutoff_30d, cutoff_7d, profile)
        message = build_message(stats, filtered, keywords)
        await notify_async(message)
    finally:
        conn.close()


def initialize() -> None:
    from src.kernel.periodic_registry import register
    register('craigslist_dog_price_analyzer', __name__, 'analyze_and_notify', 21600)