"""Wires the daily_agency_tracker capability into the event loop by providing initialization and registration functions that integrate it for periodic prompting and response checking."""

from pathlib import Path
from typing import Optional

import capabilities.daily_agency_tracker as daily_agency_tracker
import capabilities.event_loop as event_loop
from src.kernel.capability_registry import Capability, CapabilityRegistry

_loop: Optional[event_loop.EventLoop] = None


def get_event_loop_instance() -> event_loop.EventLoop:
    global _loop
    if _loop is None:
        _loop = event_loop.create_event_loop()
    return _loop


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="wire_daily_agency_tracker",
        module="capabilities.wire_daily_agency_tracker",
        description="Wires the daily_agency_tracker capability into the event loop by providing initialization and registration functions that integrate it for periodic prompting and response checking.",
        dependencies=["daily_agency_tracker", "event_loop"],
    )
    return cap


def initialize(
    data_dir: Path = Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[event_loop.EventLoop] = None,
) -> Capability:
    loop = loop or get_event_loop_instance()
    daily_agency_tracker.initialize(data_dir, registry, loop)
    daily_agency_tracker.integrate_with_event_loop(loop)
    target_cap = daily_agency_tracker.register_capability(registry)
    if target_cap is None:
        raise ValueError("Failed to register daily_agency_tracker capability")
    return target_cap