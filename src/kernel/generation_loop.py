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

# ---------------------------------------------------------------------------
# Protected files — Archi must NEVER fully rewrite these.
# When a wiring gap targets one of these, the planner should add a small
# surgical import/call — not replace the entire file.  As a hard guard,
# run_cycle() refuses to apply changes to any path in this set.
# ---------------------------------------------------------------------------
PROTECTED_FILES: set[str] = {
    "run.py",
    "capabilities/discord_listener.py",
    "capabilities/discord_gateway.py",
    "capabilities/discord_notifier.py",
    "capabilities/event_loop.py",
    "src/kernel/generation_loop.py",
    "src/kernel/gap_detector.py",
    "src/kernel/self_modifier.py",
    "src/kernel/capability_registry.py",
    "src/kernel/model_interface.py",
    "src/kernel/alignment_gates.py",
    "src/kernel/periodic_registry.py",
    "src/kernel/command_registry.py",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _error_slug(error_text: str, max_len: int = 40) -> str:
    """Turn an error message into a short, filesystem-safe slug."""
    slug = _SLUG_RE.sub("_", error_text.lower()).strip("_")
    return slug[:max_len].rstrip("_")


_LOOP_PREFIXES = ("wire_", "register_", "integrate_")


def _is_prefix_loop(name: str) -> bool:
    """Detect prefix-doubling loops like register_register_X or wire_register_X.

    Strip one known prefix; if the remainder still starts with a known prefix,
    this is a loop. Catches wire_wire_, register_register_, wire_register_, etc.
    """
    for prefix in _LOOP_PREFIXES:
        if name.startswith(prefix):
            remainder = name[len(prefix):]
            if any(remainder.startswith(p) for p in _LOOP_PREFIXES):
                return True
    return False

PLAN_SYSTEM = (
    "You are Archi's planning module. Given a capability gap, output ONLY a JSON "
    'object with keys: "file_path" (relative path), "description" (one sentence), '
    '"dependencies" (list of capability names, may be empty), "approach" (2-3 '
    'sentence plan), "prerequisites" (list of strings, may be empty). '
    "No markdown fences.\n\n"
    "PREREQUISITES RULE: If the capability requires ANY of the following to "
    "function at runtime, list them in the prerequisites field:\n"
    "  - pip/npm packages not in the standard library (e.g. 'pip install gspread google-auth')\n"
    "  - Environment variables or API keys (e.g. 'GOOGLE_SHEETS_CREDENTIALS env var "
    "pointing to a service account JSON file')\n"
    "  - External service accounts or OAuth tokens\n"
    "  - Files, databases, or hardware that must exist on the host\n"
    "If the capability only uses stdlib + packages already imported elsewhere "
    "in the project (json, pathlib, logging, etc.) and existing internal APIs, "
    "set prerequisites to an empty list [].\n\n"
    "PATH RULE: All new capability files MUST be created in `capabilities/` "
    "(e.g. `capabilities/my_capability.py`). NEVER create files under `src/` "
    "except for kernel modules under `src/kernel/`. The `src/capabilities/` "
    "directory does not exist and must not be used.\n\n"
    "PROTECTED FILE RULE: The following files are PROTECTED and must NEVER be "
    "the target of a plan's file_path: run.py, capabilities/discord_listener.py, "
    "capabilities/discord_gateway.py, capabilities/discord_notifier.py, "
    "capabilities/event_loop.py, and ALL files under src/kernel/. If a gap "
    "requires wiring into one of these files, the approach should describe "
    "what import/call to add, but the file_path MUST be a NEW file in capabilities/.\n\n"
    "INTEGRATION RULE — THIS IS CRITICAL:\n"
    "capabilities/event_loop.py is DEPRECATED and must NEVER be used as an "
    "integration target. Do NOT import EventLoop, do NOT call "
    "integrate_with_event_loop(), do NOT reference event_loop in any way.\n"
    "Instead, there are two integration mechanisms:\n"
    "  1. PERIODIC TASKS (runs on a schedule): The capability should expose an "
    "async coroutine function. The approach should state that the capability "
    "needs a periodic_registry entry, and include in the approach: "
    "'Register in data/periodic_registry.json via "
    "src.kernel.periodic_registry.register(name, module, coroutine, interval_seconds)'. "
    "The capability file itself should call periodic_registry.register() at "
    "module level or in an initialize() function.\n"
    "  2. ON-DEMAND COMMANDS (triggered by Jesse via Discord !command): The "
    "capability should expose a callable function. The approach should state: "
    "'Register in data/command_registry.json via "
    "src.kernel.command_registry.register(command, module, function, description)'. "
    "The capability file itself should call command_registry.register() at "
    "module level or in an initialize() function.\n"
    "Do NOT create wire_*.py or integrate_*.py files. These are dead code patterns."
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
    "NEVER use `src/capabilities/` — that directory does not exist.\n\n"
    "INTEGRATION RULE: NEVER import from capabilities.event_loop — it is deprecated. "
    "For periodic execution, call src.kernel.periodic_registry.register() in the "
    "module's initialize() function. For on-demand execution via Discord, call "
    "src.kernel.command_registry.register() in initialize(). Do NOT create "
    "wire_*.py or integrate_*.py wrapper files."
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
    pending_notifications: Optional[list] = None  # DMs to send (under lock)


def format_cycle_notification(result: "CycleResult") -> Optional[str]:
    """Build the Discord DM notification string for a cycle outcome.

    Returns None if the cycle was uneventful (no gaps, nothing built).
    The caller is responsible for actually sending the message.

    NOTE: This text is sent as a Discord DM to Jesse. It must NEVER contain
    the ``[build]`` prefix — that was an internal log marker that caused
    notification bleed when Discord visually grouped it with conversation
    replies.
    """
    if result.capability_registered and result.gap:
        desc = ""
        if result.plan:
            desc = result.plan.get("description", "")
        if desc:
            return f"Built **{result.gap.name}** — {desc}"
        return f"Built **{result.gap.name}**."
    if result.error and result.gap:
        return (
            f"Tried to build **{result.gap.name}** but hit an issue "
            f"at the {result.phase_reached} phase: {result.error[:150]}"
        )
    return None


def _notify_cycle_result(result: "CycleResult") -> None:
    """Legacy sync wrapper — prefer format_cycle_notification + async send."""
    msg = format_cycle_notification(result)
    if msg:
        try:
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


def _prerequisites_confirmed(gap_name: str, log_path: Path) -> bool:
    """Check whether Jesse has confirmed prerequisites for a given gap.

    Scans the operation log for ``prerequisite_confirmed`` for *gap_name*.
    Returns True ONLY if a confirmation entry exists (and is not superseded
    by a newer pending entry).  Returns False otherwise — including when no
    prerequisite entries exist at all (first encounter).
    """
    if not log_path.exists():
        return False
    confirmed = False
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = entry.get("event", "")
            cap = entry.get("missing_capability", "")
            if event == "prerequisite_pending" and cap == gap_name:
                confirmed = False  # newer pending resets
            elif event == "prerequisite_confirmed" and cap == gap_name:
                confirmed = True
    except OSError:
        return False
    return confirmed


def _prerequisite_already_asked(gap_name: str, log_path: Path) -> bool:
    """Return True if we've already logged prerequisite_pending for this gap."""
    if not log_path.exists():
        return False
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (entry.get("event") == "prerequisite_pending"
                    and entry.get("missing_capability") == gap_name):
                return True
    except OSError:
        pass
    return False


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
    plan.setdefault("prerequisites", [])
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

    # Hard guard: refuse to plan any gap with doubled prefixes — this is a
    # sign of an infinite loop (wire_wire_*, register_register_*, etc.).
    # Rule: strip known prefixes; if the remainder still starts with one, block.
    if _is_prefix_loop(gap.name):
        logger.error(
            "LOOP GUARD: Refusing to plan '%s' — prefix-doubling loop detected.",
            gap.name,
        )
        _log_operation(
            "prefix_loop_blocked", False,
            detail=f"Blocked prefix-doubling gap: {gap.name}",
            log_path=op_log,
        )
        return CycleResult(phase_reached="plan", gap=gap,
                           error=f"Blocked prefix-doubling gap: {gap.name}")

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

    # --- Phase 2b: Check prerequisites ---
    # If the plan requires external setup (packages, API keys, credentials),
    # ask Jesse to confirm they're in place before generating code.
    prerequisites = plan.get("prerequisites", [])
    if prerequisites and not _prerequisites_confirmed(gap.name, op_log):
        # Only DM Jesse if we haven't already asked (avoid spamming).
        already_asked = _prerequisite_already_asked(gap.name, op_log)
        notifications: list[str] = []
        if not already_asked:
            prereq_text = "\n".join(f"  • {p}" for p in prerequisites)
            dm_msg = (
                f"I want to build **{gap.name}** ({plan['description']}), "
                f"but I need a few things set up first:\n{prereq_text}\n\n"
                f"Once you've done these, reply with **ready** or **done** "
                f"and I'll continue building it."
            )
            _log_operation(
                "prerequisite_pending", True,
                detail=json.dumps({"gap": gap.name, "prerequisites": prerequisites}),
                missing_cap=gap.name,
                log_path=op_log,
            )
            notifications.append(dm_msg)
        logger.info(
            "Skipping %s — waiting for Jesse to confirm prerequisites.", gap.name,
        )
        return CycleResult(
            phase_reached="plan", gap=gap, plan=plan,
            error=f"Waiting for Jesse to confirm prerequisites for {gap.name}",
            pending_notifications=notifications or None,
        )

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

    # --- Phase 3b: Skip if generated code is identical to existing file ---
    target_path = Path(repo_path) / plan["file_path"]
    if target_path.exists():
        try:
            existing = target_path.read_text(encoding="utf-8")
            if existing.strip() == code.strip():
                logger.info(
                    "Generated code identical to existing %s — skipping rebuild.",
                    plan["file_path"],
                )
                _log_operation(
                    "code_unchanged_skip", True,
                    detail=f"{plan['file_path']} unchanged", log_path=op_log,
                )
                # Mark Jesse-signaled gaps as resolved even when skipping
                is_jesse_signal = any(
                    "gap_signal_from_discord" in ev for ev in gap.evidence
                )
                if is_jesse_signal:
                    _log_operation(
                        "jesse_gap_resolved", True,
                        detail=gap.name, log_path=op_log,
                    )
                return CycleResult(
                    phase_reached="integrate", gap=gap, plan=plan,
                    capability_registered=True,
                )
        except OSError:
            pass  # can't read existing file — proceed with apply_change

    # --- Phase 3c: Hard guard — refuse to overwrite protected files ---
    plan_file = plan["file_path"]
    if plan_file in PROTECTED_FILES:
        msg = (
            f"PROTECTED FILE GUARD: Refusing to overwrite '{plan_file}'. "
            f"This file is hand-maintained infrastructure. The plan must target "
            f"a new file in capabilities/ instead."
        )
        logger.error(msg)
        _log_operation(
            "protected_file_blocked", False,
            detail=msg, missing_cap=gap.name, log_path=op_log,
        )
        return CycleResult(phase_reached="generate", gap=gap, plan=plan, error=msg)

    # --- Phase 4: Test + Integrate ---
    change = apply_change(repo_path, plan["file_path"], code)
    if not change.success:
        # Build a detail string that includes test output when available,
        # so the gap detector can surface it and the planner can see
        # exactly why the previous attempt failed.
        detail = change.message
        if change.failure_type == "test_failure" and change.test_output:
            # Truncate to keep JSONL lines manageable but long enough
            # for the planner to diagnose the failure.
            truncated = change.test_output[:2000]
            detail = (
                f"{change.message}\n\n"
                f"TEST OUTPUT (last attempt):\n{truncated}"
            )
        if change.failure_type == "environment":
            env_gap = f"env_{_error_slug(change.error or change.message)}"
            _log_operation("integrate_failed", False, detail=detail,
                           missing_cap=env_gap, log_path=op_log)
        else:
            _log_operation("integrate_failed", False, detail=detail,
                           missing_cap=gap.name, log_path=op_log)
        return CycleResult(phase_reached="integrate", gap=gap, plan=plan,
                           change=change, error=change.message)

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

    # If this gap was signaled by Jesse via Discord, mark it resolved so the
    # old signal entries don't cause an infinite rebuild loop.
    is_jesse_signal = any(
        "gap_signal_from_discord" in ev for ev in gap.evidence
    )
    if is_jesse_signal:
        _log_operation(
            "jesse_gap_resolved", True,
            detail=gap.name, log_path=op_log,
        )
        logger.info("Marked Jesse-signaled gap '%s' as resolved.", gap.name)

    logger.info("Cycle complete — integrated %s.", gap.name)

    # --- Phase 5: Verify reachability ---
    # A capability that exists but isn't called from any active pathway is not
    # useful to Jesse. Check whether anything imports or references the new
    # module. If nothing does, log a wiring gap so the next cycle can fix it.
    _check_reachability(cap, registry, op_log)

    return CycleResult(
        phase_reached="integrate",
        gap=gap,
        plan=plan,
        change=change,
        capability_registered=True,
    )


def _check_reachability(
    cap: Capability,
    registry: CapabilityRegistry,
    op_log: Path,
) -> None:
    """Check whether a newly registered capability is reachable.

    A capability is reachable if ANY of these are true:
      1. Another capability file or entry point imports/references it.
      2. It is registered in periodic_registry.json (will be run by ArchiDaemon).
      3. It is registered in command_registry.json (triggerable via !command).

    If unreachable, logs a guidance gap pointing to periodic_registry or
    command_registry — NOT a wire_X file (those are dead code).
    """
    # Skip reachability for wire_, integrate_, register_ capabilities — wiring artifacts
    if cap.name.startswith(("wire_", "integrate_", "register_")):
        logger.debug("Skipping reachability check for wiring artifact: %s", cap.name)
        return

    cap_module_stem = Path(cap.module).stem

    # Check 0: Does the file self-register with periodic_registry or command_registry?
    own_path = Path(cap.module)
    if own_path.exists():
        try:
            own_content = own_path.read_text(encoding="utf-8")
            if ("periodic_registry.register(" in own_content
                    or "command_registry.register(" in own_content):
                logger.info("Reachability: %s self-registers via registry.", cap.name)
                return
        except OSError:
            pass

    # Check 1: Is it referenced by another file?
    scan_dirs = [Path("capabilities"), Path("src/kernel")]
    entry_points = [Path("run.py")]
    files_to_scan: list[Path] = list(entry_points)
    for d in scan_dirs:
        if d.is_dir():
            files_to_scan.extend(d.glob("*.py"))
    own_path = Path(cap.module)

    for fpath in files_to_scan:
        if not fpath.exists() or fpath == own_path:
            continue
        try:
            content = fpath.read_text(encoding="utf-8")
        except OSError:
            continue
        if cap_module_stem in content:
            return  # referenced — reachable

    # Check 2: Is it in periodic_registry.json?
    try:
        from src.kernel.periodic_registry import load_registry as load_periodic
        for entry in load_periodic():
            if cap.name == entry.name or cap_module_stem in entry.module:
                return  # registered as periodic — reachable
    except Exception:
        pass

    # Check 3: Is it in command_registry.json?
    try:
        from src.kernel.command_registry import load_registry as load_cmds
        for entry in load_cmds():
            if cap_module_stem in entry.module:
                return  # registered as command — reachable
    except Exception:
        pass

    # Not reachable — log a guidance gap (NOT a wire_ gap)
    detail = (
        f"Capability '{cap.name}' ({cap.module}) was built and tests pass, "
        f"but is not reachable. To make it reachable, the capability should "
        f"self-register using one of these mechanisms:\n"
        f"  - Periodic: call src.kernel.periodic_registry.register() in "
        f"its initialize() function\n"
        f"  - On-demand: call src.kernel.command_registry.register() in "
        f"its initialize() function\n"
        f"Do NOT create wire_*.py or integrate_*.py files. Do NOT import "
        f"from capabilities.event_loop (deprecated)."
    )
    logger.warning("Reachability: %s is not wired into any pathway.", cap.name)
    _log_operation(
        "reachability_check_failed", False,
        detail=detail, missing_cap=f"register_{cap.name}", log_path=op_log,
    )
