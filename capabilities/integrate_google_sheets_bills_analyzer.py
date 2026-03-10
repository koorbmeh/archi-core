"""
Thin bridge module that initializes and wires google_sheets_bills_analyzer into
the event loop for periodic bill analysis and into the Discord message queue
for handling user-submitted bill sheets.
"""

import asyncio
from pathlib import Path
from typing import List, Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.conversational_memory import store_message
from capabilities.event_loop import EventLoop, create_event_loop
from capabilities.google_sheets_bills_analyzer import (
    initialize as analyzer_initialize,
    integrate_with_event_loop as analyzer_integrate_with_event_loop,
    receive_message as analyzer_receive_message,
    process_queue_coro as analyzer_process_queue_coro,
)


_event_loop_instance: Optional[EventLoop] = None


def get_event_loop_instance() -> EventLoop:
    global _event_loop_instance
    if _event_loop_instance is None:
        _event_loop_instance = create_event_loop()
    return _event_loop_instance


def register_capability(
    registry: Optional[CapabilityRegistry] = None,
) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="wire_google_sheets_bills_analyzer",
        module="capabilities.integrate_google_sheets_bills_analyzer",
        description=(
            "Thin bridge module that initializes and wires "
            "google_sheets_bills_analyzer into the event loop for periodic bill "
            "analysis and into the Discord message queue for handling "
            "user-submitted bill sheets."
        ),
        dependencies=["google_sheets_bills_analyzer", "event_loop"],
    )
    registry.capabilities.append(cap)
    return cap


def initialize(
    registry: Optional[CapabilityRegistry] = None,
    loop: Optional[EventLoop] = None,
) -> Capability:
    analyzer_initialize()
    if loop is None:
        loop = get_event_loop_instance()
    analyzer_integrate_with_event_loop(loop)
    return register_capability(registry)


def receive_message(
    content: str,
    user_id: str,
    *,
    attachment_urls: List[str] | None = None,
) -> None:
    store_message(user_id, content)
    analyzer_receive_message(content, user_id, attachment_urls or [])


async def process_one(
    repo_path: str,
    registry: CapabilityRegistry,
) -> bool:
    await analyzer_process_queue_coro()
    return True


async def process_pending(
    repo_path: str,
    registry: CapabilityRegistry,
) -> int:
    await analyzer_process_queue_coro()
    return 1