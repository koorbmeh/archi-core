"""Wires the daily_action_recommender capability into the event loop to enable
periodic generation and delivery of daily action recommendations to Jesse via
notifications."""

import pathlib
from typing import Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry
from capabilities.daily_action_recommender import DailyActionRecommender
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
        name="wire_daily_action_recommender",
        module="capabilities.wire_daily_action_recommender",
        description="Wires the daily_action_recommender capability into the event loop to enable periodic generation and delivery of daily action recommendations to Jesse via notifications.",
        dependencies=[
            "daily_action_recommender",
            "event_loop",
            "capability_registry",
            "discord_notifier",
        ],
    )
    registry.add(cap)
    return cap


def initialize(
    data_dir: pathlib.Path = pathlib.Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    cap = register_capability(registry)
    recommender = DailyActionRecommender(data_dir)
    target_loop = loop or get_event_loop_instance()
    recommender.integrate_with_event_loop(target_loop)
    return cap