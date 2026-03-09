"""Capability registry — Archi's self-writable map of what it can and cannot do."""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = Path("data/capability_registry.json")


@dataclass
class Capability:
    """A single registered capability."""
    name: str
    module: str                          # e.g. "src/kernel/self_modifier.py"
    description: str
    status: str = "active"               # active | deprecated | failed
    dependencies: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class CapabilityRegistry:
    """Read/write store of Archi's capabilities, backed by a JSON file."""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else DEFAULT_REGISTRY_PATH
        self._capabilities: dict[str, Capability] = {}
        if self._path.exists():
            self._load()

    # --- Public API ---

    def register(self, cap: Capability) -> None:
        """Add or update a capability."""
        self._capabilities[cap.name] = cap
        self._save()
        logger.info("Registered capability: %s", cap.name)

    def remove(self, name: str) -> bool:
        """Remove a capability by name. Returns True if it existed."""
        if name in self._capabilities:
            del self._capabilities[name]
            self._save()
            logger.info("Removed capability: %s", name)
            return True
        return False

    def get(self, name: str) -> Optional[Capability]:
        return self._capabilities.get(name)

    def list_all(self) -> list[Capability]:
        return list(self._capabilities.values())

    def list_active(self) -> list[Capability]:
        return [c for c in self._capabilities.values() if c.status == "active"]

    def has(self, name: str) -> bool:
        return name in self._capabilities

    def names(self) -> set[str]:
        return set(self._capabilities.keys())

    # --- Persistence ---

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in data:
                cap = Capability(**entry)
                self._capabilities[cap.name] = cap
            logger.info("Loaded %d capabilities from %s.", len(self._capabilities), self._path)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Failed to load registry from %s: %s", self._path, exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(c) for c in self._capabilities.values()]
        self._path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
