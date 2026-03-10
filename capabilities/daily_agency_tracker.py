"""Implements daily Discord DM prompts for Jesse's agency metrics including autonomy score (1-10),
key decisions, and goal progress, with LLM-based parsing of responses from recent messages,
persistent JSONL storage, trend-based motivational insights/alerts, and integration with weekly synthesis.
"""
import asyncio
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import src.kernel.model_interface
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import call_model
from capabilities.conversational_memory import get_recent_messages
from capabilities.discord_notifier import notify_async
from capabilities.event_loop import EventLoop, PeriodicTask

JESSE_DISCORD_ID = os.environ['JESSE_DISCORD_ID']
_data_dir: Optional[Path] = None
_agency_entries_path: Optional[Path] = None
_agency_prompts_path: Optional[Path] = None
_registry: Optional[CapabilityRegistry] = None

def initialize(
    data_dir: Path,
    registry: Optional[CapabilityRegistry] = None,
    event_loop: Optional[EventLoop] = None,
) -> None:
    global _data_dir, _agency_entries_path, _agency_prompts_path, _registry
    _data_dir = data_dir
    _agency_entries_path = data_dir / "agency_entries.jsonl"
    _agency_prompts_path = data_dir / "agency_prompts.jsonl"
    _registry = registry
    register_capability(registry)

def register_capability(registry: Optional[CapabilityRegistry] = None) -> Optional[Capability]:
    if not registry:
        return None
    cap = Capability(
        name="daily_agency_tracker",
        module="capabilities.daily_agency_tracker",
        description="Daily agency metrics tracking via Discord DMs: autonomy (1-10), key decisions, goal progress. Parses responses, stores trends, alerts/insights.",
        dependencies=["conversational_memory", "discord_notifier", "model_interface", "event_loop"],
    )
    registry.add(cap)
    return cap

def parse_response(content: str) -> Tuple[Optional[int], List[str], str]:
    prompt = f"""Parse this daily agency response into JSON.
Autonomy score: integer 1-10 or null if unclear.
Key decisions: list of strings (3-5 bullets).
Goal progress: short string summary.

Response: {content}

Output ONLY valid JSON: {{"autonomy": null_or_int, "decisions": ["dec1", "dec2"], "progress": "summary"}}"""
    try:
        resp = call_model(prompt)
        data = json.loads(resp.text.strip())
        autonomy = int(data.get("autonomy")) if data.get("autonomy") is not None else None
        return autonomy, data.get("decisions", []), data.get("progress", "")
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None, [], ""

def store_entry(date_str: str, autonomy: Optional[int], decisions: List[str], progress: str) -> None:
    entry = {
        "date": date_str,
        "autonomy": autonomy,
        "decisions": decisions,
        "progress": progress,
        "timestamp": time.time(),
    }
    _agency_entries_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_agency_entries_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    _agency_entries_path.touch()

def store_prompt(date_str: str, prompt_time: float) -> None:
    entry = {"date": date_str, "prompt_time": prompt_time}
    _agency_prompts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_agency_prompts_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

def has_entry_for_date(date_str: str) -> bool:
    if not _agency_entries_path or not _agency_entries_path.exists():
        return False
    for line in _agency_entries_path.read_text().splitlines():
        if line.strip():
            try:
                entry = json.loads(line)
                if entry.get("date") == date_str:
                    return True
            except json.JSONDecodeError:
                continue
    return False

def has_prompt_today(date_str: str) -> bool:
    if not _agency_prompts_path or not _agency_prompts_path.exists():
        return False
    for line in _agency_prompts_path.read_text().splitlines():
        if line.strip():
            try:
                entry = json.loads(line)
                if entry.get("date") == date_str:
                    return True
            except json.JSONDecodeError:
                continue
    return False

async def daily_prompt_coro() -> None:
    today_str = date.today().isoformat()
    if has_entry_for_date(today_str) or has_prompt_today(today_str):
        return
    prompt_text = """🛡️ Daily Agency Check-in:

Reply with:
- Autonomy (1-10): How independent/autonomous did you feel?
- Key decisions (3-5 bullets):
- Goal progress (short update):

Ex:
Autonomy: 7
Decisions: - Chose A over B, - Delegated X
Progress: Advanced on main goal Y by 50%."""
    await notify_async(prompt_text)
    store_prompt(today_str, time.time())

async def check_responses_coro() -> None:
    today_str = date.today().isoformat()
    if has_entry_for_date(today_str):
        return
    msgs = get_recent_messages(JESSE_DISCORD_ID, 20)
    for msg in reversed(msgs):
        if msg.get("role") != "user":
            continue
        autonomy, decisions, progress = parse_response(msg["content"])
        if autonomy is not None:
            store_entry(today_str, autonomy, decisions, progress)
            await check_trends_and_notify_async(today_str)
            return

async def check_trends_and_notify_async(date_str: str) -> None:
    if not _agency_entries_path.exists():
        return
    entries = []
    for line in _agency_entries_path.read_text().splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    valid_recent = [e for e in sorted(entries, key=lambda e: e.get("date"))[-7:] if e.get("autonomy") is not None]
    if len(valid_recent) < 3:
        return
    avg = sum(e["autonomy"] for e in valid_recent) / len(valid_recent)
    if avg < 5.0:
        await notify_async(f"🚨 Agency Alert: 7-day avg autonomy {avg:.1f}/10. Review decisions/progress for blocks.")
    elif avg > 7.0:
        await notify_async(f"🚀 Strong agency! Avg {avg:.1f}/10. Your decisions are driving progress.")

async def weekly_summary_coro() -> None:
    today = date.today()
    if today.weekday() != 6:  # Sunday
        return
    week_ago = (today - timedelta(days=7)).isoformat()
    if not _agency_entries_path.exists():
        return
    entries = []
    for line in _agency_entries_path.read_text().splitlines():
        if line.strip():
            try:
                entry = json.loads(line)
                if entry.get("date", "") >= week_ago:
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    if len([e for e in entries if e.get("autonomy") is not None]) < 3:
        return
    summary_prompt = "Weekly Agency Synthesis (trends, wins, suggestions):\n\n"
    for e in sorted(entries, key=lambda x: x.get("date")):
        aut = e.get("autonomy")
        decs = "; ".join(e.get("decisions", []))
        prog = e.get("progress", "")[:80]
        summary_prompt += f"{e['date']}: Aut{aut}, Decs: {decs}, Prog: {prog}\n"
    resp = call_model(summary_prompt + "\nConcise insights + action items.")
    await notify_async(f"📊 Weekly Agency Summary:\n{resp.text}")

def integrate_with_event_loop(loop: EventLoop) -> None:
    loop.add_periodic_task(PeriodicTask("daily_agency_prompt", daily_prompt_coro, 86400.0))
    loop.add_periodic_task(PeriodicTask("agency_check_responses", check_responses_coro, 1800.0))
    loop.add_periodic_task(PeriodicTask("weekly_agency_summary", weekly_summary_coro, 604800.0))