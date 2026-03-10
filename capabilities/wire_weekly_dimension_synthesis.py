"""Integration capability that initializes, registers, and wires the weekly_dimension_synthesis
into the event loop for periodic execution."""

from pathlib import Path
from typing import Optional

import src.kernel.capability_registry
from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.event_loop import EventLoop, create_event_loop
from capabilities.weekly_dimension_synthesis import (
    initialize as wds_initialize,
    integrate_with_event_loop as wds_integrate_with_event_loop,
    register_capability as wds_register_capability,
)


_loop_instance: Optional[EventLoop] = None


def get_event_loop_instance() -> EventLoop:
    global _loop_instance
    if _loop_instance is None:
        _loop_instance = create_event_loop()
    return _loop_instance


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Capability:
    return Capability(
        name="wire_weekly_dimension_synthesis",
        module="capabilities.wire_weekly_dimension_synthesis",
        description=(
            "Integration capability that initializes, registers, and wires the "
            "weekly_dimension_synthesis into the event loop for periodic execution."
        ),
        dependencies=["weekly_dimension_synthesis", "event_loop", "capability_registry"],
    )


def initialize(
    data_dir: Path = Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    if loop is None:
        loop = get_event_loop_instance()
    if registry is None:
        registry = CapabilityRegistry(data_dir / "capabilities.json")
    wds_register_capability(registry)
    wds_initialize(data_dir, registry, loop)
    wds_integrate_with_event_loop(loop)
    return register_capability(registry)