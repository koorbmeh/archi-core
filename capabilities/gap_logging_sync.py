"""
capabilities/gap_logging_sync.py

Synchronizes gap detection logging between backend terminal outputs and frontend
Discord notifications to ensure accurate gap reporting to users like Jesse.

Captures gaps signaled from Discord DMs, logs them using gap_detector's operational
gap detection, notifies discrepancies via discord_notifier, and periodically syncs
backend logs with the frontend by scanning operation logs and pushing updates to Discord.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.gap_detector import Gap, detect_operational_gaps, detect_gaps
from capabilities.discord_notifier import notify, notify_async
from capabilities.discord_listener import receive_message

logger = logging.getLogger(__name__)

# Module-level state
_last_sync_time: float = 0.0
_known_gap_names: set[str] = set()
_sync_interval_seconds: float = 60.0

DEFAULT_LOG_PATH = Path("logs") / "operations.log"


def _format_gap_for_discord(gap: Gap) -> str:
    """Format a Gap object into a human-readable Discord message."""
    lines = [
        f"**Gap Detected:** `{gap.name}`",
        f"  Source: {gap.source}",
        f"  Reason: {gap.reason}",
        f"  Priority: {gap.priority:.2f}",
    ]
    if gap.detail:
        lines.append(f"  Detail: {gap.detail}")
    if gap.evidence:
        evidence_str = ", ".join(gap.evidence[:3])
        lines.append(f"  Evidence: {evidence_str}")
    return "\n".join(lines)


def _load_known_gaps(log_path: Optional[Path] = None) -> list[Gap]:
    """Detect operational gaps from the backend log file."""
    path = log_path or DEFAULT_LOG_PATH
    try:
        gaps = detect_operational_gaps(log_path=path)
        logger.debug("Loaded %d operational gaps from %s", len(gaps), path)
        return gaps
    except Exception as exc:
        logger.warning("Failed to load operational gaps: %s", exc)
        return []


def _find_new_gaps(gaps: list[Gap]) -> list[Gap]:
    """Return gaps not previously seen in this session."""
    new_gaps = [g for g in gaps if g.name not in _known_gap_names]
    for g in new_gaps:
        _known_gap_names.add(g.name)
    return new_gaps


def sync_gaps_to_discord(log_path: Optional[Path] = None) -> int:
    """
    Scan backend operation logs for gaps and push new ones to Discord.

    Returns the count of new gaps notified.
    """
    global _last_sync_time
    gaps = _load_known_gaps(log_path)
    new_gaps = _find_new_gaps(gaps)

    if not new_gaps:
        logger.debug("No new gaps to sync to Discord.")
        _last_sync_time = time.time()
        return 0

    notified = 0
    for gap in new_gaps:
        message = _format_gap_for_discord(gap)
        success = notify(message)
        if success:
            notified += 1
            logger.info("Notified Discord of gap: %s", gap.name)
        else:
            logger.warning("Failed to notify Discord of gap: %s", gap.name)

    _last_sync_time = time.time()
    return notified


async def sync_gaps_to_discord_async(log_path: Optional[Path] = None) -> int:
    """
    Async version of sync_gaps_to_discord for use within the event loop.

    Returns the count of new gaps notified.
    """
    global _last_sync_time
    gaps = _load_known_gaps(log_path)
    new_gaps = _find_new_gaps(gaps)

    if not new_gaps:
        logger.debug("No new gaps to sync (async).")
        _last_sync_time = time.time()
        return 0

    notified = 0
    for gap in new_gaps:
        message = _format_gap_for_discord(gap)
        success = await notify_async(message)
        if success:
            notified += 1
            logger.info("Async-notified Discord of gap: %s", gap.name)
        else:
            logger.warning("Async failed to notify Discord of gap: %s", gap.name)

    _last_sync_time = time.time()
    return notified


def capture_gap_from_discord(content: str, user_id: str) -> Optional[Gap]:
    """
    Parse a Discord DM for a user-reported gap and enqueue it for processing.

    Returns a Gap if the message describes one, otherwise None.
    """
    lower = content.lower()
    if "gap" not in lower and "missing" not in lower and "can't" not in lower:
        return None

    gap = Gap(
        name=f"user_reported_{int(time.time())}",
        source="discord_dm",
        reason=f"User {user_id} reported: {content[:120]}",
        priority=0.7,
        evidence=[content[:200]],
        detail="Gap reported via Discord DM",
    )
    logger.info("Captured user-reported gap from Discord DM: %s", gap.name)

    receive_message(content, user_id)
    _known_gap_names.add(gap.name)
    return gap


def report_discrepancy(backend_gap: Gap, discord_gap: Optional[Gap] = None) -> bool:
    """
    Notify Discord of a discrepancy between backend gap state and frontend report.

    Returns True if notification succeeded.
    """
    if discord_gap is None:
        message = (
            f"⚠️ **Gap Discrepancy:** Backend detected `{backend_gap.name}` "
            f"but no matching frontend report found.\n"
            f"  Reason: {backend_gap.reason}"
        )
    else:
        message = (
            f"⚠️ **Gap Sync Issue:** Backend gap `{backend_gap.name}` differs "
            f"from Discord-reported `{discord_gap.name}`."
        )

    success = notify(message)
    if success:
        logger.info("Reported discrepancy to Discord for gap: %s", backend_gap.name)
    else:
        logger.warning("Failed to report discrepancy for gap: %s", backend_gap.name)
    return success


def check_and_report_discrepancies(
    registry: CapabilityRegistry,
    log_path: Optional[Path] = None,
) -> int:
    """
    Compare full gap detection results with known gaps; report any discrepancies.

    Returns the number of discrepancies reported.
    """
    try:
        all_gaps = detect_gaps(registry, log_path=log_path or DEFAULT_LOG_PATH)
    except Exception as exc:
        logger.warning("detect_gaps failed: %s", exc)
        return 0

    discrepancies = 0
    for gap in all_gaps:
        if gap.name not in _known_gap_names:
            reported = report_discrepancy(gap)
            if reported:
                _known_gap_names.add(gap.name)
                discrepancies += 1

    return discrepancies


def register_capability(
    registry: Optional[CapabilityRegistry] = None,
) -> Capability:
    """Register the gap_logging_sync capability with the capability registry."""
    if registry is None:
        registry = CapabilityRegistry()

    cap = Capability(
        name="gap_logging_sync",
        module="capabilities.gap_logging_sync",
        description=(
            "Synchronizes gap detection logging between backend terminal outputs "
            "and frontend Discord notifications to ensure accurate gap reporting."
        ),
        status="active",
        dependencies=["gap_detector", "discord_listener", "discord_notifier"],
        metadata={"sync_interval_seconds": _sync_interval_seconds},
    )
    registry.register(cap)
    logger.info("Registered capability: gap_logging_sync")
    return cap


async def _periodic_sync_task(log_path: Optional[Path] = None) -> None:
    """Coroutine factory target: run one sync cycle for the event loop."""
    await sync_gaps_to_discord_async(log_path)


def initialize(
    registry: Optional[CapabilityRegistry] = None,
    log_path: Optional[Path] = None,
    sync_interval: float = 60.0,
) -> Capability:
    """
    Initialize gap_logging_sync: register capability and return it.

    Call this at startup. To wire into the event loop, use integrate_with_event_loop().
    """
    global _sync_interval_seconds
    _sync_interval_seconds = sync_interval

    cap = register_capability(registry)
    logger.info(
        "gap_logging_sync initialized (sync_interval=%.1fs)", sync_interval
    )
    return cap


def integrate_with_event_loop(
    loop,
    log_path: Optional[Path] = None,
) -> None:
    """
    Register a periodic gap-sync task with an EventLoop instance.

    Parameters
    ----------
    loop : EventLoop
        An EventLoop instance (from capabilities.event_loop).
    log_path : Path, optional
        Path to the operations log file.
    """
    from capabilities.event_loop import PeriodicTask

    def coro_factory():
        return _periodic_sync_task(log_path)

    task = PeriodicTask(
        name="gap_logging_sync",
        coro_factory=coro_factory,
        interval=_sync_interval_seconds,
    )
    loop.add_task(task)
    logger.info(
        "gap_logging_sync registered with event loop (interval=%.1fs)",
        _sync_interval_seconds,
    )