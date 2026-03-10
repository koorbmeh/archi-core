"""
run.py — Main entry point for Archi's persistent event loop.

Imports and integrates overnight_self_improvement into the event loop
for periodic overnight execution, alongside existing capabilities such
as self_evaluator and gap_logging_sync.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from capabilities import event_loop as event_loop_module
from capabilities import gap_logging_sync
from capabilities import self_evaluator
from capabilities import overnight_self_improvement
from src.kernel.capability_registry import CapabilityRegistry

load_dotenv()

LOG_LEVEL = os.environ.get("ARCHI_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _resolve_log_path() -> Path:
    """Return the operational log path used across capabilities."""
    fallback = os.environ.get("ARCHI_COMMS_FALLBACK_PATH", "logs/operations.log")
    log_path = Path(fallback)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path


def _build_registry() -> CapabilityRegistry:
    """Construct and return the shared CapabilityRegistry."""
    registry_path = Path("capabilities_registry.json")
    return CapabilityRegistry(path=registry_path)


def _wire_capabilities(loop, registry: CapabilityRegistry, log_path: Path) -> None:
    """Attach all periodic capabilities to the event loop."""
    logger.info("Wiring self_evaluator into event loop")
    self_evaluator.initialize(loop=loop, registry=registry, log_path=log_path)

    logger.info("Wiring gap_logging_sync into event loop")
    gap_logging_sync.integrate_with_event_loop(loop=loop, log_path=log_path)

    logger.info("Wiring overnight_self_improvement into event loop")
    overnight_self_improvement.integrate_with_event_loop(
        loop=loop,
        registry=registry,
        log_path=log_path,
    )


def main() -> None:
    """Create the event loop, wire all capabilities, and start Archi."""
    logger.info("Archi starting up")

    log_path = _resolve_log_path()
    registry = _build_registry()

    loop = event_loop_module.create_event_loop(
        poll_interval=5.0,
        gap_check_interval=60.0,
        heartbeat_interval=30.0,
    )

    _wire_capabilities(loop, registry, log_path)

    logger.info("Starting event loop — running indefinitely")
    loop.run()


if __name__ == "__main__":
    main()