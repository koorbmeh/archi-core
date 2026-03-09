"""
Event loop module for Archi's persistent asyncio runtime.

Provides an infinite event loop that continuously monitors for capability gaps
from operational history and executes actions without exiting after each cycle.
Integrates with Discord notifications, gap detection, and capability registry
to maintain a responsive, self-improving agent runtime.

Note: Inbound message polling (receiving messages from Jesse via Discord)
requires a bot gateway connection that does not exist yet. The poll task
is stubbed and will log a warning until a gateway capability is built.
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Coroutine, List, Optional

from capabilities.discord_notifier import notify_async
from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.gap_detector import detect_gaps

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL: float = 5.0
DEFAULT_GAP_CHECK_INTERVAL: float = 60.0
DEFAULT_HEARTBEAT_INTERVAL: float = 30.0


class PeriodicTask:
    """Represents a coroutine scheduled to run at a fixed interval."""

    def __init__(self, name: str, coro_factory: Callable[[], Coroutine], interval: float) -> None:
        self.name = name
        self.coro_factory = coro_factory
        self.interval = interval
        self.last_run: Optional[datetime] = None
        self.error_count: int = 0

    def is_due(self) -> bool:
        if self.last_run is None:
            return True
        return datetime.utcnow() >= self.last_run + timedelta(seconds=self.interval)

    def mark_run(self) -> None:
        self.last_run = datetime.utcnow()


class EventLoop:
    """
    Persistent asyncio event loop for Archi's continuous operation.

    Schedules and executes periodic tasks for message polling, gap detection,
    and heartbeat signals while handling shutdown gracefully.
    """

    def __init__(
        self,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        gap_check_interval: float = DEFAULT_GAP_CHECK_INTERVAL,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self.poll_interval = poll_interval
        self.gap_check_interval = gap_check_interval
        self.heartbeat_interval = heartbeat_interval
        self._running: bool = False
        self._tasks: List[PeriodicTask] = []
        self._asyncio_tasks: List[asyncio.Task] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def register_task(self, name: str, coro_factory: Callable[[], Coroutine], interval: float) -> None:
        """Register a periodic coroutine to be executed at the given interval."""
        task = PeriodicTask(name=name, coro_factory=coro_factory, interval=interval)
        self._tasks.append(task)
        logger.info("Registered periodic task '%s' with interval %.1fs", name, interval)

    async def _run_task_safely(self, task: PeriodicTask) -> None:
        """Execute a periodic task, handling and logging any exceptions."""
        try:
            await task.coro_factory()
            task.error_count = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task.error_count += 1
            logger.error(
                "Task '%s' raised an error (count=%d): %s",
                task.name,
                task.error_count,
                exc,
                exc_info=True,
            )
            if task.error_count >= 5:
                logger.critical("Task '%s' has failed %d times consecutively.", task.name, task.error_count)
                await _notify_critical_failure(task.name, exc)
        finally:
            task.mark_run()

    async def _scheduler_loop(self) -> None:
        """Core scheduling loop that dispatches due tasks concurrently."""
        logger.info("Archi event loop scheduler started.")
        while self._running:
            due_tasks = [t for t in self._tasks if t.is_due()]
            if due_tasks:
                await asyncio.gather(
                    *[self._run_task_safely(t) for t in due_tasks],
                    return_exceptions=False,
                )
            await asyncio.sleep(1.0)

    def _register_default_tasks(self) -> None:
        """Register built-in periodic tasks for message polling, gap detection, and heartbeat."""
        self.register_task(
            name="poll_user_messages",
            coro_factory=_poll_user_messages,
            interval=self.poll_interval,
        )
        self.register_task(
            name="detect_capability_gaps",
            coro_factory=_detect_capability_gaps,
            interval=self.gap_check_interval,
        )
        self.register_task(
            name="heartbeat",
            coro_factory=_send_heartbeat,
            interval=self.heartbeat_interval,
        )

    def _setup_signal_handlers(self) -> None:
        """Attach OS-level signal handlers for graceful shutdown."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._handle_shutdown_signal, sig)
            except (NotImplementedError, RuntimeError):
                logger.warning("Could not register signal handler for %s (platform limitation).", sig)

    def _handle_shutdown_signal(self, sig: signal.Signals) -> None:
        logger.info("Received shutdown signal %s. Initiating graceful shutdown.", sig.name)
        self._running = False

    async def _cancel_all_asyncio_tasks(self) -> None:
        for task in self._asyncio_tasks:
            if not task.done():
                task.cancel()
        if self._asyncio_tasks:
            await asyncio.gather(*self._asyncio_tasks, return_exceptions=True)
        self._asyncio_tasks.clear()

    async def run_async(self) -> None:
        """Start the event loop and run until a shutdown signal is received."""
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._register_default_tasks()
        self._setup_signal_handlers()

        logger.info("Archi persistent event loop starting at %s UTC.", datetime.utcnow().isoformat())
        await notify_async("Archi event loop started and running continuously.")

        scheduler = asyncio.create_task(self._scheduler_loop(), name="archi_scheduler")
        self._asyncio_tasks.append(scheduler)

        try:
            await scheduler
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled.")
        finally:
            await self._cancel_all_asyncio_tasks()
            await notify_async("Archi event loop has shut down gracefully.")
            logger.info("Archi event loop shut down at %s UTC.", datetime.utcnow().isoformat())

    def run(self) -> None:
        """Blocking entry point that runs the async event loop."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received; event loop stopping.")


# ---------------------------------------------------------------------------
# Standalone periodic coroutine factories
# ---------------------------------------------------------------------------

async def _poll_user_messages() -> None:
    """Poll for new messages from Jesse.

    Stub: Inbound message polling requires a Discord bot gateway connection
    (discord_gateway capability) that has not been built yet. This task will
    log a debug message and return until that capability exists.
    """
    logger.debug("poll_user_messages: no gateway capability yet — skipping.")


async def _detect_capability_gaps() -> None:
    """Analyse operational history for capability gaps and trigger logging."""
    registry = CapabilityRegistry()
    op_log = Path("data/operation_log.jsonl")
    gaps = detect_gaps(registry, log_path=op_log)
    if not gaps:
        logger.debug("No capability gaps detected.")
        return
    logger.info("Detected %d capability gap(s).", len(gaps))
    summary_lines = []
    for gap in gaps:
        logger.info("Gap: %s (priority=%.2f, source=%s)", gap.name, gap.priority, gap.source)
        summary_lines.append(f"- {gap.name} (priority={gap.priority:.2f})")
    summary = "\n".join(summary_lines)
    await notify_async(f"Capability gaps detected:\n{summary}")


async def _send_heartbeat() -> None:
    """Emit a periodic heartbeat to confirm the event loop is alive."""
    registry = CapabilityRegistry()
    count = len(registry.list_all())
    logger.debug("Heartbeat — registered capabilities: %d", count)


async def _notify_critical_failure(task_name: str, exc: Exception) -> None:
    """Notify Jesse of a critically failing task via Discord."""
    message = (
        f"Critical: Archi task '{task_name}' has failed 5 or more times consecutively.\n"
        f"Last error: {type(exc).__name__}: {exc}"
    )
    try:
        await notify_async(message)
    except Exception as notify_exc:
        logger.error("Failed to send critical failure notification: %s", notify_exc)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def create_event_loop(
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    gap_check_interval: float = DEFAULT_GAP_CHECK_INTERVAL,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
) -> EventLoop:
    """Factory that constructs a pre-configured EventLoop instance."""
    return EventLoop(
        poll_interval=poll_interval,
        gap_check_interval=gap_check_interval,
        heartbeat_interval=heartbeat_interval,
    )


def start() -> None:
    """Convenience entry point to create and run the default Archi event loop."""
    loop = create_event_loop()
    loop.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    start()
