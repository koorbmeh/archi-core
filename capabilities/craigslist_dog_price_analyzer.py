"""Craigslist dog price analyzer.

Fetches Craigslist RSS feed for dog and puppy listings every 2 hours, parses prices
and details, stores in local SQLite database, computes rolling statistics (7d/30d
averages, counts, ranges), identifies new listings matching keywords, and notifies
via Discord on updates and potential deals.
"""

import asyncio
import sqlite3
import time
import re
import email.utils
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any

try:
    from capabilities.discord_notifier import notify
except ImportError:
    def notify(text: str) -> bool:
        print(text)
        return True

from src.kernel.periodic_registry import periodic_registry


def get_db_path() -> Path:
    return Path("data") / "craigslist_dogs.db"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            link TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            price REAL,
            posted_ts REAL,
            fetch_time REAL NOT NULL
        )
    """)
    conn.commit()


def fetch_and_parse_rss(url: str) -> List[Dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            if response.status != 200:
                return []
            tree = ET.parse(response)
        root = tree.getroot()
        channel = root.find("channel")
        if channel is None:
            return []
        items: List[Dict[str, str]] = []
        for item in channel.findall("item"):
            title = item.find("title").text or ""
            desc = item.find("description").text or ""
            link = item.find("link").text or ""
            pubdate_str = item.find("pubDate").text or ""
            items.append({"title": title, "description": desc, "link": link, "pubdate_str": pubdate_str})
        return items
    except Exception:
        return []


def parse_listing(title: str, desc: str, link: str, pubdate_str: str) -> Dict[str, Any]:
    listing: Dict[str, Any] = {"title": title, "description": desc, "link": link}
    full_text = f"{title} {desc}"
    price_match = re.search(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", full_text)
    if price_match:
        price_str = price_match.group(1).replace(",", "")
        listing["price"] = float(price_str)
    try:
        dt = email.utils.parsedate_tz(pubdate_str)
        if dt is not None:
            tt = dt[:6]
            offset = dt[9] or 0
            listing["posted_ts"] = time.mktime(tt) - offset / 3600.0
        else:
            listing["posted_ts"] = 0.0
    except Exception:
        listing["posted_ts"] = 0.0
    return listing


def filter_and_extract(items: List[Dict[str, str]], keywords: List[str], fetch_time: float) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for item in items:
        parsed = parse_listing(**item)
        full_text = f"{parsed['title']} {parsed['description']}".lower()
        if any(kw.lower() in full_text for kw in keywords):
            parsed["fetch_time"] = fetch_time
            results.append(parsed)
    return results


def upsert_listings(conn: sqlite3.Connection, listings: List[Dict[str, Any]]) -> None:
    for listing in listings:
        conn.execute(
            """
            INSERT OR REPLACE INTO listings (link, title, description, price, posted_ts, fetch_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                listing["link"],
                listing["title"],
                listing["description"],
                listing.get("price"),
                listing.get("posted_ts"),
                listing["fetch_time"],
            ),
        )
    conn.commit()


def compute_stats(
    conn: sqlite3.Connection, cutoff_30d: float, cutoff_7d: float, profile: Dict[str, Any]
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    cur = conn.execute("SELECT price FROM listings WHERE fetch_time > ? AND price IS NOT NULL", (cutoff_7d,))
    prices_7d = [row[0] for row in cur.fetchall()]
    if prices_7d:
        stats["avg_price_7d"] = sum(prices_7d) / len(prices_7d)
        stats["median_price_7d"] = sorted(prices_7d)[len(prices_7d) // 2]
        stats["min_price_7d"] = min(prices_7d)
        stats["max_price_7d"] = max(prices_7d)
        stats["count_7d"] = len(prices_7d)
    else:
        stats["avg_price_7d"] = 0
        stats["count_7d"] = 0
    cur = conn.execute("SELECT price FROM listings WHERE fetch_time > ? AND price IS NOT NULL", (cutoff_30d,))
    prices_30d = [row[0] for row in cur.fetchall()]
    if prices_30d:
        stats["avg_price_30d"] = sum(prices_30d) / len(prices_30d)
        stats["count_30d"] = len(prices_30d)
    else:
        stats["avg_price_30d"] = 0
        stats["count_30d"] = 0
    max_p = profile.get("max_price")
    if max_p and prices_7d:
        low_prices_7d = [p for p in prices_7d if p <= max_p]
        if low_prices_7d:
            stats["avg_low_price_7d"] = sum(low_prices_7d) / len(low_prices_7d)
    return stats


def build_message(stats: Dict[str, Any], new_listings: List[Dict[str, Any]], keywords: List[str]) -> str:
    lines = ["**Craigslist Dog Price Analyzer Update**"]
    lines.append(f"Listings (7d): {stats.get('count_7d', 0)} | Avg: ${stats.get('avg_price_7d', 0):.0f}")
    lines.append(f"Listings (30d): {stats.get('count_30d', 0)} | Avg: ${stats.get('avg_price_30d', 0):.0f}")
    if "min_price_7d" in stats:
        lines.append(f"7d Range: ${stats['min_price_7d']:.0f} - ${stats['max_price_7d']:.0f}")
    if new_listings:
        lines.append(f"\n**New listings ({len(new_listings)}):**")
        for listing in new_listings[:10]:
            price = f"${listing.get('price', 'N/A'):.0f}" if isinstance(listing.get('price'), (int, float)) else "No price"
            title_snip = listing["title"][:60] + "..." if len(listing["title"]) > 60 else listing["title"]
            lines.append(f"{title_snip} | {price}")
            lines.append(listing["link"])
    else:
        lines.append("No new listings this scan.")
    if "avg_low_price_7d" in stats:
        lines.append(f"Avg good deals (7d <= ${stats['avg_low_price_7d']:.0f}")
    return "\n".join(lines)


async def analyze_and_notify() -> None:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = "https://seattle.craigslist.org/search/sss?format=rss&query=dogs+puppies"
    keywords = ["lab", "golden", "poodle", "retriever", "doodle", "puppy"]
    profile = {"max_price": 1500}
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        items = fetch_and_parse_rss(url)
        fetch_time = time.time()
        all_listings = filter_and_extract(items, keywords, fetch_time)
        existing_links = {row[0] for row in conn.execute("SELECT link FROM listings")}
        new_listings = [l for l in all_listings if l["link"] not in existing_links]
        upsert_listings(conn, all_listings)
        cutoff_7d = fetch_time - 7 * 86400
        cutoff_30d = fetch_time - 30 * 86400
        stats = compute_stats(conn, cutoff_30d, cutoff_7d, profile)
        msg = build_message(stats, new_listings, keywords)
        notify(msg)
    finally:
        conn.close()


def initialize() -> None:
    periodic_registry.register(
        name="craigslist_dog_price_analyzer",
        module="capabilities.craigslist_dog_price_analyzer",
        coroutine="analyze_and_notify",
        interval_seconds=7200,
    )


initialize()