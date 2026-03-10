"""Integrates weekly_full_dimension_synthesis into the active event loop pathway by providing
initialization, registration, and event loop integration functions following established wiring patterns."""

from pathlib import Path
from typing import Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.event_loop import EventLoop, create_event_loop
from capabilities.weekly_full_dimension_synthesis import (
    initialize as wfd_initialize,
    integrate_with_event_loop as wfd_integrate_with_event_loop,
)

_event_loop: Optional[EventLoop] = None


def get_event_loop_instance() -> EventLoop:
    global _event_loop
    if _event_loop is None:
        _event_loop = create_event_loop()
    return _event_loop


def register_capability(
    registry: Optional[CapabilityRegistry] = None,
) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="wire_weekly_full_dimension_synthesis",
        module=__name__,
        description=(
            "Integrates weekly_full_dimension_synthesis into the active event loop pathway."
        ),
        dependencies=["weekly_full_dimension_synthesis", "event_loop"],
    )
    registry.register(cap)
    return cap


def initialize(
    data_dir: Path = Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    loop = loop or get_event_loop_instance()
    wfd_initialize(data_dir, registry, loop)
    wfd_integrate_with_event_loop(loop)
    return register_capability(registry)