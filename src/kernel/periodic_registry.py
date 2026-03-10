"""Periodic task registry — maps capability names to callable schedules.

ArchiDaemon reads data/periodic_registry.json at startup and launches
an asyncio task for each registered entry. Capabilities self-register by
calling ``register()`` or by having Archi write an entry via the generation
loop.

Registry format (data/periodic_registry.json):
    {
        "daily_health_tracker": {
            "module": "capabilities.daily_health_tracker",
            "coroutine": "daily_health_coro",
            "interval_seconds": 86400,
            "enabled": true
        },
        ...
    }

Each entry names a module and an async callable (coroutine function) inside
it. ArchiDaemon imports the module, resolves the callable, and wraps it in
a periodic loop that sleeps for ``interval_seconds`` between runs.
"""

import asyncio
import importlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = Path("data/periodic_registry.json")


@dataclass
class PeriodicEntry:
    """One registered periodic task."""
    name: str
    module: str
    coroutine: str
    interval_seconds: int
    enabled: bool = True


def load_registry(path: Optional[Path] = None) -> list[PeriodicEntry]:
    """Load all periodic entries from the JSON registry."""
    reg_path = path or DEFAULT_REGISTRY_PATH
    if not reg_path.exists():
        return []
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read periodic registry: %s", exc)
        return []
    entries = []
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        entries.append(PeriodicEntry(
            name=name,
            module=cfg.get("module", ""),
            coroutine=cfg.get("coroutine", ""),
            interval_seconds=int(cfg.get("interval_seconds", 86400)),
            enabled=cfg.get("enabled", True),
        ))
    return entries


def save_registry(entries: list[PeriodicEntry], path: Optional[Path] = None) -> None:
    """Write entries back to the JSON registry."""
    reg_path = path or DEFAULT_REGISTRY_PATH
    data = {}
    for e in entries:
        data[e.name] = {
            "module": e.module,
            "coroutine": e.coroutine,
            "interval_seconds": e.interval_seconds,
            "enabled": e.enabled,
        }
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def register(
    name: str,
    module: str,
    coroutine: str,
    interval_seconds: int = 86400,
    enabled: bool = True,
    path: Optional[Path] = None,
) -> PeriodicEntry:
    """Add or update a periodic entry in the registry."""
    entries = load_registry(path)
    existing = {e.name: e for e in entries}
    entry = PeriodicEntry(
        name=name, module=module, coroutine=coroutine,
        interval_seconds=interval_seconds, enabled=enabled,
    )
    existing[name] = entry
    save_registry(list(existing.values()), path)
    logger.info("Registered periodic task: %s (interval=%ds)", name, interval_seconds)
    return entry


def resolve_coroutine(entry: PeriodicEntry) -> Optional[Callable[[], Coroutine]]:
    """Import the module and return the coroutine function, or None on failure."""
    try:
        mod = importlib.import_module(entry.module)
    except ImportError as exc:
        logger.error("Cannot import %s for periodic task %s: %s",
                     entry.module, entry.name, exc)
        return None

    parts = entry.coroutine.split(".")
    obj = mod
    for part in parts:
        obj = getattr(obj, part, None)
        if obj is None:
            logger.error("Cannot find %s in %s for periodic task %s",
                         entry.coroutine, entry.module, entry.name)
            return None

    if not asyncio.iscoroutinefunction(obj):
        logger.error("%s.%s is not a coroutine function (periodic task %s)",
                     entry.module, entry.coroutine, entry.name)
        return None

    return obj


async def run_periodic(entry: PeriodicEntry, coro_fn: Callable[[], Coroutine]) -> None:
    """Run a single periodic task forever, sleeping between invocations."""
    logger.info("Periodic task '%s' started (interval=%ds)", entry.name, entry.interval_seconds)
    while True:
        try:
            await coro_fn()
        except Exception as exc:
            logger.exception("Periodic task '%s' failed: %s", entry.name, exc)
        await asyncio.sleep(entry.interval_seconds)
