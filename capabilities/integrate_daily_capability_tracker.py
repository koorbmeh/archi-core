"""Integration bridge for daily_capability_tracker.

Imports the tracker, initializes it, and registers its periodic tasks
with the event loop so it becomes reachable at runtime.
"""

import logging
from pathlib import Path

from capabilities.daily_capability_tracker import initialize, register_capability
from capabilities.event_loop import EventLoop, PeriodicTask
from src.kernel.capability_registry import Capability, CapabilityRegistry

logger = logging.getLogger(__name__)


def wire(
    registry: CapabilityRegistry | None = None,
    event_loop: EventLoop | None = None,
    data_dir: Path = Path("data"),
) -> None:
    """Initialize and wire the daily_capability_tracker into the system."""
    initialize(data_dir=data_dir, registry=registry, event_loop=event_loop)
    logger.info("daily_capability_tracker wired into event loop.")


def register_wire_capability(
    registry: CapabilityRegistry | None = None,
) -> Capability:
    """Register the wire capability itself."""
    cap = Capability(
        name="wire_daily_capability_tracker",
        module="capabilities.integrate_daily_capability_tracker",
        description="Wires daily_capability_tracker into the event loop.",
        status="active",
        dependencies=["daily_capability_tracker", "event_loop"],
    )
    if registry is not None:
        registry.register(cap)
    return cap
