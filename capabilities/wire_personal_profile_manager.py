"""
Integrates the personal_profile_manager into the event loop via a periodic task for
profile updates and gap prompting, ensuring reachability and active usage.
"""

import asyncio
from pathlib import Path
from typing import Optional

import src.kernel.capability_registry
from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.event_loop import EventLoop, PeriodicTask, create_event_loop
from capabilities.personal_profile_manager import (
    get_manager,
    periodic_update,
    register_capability as ppm_register_capability,
)


_event_loop: Optional[EventLoop] = None


def get_event_loop_instance() -> EventLoop:
    global _event_loop
    if _event_loop is None:
        _event_loop = create_event_loop()
    return _event_loop


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Optional[Capability]:
    if registry is None:
        return None
    cap = Capability(
        name="wire_personal_profile_manager",
        module="capabilities.wire_personal_profile_manager",
        description="Integrates personal_profile_manager into the event loop via a periodic task for profile updates and gap prompting.",
        dependencies=["personal_profile_manager", "event_loop"],
    )
    registry.register(cap)
    return cap


def initialize(
    data_dir: Path = Path("data"),
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    if registry is None:
        registry_path = data_dir / "capabilities.json"
        registry = CapabilityRegistry(registry_path)
    ppm_register_capability(registry)
    wire_cap = register_capability(registry)
    if loop is None:
        loop = get_event_loop_instance()
    get_manager()
    async def update_coro() -> None:
        await asyncio.to_thread(periodic_update)

    task = PeriodicTask(
        name="personal_profile_update",
        coro_factory=update_coro,
        interval=3600.0,
    )
    loop.add_periodic_task(task)
    return wire_cap