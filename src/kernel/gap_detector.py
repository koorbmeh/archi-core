"""Gap detector — surfaces capability gaps from operational evidence.

Not a hardcoded wish list. Gaps emerge from three sources:
1. Structural: the kernel's own dependency graph has unresolved edges.
2. Operational: logged failures reference capabilities that don't exist.
3. Registry: capabilities marked "failed" or with unmet dependencies.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.kernel.capability_registry import CapabilityRegistry

logger = logging.getLogger(__name__)


@dataclass
class Gap:
    """A detected capability gap."""
    name: str
    source: str          # "structural" | "operational" | "registry"
    reason: str
    priority: float      # 0.0 (low) to 1.0 (critical)
    evidence: list[str] = field(default_factory=list)


# --- Structural gap detection ---
# The kernel defines its own expected components. If any are missing from
# the registry, that's a structural gap. This list is the ONE hardcoded
# thing — and it's intentionally minimal: just the kernel's own wiring.

KERNEL_COMPONENTS = {
    "self_modifier":       "src/kernel/self_modifier.py",
    "gap_detector":        "src/kernel/gap_detector.py",
    "capability_registry": "src/kernel/capability_registry.py",
    "model_interface":     "src/kernel/model_interface.py",
    "generation_loop":     "src/kernel/generation_loop.py",
    "alignment_gates":     "src/kernel/alignment_gates.py",
}


def detect_structural_gaps(registry: CapabilityRegistry) -> list[Gap]:
    """Find kernel components not yet in the registry."""
    registered = registry.names()
    gaps = []
    for name, module in KERNEL_COMPONENTS.items():
        if name not in registered:
            gaps.append(Gap(
                name=name,
                source="structural",
                reason=f"Kernel component {module} not registered.",
                priority=0.9,
                evidence=[f"Expected in KERNEL_COMPONENTS, absent from registry."],
            ))
    return gaps


def detect_registry_gaps(registry: CapabilityRegistry) -> list[Gap]:
    """Find capabilities with unmet dependencies or in failed state."""
    gaps = []
    registered = registry.names()
    for cap in registry.list_all():
        if cap.status == "failed":
            gaps.append(Gap(
                name=cap.name,
                source="registry",
                reason=f"Capability '{cap.name}' is in failed state.",
                priority=0.8,
                evidence=[f"status={cap.status}"],
            ))
        for dep in cap.dependencies:
            if dep not in registered:
                gaps.append(Gap(
                    name=dep,
                    source="registry",
                    reason=f"Unmet dependency of '{cap.name}'.",
                    priority=0.85,
                    evidence=[f"Required by {cap.name}, not in registry."],
                ))
    return gaps


def detect_operational_gaps(log_path: Optional[Path] = None) -> list[Gap]:
    """Scan operation logs for failure patterns referencing missing capabilities.

    Log format: one JSON object per line with at minimum:
        {"event": "...", "success": bool, "missing_capability": "..." (optional)}
    """
    path = log_path or Path("data/operation_log.jsonl")
    if not path.exists():
        return []
    gaps: dict[str, Gap] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("success"):
                continue
            missing = entry.get("missing_capability")
            if not missing:
                continue
            if missing in gaps:
                gaps[missing].evidence.append(entry.get("event", "unknown"))
            else:
                gaps[missing] = Gap(
                    name=missing,
                    source="operational",
                    reason=f"Operation failed due to missing '{missing}'.",
                    priority=0.7,
                    evidence=[entry.get("event", "unknown")],
                )
    except OSError as exc:
        logger.error("Could not read operation log: %s", exc)
    # Boost priority for gaps with many failure instances
    for gap in gaps.values():
        gap.priority = min(1.0, 0.7 + 0.05 * len(gap.evidence))
    return list(gaps.values())


def detect_gaps(
    registry: CapabilityRegistry,
    log_path: Optional[Path] = None,
) -> list[Gap]:
    """Run all gap detection strategies and return deduplicated, ranked gaps."""
    all_gaps: dict[str, Gap] = {}
    for gap in (detect_structural_gaps(registry)
                + detect_registry_gaps(registry)
                + detect_operational_gaps(log_path)):
        if gap.name in all_gaps:
            existing = all_gaps[gap.name]
            existing.priority = max(existing.priority, gap.priority)
            existing.evidence.extend(gap.evidence)
            existing.source = _higher_source(existing.source, gap.source)
        else:
            all_gaps[gap.name] = gap
    return sorted(all_gaps.values(), key=lambda g: g.priority, reverse=True)


def _higher_source(a: str, b: str) -> str:
    """Prefer the more actionable source."""
    rank = {"structural": 2, "registry": 1, "operational": 0}
    return a if rank.get(a, 0) >= rank.get(b, 0) else b
