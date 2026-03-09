#!/usr/bin/env python3
"""Archi — entry point.

Loads config, wires the two-model routing, seeds the capability registry
with the kernel components, and runs the generation loop.

Usage:
    python run.py              # run one cycle
    python run.py --loop N     # run up to N cycles (stops when no gaps remain)
    python run.py --dry-run    # detect gaps and print them, no model calls
"""

import argparse
import logging
import sys
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


def clean_stale_git_locks() -> None:
    """Remove 0-byte git lock files left by crashed processes."""
    git_dir = REPO_ROOT_PATH / ".git"
    for lock_name in _GIT_LOCK_FILES:
        lock = git_dir / lock_name
        if lock.exists() and lock.stat().st_size == 0:
            try:
                lock.unlink()
                logging.getLogger("archi.run").info(
                    "Removed stale lock: %s", lock_name)
            except OSError:
                logging.getLogger("archi.run").warning(
                    "Could not remove stale lock: %s", lock_name)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Archi's generation loop.")
    parser.add_argument("--loop", type=int, default=5, metavar="N",
                        help="Run up to N cycles (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect gaps and print them; no model calls")
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

    # Show routing config
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
