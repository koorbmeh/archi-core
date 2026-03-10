"""
capabilities/event_loop.py

Persistent asyncio event loop for Archi's continuous operation.
Integrates gap_logging_sync to enable periodic synchronization of
detected gaps with Discord.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from src.kernel.capability_registry import CapabilityRegistry
from capabilities import gap_logging_sync

logger = logging.getLogger(__name__)


@dataclass
class PeriodicTask:
    """Represents a coroutine scheduled to run at a fixed interval."""

    name: str
    coro_factory: Callable[[], Coroutine]
    interval: float


class EventLoop:
    """Persistent asyncio event loop for Archi's continuous operation."""

    def __init__(
        self,
        poll_interval: float = 5.0,
        gap_check_interval: float = 60.0,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self.poll_interval = poll_interval
        self.gap_check_interval = gap_check_interval
        self.heartbeat_interval = heartbeat_interval
        self._tasks: list[PeriodicTask] = []
        self._running = False

    def register_task(self, task: PeriodicTask) -> None:
        """Register a periodic task with this event loop."""
        self._tasks.append(task)
        logger.info("Registered periodic task: %s (interval=%.1fs)", task.name, task.interval)

    # Alias — auto-generated capabilities often use this name
    add_periodic_task = register_task

    async def _run_periodic(self, task: PeriodicTask) -> None:
        """Drive a single periodic task at its configured interval."""
        while self._running:
            try:
                await task.coro_factory()
            except Exception as exc:
                logger.exception("Error in periodic task %s: %s", task.name, exc)
            await asyncio.sleep(task.interval)

    async def _heartbeat(self) -> None:
        """Emit a heartbeat log entry."""
        logger.debug("EventLoop heartbeat")

    async def run(self) -> None:
        """Start all registered periodic tasks and run until cancelled."""
        self._running = True
        logger.info("EventLoop starting with %d registered tasks.", len(self._tasks))

        heartbeat = PeriodicTask(
            name="heartbeat",
            coro_factory=self._heartbeat,
            interval=self.heartbeat_interval,
        )
        all_tasks = self._tasks + [heartbeat]

        coroutines = [self._run_periodic(t) for t in all_tasks]
        try:
            await asyncio.gather(*coroutines)
        except asyncio.CancelledError:
            logger.info("EventLoop cancelled.")
        finally:
            self._running = False

    def stop(self) -> None:
        """Signal the event loop to stop."""
        self._running = False


def create_event_loop(
    poll_interval: float = 5.0,
    gap_check_interval: float = 60.0,
    heartbeat_interval: float = 30.0,
) -> "EventLoop":
    """Factory that constructs a pre-configured EventLoop instance."""
    loop = EventLoop(
        poll_interval=poll_interval,
        gap_check_interval=gap_check_interval,
        heartbeat_interval=heartbeat_interval,
    )

    registry = CapabilityRegistry()
    gap_logging_sync.initialize(registry)
    gap_logging_sync.integrate_with_event_loop(loop)

    logger.info("EventLoop created with gap_logging_sync integrated.")
    return loop


def start() -> None:
    """Convenience entry point to create and run the default Archi event loop."""
    loop = create_event_loop()
    try:
        asyncio.run(loop.run())
    except KeyboardInterrupt:
        logger.info("EventLoop stopped by keyboard interrupt.")