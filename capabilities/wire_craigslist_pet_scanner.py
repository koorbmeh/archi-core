"""Integrates craigslist_pet_scanner into the event loop.

Provides self-registration, initialization with scanner setup, and shared event
loop access following the standard wire_* pattern.
"""

from pathlib import Path
from typing import Optional

import capabilities.craigslist_pet_scanner as pet_scanner
from capabilities.craigslist_pet_scanner import get_scanner, integrate_with_event_loop
from capabilities.event_loop import EventLoop, create_event_loop

from src.kernel.capability_registry import Capability, CapabilityRegistry


def get_event_loop_instance() -> EventLoop:
    if not hasattr(get_event_loop_instance, "_instance"):
        get_event_loop_instance._instance = create_event_loop()
    return get_event_loop_instance._instance


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="craigslist_pet_scanner",
        module="capabilities.craigslist_pet_scanner",
        description="Periodically scans Craigslist for pet listings via the event loop.",
        dependencies=["event_loop"],
    )
    registry.add(cap)
    return cap


def initialize(
    data_dir: Path = Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    loop = loop or get_event_loop_instance()
    scanner = get_scanner(data_dir)
    integrate_with_event_loop(loop)
    return register_capability(registry)