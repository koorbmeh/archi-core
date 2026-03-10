#!/usr/bin/env python3
"""Archi — entry point.

Loads config, wires the two-model routing, seeds the capability registry
with the kernel components, and runs the generation loop.

Usage:
    python run.py              # run up to 5 cycles then exit
    python run.py --loop N     # run up to N cycles (stops when no gaps remain)
    python run.py --dry-run    # detect gaps and print them, no model calls
    python run.py --daemon     # persistent mode — run cycles on a schedule, notify via Discord
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from functools import partial
from pathlib import Path

# Load .env before any kernel imports that read env vars
from dotenv import load_dotenv
load_dotenv()

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.gap_detector import KERNEL_COMPONENTS
from src.kernel.generation_loop import CycleResult, run_cycle
from src.kernel.model_interface import (
    call_model, get_session_cost, get_task_config, reset_session,
)

REPO_ROOT = str(Path(__file__).resolve().parent)
REPO_ROOT_PATH = Path(__file__).resolve().parent
REGISTRY_PATH = Path("data/capability_registry.json")
OP_LOG_PATH = Path("data/operation_log.jsonl")

_GIT_LOCK_FILES = ["HEAD.lock", "index.lock"]
_STALE_LOCK_AGE_SECONDS = 60

# Daemon defaults
DEFAULT_CYCLE_INTERVAL_SECONDS = 300  # 5 minutes


def clean_stale_git_locks() -> None:
    """Remove stale git lock files left by crashed processes.

    A lock is stale if it is zero bytes OR older than 60 seconds.
    """
    git_dir = REPO_ROOT_PATH / ".git"
    log = logging.getLogger("archi.run")
    for lock_name in _GIT_LOCK_FILES:
        lock = git_dir / lock_name
        if not lock.exists():
            continue
        stat = lock.stat()
        age = time.time() - stat.st_mtime
        is_stale = stat.st_size == 0 or age > _STALE_LOCK_AGE_SECONDS
        if not is_stale:
            continue
        try:
            lock.unlink()
            log.info("Removed stale lock: %s (size=%d, age=%.0fs)",
                     lock_name, stat.st_size, age)
        except OSError:
            log.warning("Could not remove stale lock: %s", lock_name)


def setup_logging() -> None:
    import os
    level = os.environ.get("ARCHI_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(name)-30s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


def seed_kernel_capabilities(registry: CapabilityRegistry) -> int:
    """Register the six kernel components if they aren't already registered.

    Returns how many were newly registered. This is idempotent — on subsequent
    runs it does nothing because the registry already has them.
    """
    added = 0
    for name, module in KERNEL_COMPONENTS.items():
        if not registry.has(name):
            registry.register(Capability(
                name=name,
                module=module,
                description=f"Kernel component: {name}",
                status="active",
            ))
            added += 1
    return added


def make_plan_fn():
    """Build the plan callable using task-specific model config."""
    provider, model = get_task_config("plan")
    return partial(call_model, provider=provider, model=model)


def make_codegen_fn():
    """Build the codegen callable using task-specific model config."""
    provider, model = get_task_config("codegen")
    return partial(call_model, provider=provider, model=model)


def print_result(result: CycleResult, cycle_num: int) -> None:
    print(f"\n{'='*60}")
    print(f"  Cycle {cycle_num} — reached phase: {result.phase_reached}")
    if result.gap:
        print(f"  Gap: {result.gap.name} (priority {result.gap.priority:.2f})")
    if result.plan:
        print(f"  Plan: {result.plan.get('file_path')} — {result.plan.get('description')}")
    if result.change:
        print(f"  Change: {'SUCCESS' if result.change.success else 'FAILED'} — {result.change.message}")
        if not result.change.success and result.change.test_output:
            print(f"  Test output:\n{result.change.test_output}")
    if result.capability_registered:
        print(f"  Capability registered: {result.gap.name}")
    if result.error:
        print(f"  Error: {result.error}")
    print(f"  Session cost so far: ${get_session_cost():.4f}")
    print(f"{'='*60}\n")


def run_dry(registry: CapabilityRegistry) -> None:
    """Detect and print gaps without making any model calls."""
    from src.kernel.gap_detector import detect_gaps
    gaps = detect_gaps(registry, OP_LOG_PATH)
    if not gaps:
        print("No gaps detected. Archi has nothing to do.")
        return
    print(f"\n{len(gaps)} gap(s) detected:\n")
    for i, g in enumerate(gaps, 1):
        print(f"  {i}. {g.name}  (priority={g.priority:.2f}, source={g.source})")
        print(f"     Reason: {g.reason}")
        if g.evidence:
            print(f"     Evidence: {', '.join(g.evidence)}")
    print()


# ---------------------------------------------------------------------------
# Discord notification helper (lazy import, graceful fallback)
# ---------------------------------------------------------------------------

def _discord_notify(text: str) -> bool:
    """Send a Discord DM. Returns False silently if not configured."""
    try:
        from capabilities.discord_notifier import notify
        return notify(text)
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Daemon mode
# ---------------------------------------------------------------------------

class ArchiDaemon:
    """Persistent Archi process with two parallel tasks.

    Architecture (Option B):
    - **Message task**: polls the Discord listener queue every 2 seconds and
      processes messages immediately.  Conversations feel near-instant.
    - **Generation task**: runs gap-detect → plan → generate → integrate on a
      timer, but only when the message task isn't holding the lock.

    Both tasks share an asyncio.Lock so they never write to the operation log,
    capability registry, or git working tree at the same time.  The message
    task holds the lock briefly (classify + log + respond).  The generation
    task holds it for the full build sequence.
    """

    MESSAGE_POLL_SECONDS = 2  # how often we check for new Discord messages

    def __init__(
        self,
        registry: CapabilityRegistry,
        plan_fn,
        codegen_fn,
        interval: int = DEFAULT_CYCLE_INTERVAL_SECONDS,
        max_cycles_per_wake: int = 5,
    ):
        self.registry = registry
        self.plan_fn = plan_fn
        self.codegen_fn = codegen_fn
        self.interval = interval
        self.max_cycles_per_wake = max_cycles_per_wake
        self._running = False
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger("archi.daemon")

    # ------------------------------------------------------------------
    # Message task — responds to Jesse's DMs in near-realtime
    # ------------------------------------------------------------------

    async def _message_loop(self) -> None:
        """Continuously poll the Discord message queue and process each one.

        Acquires the shared lock only while processing a single message so
        that generation cycles can interleave between messages.
        """
        self._logger.info("Message loop started (poll every %ds).", self.MESSAGE_POLL_SECONDS)
        while self._running:
            try:
                from capabilities.discord_listener import process_one
                # Drain one message at a time so we release the lock between
                # messages and give the generation task a chance to acquire it.
                while self._running:
                    async with self._lock:
                        processed = await process_one(REPO_ROOT, self.registry)
                    if not processed:
                        break  # queue is empty
                    self._logger.info("Processed a Discord message.")
            except Exception as exc:
                self._logger.debug("Message loop poll: %s", exc)

            # Wait before polling again — short enough for near-instant feel
            for _ in range(self.MESSAGE_POLL_SECONDS):
                if not self._running:
                    return
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Generation task — builds capabilities on a schedule
    # ------------------------------------------------------------------

    async def _generation_loop(self) -> None:
        """Run generation cycles on a timer, acquiring the lock for each cycle.

        Only starts a cycle when the lock is available (i.e. no message is
        being processed).  After a batch of up to max_cycles_per_wake cycles,
        sleeps for the configured interval before trying again.
        """
        self._logger.info("Generation loop started (interval %ds).", self.interval)
        loop = asyncio.get_running_loop()

        while self._running:
            clean_stale_git_locks()
            reset_session()

            for cycle_num in range(1, self.max_cycles_per_wake + 1):
                if not self._running:
                    return
                self._logger.info(
                    "--- Generation cycle %d of %d ---",
                    cycle_num, self.max_cycles_per_wake,
                )
                async with self._lock:
                    result = await loop.run_in_executor(
                        None,
                        lambda: run_cycle(
                            REPO_ROOT, self.registry, OP_LOG_PATH,
                            plan_fn=self.plan_fn, generate_fn=self.codegen_fn,
                        ),
                    )
                print_result(result, cycle_num)

                if result.phase_reached == "observe":
                    self._logger.info("No gaps remain. Sleeping until next interval.")
                    break
                if result.error and "budget" in result.error.lower():
                    self._logger.warning("Budget limit hit — sleeping until next interval.")
                    break

            self._logger.info(
                "Generation batch done. Session cost: $%.4f. Sleeping %ds.",
                get_session_cost(), self.interval,
            )

            # Sleep in 1-second increments so shutdown is responsive
            for _ in range(self.interval):
                if not self._running:
                    return
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start both tasks and wait for shutdown."""
        self._running = True
        loop = asyncio.get_running_loop()

        # Register signal handlers for graceful shutdown
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown, sig)
            except (NotImplementedError, RuntimeError):
                pass  # Windows doesn't support add_signal_handler

        # Startup notification
        cap_count = len(self.registry.list_active())
        _discord_notify(
            f"Archi is online. {cap_count} capabilities registered. "
            f"Monitoring for gaps every {self.interval}s."
        )
        self._logger.info(
            "Archi daemon started. %d capabilities. Cycle interval: %ds.",
            cap_count, self.interval,
        )

        # Start Discord gateway so Archi can receive DMs from Jesse
        try:
            from capabilities.discord_gateway import start_gateway
            start_gateway()
            self._logger.info("Discord gateway started — listening for DMs.")
            # Let the gateway connect before spawning tasks
            await asyncio.sleep(3)
        except Exception as exc:
            self._logger.warning("Could not start Discord gateway: %s", exc)

        # Spawn the two independent tasks
        msg_task = asyncio.create_task(
            self._message_loop(), name="archi_message_loop",
        )
        gen_task = asyncio.create_task(
            self._generation_loop(), name="archi_generation_loop",
        )

        try:
            # Wait until both finish (which only happens on shutdown)
            await asyncio.gather(msg_task, gen_task, return_exceptions=True)
        finally:
            # Send offline notification before tearing down connections
            try:
                from capabilities.discord_notifier import notify_async, shutdown as _discord_shutdown
                await notify_async("Archi going offline.")
                await _discord_shutdown()
            except Exception:
                pass
            # Stop Discord gateway after notifier is done
            try:
                from capabilities.discord_gateway import stop_gateway
                await stop_gateway()
            except Exception:
                pass
            self._logger.info("Archi daemon stopped.")

    def _shutdown(self, sig) -> None:
        self._logger.info("Received %s — shutting down.", sig.name if hasattr(sig, 'name') else sig)
        self._running = False


