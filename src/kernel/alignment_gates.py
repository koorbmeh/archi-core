"""Alignment gates — kernel-level constraints that survive self-modification.

Gates enforce budget ceilings, protected file integrity, external action
transparency, and scope boundaries. They return pass/fail with a reason.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROTECTED_FILES = frozenset({
    "src/kernel/alignment_gates.py",
    "config/rules.yaml",
    ".env",
})

DEFAULT_SESSION_CEILING = 0.50
DEFAULT_DAILY_CEILING = 5.00
DEFAULT_MONTHLY_CEILING = 100.00
COST_LOG_PATH = Path("data/cost_log.jsonl")


@dataclass
class GateResult:
    """Outcome of a gate check."""
    passed: bool
    gate: str
    reason: str = ""


@dataclass
class ActionContext:
    """Describes a proposed action for gate checking."""
    action_type: str              # "file_write" | "model_call" | "external"
    target: str = ""
    estimated_cost: float = 0.0
    metadata: dict = field(default_factory=dict)


def _get_ceiling(env_key: str, default: float) -> float:
    raw = os.environ.get(env_key)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


def _read_cost_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    entries = []
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.error("Could not read cost log: %s", exc)
    return entries


def _daily_spend(entries: list[dict]) -> float:
    today = time.strftime("%Y-%m-%d")
    return sum(e.get("cost", 0.0) for e in entries if e.get("date", "")[:10] == today)


def _monthly_spend(entries: list[dict]) -> float:
    month = time.strftime("%Y-%m")
    return sum(e.get("cost", 0.0) for e in entries if e.get("date", "")[:7] == month)


def log_cost(cost: float, detail: str = "", log_path: Optional[Path] = None) -> None:
    """Append a cost entry for budget tracking."""
    path = log_path or COST_LOG_PATH
    entry: dict = {"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "cost": cost}
    if detail:
        entry["detail"] = detail
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.error("Failed to write cost log: %s", exc)


def check_protected_file(ctx: ActionContext) -> GateResult:
    """Block writes to protected files."""
    if ctx.action_type != "file_write":
        return GateResult(True, "protected_file")
    target = str(Path(ctx.target)).replace("\\", "/")
    for protected in PROTECTED_FILES:
        if target == protected or target.endswith("/" + protected):
            return GateResult(False, "protected_file",
                              f"Refused: {ctx.target} is a protected file.")
    return GateResult(True, "protected_file")


def check_budget(ctx: ActionContext, session_cost: float = 0.0,
                 cost_log_path: Optional[Path] = None) -> GateResult:
    """Enforce session, daily, and monthly budget ceilings."""
    if ctx.action_type != "model_call":
        return GateResult(True, "budget")
    session_ceiling = _get_ceiling("ARCHI_SESSION_BUDGET", DEFAULT_SESSION_CEILING)
    if session_cost + ctx.estimated_cost > session_ceiling:
        return GateResult(False, "budget",
                          f"Session ceiling ${session_ceiling:.2f} would be exceeded "
                          f"(current ${session_cost:.4f} + ${ctx.estimated_cost:.4f}).")
    log_path = cost_log_path or COST_LOG_PATH
    entries = _read_cost_log(log_path)
    daily_ceiling = _get_ceiling("ARCHI_DAILY_BUDGET", DEFAULT_DAILY_CEILING)
    daily = _daily_spend(entries)
    if daily + ctx.estimated_cost > daily_ceiling:
        return GateResult(False, "budget",
                          f"Daily ceiling ${daily_ceiling:.2f} would be exceeded "
                          f"(today ${daily:.4f} + ${ctx.estimated_cost:.4f}).")
    monthly_ceiling = _get_ceiling("ARCHI_MONTHLY_BUDGET", DEFAULT_MONTHLY_CEILING)
    monthly = _monthly_spend(entries)
    if monthly + ctx.estimated_cost > monthly_ceiling:
        return GateResult(False, "budget",
                          f"Monthly ceiling ${monthly_ceiling:.2f} would be exceeded "
                          f"(month ${monthly:.4f} + ${ctx.estimated_cost:.4f}).")
    return GateResult(True, "budget")


def check_external_action(ctx: ActionContext) -> GateResult:
    """Require that external actions carry logging metadata."""
    if ctx.action_type != "external":
        return GateResult(True, "external_action")
    if not ctx.metadata.get("logged"):
        return GateResult(False, "external_action",
                          f"External action '{ctx.target}' must be logged before execution.")
    return GateResult(True, "external_action")


def check_scope(ctx: ActionContext) -> GateResult:
    """Prevent generated code from writing into kernel paths."""
    if ctx.action_type != "file_write":
        return GateResult(True, "scope")
    target = str(Path(ctx.target))
    if ctx.metadata.get("source") == "generated" and target.startswith("src/kernel/"):
        return GateResult(False, "scope",
                          f"Generated code cannot write to kernel path: {ctx.target}")
    return GateResult(True, "scope")


ALL_GATES = [check_protected_file, check_budget, check_external_action, check_scope]


def check_gates(ctx: ActionContext, *, session_cost: float = 0.0,
                cost_log_path: Optional[Path] = None) -> list[GateResult]:
    """Run all alignment gates. Returns only failures (empty = all passed)."""
    failures = []
    for gate_fn in ALL_GATES:
        if gate_fn is check_budget:
            result = gate_fn(ctx, session_cost=session_cost,
                             cost_log_path=cost_log_path)
        else:
            result = gate_fn(ctx)
        if not result.passed:
            logger.warning("Gate '%s' blocked: %s", result.gate, result.reason)
            failures.append(result)
    return failures
