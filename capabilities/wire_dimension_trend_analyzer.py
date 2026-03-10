"""Wiring module that initializes and integrates the dimension_trend_analyzer into the
event loop for periodic trend analysis execution.
"""

import pathlib
from typing import Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.dimension_trend_analyzer import DimensionTrendAnalyzer
from capabilities.event_loop import EventLoop, create_event_loop


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
        name="wire_dimension_trend_analyzer",
        module="capabilities.wire_dimension_trend_analyzer",
        description="Wiring module that initializes and integrates the dimension_trend_analyzer into the event loop for periodic trend analysis execution.",
        dependencies=["dimension_trend_analyzer", "event_loop", "capability_registry"],
    )
    registry.register(cap)
    return cap


def initialize(
    data_dir: pathlib.Path = pathlib.Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    if loop is None:
        loop = get_event_loop_instance()
    analyzer = DimensionTrendAnalyzer(data_dir)
    analyzer.initialize(data_dir=data_dir, registry=registry, event_loop=loop)
    analyzer.integrate_with_event_loop(loop)
    analyzer.register_capability(registry)
    return register_capability(registry)