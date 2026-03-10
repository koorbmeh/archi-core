"""Implements daily Discord DM prompts for Jesse's capability metrics including hours studied,
new skills, projects advanced, and self-assessed score, with NL parsing, persistent storage,
and weekly trend previews."""
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Tuple, List, Dict, Any

import src.kernel.model_interface
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import call_model
from capabilities.conversational_memory import get_recent_messages
from capabilities.discord_notifier import notify_async
from capabilities.event_loop import EventLoop

DATA_DIR: Path | None = None
ENTRIES_PATH: Path | None = None
PROMPTS_PATH: Path | None = None
USER_ID: str = os.getenv("JESSE_DISCORD_ID", "")

def _load_entries() -> List[Dict[str, Any]]:
    if not ENTRIES_PATH or not ENTRIES_PATH.exists():
        return []
    lines = ENTRIES_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]

def _load_prompts() -> List[Dict[str, Any]]:
    if not PROMPTS_PATH or not PROMPTS_PATH.exists():
        return []
    lines = PROMPTS_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]

def has_entry_for_date(date_str: str) -> bool:
    return any(e["date"] == date_str for e in _load_entries())

def has_prompt_today(date_str: str) -> bool:
    return any(p["date"] == date_str for p in _load_prompts())

def store_entry(date_str: str, hours: float | None, skills: List[str], projects: str, score: int | None) -> None:
    entry = {
        "date": date_str,
        "hours": hours,
        "skills": skills,
        "projects": projects,
        "score": score,
        "stored_at": time.time(),
    }
    ENTRIES_PATH.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")

def store_prompt(date_str: str, prompt_time: float) -> None:
    entry = {"date": date_str, "prompt_time": prompt_time}
    PROMPTS_PATH.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")

def parse_response(content: str) -> Tuple[float | None, List[str], str, int | None]:
    system = """Extract ONLY: {"hours": float|null, "skills": [str,...], "projects": str, "score": int|null}. Use null/empty if unclear."""
    response = call_model(f"Parse:\n{content}", system=system)
    try:
        data = json.loads(response.text.strip())
        return (
            data.get("hours"),
            data.get("skills", []),
            data.get("projects", ""),
            data.get("score"),
        )
    except (json.JSONDecodeError, KeyError):
        return None, [], "", None

def _compute_weekly_summary() -> str:
    today = date.today()
    week_start = today - timedelta(days=6)
    entries = [
        e for e in _load_entries()
        if date.fromisoformat(e["date"]) >= week_start
    ]
    if not entries:
        return "No data. Start logging!"
    hours = [e["hours"] for e in entries if e["hours"] is not None]
    avg_hours = sum(hours) / len(hours) if hours else 0.0
    scores = [e["score"] for e in entries if e["score"] is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    unique_skills = list(
        set(s for e in entries for s in e.get("skills", []))
    )[:12]
    projects = set(e.get("projects", "") for e in entries if e["projects"].strip())
    gaps = []
    if avg_hours < 2.0:
        gaps.append("Boost study hours")
    if avg_score < 7:
        gaps.append("Raise self-score")
    if len(unique_skills) < 3:
        gaps.append("Log more skills")
    gap_str = "\n• " + "\n• ".join(gaps) if gaps else "Strong trends!"
    return (
        f"H: {avg_hours:.1f}/d | S: {avg_score:.1f}/10 | Skills: {len(unique_skills)} "
        f"({', '.join(unique_skills)}) | P: {'; '.join(projects)}\n{gap_str}"
    )

def _compute_weekly_report() -> str:
    today = date.today()
    if today.weekday() != 6:
        return ""
    week_start = today - timedelta(days=6)
    entries = [
        e for e in _load_entries()
        if date.fromisoformat(e["date"]) >= week_start
    ]
    total_hours = sum(e.get("hours", 0) or 0 for e in entries)
    skill_count = len(set(s for e in entries for s in e.get("skills", [])))
    scores = [e["score"] for e in entries if e["score"] is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    return (
        f"📊 Week of {week_start}: {total_hours:.1f}h total, "
        f"{skill_count} skills, avg score {avg_score:.1f}/10.\n"
        f"Stay consistent to close gaps!"
    )

def _ensure_initialized() -> None:
    """Auto-initialize paths if initialize() was never called."""
    global DATA_DIR, ENTRIES_PATH, PROMPTS_PATH
    if PROMPTS_PATH is None:
        DATA_DIR = Path("data")
        ENTRIES_PATH = DATA_DIR / "capability_entries.jsonl"
        PROMPTS_PATH = DATA_DIR / "capability_prompts.jsonl"
        DATA_DIR.mkdir(parents=True, exist_ok=True)


async def daily_prompt_coro() -> None:
    _ensure_initialized()
    date_str = date.today().isoformat()
    if has_prompt_today(date_str) or has_entry_for_date(date_str):
        return
    weekly = _compute_weekly_summary()
    prompt_text = (
        f"🧠 Daily Capabilities\n\nWeekly: {weekly}\n\nToday:\n"
        f"• Hours studied:\n• New skills:\n• Projects advanced:\n"
        f"• Self-score (1-10):\n\nReply to log. Consistency matters!"
    )
    await notify_async(prompt_text)
    store_prompt(date_str, time.time())

async def check_responses_coro() -> None:
    _ensure_initialized()
    date_str = date.today().isoformat()
    if not (has_prompt_today(date_str) and not has_entry_for_date(date_str)):
        return
    recent = get_recent_messages(USER_ID, 20)
    for msg in reversed(recent[-10:]):
        parsed = parse_response(msg["content"])
        hours, skills, projects, score = parsed
        if hours is not None or score is not None or skills or projects.strip():
            store_entry(date_str, hours, skills, projects, score)
            await notify_async("✅ Today's capabilities logged!")
            return

async def weekly_summary_coro() -> None:
    _ensure_initialized()
    report = _compute_weekly_report()
    if report:
        await notify_async(report)

def initialize(
    data_dir: Path = Path("data"),
    registry: CapabilityRegistry | None = None,
    event_loop: EventLoop | None = None,
) -> None:
    global DATA_DIR, ENTRIES_PATH, PROMPTS_PATH
    DATA_DIR = data_dir
    ENTRIES_PATH = data_dir / "capability_entries.jsonl"
    PROMPTS_PATH = data_dir / "capability_prompts.jsonl"
    data_dir.mkdir(exist_ok=True)
    if registry is not None:
        register_capability(registry)
    if event_loop is not None:
        integrate_with_event_loop(event_loop)

def register_capability(
    registry: CapabilityRegistry | None = None,
) -> Capability | None:
    if registry is None:
        return None
    cap = Capability(
        name="daily_capability_tracker",
        module="capabilities.daily_capability_tracker",
        description=(
            "Daily Discord DMs for capability metrics (hours, skills, projects, score). "
            "LLM-parses responses, JSONL storage, trend previews w/ gaps."
        ),
        dependencies=[
            "event_loop",
            "discord_notifier",
            "capability_registry",
            "conversational_memory",
        ],
    )
    # Registry integration handled by caller or kernel
    return cap

def integrate_with_event_loop(loop: EventLoop) -> None:
    from capabilities.event_loop import PeriodicTask
    loop.register_task(PeriodicTask("daily_capability_prompt", daily_prompt_coro, 86400.0))
    loop.register_task(PeriodicTask("capability_check_responses", check_responses_coro, 1800.0))
    loop.register_task(PeriodicTask("capability_weekly_summary", weekly_summary_coro, 604800.0))