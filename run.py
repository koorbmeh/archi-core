"""
run.py — Main entry point for Archi's persistent daemon.

Architecture: Two parallel asyncio tasks with fine-grained locking.
  1. Message task:  Polls Discord queue every ~2s, processes immediately.
  2. Generation task: Runs self-development cycles on an interval.

Lock granularity:
  - _build_lock: Protects shared mutable state (operation log, capability
    registry, git working tree). Held by generation cycles and state-mutating
    message handlers (GAP, PREREQ_CONFIRM, TRIGGER).
  - Conversation responses run WITHOUT any lock — they only read the profile
    and call the model, so Jesse gets replies in <5s even mid-build.

Usage:
    python run.py --daemon --interval 300
    python run.py --loop          # run one generation cycle then exit
    python run.py --dry-run       # detect gaps only, no code generation

PROTECTED FILE — Archi's generation loop must NOT rewrite this file.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.generation_loop import CycleResult, run_cycle, format_cycle_notification, _notify_cycle_result
from src.kernel.periodic_registry import load_registry, resolve_coroutine, run_periodic

LOG_LEVEL = os.environ.get("ARCHI_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

REPO_PATH = str(Path(__file__).resolve().parent)
DEFAULT_OP_LOG = Path("data/operation_log.jsonl")
DEFAULT_REGISTRY = Path("data/capability_registry.json")


# ---------------------------------------------------------------------------
# Git lock cleanup — stale lock files block all git operations
# ---------------------------------------------------------------------------

_GIT_LOCK_FILES = ["HEAD.lock", "index.lock"]
_STALE_AGE_SECONDS = 30  # locks older than this are considered stale


def _cleanup_stale_git_locks() -> None:
    """Remove stale git lock files that would block self_modifier.

    A lock is considered stale if it is either 0 bytes (crashed process
    never wrote to it) or older than 30 seconds (process died mid-write).
    This runs automatically before every generation cycle.
    """
    import time

    git_dir = Path(REPO_PATH) / ".git"
    for lock_name in _GIT_LOCK_FILES:
        lock_path = git_dir / lock_name
        if not lock_path.exists():
            continue
        try:
            stat = lock_path.stat()
            is_empty = stat.st_size == 0
            is_old = (time.time() - stat.st_mtime) > _STALE_AGE_SECONDS
            if is_empty or is_old:
                lock_path.unlink()
                logger.info(
                    "Removed stale git lock: %s (size=%d, age=%.0fs)",
                    lock_name, stat.st_size, time.time() - stat.st_mtime,
                )
        except OSError as exc:
            logger.warning("Could not remove %s: %s", lock_name, exc)


# ---------------------------------------------------------------------------
# Kernel capability seeding
# ---------------------------------------------------------------------------

KERNEL_CAPABILITIES = {
    "self_modifier":       ("src/kernel/self_modifier.py",       "Kernel component: self_modifier"),
    "gap_detector":        ("src/kernel/gap_detector.py",        "Kernel component: gap_detector"),
    "capability_registry": ("src/kernel/capability_registry.py", "Kernel component: capability_registry"),
    "model_interface":     ("src/kernel/model_interface.py",     "Kernel component: model_interface"),
    "generation_loop":     ("src/kernel/generation_loop.py",     "Kernel component: generation_loop"),
    "alignment_gates":     ("src/kernel/alignment_gates.py",     "Kernel component: alignment_gates"),
}


def seed_kernel_capabilities(registry: CapabilityRegistry) -> None:
    """Ensure all kernel components are registered on first boot."""
    for name, (module, desc) in KERNEL_CAPABILITIES.items():
        if name not in registry.names():
            cap = Capability(
                name=name, module=module, description=desc,
                status="active", dependencies=[],
            )
            registry.register(cap)
            logger.info("Seeded kernel capability: %s", name)


# ---------------------------------------------------------------------------
# ArchiDaemon — parallel message + generation tasks
# ---------------------------------------------------------------------------

class ArchiDaemon:
    """
    Manages two independent asyncio tasks:

    - **message_task**: Polls the Discord listener queue every ~2s and processes
      messages immediately. Conversations run lock-free for instant responses.
    - **generation_task**: Runs generation loop cycles on an interval
      (default 5 min). Builds new capabilities autonomously.

    Only state-mutating operations (generation cycles, gap logging, prereq
    confirmation) acquire ``_build_lock``. Conversation responses bypass it
    entirely so Jesse gets replies in <5s even during a build.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        interval: float = 300.0,
        log_path: Path = DEFAULT_OP_LOG,
        dry_run: bool = False,
    ) -> None:
        self.registry = registry
        self.interval = interval
        self.log_path = log_path
        self.dry_run = dry_run
        self._build_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="archi")
        self._running = False
        self._tasks: list[asyncio.Task] = []

    # --- Message processing task ---

    async def _message_task(self) -> None:
        """Poll Discord queue every 2s and process messages."""
        logger.info("Message task started (poll interval=2s)")
        while self._running:
            try:
                await self._process_pending_messages()
            except Exception as exc:
                logger.exception("Error in message task: %s", exc)
            await asyncio.sleep(2.0)

    async def _process_pending_messages(self) -> None:
        """Process all pending Discord messages.

        Conversations run lock-free.  State-mutating intents (GAP,
        PREREQ_CONFIRM, TRIGGER) acquire ``_build_lock`` inside
        ``process_pending`` via the *build_lock* parameter.
        """
        try:
            from capabilities.discord_listener import process_pending
        except ImportError:
            return

        count = await process_pending(
            REPO_PATH, self.registry, build_lock=self._build_lock,
        )
        if count:
            logger.info("Processed %d message(s) from Discord queue.", count)

    # --- Generation cycle task ---

    async def _generation_task(self) -> None:
        """Run generation loop cycles on an interval."""
        logger.info("Generation task started (interval=%.0fs)", self.interval)
        # Small initial delay so the message task can start receiving first
        await asyncio.sleep(10.0)

        while self._running:
            try:
                await self._run_one_cycle()
            except Exception as exc:
                logger.exception("Error in generation task: %s", exc)
            await asyncio.sleep(self.interval)

    async def _run_one_cycle(self) -> None:
        """Execute one generation cycle in a thread executor (CPU-bound model calls)."""
        _cleanup_stale_git_locks()

        if self.dry_run:
            async with self._build_lock:
                from src.kernel.gap_detector import detect_gaps
                gaps = detect_gaps(self.registry, self.log_path)
                if gaps:
                    for g in gaps[:5]:
                        logger.info("  [DRY-RUN] Gap: %s (%.2f) — %s", g.name, g.priority, g.reason)
                else:
                    logger.info("  [DRY-RUN] No gaps detected.")
            return

        loop = asyncio.get_event_loop()

        # Run the blocking generation cycle in a thread, under the lock.
        # Notification also happens INSIDE the lock so it cannot interleave
        # with a conversational response from the message task.
        async with self._build_lock:
            result: CycleResult = await loop.run_in_executor(
                self._executor,
                lambda: run_cycle(REPO_PATH, self.registry, self.log_path),
            )

            # Send all notifications while still holding the lock.
            # This guarantees build messages are fully sent before
            # the message task can process a conversation and send a reply.
            from capabilities.discord_notifier import notify_async

            # First, send any pending notifications (e.g. prerequisite DMs)
            # that run_cycle accumulated instead of sending directly.
            if result.pending_notifications:
                for pending_msg in result.pending_notifications:
                    try:
                        await notify_async(pending_msg)
                    except Exception as exc:
                        logger.debug("Pending notification failed (non-fatal): %s", exc)

            # Then send the cycle-result notification.
            msg = format_cycle_notification(result)
            if msg:
                try:
                    await notify_async(msg)
                except Exception as exc:
                    logger.debug("Cycle notification failed (non-fatal): %s", exc)

        if result.capability_registered and result.gap:
            logger.info(
                "Cycle complete: built %s (%s)",
                result.gap.name,
                result.plan.get("description", "") if result.plan else "",
            )
        elif result.error:
            logger.warning("Cycle ended with error at %s: %s", result.phase_reached, result.error)
        elif result.phase_reached == "observe":
            # No gaps found — Archi is idle. Run self-evaluation to generate
            # a new gap oriented toward Jesse's six dimensions.
            logger.info("No gaps — running self-evaluator to find improvements")
            async with self._build_lock:
                try:
                    from capabilities.self_evaluator import SelfEvaluator
                    evaluator = SelfEvaluator(
                        registry=self.registry, log_path=self.log_path,
                    )
                    gap_name = await loop.run_in_executor(
                        self._executor, evaluator.evaluate_sync,
                    )
                    if gap_name:
                        logger.info("Self-evaluator proposed gap: %s", gap_name)
                    else:
                        logger.info("Self-evaluator: nothing to propose (heartbeat logged)")
                except Exception as exc:
                    logger.warning("Self-evaluator failed: %s", exc)

    # --- Periodic capability tasks ---

    def _start_periodic_tasks(self) -> list[asyncio.Task]:
        """Load periodic_registry.json and launch an asyncio task for each enabled entry."""
        tasks: list[asyncio.Task] = []
        entries = load_registry()
        for entry in entries:
            if not entry.enabled:
                logger.info("Periodic task '%s' is disabled — skipping.", entry.name)
                continue
            coro_fn = resolve_coroutine(entry)
            if coro_fn is None:
                logger.warning("Could not resolve periodic task '%s' — skipping.", entry.name)
                continue
            task = asyncio.create_task(
                run_periodic(entry, coro_fn),
                name=f"archi_periodic_{entry.name}",
            )
            tasks.append(task)
            logger.info("Launched periodic task: %s", entry.name)
        return tasks

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the daemon: launch Discord gateway + core + periodic tasks."""
        self._running = True
        logger.info("ArchiDaemon starting up")

        # Start the Discord gateway (bot WebSocket connection)
        try:
            from capabilities.discord_gateway import start_gateway
            start_gateway()
            logger.info("Discord gateway started")
        except Exception as exc:
            logger.warning("Could not start Discord gateway: %s", exc)

        # Launch core tasks
        self._tasks = [
            asyncio.create_task(self._message_task(), name="archi_message_task"),
            asyncio.create_task(self._generation_task(), name="archi_generation_task"),
        ]

        # Launch periodic capability tasks from registry
        periodic_tasks = self._start_periodic_tasks()
        self._tasks.extend(periodic_tasks)

        logger.info(
            "ArchiDaemon running — message + generation + %d periodic task(s) active",
            len(periodic_tasks),
        )

        # Wait until cancelled or stopped
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("ArchiDaemon tasks cancelled")

    async def stop(self) -> None:
        """Gracefully shut down both tasks and the Discord gateway."""
        logger.info("ArchiDaemon stopping...")
        self._running = False

        for task in self._tasks:
            task.cancel()

        # Shut down Discord gateway
        try:
            from capabilities.discord_gateway import stop_gateway
            await stop_gateway()
        except Exception as exc:
            logger.debug("Discord gateway shutdown: %s", exc)

        # Shut down Discord notifier session
        try:
            from capabilities.discord_notifier import shutdown
            await shutdown()
        except Exception as exc:
            logger.debug("Discord notifier shutdown: %s", exc)

        self._executor.shutdown(wait=False)
        logger.info("ArchiDaemon stopped")


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_daemon_instance: ArchiDaemon | None = None


def _handle_signal(sig, frame):
    """Handle SIGINT/SIGTERM by stopping the daemon."""
    logger.info("Received signal %s — requesting shutdown", sig)
    if _daemon_instance:
        loop = asyncio.get_event_loop()
        loop.create_task(_daemon_instance.stop())


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_daemon(interval: float = 300.0, dry_run: bool = False) -> None:
    """Start the ArchiDaemon with parallel tasks."""
    global _daemon_instance

    registry = CapabilityRegistry(path=DEFAULT_REGISTRY)
    seed_kernel_capabilities(registry)

    daemon = ArchiDaemon(
        registry=registry,
        interval=interval,
        log_path=DEFAULT_OP_LOG,
        dry_run=dry_run,
    )
    _daemon_instance = daemon

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    asyncio.run(daemon.start())


def run_single_cycle(dry_run: bool = False) -> None:
    """Run one generation cycle and exit."""
    registry = CapabilityRegistry(path=DEFAULT_REGISTRY)
    seed_kernel_capabilities(registry)

    if dry_run:
        from src.kernel.gap_detector import detect_gaps
        gaps = detect_gaps(registry, DEFAULT_OP_LOG)
        if gaps:
            for g in gaps[:10]:
                print(f"  Gap: {g.name} (priority={g.priority:.2f}) — {g.reason}")
        else:
            print("  No gaps detected.")
        return

    _cleanup_stale_git_locks()
    result = run_cycle(REPO_PATH, registry, DEFAULT_OP_LOG)

    # Single-cycle mode: sync notification is fine (no concurrency)
    # Send any pending notifications first (e.g. prerequisite DMs)
    if result.pending_notifications:
        for pending_msg in result.pending_notifications:
            try:
                from capabilities.discord_notifier import notify as _notify_sync
                _notify_sync(pending_msg)
            except Exception:
                pass
    msg = format_cycle_notification(result)
    if msg:
        _notify_cycle_result(result)

    if result.capability_registered and result.gap:
        print(f"Built: {result.gap.name}")
    elif result.error:
        print(f"Error at {result.phase_reached}: {result.error}")
    else:
        print("No gaps to address.")


def main() -> None:
    """Parse CLI arguments and dispatch."""
    parser = argparse.ArgumentParser(description="Archi — autonomous self-developing AI")
    parser.add_argument("--daemon", action="store_true", help="Run as persistent daemon")
    parser.add_argument("--interval", type=float, default=300.0,
                        help="Generation cycle interval in seconds (default: 300)")
    parser.add_argument("--loop", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--dry-run", action="store_true", help="Detect gaps only, no codegen")

    args = parser.parse_args()

    if args.daemon:
        logger.info("Starting Archi in daemon mode (interval=%.0fs)", args.interval)
        run_daemon(interval=args.interval, dry_run=args.dry_run)
    elif args.loop:
        logger.info("Running single generation cycle")
        run_single_cycle(dry_run=args.dry_run)
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python run.py --daemon --interval 300   # daemon with 5-min cycles")
        print("  python run.py --loop                    # one cycle then exit")
        print("  python run.py --loop --dry-run           # show gaps without building")


if __name__ == "__main__":
    main()
