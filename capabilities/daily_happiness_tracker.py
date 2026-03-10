"""
Daily Happiness Tracker Capability.

Sends daily Discord DM prompts for happiness metrics at ~20:00, fetches and parses
responses using regex/heursitics (with LLM fallback), stores structured entries in
data/happiness_log.jsonl, sends weekly summaries with trends/insights on Sundays ~21:00.
Integrates periodic tasks with event_loop.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import src.kernel.model_interface
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import ModelResponse
from capabilities.discord_notifier import notify_async
from capabilities.event_loop import EventLoop, PeriodicTask
from capabilities.timestamped_chat_history_recall import recall_messages_in_range

USER_ID: str = os.getenv('JESSE_DISCORD_ID', '')
_data_dir: Path = Path('data')
log_path: Path = _data_dir / 'happiness_log.jsonl'
prompt_log_path: Path = _data_dir / 'happiness_prompts.jsonl'

def _init_paths(data_dir: Path) -> None:
    global _data_dir, log_path, prompt_log_path
    _data_dir = data_dir
    log_path = data_dir / 'happiness_log.jsonl'
    prompt_log_path = data_dir / 'happiness_prompts.jsonl'
    data_dir.mkdir(exist_ok=True)

def has_entry_for_date(date_str: str) -> bool:
    if not log_path.exists():
        return False
    with log_path.open('r') as f:
        for line in f:
            try:
                if json.loads(line).get('date') == date_str:
                    return True
            except json.JSONDecodeError:
                continue
    return False

def has_prompt_today(date_str: str) -> bool:
    return get_last_prompt_time_for_date(date_str) is not None

def get_last_prompt_time_for_date(date_str: str) -> Optional[float]:
    if not prompt_log_path.exists():
        return None
    try:
        lines = prompt_log_path.read_text().splitlines()
        for line in reversed(lines):
            data = json.loads(line)
            if data.get('date') == date_str:
                return data['prompt_time']
    except (json.JSONDecodeError, KeyError):
        pass
    return None

def store_prompt(date_str: str, prompt_time: float) -> None:
    with prompt_log_path.open('a') as f:
        json.dump({'date': date_str, 'prompt_time': prompt_time}, f)
        f.write('\n')

def store_entry(date_str: str, mood: int, gratitudes: List[str], interactions: str,
                stressors: List[str]) -> None:
    entry = {'date': date_str, 'mood': mood, 'gratitudes': gratitudes,
             'interactions': interactions, 'stressors': stressors}
    with log_path.open('a') as f:
        json.dump(entry, f)
        f.write('\n')

def parse_response(content: str) -> Tuple[Optional[int], List[str], str, List[str]]:
    mood_match = re.search(r'\b([1-9]|10)(?:/10)?\b', content)
    mood = int(mood_match.group(1)) if mood_match else None
    grat_raw = re.findall(r'(?i)(?:gratu|thank|appreciat)\S*\s*:?\s*([^.\n;]{5,100})', content)[:3]
    gratitudes = [g.strip() for g in grat_raw]
    if len(gratitudes) < 1:
        num1 = re.findall(r'(?i)(?:1st?|one)\s*:?\s*([^.\n;]{5,100})', content)
        num2 = re.findall(r'(?i)(?:2nd?|two)\s*:?\s*([^.\n;]{5,100})', content)
        num3 = re.findall(r'(?i)(?:3rd?|three)\s*:?\s*([^.\n;]{5,100})', content)
        gratitudes = [g.strip() for g in (num1 + num2 + num3)[:3]]
    stress_raw = re.findall(r'(?i)(?:stress|worry|frustrat|annoy)\S*\s*:?\s*([^.\n;]{5,100})', content)[:5]
    stressors = [s.strip() for s in stress_raw]
    inter_match = re.search(r'(?i)(?:social|interact|talk|chat|meet|hang|call)\s*:?\s*([^.\n!]{0,200})', content)
    interactions = inter_match.group(1).strip() if inter_match else ''
    if mood is None:
        try:
            resp: ModelResponse = call_model(
                f"""Parse: mood (1-10), 3 gratitudes list, interactions str, stressors list.
JSON only: {{"mood":int,"gratitudes":["a","b","c"],"interactions":"str","stressors":["d"]}}

