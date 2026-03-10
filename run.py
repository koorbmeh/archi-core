"""
run.py — Main entry point for Archi's persistent daemon.

Architecture: Two parallel asyncio tasks sharing a lock for safe state access.
  1. Message task:  Polls Discord queue every ~2s, processes immediately.
  2. Generation task: Runs self-development cycles on an interval.

Both tasks share an asyncio.Lock that protects shared state: the operation log,
capability registry, and git working tree.

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
      messages immediately. Conversations feel near-instant to Jesse.
    - **generation_task**: Runs generation loop cycles on an interval
      (default 5 min). Builds new capabilities autonomously.

    Both acquire `self._lock` before touching shared state (registry, op log,
    git working tree) to prevent race conditions.
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
        self._lock = asyncio.Lock()
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
        """Process all pending Discord messages under the shared lock."""
        try:
            from capabilities.discord_listener import process_pending
        except ImportError:
            return

        async with self._lock:
            count = await process_pending(REPO_PATH, self.registry)
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
        if self.dry_run:
            async with self._lock:
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
        async with self._lock:
            result: CycleResult = await loop.run_in_executor(
                self._executor,
                lambda: run_cycle(REPO_PATH, self.registry, self.log_path),
            )

            # Send cycle notification while still holding the lock.
            # This guarantees the "[build]" message is fully sent before
            # the message task can process a conversation and send a reply.
            msg = format_cycle_notification(result)
            if msg:
                try:
                    from capabilities.discord_notifier import notify_async
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
            async with self._lock:
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

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the daemon: launch Discord gateway + both async tasks."""
        self._running = True
        logger.info("ArchiDaemon starting up")

        # Start the Discord gateway (bot WebSocket connection)
        try:
            from capabilities.discord_gateway import start_gateway
            start_gateway()
            logger.info("Discord gateway started")
        except Exception as exc:
            logger.warning("Could not start Discord gateway: %s", exc)

        # Launch parallel tasks
        self._tasks = [
            asyncio.create_task(self._message_task(), name="archi_message_task"),
            asyncio.create_task(self._generation_task(), name="archi_generation_task"),
        ]

        logger.info("ArchiDaemon running — message task + generation task active")

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

    result = run_cycle(REPO_PATH, registry, DEFAULT_OP_LOG)

    # Single-cycle mode: sync notification is fine (no concurrency)
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
