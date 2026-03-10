"""
Integrates detailed_skills_inventory_builder into the event loop as a periodic task
to maintain an up-to-date skills inventory in Jesse's personal profile.
"""

import asyncio

from src.kernel.capability_registry import Capability, CapabilityRegistry
from capabilities.detailed_skills_inventory_builder import DetailedSkillsInventoryBuilder
from capabilities.event_loop import EventLoop, create_event_loop
from capabilities.personal_profile_manager import get_manager


_event_loop: EventLoop | None = None
_builder: DetailedSkillsInventoryBuilder | None = None


def get_event_loop_instance() -> EventLoop:
    global _event_loop
    if _event_loop is None:
        _event_loop = create_event_loop()
    return _event_loop


def get_builder() -> DetailedSkillsInventoryBuilder:
    global _builder
    if _builder is None:
        profile_path = get_manager().profile_path
        _builder = DetailedSkillsInventoryBuilder(profile_path)
    return _builder


def register_capability(registry: CapabilityRegistry | None = None) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name='wire_detailed_skills_inventory_builder',
        module=__name__,
        description='Integrates detailed_skills_inventory_builder into the event loop as a periodic task to maintain an up-to-date skills inventory in Jesse\'s personal profile.',
        status='active',
        dependencies=['detailed_skills_inventory_builder', 'event_loop', 'personal_profile_manager']
    )
    registry.add(cap)
    return cap


def initialize(registry: CapabilityRegistry | None = None, loop: EventLoop | None = None) -> Capability:
    if loop is None:
        loop = get_event_loop_instance()
    get_builder()
    loop.add_periodic_task(
        name='detailed_skills_inventory_update',
        coro_factory=lambda: asyncio.to_thread(get_builder().update),
        interval=86400.0  # Daily
    )
    return register_capability(registry)