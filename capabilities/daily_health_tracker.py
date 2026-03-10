"""Implements daily Discord DM prompts for Jesse's health metrics (sleep, steps, water, energy), parses responses from recent conversation history, stores data in JSON with trend analysis, and sends weekly summaries with rule-based insights integrated with conversational context."""

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.event_loop import EventLoop, PeriodicTask
from capabilities.conversational_memory import get_recent_messages
from capabilities.discord_notifier import notify_async


JESSE_DISCORD_ID = os.getenv('JESSE_DISCORD_ID')
HEALTH_LOG_PATH = Path('data') / 'health_log.json'
PROMPT_TEXT = """Hey Jesse! Daily health check-in:

Please reply with something like:
Sleep: 8 hours
Steps: 12000
Water: 3 L
Energy: 9/10"""


def load_health_log() -> Dict[str, Any]:
    HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(HEALTH_LOG_PATH.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, FileNotFoundError):
        return {'entries': [], 'metadata': {'last_prompt_date': None}}


def save_health_log(data: Dict[str, Any]) -> None:
    HEALTH_LOG_PATH.write_text(json.dumps(data, indent=2, default=str), encoding='utf-8')


def parse_metrics(content: str) -> Dict[str, float] | None:
    patterns = {
        'sleep': r'sleep[:\s]*([0-9]+\.?[0-9]*)',
        'steps': r'steps?[:\s]*([0-9,]+)',
        'water': r'water[:\s]*([0-9]+\.?[0-9]*)',
        'energy': r'energy[:\s]*([0-9]+\.?[0-9]*)',
    }
    metrics: Dict[str, Any] = {}
    for key, pat in patterns.items():
        match = re.search(pat, content, re.IGNORECASE)
        if not match:
            return None
        val_str = match.group(1).replace(',', '')
        metrics[key] = float(val_str) if key != 'steps' else int(val_str)
    return metrics  # type: ignore[return-value]


def generate_insights(entries: List[Dict[str, Any]]) -> List[str]:
    n = len(entries)
    if n < 7:
        return ['Insufficient data for full insights.']
    avgs = {
        k: round(sum(e.get(k, 0) for e in entries) / n, 1)
        for k in ('sleep', 'steps', 'water', 'energy')
    }
    insights: List[str] = []
    if avgs['sleep'] < 7.0:
        insights.append('Low sleep avg: aim for 7+ hours.')
    if avgs['steps'] < 8000:
        insights.append('Steps low: target 10k daily.')
    if avgs['water'] < 2.0:
        insights.append('Increase water intake to 2L+.')
    if avgs['energy'] < 6.0:
        insights.append('Energy low: check sleep & activity.')
    if n >= 14:
        prev_entries = entries[-14:-7]
        prev_avgs = {
            k: sum(e.get(k, 0) for e in prev_entries) / 7
            for k in avgs
        }
        for k, curr in avgs.items():
            prev = prev_avgs[k]
            delta_pct = ((curr - prev) / prev * 100) if prev > 0 else 0
            if abs(delta_pct) > 10:
                trend = 'up' if delta_pct > 0 else 'down'
                insights.append(f'{k.title()} trending {trend} {abs(delta_pct):.0f}%')
    return insights


async def daily_health_coro() -> None:
    if not JESSE_DISCORD_ID:
        return
    data = load_health_log()
    recent_msgs = get_recent_messages(JESSE_DISCORD_ID, 30)
    existing_dates = {e['date'] for e in data['entries']}
    new_entries: List[Dict[str, Any]] = []
    for msg in recent_msgs:
        if msg.get('role') != 'user':
            continue
        metrics = parse_metrics(msg['content'])
        if not metrics:
            continue
        try:
            ts_dt = datetime.fromisoformat(msg['timestamp'])
            msg_date = ts_dt.date().isoformat()
        except (KeyError, ValueError):
            continue
        if msg_date not in existing_dates:
            entry: Dict[str, Any] = {
                'date': msg_date,
                **metrics,
                'source_timestamp': msg['timestamp']
            }
            new_entries.append(entry)
            existing_dates.add(msg_date)
    if new_entries:
        data['entries'].extend(new_entries)
        data['entries'].sort(key=lambda e: e['date'])
        save_health_log(data)
    metadata = data.setdefault('metadata', {})
    today = date.today().isoformat()
    if metadata.get('last_prompt_date') != today:
        success = await notify_async(PROMPT_TEXT)
        if success:
            metadata['last_prompt_date'] = today
            save_health_log(data)


async def weekly_summary_coro() -> None:
    data = load_health_log()
    entries = data['entries']
    if len(entries) < 7:
        return
    week_entries = sorted(entries, key=lambda e: e['date'])[-7:]
    avgs = {
        k: round(sum(e.get(k, 0) for e in week_entries) / 7, 1)
        for k in ('sleep', 'steps', 'water', 'energy')
    }
    insight_entries = entries[-14:] if len(entries) >= 14 else entries
    insights = generate_insights(insight_entries)
    text = '**Weekly Health Summary**\n\n'
    text += f'Sleep: {avgs["sleep"]}h\n'
    text += f'Steps: {avgs["steps"]:,}\n'
    text += f'Water: {avgs["water"]}L\n'
    text += f'Energy: {avgs["energy"]}/10\n\n'
    text += '**Insights:**\n' + '\n'.join(f'• {i}' for i in insights)
    await notify_async(text)


def register_capability(registry: CapabilityRegistry | None = None) -> Capability:
    registry = registry or CapabilityRegistry()
    cap = Capability(
        name='daily_health_tracker',
        module='capabilities.daily_health_tracker',
        description=__doc__.strip(),
        dependencies=['event_loop', 'discord_notifier', 'conversational_memory']
    )
    registry.add(cap)
    return cap


def integrate_with_event_loop(loop: EventLoop) -> None:
    loop.add_periodic_task(PeriodicTask('daily_health_tracker.daily', daily_health_coro, 86400.0))
    loop.add_periodic_task(PeriodicTask('daily_health_tracker.weekly', weekly_summary_coro, 604800.0))


def initialize(registry: CapabilityRegistry | None = None, loop: EventLoop | None = None) -> Capability:
    cap = register_capability(registry)
    HEALTH_LOG_PATH.parent.mkdir(exist_ok=True)
    _ = load_health_log()
    if loop:
        integrate_with_event_loop(loop)
    return cap