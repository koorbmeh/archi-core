"""Scans Craigslist RSS feeds for madison.craigslist.org pets/dogs listings matching user keywords
from personal_profile_manager, analyzes prices/contacts/dates/location relevance, and sends
periodic Discord DM summaries with action links for rehoming/resale."""

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set

import feedparser

from capabilities.discord_notifier import notify_async
from capabilities.event_loop import EventLoop, PeriodicTask


class CraigslistPetScanner:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {'seen_guids': [], 'last_scan': 0.0}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def _get_keywords(self) -> List[str]:
        profile_path = Path('data') / 'personal_profile.json'
        try:
            profile = json.loads(profile_path.read_text())
            return profile.get('dog_keywords', ['border collie', 'puppy'])
        except (json.JSONDecodeError, FileNotFoundError):
            return ['border collie', 'puppy']

    def _get_rss_urls(self) -> List[str]:
        return ['https://madison.craigslist.org/search/dog/rss']

    async def periodic_scan_coro(self) -> None:
        keywords = self._get_keywords()
        urls = self._get_rss_urls()
        seen: Set[str] = set(self.state['seen_guids'])
        new_matches: List[Dict[str, Any]] = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
                for entry in getattr(feed, 'entries', []):
                    guid = entry.get('guid') or entry.get('id') or entry.get('link', '')
                    if not guid or guid in seen:
                        continue
                    title_lower = entry.get('title', '').lower()
                    summary_lower = entry.get('summary', '').lower()
                    if any(kw.lower() in title_lower or kw.lower() in summary_lower for kw in keywords):
                        match = self._analyze_entry(entry)
                        new_matches.append(match)
                        seen.add(guid)
            except Exception:  # nosec
                pass
        if new_matches:
            summary_md = self._build_summary(new_matches)
            await notify_async(summary_md)
        self.state['seen_guids'] = list(seen)
        self.state['last_scan'] = time.time()
        self._save_state()

    def _analyze_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        title = entry.get('title', '')
        link = entry.get('link', '')
        summary = entry.get('summary', '')[:500]
        price_match = re.search(r'\$([0-9,]+)', summary)
        price = price_match.group(1) if price_match else 'N/A'
        pub_parsed = entry.get('published_parsed')
        date_str = 'N/A'
        if pub_parsed:
            try:
                dt = datetime(*pub_parsed[:6])
                date_str = dt.strftime('%m/%d %H:%M')
            except ValueError:
                pass
        loc_match = re.search(
            r'(madison|waunakee|verona|oregon|fitchburg|sun prairie|middleton|wi\s+\d{5}|\d{5})',
            summary.lower()
        )
        location = loc_match.group(1).title() if loc_match else 'Madison area'
        return {
            'title': title,
            'price': price,
            'date': date_str,
            'location': location,
            'snippet': summary,
            'link': link,
        }

    def _build_summary(self, matches: List[Dict[str, Any]]) -> str:
        lines = ["🚀 **New Dog Listings Matching Your Keywords!** 🚀\n\n"]
        for match in matches:
            lines.append(f"🐕 **{match['title']}**\n")
            lines.append(f"💰 Price: ${match['price']}\n")
            lines.append(f"📍 {match['location']}\n")
            lines.append(f"📅 Posted: {match['date']}\n")
            lines.append(f"📄 {match['snippet'][:250]}...\n")
            lines.append(f"[👀 View & Reply]({match['link']})\n\n")
        lines.append("---\n*Powered by Archi*")
        return ''.join(lines)


_scanner: CraigslistPetScanner | None = None


def get_scanner(data_dir: Path = Path('data')) -> CraigslistPetScanner:
    global _scanner
    if _scanner is None:
        state_path = data_dir / 'craigslist_pet_scanner_state.json'
        _scanner = CraigslistPetScanner(state_path)
    return _scanner


def integrate_with_event_loop(loop: EventLoop) -> None:
    def coro_factory():
        return get_scanner().periodic_scan_coro()
    loop.periodic_tasks.append(PeriodicTask(
        name='craigslist_pet_scanner',
        coro_factory=coro_factory,
        interval=3600.0  # hourly
    ))