{content}""",
                system="Output valid JSON only, no extra text."
            )
            parsed = json.loads(resp.text)
            mood = parsed.get('mood')
            if isinstance(mood, int) and 1 <= mood <= 10:
                gratitudes = parsed.get('gratitudes', gratitudes)[:3]
                interactions = parsed.get('interactions', interactions)
                stressors = parsed.get('stressors', stressors)
        except Exception:
            pass
    return mood, gratitudes, interactions, stressors

async def daily_prompt_coro() -> None:
    now = datetime.now()
    if now.hour != 20 or now.minute > 4:
        return
    date_str = now.strftime('%Y-%m-%d')
    if has_prompt_today(date_str):
        return
    prompt_text = """Daily happiness check-in!
Rate mood 1-10
3 gratitudes
Social interactions
Stressors?"""
    if await notify_async(prompt_text):
        store_prompt(date_str, time.time())

async def check_responses_coro() -> None:
    date_str = datetime.now().strftime('%Y-%m-%d')
    if has_entry_for_date(date_str):
        return
    prompt_t = get_last_prompt_time_for_date(date_str)
    if prompt_t is None or not USER_ID:
        return
    msgs = recall_messages_in_range(USER_ID, prompt_t, time.time())
    user_msgs = [m for m in msgs if m.get('role') == 'user']
    for m in sorted(user_msgs, key=lambda x: x.get('timestamp', 0), reverse=True)[:3]:
        mood, grats, inter, stress = parse_response(m['content'])
        if mood is not None and len(grats) >= 1:
            store_entry(date_str, mood, grats, inter, stress)
            await notify_async(f"✅ Logged {date_str}: mood {mood}, {len(grats)} gratitudes. Thanks!")
            return

async def weekly_summary_coro() -> None:
    now = datetime.now()
    if now.weekday() != 6 or now.hour != 21 or now.minute > 4:  # Sunday
        return
    summary = _generate_weekly_summary()
    if summary:
        await notify_async(summary)

def _generate_weekly_summary() -> str:
    now = datetime.now()
    start_d = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    entries: List[Dict[str, Any]] = []
    if log_path.exists():
        with log_path.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get('date', '') >= start_d:
                        entries.append(e)
                except json.JSONDecodeError:
                    continue
    n = len(entries)
    if n < 3:
        return f"Insufficient data ({n}/7 days) for summary."
    moods = [e['mood'] for e in entries]
    avg_mood = sum(moods) / n
    delta = moods[-1] - moods[0]
    trend = 'improving' if delta > 1 else 'declining' if delta < -1 else 'stable'
    insights = []
    if avg_mood < 6:
        insights.append('💡 Suggestion: Try mindfulness or a walk.')
    strs = [s for e in entries for s in e.get('stressors', [])]
    if strs:
        top_stress = max(set(strs), key=strs.count)
        insights.append(f'🔄 Common stressor: {top_stress[:40]}')
    grats = [g for e in entries for g in e.get('gratitudes', [])]
    if grats:
        top_grat = max(set(grats), key=grats.count)
        insights.append(f'👍 Favorite gratitude: {top_grat[:40]}')
    return f"""📊 Weekly Happiness ({start_d}–{now.strftime('%Y-%m-%d')})

Avg mood: {avg_mood:.1f}/10
Trend: {trend}
{chr(10).join(insights) if insights else 'Keep tracking!'}

{n} days logged."""

def register_capability(registry: Optional[CapabilityRegistry] = None) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name='daily_happiness_tracker',
        module='capabilities.daily_happiness_tracker',
        description='Daily/weekly happiness tracking via Discord DMs: prompts, parse/store, trends.',
        dependencies=['discord_notifier', 'timestamped_chat_history_recall', 'conversational_memory']
    )
    registry.register(cap)
    return cap

def integrate_with_event_loop(loop: EventLoop) -> None:
    loop.periodic_tasks.append(PeriodicTask('daily_happiness_prompt', daily_prompt_coro, 1800.0))
    loop.periodic_tasks.append(PeriodicTask('happiness_responses', check_responses_coro, 900.0))
    loop.periodic_tasks.append(PeriodicTask('weekly_happiness_summary', weekly_summary_coro, 86400.0))

def initialize(data_dir: Path = Path('data'), registry: Optional[CapabilityRegistry] = None,
               event_loop: Optional[EventLoop] = None) -> None:
    _init_paths(data_dir)
    register_capability(registry)
    if event_loop is not None:
        integrate_with_event_loop(event_loop)