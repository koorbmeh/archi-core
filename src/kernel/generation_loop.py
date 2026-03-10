"""Generation loop — Archi's self-development cycle.

Observe → Detect Gap → Plan → Generate Code → Test → Integrate.
Wires self_modifier + gap_detector + capability_registry + model_interface.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from src.kernel.alignment_gates import ActionContext, check_gates
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.gap_detector import Gap, detect_gaps
from src.kernel.model_interface import (
    BudgetExceededError, ModelResponse, call_model, get_session_cost,
)
from src.kernel.self_modifier import ChangeResult, apply_change

try:
    from capabilities.api_introspection import build_api_context
except ImportError:
    def build_api_context(registry=None):  # type: ignore[misc]
        return ""

try:
    from capabilities.discord_notifier import notify as _discord_notify
except ImportError:
    def _discord_notify(text: str) -> bool:  # type: ignore[misc]
        return False

logger = logging.getLogger(__name__)

DEFAULT_OP_LOG = Path("data/operation_log.jsonl")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _error_slug(error_text: str, max_len: int = 40) -> str:
    """Turn an error message into a short, filesystem-safe slug."""
    slug = _SLUG_RE.sub("_", error_text.lower()).strip("_")
    return slug[:max_len].rstrip("_")

PLAN_SYSTEM = (
    "You are Archi's planning module. Given a capability gap, output ONLY a JSON "
    'object with keys: "file_path" (relative path), "description" (one sentence), '
    '"dependencies" (list of capability names, may be empty), "approach" (2-3 '
    "sentence plan). No markdown fences.\n\n"
    "PATH RULE: All new capability files MUST be created in `capabilities/` "
    "(e.g. `capabilities/my_capability.py`). NEVER create files under `src/` "
    "except for kernel modules under `src/kernel/`. The `src/capabilities/` "
    "directory does not exist and must not be used."
)

GENERATE_SYSTEM = (
    "You are Archi's code generation module. Write the complete Python source file. "
    "Requirements: module docstring, clean imports (stdlib/third-party/local), "
    "functions under 40 lines, pathlib not hardcoded paths, snake_case for "
    "functions/variables, PascalCase for classes. Output ONLY Python code, "
    "no markdown fences.\n\n"
    "PATH RULE: Capability files live in `capabilities/` (e.g. `capabilities/foo.py`). "
    "Imports from kernel use `from src.kernel.<module> import ...`. "
    "Imports from other capabilities use `from capabilities.<module> import ...`. "
    "NEVER use `src/capabilities/` — that directory does not exist."
)


@dataclass
class CycleResult:
    """Outcome of one generation loop cycle."""
    phase_reached: str          # observe | plan | generate | integrate
    gap: Optional[Gap] = None
    plan: Optional[dict] = None
    change: Optional[ChangeResult] = None
    capability_registered: bool = False
    error: Optional[str] = None


def _notify_cycle_result(result: "CycleResult") -> None:
    """Send a Discord notification summarizing a cycle outcome."""
    try:
        cost = f"${get_session_cost():.4f}"
        if result.capability_registered and result.gap:
            msg = (
                f"Integrated: {result.gap.name}\n"
                f"File: {result.plan.get('file_path', '?') if result.plan else '?'}\n"
                f"Session cost: {cost}"
            )
        elif result.error and result.gap:
            msg = (
                f"Failed: {result.gap.name} at {result.phase_reached} phase\n"
                f"Reason: {result.error[:200]}\n"
                f"Session cost: {cost}"
            )
        elif result.phase_reached == "observe":
            # No gaps — not worth a notification every time
            return
        else:
            return
        _discord_notify(msg)
    except Exception as exc:
        logger.debug("Discord notification failed (non-fatal): %s", exc)


def _log_operation(event: str, success: bool, detail: str = "",
                   missing_cap: str = "", log_path: Path = DEFAULT_OP_LOG):
    """Append a structured entry to the operation log."""
    entry = {"event": event, "success": success}
    if detail:
        entry["detail"] = detail
    if missing_cap:
        entry["missing_capability"] = missing_cap
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.error("Failed to write operation log: %s", exc)


def _parse_plan(response: ModelResponse) -> Optional[dict]:
    """Extract the JSON plan from the model response."""
    text = response.text.strip()
    # Strip markdown fences if model included them despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        return None
    required = {"file_path", "description", "approach"}
    if not required.issubset(plan.keys()):
        return None
    plan.setdefault("dependencies", [])
    return plan


def run_cycle(
    repo_path: str,
    registry: CapabilityRegistry,
    log_path: Optional[Path] = None,
    *,
    plan_fn=call_model,
    generate_fn=call_model,
) -> CycleResult:
    """Execute one full generation loop cycle."""
    op_log = log_path or DEFAULT_OP_LOG

    # --- Phase 1: Observe + Detect Gap ---
    gaps = detect_gaps(registry, op_log)
    if not gaps:
        logger.info("No gaps detected — nothing to do.")
        _log_operation("cycle_no_gaps", True, log_path=op_log)
        return CycleResult(phase_reached="observe")

    gap = gaps[0]  # highest priority
    logger.info("Top gap: %s (priority %.2f, source=%s)", gap.name, gap.priority, gap.source)
    _log_operation("gap_selected", True, detail=gap.name, log_path=op_log)

    # --- Phase 2: Plan (pre-flight gate check) ---
    gate_ctx = ActionContext(
        action_type="model_call", target="plan",
        estimated_cost=0.01,  # conservative estimate for a plan call
    )
    gate_failures = check_gates(gate_ctx, session_cost=get_session_cost())
    if gate_failures:
        reasons = "; ".join(f.reason for f in gate_failures)
        _log_operation("plan_gate_blocked", False, detail=reasons, log_path=op_log)
        return CycleResult(phase_reached="plan", gap=gap, error=f"Gate blocked: {reasons}")

    detail_line = ""
    if gap.detail:
        detail_line = f"  Context from operational history: {gap.detail}\n"

    # Build real API context so the planner sees actual function signatures
    api_context = build_api_context(registry)
    api_block = ""
    if api_context:
        api_block = (
            f"\nAvailable module APIs (use these exact function names and signatures):\n"
            f"{api_context}\n"
        )

    plan_prompt = (
        f"Capability gap to close:\n"
        f"  Name: {gap.name}\n"
        f"  Reason: {gap.reason}\n"
        f"  Evidence: {', '.join(gap.evidence)}\n"
        f"{detail_line}\n"
        f"Currently registered capabilities: "
        f"{', '.join(registry.names()) or '(none)'}\n\n"
        f"{api_block}"
        f"Produce a plan to close this gap."
    )
    try:
        plan_response = plan_fn(plan_prompt, system=PLAN_SYSTEM)
    except BudgetExceededError as exc:
        _log_operation("plan_budget_exceeded", False, detail=str(exc), log_path=op_log)
        return CycleResult(phase_reached="plan", gap=gap, error=str(exc))

    if plan_response.error:
        _log_operation("plan_model_error", False, detail=plan_response.error,
                       missing_cap=gap.name, log_path=op_log)
        return CycleResult(phase_reached="plan", gap=gap, error=plan_response.error)

    plan = _parse_plan(plan_response)
    if not plan:
        msg = "Failed to parse plan from model response."
        _log_operation("plan_parse_failed", False, detail=msg,
                       missing_cap=gap.name, log_path=op_log)
        return CycleResult(phase_reached="plan", gap=gap, error=msg)

    logger.info("Plan: %s → %s", plan["file_path"], plan["description"])
    _log_operation("plan_created", True, detail=plan["file_path"], log_path=op_log)

    # --- Phase 3: Generate Code (pre-flight gate check) ---
    gate_ctx = ActionContext(
        action_type="model_call", target="generate",
        estimated_cost=0.02,  # conservative estimate for a codegen call
    )
    gate_failures = check_gates(gate_ctx, session_cost=get_session_cost())
    if gate_failures:
        reasons = "; ".join(f.reason for f in gate_failures)
        _log_operation("generate_gate_blocked", False, detail=reasons, log_path=op_log)
        return CycleResult(phase_reached="generate", gap=gap, plan=plan,
                           error=f"Gate blocked: {reasons}")

    gen_prompt = (
        f"Write a Python module for this plan:\n"
        f"  File: {plan['file_path']}\n"
        f"  Description: {plan['description']}\n"
        f"  Approach: {plan['approach']}\n"
        f"  Dependencies: {', '.join(plan.get('dependencies', [])) or 'none'}\n\n"
        f"{api_block}"
        f"IMPORTANT: Only import and call functions that appear in the API listing above. "
        f"Do not invent function names or assume interfaces exist.\n\n"
        f"Write the complete file contents."
    )
    try:
        gen_response = generate_fn(gen_prompt, system=GENERATE_SYSTEM)
    except BudgetExceededError as exc:
        _log_operation("generate_budget_exceeded", False, detail=str(exc), log_path=op_log)
        return CycleResult(phase_reached="generate", gap=gap, plan=plan, error=str(exc))

    if gen_response.error:
        _log_operation("generate_model_error", False, detail=gen_response.error,
                       missing_cap=gap.name, log_path=op_log)
        return CycleResult(phase_reached="generate", gap=gap, plan=plan,
                           error=gen_response.error)

    code = gen_response.text.strip()
    if code.startswith("```"):
        lines = code.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        code = "\n".join(lines)

    if len(code) < 20:
        msg = "Generated code too short — likely invalid."
        _log_operation("generate_too_short", False, detail=msg,
                       missing_cap=gap.name, log_path=op_log)
        return CycleResult(phase_reached="generate", gap=gap, plan=plan, error=msg)

    logger.info("Generated %d chars for %s.", len(code), plan["file_path"])
    _log_operation("code_generated", True, detail=f"{len(code)} chars", log_path=op_log)

    # --- Phase 4: Test + Integrate ---
    change = apply_change(repo_path, plan["file_path"], code)
    if not change.success:
        if change.failure_type == "environment":
            env_gap = f"env_{_error_slug(change.error or change.message)}"
            _log_operation("integrate_failed", False, detail=change.message,
                           missing_cap=env_gap, log_path=op_log)
        else:
            _log_operation("integrate_failed", False, detail=change.message,
                           missing_cap=gap.name, log_path=op_log)
        fail_result = CycleResult(phase_reached="integrate", gap=gap, plan=plan,
                                  change=change, error=change.message)
        _notify_cycle_result(fail_result)
        return fail_result

    # Register the new capability
    cap = Capability(
        name=gap.name,
        module=plan["file_path"],
        description=plan["description"],
        status="active",
        dependencies=plan.get("dependencies", []),
    )
    registry.register(cap)
    _log_operation("capability_integrated", True, detail=gap.name, log_path=op_log)
    logger.info("Cycle complete — integrated %s.", gap.name)

    success_result = CycleResult(
        phase_reached="integrate",
        gap=gap,
        plan=plan,
        change=change,
        capability_registered=True,
    )
    _notify_cycle_result(success_result)
    return success_result
