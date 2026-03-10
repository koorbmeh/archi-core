"""Integrates the daily_health_tracker capability into the event_loop by adding a daily
periodic task for health monitoring."""

from pathlib import Path
from typing import Callable, Awaitable

from src.kernel.capability_registry import Capability, CapabilityRegistry
from capabilities.event_loop import EventLoop, PeriodicTask
from capabilities.daily_health_tracker import run_daily_check


def integrate_with_event_loop(loop: EventLoop, log_path: Path | None = None) -> None:
    """Add a daily PeriodicTask to the given EventLoop that runs daily_health_tracker."""
    async def daily_health_coro() -> None:
        run_daily_check(log_path)

    coro_factory: Callable[[], Awaitable[None]] = lambda: daily_health_coro()
    task = PeriodicTask(
        name="daily_health_tracker",
        coro_factory=coro_factory,
        interval=86400.0,  # 24 hours in seconds
    )
    loop.add_task(task)


def register_capability(registry: CapabilityRegistry | None = None) -> Capability:
    """Register this integration capability with the given registry."""
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="integrate_daily_health_tracker",
        module="capabilities.integrate_daily_health_tracker",
        description=__doc__,
        status="active",
        dependencies=["daily_health_tracker", "event_loop"],
    )
    registry.add(cap)
    return cap