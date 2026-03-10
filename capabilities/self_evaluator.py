"""
Periodic self-reflection loop for Archi.

Analyzes interaction logs and operational history every 6 hours to proactively
generate capability gaps and insights on improving service to Jesse.
"""

import asyncio
import logging
from pathlib import Path

from src.kernel.alignment_gates import ActionContext, check_gates
from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.gap_detector import Gap, detect_gaps
from src.kernel.model_interface import BudgetExceededError, call_model, get_session_cost

from capabilities import discord_notifier
from capabilities import event_loop as event_loop_module
from capabilities import api_introspection

logger = logging.getLogger(__name__)

_EVALUATE_INTERVAL_SECONDS = 6 * 3600  # 6 hours

SYSTEM_PROMPT = (
    "You are Archi's self-reflection engine. Analyze the provided operational context "
    "and identify patterns, capability gaps, and concrete opportunities to better serve Jesse. "
    "Be specific, honest, and prioritise actionable insights."
)


class SelfEvaluator:
    """Periodically reflects on Archi's operational history to surface insights and gaps."""

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._registry = registry or CapabilityRegistry()
        self._log_path = log_path

    def _build_reflection_prompt(self, capability_summary: str, detected_gaps: list[Gap]) -> str:
        gap_lines = "\n".join(
            f"- [{g.priority:.1f}] {g.name}: {g.reason}" for g in detected_gaps[:10]
        )
        return (
            "## Archi Self-Reflection Report\n\n"
            "### Capability Summary\n"
            f"{capability_summary}\n\n"
            "### Detected Gaps (top 10)\n"
            f"{gap_lines or 'None detected.'}\n\n"
            "### Reflection Tasks\n"
            "1. Identify recurring patterns in Jesse's interactions that Archi is not handling well.\n"
            "2. Highlight life-context areas (e.g. scheduling, health, projects) where coverage is thin.\n"
            "3. Evaluate Archi's response quality, latency, and proactivity over recent interactions.\n"
            "4. Propose 3-5 prioritised improvements or new capabilities.\n"
            "5. Flag any alignment or safety concerns observed.\n"
        )

    def _check_alignment(self, estimated_cost: float) -> list:
        ctx = ActionContext(
            action_type="model_call",
            target="self_evaluation_reflection",
            estimated_cost=estimated_cost,
            metadata={"source": "self_evaluator"},
        )
        return check_gates(ctx, session_cost=get_session_cost())

    def _collect_gaps(self) -> list[Gap]:
        try:
            return detect_gaps(self._registry, self._log_path)
        except Exception as exc:
            logger.warning("Gap detection failed: %s", exc)
            return []

    def _get_capability_summary(self) -> str:
        try:
            return api_introspection.summarize_all(self._registry)
        except Exception as exc:
            logger.warning("API introspection failed: %s", exc)
            return "(capability summary unavailable)"

    async def _notify_insight(self, insight_text: str) -> None:
        try:
            await discord_notifier.notify_async(
                f"🔍 **Archi Self-Reflection Insight**\n\n{insight_text[:1900]}"
            )
        except Exception as exc:
            logger.error("Discord notification failed: %s", exc)

    async def _notify_gaps(self, gaps: list[Gap]) -> None:
        if not gaps:
            return
        top = gaps[:5]
        lines = "\n".join(f"• [{g.priority:.1f}] **{g.name}**: {g.reason}" for g in top)
        try:
            await discord_notifier.notify_async(
                f"⚠️ **Archi Capability Gaps Detected**\n\n{lines}"
            )
        except Exception as exc:
            logger.error("Discord gap notification failed: %s", exc)

    async def evaluate(self) -> None:
        """Core self-evaluation coroutine: reflect, detect gaps, and notify Jesse."""
        logger.info("SelfEvaluator: starting evaluation cycle")

        # Alignment gate check (use a small cost estimate for budgeting)
        failures = self._check_alignment(estimated_cost=0.05)
        if failures:
            reasons = "; ".join(f.reason for f in failures)
            logger.warning("SelfEvaluator: alignment gate blocked evaluation – %s", reasons)
            return

        detected_gaps = await asyncio.get_event_loop().run_in_executor(
            None, self._collect_gaps
        )
        capability_summary = await asyncio.get_event_loop().run_in_executor(
            None, self._get_capability_summary
        )

        prompt = self._build_reflection_prompt(capability_summary, detected_gaps)

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: call_model(prompt, system=SYSTEM_PROMPT),
            )
        except BudgetExceededError as exc:
            logger.warning("SelfEvaluator: budget exceeded, skipping evaluation – %s", exc)
            return
        except Exception as exc:
            logger.error("SelfEvaluator: model call failed – %s", exc)
            return

        logger.info(
            "SelfEvaluator: evaluation complete (tokens_in=%d, tokens_out=%d, cost=%.4f)",
            response.tokens_in,
            response.tokens_out,
            response.cost_estimate,
        )

        await self._notify_insight(response.text)
        await self._notify_gaps(detected_gaps)

        logger.info("SelfEvaluator: cycle complete, %d gaps surfaced", len(detected_gaps))


def initialize(
    loop: event_loop_module.EventLoop | None = None,
    registry: CapabilityRegistry | None = None,
    log_path: Path | None = None,
) -> SelfEvaluator:
    """
    Register a periodic self-evaluation task with the event loop.

    Creates a SelfEvaluator and schedules its evaluate() coroutine to run
    every 6 hours via a PeriodicTask. Returns the SelfEvaluator instance.
    """
    evaluator = SelfEvaluator(registry=registry, log_path=log_path)

    if loop is None:
        loop = event_loop_module.create_event_loop()

    task = event_loop_module.PeriodicTask(
        name="self_evaluator",
        coro_factory=evaluator.evaluate,
        interval=_EVALUATE_INTERVAL_SECONDS,
    )
    loop.register_task(task)

    logger.info(
        "SelfEvaluator: periodic task registered (interval=%ds)", _EVALUATE_INTERVAL_SECONDS
    )
    return evaluator