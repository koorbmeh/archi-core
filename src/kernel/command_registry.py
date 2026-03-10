"""On-demand command registry — maps Discord commands to capability functions.

discord_listener checks incoming messages for ``!command`` patterns and
dispatches to the registered handler. Capabilities self-register by calling
``register()`` or Archi writes entries via the generation loop.

Registry format (data/command_registry.json):
    {
        "scan craigslist": {
            "module": "capabilities.craigslist_pet_scanner",
            "function": "get_scanner().periodic_scan_coro",
            "description": "Scan Craigslist pet listings",
            "is_async": true
        },
        ...
    }

Commands are matched by prefix: ``!scan craigslist dogs`` matches
``scan craigslist``. The remainder after the command name is passed
as ``args`` to the handler.
"""

import asyncio
import importlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = Path("data/command_registry.json")


@dataclass
class CommandEntry:
    """One registered on-demand command."""
    command: str
    module: str
    function: str
    description: str = ""
    is_async: bool = True


def load_registry(path: Optional[Path] = None) -> list[CommandEntry]:
    """Load all command entries from the JSON registry."""
    reg_path = path or DEFAULT_REGISTRY_PATH
    if not reg_path.exists():
        return []
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read command registry: %s", exc)
        return []
    entries = []
    for cmd, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        entries.append(CommandEntry(
            command=cmd,
            module=cfg.get("module", ""),
            function=cfg.get("function", ""),
            description=cfg.get("description", ""),
            is_async=cfg.get("is_async", True),
        ))
    return entries


def save_registry(entries: list[CommandEntry], path: Optional[Path] = None) -> None:
    """Write entries back to the JSON registry."""
    reg_path = path or DEFAULT_REGISTRY_PATH
    data = {}
    for e in entries:
        data[e.command] = {
            "module": e.module,
            "function": e.function,
            "description": e.description,
            "is_async": e.is_async,
        }
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def register(
    command: str,
    module: str,
    function: str,
    description: str = "",
    is_async: bool = True,
    path: Optional[Path] = None,
) -> CommandEntry:
    """Add or update a command entry in the registry."""
    entries = load_registry(path)
    existing = {e.command: e for e in entries}
    entry = CommandEntry(
        command=command, module=module, function=function,
        description=description, is_async=is_async,
    )
    existing[command] = entry
    save_registry(list(existing.values()), path)
    logger.info("Registered command: !%s → %s.%s", command, module, function)
    return entry


def match_command(text: str, entries: list[CommandEntry]) -> Optional[tuple[CommandEntry, str]]:
    """Match a ``!command`` message against registered entries.

    Returns (entry, remaining_args) or None if no match.
    Longest-prefix match wins.
    """
    if not text.startswith("!"):
        return None
    body = text[1:].strip()
    best: Optional[tuple[CommandEntry, str]] = None
    best_len = 0
    for entry in entries:
        cmd = entry.command.lower()
        if body.lower().startswith(cmd) and len(cmd) > best_len:
            remainder = body[len(cmd):].strip()
            best = (entry, remainder)
            best_len = len(cmd)
    return best


def resolve_function(entry: CommandEntry) -> Optional[Callable]:
    """Import the module and return the callable, or None on failure."""
    try:
        mod = importlib.import_module(entry.module)
    except ImportError as exc:
        logger.error("Cannot import %s for command !%s: %s",
                     entry.module, entry.command, exc)
        return None

    parts = entry.function.split(".")
    obj = mod
    for part in parts:
        if part.endswith("()"):
            # Call a factory: e.g. "get_scanner()" → getattr(mod, "get_scanner")()
            factory_name = part[:-2]
            factory = getattr(obj, factory_name, None)
            if factory is None:
                logger.error("Cannot find factory %s in %s for command !%s",
                             factory_name, entry.module, entry.command)
                return None
            try:
                obj = factory()
            except Exception as exc:
                logger.error("Factory %s() failed for command !%s: %s",
                             factory_name, entry.command, exc)
                return None
        else:
            obj = getattr(obj, part, None)
            if obj is None:
                logger.error("Cannot find %s in %s for command !%s",
                             part, entry.module, entry.command)
                return None
    return obj


def list_commands_text() -> str:
    """Return a human-readable list of registered commands (used by !help)."""
    entries = load_registry()
    if not entries:
        return "No commands registered yet."
    lines = ["**Available commands:**"]
    for e in sorted(entries, key=lambda x: x.command):
        desc = f" — {e.description}" if e.description else ""
        lines.append(f"  `!{e.command}`{desc}")
    return "\n".join(lines)
