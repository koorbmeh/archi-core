"""Daily wealth tracker capability.

Implements daily Discord DM prompts for wealth metrics (expenses, income, investments,
net worth); parses natural language responses; stores data persistently; integrates
with health metrics for trends; generates weekly summaries, overspending alerts,
optimization suggestions. Compatible with discord_listener queue and event_loop tasks.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.kernel.model_interface import call_model, ModelResponse
from src.kernel.capability_registry import CapabilityRegistry, Capability

from capabilities.conversational_memory import get_context, store_message
from capabilities.discord_notifier import notify_async
from capabilities.discord_listener import message_queue
from capabilities.event_loop import EventLoop, PeriodicTask
from capabilities.image_ocr import process_discord_attachments, ocr_summary

_storage: Optional['WealthMetricsStorage'] = None
_storage_path: Optional[Path] = None


class WealthMetricsStorage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(exist_ok=True, parents=True)
        self._data: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if self.path.exists():
            with self.path.open() as f:
                return json.loads(f.read())
        return []

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2))

    def add_entry(self, entry: Dict[str, Any]) -> None:
        entry['date'] = datetime.now().isoformat()
        self._data.append(entry)
        self._save()

    def get_entries(self, days: int = 30) -> List[Dict[str, Any]]:
        now = datetime.now()
        recent: List[Dict[str, Any]] = []
        for entry in reversed(self._data):
            try:
                entry_date = datetime.fromisoformat(entry['date'])
                if (now - entry_date).days <= days:
                    recent.append(entry)
            except ValueError:
                continue
        return recent


def get_storage(data_path: Optional[Path] = None) -> WealthMetricsStorage:
    global _storage, _storage_path
    if data_path:
        _storage_path = data_path / 'daily_wealth.json'
    if _storage is None:
        path = _storage_path or Path('data') / 'daily_wealth.json'
        _storage = WealthMetricsStorage(path)
    return _storage


def is_wealth_report(content: str) -> bool:
    keywords = {'expense', 'spent', 'cost', 'income', 'earned', 'salary', 'invest', 'stock', 'net worth', 'savings', 'budget', '$'}
    return bool(keywords.intersection(word.lower() for word in re.split(r'\W+', content)))


def parse_wealth_update(content: str, user_id: str, attachments: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    if attachments:
        ocr_results = process_discord_attachments(attachments)
        content += f"\n[OCR from images: {ocr_summary(ocr_results)}]"
    context = get_context(user_id)
    system_prompt = (
        "Extract structured daily wealth metrics from the user message. Use totals where possible. "
        "Output ONLY valid JSON: {'expenses': float|dict[str,float], 'income': float|0, "
        "'investments': float|0, 'net_worth': float|0, 'notes': str}"
    )
    user_prompt = f"Context:\n{context}\n\nMessage:\n{content}"
    try:
        resp: ModelResponse = call_model(user_prompt, system_prompt)
        data = json.loads(resp.text.strip())
        store_message(user_id, content)
        return data
    except (json.JSONDecodeError, KeyError):
        store_message(user_id, content)
        return None


async def send_daily_prompt() -> bool:
    text = (
        "🌅 Daily wealth check-in!\n\n"
        "Report today's:\n• Expenses (total/breakdown)\n• Income\n• Investments value\n• Net worth\n• Notes\n\n"
        "E.g. 'Spent $60 (food $40, gas $20). Earned $150. Investments $5200. Net worth $28500.'"
    )
    return await notify_async(text)


def generate_weekly_summary() -> str:
    storage = get_storage()
    entries = storage.get_entries(30)
    if not entries:
        return "No wealth data yet. Start reporting daily!"
    health_path = storage.path.parent / 'daily_health.json'
    health_entries: List[Any] = []
    if health_path.exists():
        health_entries = json.loads(health_path.read_text())[-7:]
    prompt = (
        f"Wealth entries (recent):\n{json.dumps(entries[-14:], indent=2)}\n"
        f"Health metrics (balance):\n{json.dumps(health_entries, indent=2)}\n\n"
        "Generate: weekly summary, key trends (avg expense/income, net worth Δ), "
        "overspending alerts (expense >50% income?), optimization suggestions. Encouraging tone."
    )
    system = "Financial advisor: concise, actionable, positive."
    resp: ModelResponse = call_model(prompt, system)
    return resp.text


async def weekly_review_coro() -> None:
    summary = generate_weekly_summary()
    await notify_async(f"📈 Weekly Wealth Review:\n\n{summary}")


async def process_one(repo_path: str, registry: CapabilityRegistry) -> bool:
    try:
        payload = message_queue.get_nowait()
    except asyncio.QueueEmpty:
        return False
    content = payload['content']
    user_id = payload['user_id']
    attachments = payload.get('attachment_urls')
    if not is_wealth_report(content):
        message_queue.put_nowait(payload)
        return False
    data = parse_wealth_update(content, user_id, attachments)
    storage = get_storage(Path(repo_path).parent / 'data')
    if data:
        storage.add_entry(data)
        await notify_async("✅ Wealth update recorded! Keep it up. 💰")
    else:
        await notify_async("❓ Couldn't parse metrics. Use numbers/currency (e.g. $50 food). Try again!")
    return True


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Optional[Capability]:
    from capabilities.discord_listener import processors
    processors.append(process_one)
    if not registry:
        return None
    cap = Capability(
        name='daily_wealth_tracker',
        module='capabilities.daily_wealth_tracker',
        description=__doc__.strip(),
        dependencies=['daily_health_tracker', 'event_loop', 'discord_notifier', 'conversational_memory', 'model_interface']
    )
    registry.add(cap)
    return cap


def integrate_with_event_loop(event_loop: EventLoop, data_path: Optional[Path] = None) -> None:
    get_storage(data_path)
    event_loop.add_periodic_task(PeriodicTask('wealth_daily_prompt', send_daily_prompt, 86400.0))
    event_loop.add_periodic_task(PeriodicTask('wealth_weekly_review', weekly_review_coro, 604800.0))