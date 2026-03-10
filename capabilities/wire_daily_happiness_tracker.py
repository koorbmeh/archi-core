"""
Integrates the daily_happiness_tracker into the active event loop pathway by
initializing it and scheduling its periodic tasks for daily happiness prompts,
response checks, and weekly summaries to benefit Jesse.
"""

from pathlib import Path
from typing import Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.daily_happiness_tracker import (
    initialize as tracker_initialize,
    integrate_with_event_loop as tracker_integrate,
    register_capability as tracker_register,
)
from capabilities.event_loop import EventLoop, create_event_loop


_loop_instance: Optional[EventLoop] = None
_initialized: bool = False


def get_event_loop_instance() -> EventLoop:
    global _loop_instance
    if _loop_instance is None:
        _loop_instance = create_event_loop()
    return _loop_instance


def initialize(
    data_dir: Path = Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    global _initialized
    if _initialized:
        reg = registry or CapabilityRegistry()
        # Return self capability assuming already registered
        return Capability(
            name="wire_daily_happiness_tracker",
            module="capabilities.wire_daily_happiness_tracker",
            description=(
                "Integrates the daily_happiness_tracker into the active event loop "
                "pathway by initializing it and scheduling its periodic tasks for "
                "daily happiness prompts, response checks, and weekly summaries "
                "to benefit Jesse."
            ),
            dependencies=["daily_happiness_tracker", "event_loop", "capability_registry"],
        )

    if registry is None:
        registry = CapabilityRegistry()

    tracker_register(registry)

    if loop is None:
        loop = get_event_loop_instance()

    tracker_initialize(data_dir=data_dir, registry=registry, event_loop=loop)
    tracker_integrate(loop)

    cap = Capability(
        name="wire_daily_happiness_tracker",
        module="capabilities.wire_daily_happiness_tracker",
        description=(
            "Integrates the daily_happiness_tracker into the active event loop "
            "pathway by initializing it and scheduling its periodic tasks for "
            "daily happiness prompts, response checks, and weekly summaries "
            "to benefit Jesse."
        ),
        status="active",
        dependencies=["daily_happiness_tracker", "event_loop", "capability_registry"],
    )
    registry.register(cap)

    _initialized = True
    return cap


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="wire_daily_happiness_tracker",
        module="capabilities.wire_daily_happiness_tracker",
        description=(
            "Integrates the daily_happiness_tracker into the active event loop "
            "pathway by initializing it and scheduling its periodic tasks for "
            "daily happiness prompts, response checks, and weekly summaries "
            "to benefit Jesse."
        ),
        status="active",
        dependencies=["daily_happiness_tracker", "event_loop", "capability_registry"],
    )
    registry.register(cap)
    return cap