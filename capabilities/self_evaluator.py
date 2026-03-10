"""
Periodic self-evaluation engine for Archi.

Runs when Archi is idle (no gaps detected by gap_detector). Asks the model
to propose one actionable capability gap oriented toward Jesse's six life
dimensions: Health, Wealth, Happiness, Agency, Capability, Synthesis.

The gap is written directly to operation_log.jsonl so the generation loop
picks it up on the next cycle. When genuinely nothing is needed, logs a
heartbeat so we can verify the evaluator is alive.

PROTECTED FILE — Archi's generation loop must NOT rewrite this file.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from src.kernel.alignment_gates import ActionContext, check_gates
from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.model_interface import BudgetExceededError, call_model, get_session_cost

logger = logging.getLogger(__name__)

DEFAULT_OP_LOG = Path("data/operation_log.jsonl")

SIX_DIMENSIONS = ["Health", "Wealth", "Happiness", "Agency", "Capability", "Synthesis"]

SYSTEM_PROMPT = (
    "You are Archi's self-evaluation engine. Jesse cares about six life dimensions: "
    "Health, Wealth, Happiness, Agency, Capability, and Synthesis.\n\n"
    "Given the current capability list and recent conversation context, propose ONE "
    "concrete new capability that would meaningfully improve Jesse's life in one of "
    "these dimensions. The capability must be something Archi can actually build as "
    "a Python module.\n\n"
    "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
    '  "name": snake_case capability name (e.g. "daily_health_checkin"),\n'
    '  "dimension": which of the six dimensions this serves,\n'
    '  "reason": one sentence explaining why Jesse needs this,\n'
    '  "priority": float 0.0-1.0 (higher = more impactful),\n'
    '  "detail": 2-3 sentences describing what the capability should do.\n\n'
    "If Archi genuinely has excellent coverage across all dimensions and nothing "
    "useful can be built, respond with exactly: {\"name\": null}\n"
)

# Slug pattern for sanitizing names
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


class SelfEvaluator:
    """Generates actionable gaps when Archi is idle, writes them to the operation log."""

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._registry = registry or CapabilityRegistry()
        self._log_path = log_path or DEFAULT_OP_LOG

    def _check_alignment(self, estimated_cost: float) -> list:
        ctx = ActionContext(
            action_type="model_call",
            target="self_evaluation",
            estimated_cost=estimated_cost,
            metadata={"source": "self_evaluator"},
        )
        return check_gates(ctx, session_cost=get_session_cost())

    def _get_capability_list(self) -> str:
        """Return a concise list of registered capabilities."""
        caps = self._registry.list_all()
        if not caps:
            return "(no capabilities registered)"
        lines = []
        for c in caps:
            lines.append(f"- {c.name}: {c.description[:100]}")
        return "\n".join(lines)

    def _get_conversation_context(self) -> str:
        """Pull recent conversation context if available."""
        try:
            from capabilities.conversational_memory import get_recent_messages
            import os
            user_id = os.environ.get("JESSE_DISCORD_ID", "jesse")
            messages = get_recent_messages(user_id, n=15)
            if not messages:
                return "(no recent conversations)"
            lines = []
            for m in messages:
                role = m.get("role", "?")
                content = str(m.get("content", ""))[:150]
                lines.append(f"[{role}] {content}")
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("Could not fetch conversation context: %s", exc)
            return "(conversation history unavailable)"

    def _build_prompt(self) -> str:
        cap_list = self._get_capability_list()
        conv_context = self._get_conversation_context()
        return (
            f"## Current Capabilities\n{cap_list}\n\n"
            f"## Recent Conversation Context\n{conv_context}\n\n"
            f"## Jesse's Six Dimensions\n"
            f"{', '.join(SIX_DIMENSIONS)}\n\n"
            f"Propose ONE new capability. Respond with JSON only."
        )

    def _log_operation(self, event: str, success: bool, detail: str = "",
                       missing_cap: str = "") -> None:
        """Append a structured entry to the operation log."""
        entry = {"event": event, "success": success}
        if detail:
            entry["detail"] = detail
        if missing_cap:
            entry["missing_capability"] = missing_cap
        entry["timestamp"] = time.time()
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.error("Failed to write operation log: %s", exc)

    def _parse_gap_response(self, text: str) -> Optional[dict]:
        """Parse the model's JSON response into a gap dict."""
        text = text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                return None
        except json.JSONDecodeError:
            return None

        # Model says nothing to build
        if data.get("name") is None:
            return None

        name = data.get("name", "")
        if not name or not isinstance(name, str):
            return None

        # Sanitize the name
        name = _SLUG_RE.sub("_", name.lower()).strip("_")
        if not name:
            return None

        return {
            "name": name,
            "dimension": data.get("dimension", "Capability"),
            "reason": data.get("reason", "Self-evaluation identified this gap."),
            "priority": min(1.0, max(0.0, float(data.get("priority", 0.6)))),
            "detail": data.get("detail", ""),
        }

    def evaluate_sync(self) -> Optional[str]:
        """Run one evaluation cycle synchronously.

        Returns the name of the gap written, or None if nothing was needed.
        """
        logger.info("SelfEvaluator: starting evaluation")

        # Gate check
        failures = self._check_alignment(estimated_cost=0.03)
        if failures:
            reasons = "; ".join(f.reason for f in failures)
            logger.warning("SelfEvaluator: gate blocked — %s", reasons)
            return None

        prompt = self._build_prompt()

        try:
            response = call_model(prompt, system=SYSTEM_PROMPT)
        except BudgetExceededError:
            logger.warning("SelfEvaluator: budget exceeded")
            return None
        except Exception as exc:
            logger.error("SelfEvaluator: model call failed — %s", exc)
            return None

        logger.info(
            "SelfEvaluator: model responded (tokens_in=%d, tokens_out=%d, cost=%.4f)",
            response.tokens_in, response.tokens_out, response.cost_estimate,
        )

        gap_data = self._parse_gap_response(response.text)

        if gap_data is None:
            # Nothing to build — log a heartbeat so we know it ran
            self._log_operation(
                "self_evaluator_heartbeat", True,
                detail="Evaluated all six dimensions; no new gaps identified.",
            )
            logger.info("SelfEvaluator: no new gaps — heartbeat logged")
            return None

        gap_name = gap_data["name"]

        # Don't re-propose something that already exists
        if gap_name in self._registry.names():
            self._log_operation(
                "self_evaluator_heartbeat", True,
                detail=f"Proposed '{gap_name}' but it already exists. No new gap.",
            )
            logger.info("SelfEvaluator: proposed '%s' already registered — skipping", gap_name)
            return None

        # Write the gap to the operation log so the generation loop picks it up
        detail = (
            f"[{gap_data['dimension']}] {gap_data['reason']}\n"
            f"{gap_data['detail']}"
        )
        self._log_operation(
            "self_evaluator_gap", False,
            detail=detail,
            missing_cap=gap_name,
        )
        logger.info(
            "SelfEvaluator: wrote gap '%s' (dimension=%s, priority=%.2f) to operation log",
            gap_name, gap_data["dimension"], gap_data["priority"],
        )

        return gap_name

    async def evaluate(self) -> Optional[str]:
        """Async wrapper — runs evaluate_sync in a thread executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.evaluate_sync)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def evaluate_once(
    registry: CapabilityRegistry | None = None,
    log_path: Path | None = None,
) -> Optional[str]:
    """Run a single evaluation cycle synchronously. Returns gap name or None."""
    evaluator = SelfEvaluator(registry=registry, log_path=log_path)
    return evaluator.evaluate_sync()
