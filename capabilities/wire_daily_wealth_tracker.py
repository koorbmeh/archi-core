"""Integrates the daily_wealth_tracker capability into the event loop as a daily
periodic task, making it reachable and active for tracking Jesse's wealth.
"""

from capabilities.daily_wealth_tracker import track_daily_wealth
from capabilities.event_loop import EventLoop, PeriodicTask
from src.kernel.capability_registry import Capability, CapabilityRegistry


def register_capability(
    registry: CapabilityRegistry | None = None,
) -> Capability:
    """Register the wire_daily_wealth_tracker capability."""
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="wire_daily_wealth_tracker",
        module="capabilities.wire_daily_wealth_tracker",
        description="Integrates daily_wealth_tracker into the event loop as a daily periodic task for tracking Jesse's wealth.",
        dependencies=["daily_wealth_tracker", "event_loop"],
    )
    registry.add(cap)
    return cap


def integrate_with_event_loop(
    event_loop: EventLoop,
) -> None:
    """Add daily wealth tracker as a periodic task to the event loop."""
    task = PeriodicTask(
        name="daily_wealth_tracker",
        coro_factory=track_daily_wealth,
        interval=86400.0,  # 24 hours
    )
    event_loop.add_periodic_task(task)