def run_daemon(registry: CapabilityRegistry, interval: int) -> None:
    """Entry point for daemon mode."""
    plan_fn = make_plan_fn()
    codegen_fn = make_codegen_fn()
    daemon = ArchiDaemon(registry, plan_fn, codegen_fn, interval=interval)

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass  # Shutdown notification already sent in the finally block


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run Archi's generation loop.")
    parser.add_argument("--loop", type=int, default=5, metavar="N",
                        help="Run up to N cycles (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect gaps and print them; no model calls")
    parser.add_argument("--daemon", action="store_true",
                        help="Persistent mode — run cycles on a schedule, notify via Discord")
    parser.add_argument("--interval", type=int, default=DEFAULT_CYCLE_INTERVAL_SECONDS,
                        metavar="S",
                        help=f"Daemon cycle interval in seconds (default: {DEFAULT_CYCLE_INTERVAL_SECONDS})")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("archi.run")

    # Initialize registry and seed kernel components
    registry = CapabilityRegistry(REGISTRY_PATH)
    seeded = seed_kernel_capabilities(registry)
    if seeded:
        logger.info("Seeded %d kernel capabilities into registry.", seeded)

    if args.dry_run:
        run_dry(registry)
        return 0

    if args.daemon:
        # Show routing config
        plan_p, plan_m = get_task_config("plan")
        code_p, code_m = get_task_config("codegen")
        logger.info("Plan model:    %s/%s", plan_p, plan_m)
        logger.info("Codegen model: %s/%s", code_p, code_m)
        logger.info("Daemon mode — interval: %ds", args.interval)
        run_daemon(registry, args.interval)
        return 0

    # Normal one-shot mode
    plan_p, plan_m = get_task_config("plan")
    code_p, code_m = get_task_config("codegen")
    logger.info("Plan model:    %s/%s", plan_p, plan_m)
    logger.info("Codegen model: %s/%s", code_p, code_m)

    plan_fn = make_plan_fn()
    codegen_fn = make_codegen_fn()
    reset_session()
    clean_stale_git_locks()

    for cycle_num in range(1, args.loop + 1):
        logger.info("--- Cycle %d of %d ---", cycle_num, args.loop)
        result = run_cycle(
            REPO_ROOT, registry, OP_LOG_PATH,
            plan_fn=plan_fn, generate_fn=codegen_fn,
        )
        print_result(result, cycle_num)

        if result.phase_reached == "observe":
            logger.info("No gaps remain. Archi is caught up.")
            break
        if result.error and "budget" in result.error.lower():
            logger.warning("Budget limit hit — stopping.")
            break

    logger.info("Final session cost: $%.4f", get_session_cost())
    _cleanup_sessions()
    return 0


def _cleanup_sessions() -> None:
    """Close any open aiohttp sessions (discord_notifier, etc.) on exit."""
    try:
        from capabilities.discord_notifier import shutdown as _discord_shutdown
        asyncio.run(_discord_shutdown())
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
