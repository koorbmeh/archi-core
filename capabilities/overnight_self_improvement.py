"""
Overnight Self-Improvement module for Archi.

Analyzes conversation history, capability usage, and operational gaps across
Health, Wealth, Happiness, Agency, Capability, and Synthesis dimensions to
implement targeted enhancements while Jesse is away. Delivers a summary on return.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.gap_detector import Gap, detect_gaps
from src.kernel.model_interface import BudgetExceededError, call_model, get_session_cost
from src.kernel.alignment_gates import ActionContext, check_gates
from src.kernel.self_modifier import apply_change

from capabilities.conversational_memory import get_context, get_recent_messages
from capabilities.timestamped_chat_history_recall import recall_messages_in_range
from capabilities.self_evaluator import SelfEvaluator
from capabilities.generation_loop import run_cycle, CycleResult
from capabilities.discord_notifier import notify

logger = logging.getLogger(__name__)

JESSE_USER_ID = os.environ.get("JESSE_DISCORD_ID", "jesse")
REPO_PATH = str(Path(__file__).resolve().parent.parent)
SIX_DIMENSIONS = ["Health", "Wealth", "Happiness", "Agency", "Capability", "Synthesis"]

ANALYSIS_SYSTEM = (
    "You are Archi's self-improvement planner. Analyze conversation history and gaps "
    "against Jesse's six life dimensions: Health, Wealth, Happiness, Agency, Capability, "
    "and Synthesis. Be concise, specific, and prioritize high-impact improvements."
)


@dataclass
class ImprovementResult:
    """Summary of one overnight self-improvement run."""
    cycles_run: int = 0
    gaps_addressed: list[str] = field(default_factory=list)
    changes_applied: list[str] = field(default_factory=list)
    dimension_insights: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    summary: str = ""


class OvernightSelfImprovement:
    """
    Autonomous overnight self-improvement engine.

    Runs gap detection, analyzes Jesse's conversation history across the six
    dimensions, executes generation loop cycles to close high-impact gaps, and
    delivers a summary via Discord when Jesse returns.
    """

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        max_cycles: int = 5,
        log_path: Path | None = None,
    ) -> None:
        self.registry = registry or CapabilityRegistry()
        self.max_cycles = max_cycles
        self.log_path = log_path
        self._result = ImprovementResult()

    def run(self) -> ImprovementResult:
        """Execute the full overnight self-improvement pipeline."""
        logger.info("Overnight self-improvement session starting.")
        self._result = ImprovementResult()

        try:
            history = self._fetch_history()
            gaps = self._detect_all_gaps()
            insights = self._analyze_dimensions(history, gaps)
            self._result.dimension_insights = insights
            prioritized = self._prioritize_gaps(gaps, insights)
            self._run_improvement_cycles(prioritized)
        except BudgetExceededError:
            logger.warning("Budget exceeded during overnight session; stopping early.")
            self._result.errors.append("Budget exceeded — session ended early.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in overnight session: %s", exc)
            self._result.errors.append(str(exc))

        self._result.summary = self._build_summary()
        self._deliver_summary()
        logger.info("Overnight self-improvement session complete.")
        return self._result

    def _fetch_history(self) -> list[dict[str, Any]]:
        """Retrieve recent conversation history for Jesse."""
        try:
            messages = get_recent_messages(JESSE_USER_ID, n=100)
            logger.debug("Fetched %d messages from conversational memory.", len(messages))
            return messages
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch history: %s", exc)
            return []

    def _fetch_timestamped_range(self, hours_back: int = 24) -> list[dict[str, Any]]:
        """Fetch messages from the past N hours using timestamped recall."""
        end = time.time()
        start = end - hours_back * 3600
        try:
            return recall_messages_in_range(JESSE_USER_ID, start, end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Timestamped recall failed: %s", exc)
            return []

    def _detect_all_gaps(self) -> list[Gap]:
        """Run gap detection and log results."""
        try:
            gaps = detect_gaps(self.registry, self.log_path)
            logger.info("Detected %d gaps.", len(gaps))
            return gaps
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gap detection failed: %s", exc)
            return []

    def _analyze_dimensions(
        self, history: list[dict[str, Any]], gaps: list[Gap]
    ) -> dict[str, str]:
        """Use the model to analyze history and gaps across six dimensions."""
        history_text = _format_messages(history[:40])
        gap_text = _format_gaps(gaps[:15])
        prompt = (
            f"Conversation history (recent):\n{history_text}\n\n"
            f"Detected capability gaps:\n{gap_text}\n\n"
            f"For each dimension ({', '.join(SIX_DIMENSIONS)}), identify the single most "
            "impactful improvement Archi can make tonight. Return one line per dimension: "
            "'DimensionName: insight'."
        )
        insights: dict[str, str] = {}
        try:
            response = call_model(prompt, system=ANALYSIS_SYSTEM)
            for line in response.text.strip().splitlines():
                for dim in SIX_DIMENSIONS:
                    if line.strip().startswith(dim):
                        insights[dim] = line.strip()
                        break
        except BudgetExceededError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dimension analysis failed: %s", exc)
        return insights

    def _prioritize_gaps(
        self, gaps: list[Gap], insights: dict[str, str]
    ) -> list[Gap]:
        """Return gaps sorted by priority, limited to max_cycles."""
        sorted_gaps = sorted(gaps, key=lambda g: g.priority, reverse=True)
        return sorted_gaps[: self.max_cycles]

    def _run_improvement_cycles(self, gaps: list[Gap]) -> None:
        """Execute generation loop cycles for prioritized gaps."""
        for gap in gaps:
            if get_session_cost() > _session_budget():
                logger.warning("Session budget reached; stopping cycles.")
                break
            ctx = ActionContext(
                action_type="self_improvement",
                target=gap.name,
                estimated_cost=0.05,
                metadata={"gap_source": gap.source},
            )
            failures = check_gates(ctx, session_cost=get_session_cost())
            if failures:
                reasons = [f.reason for f in failures]
                logger.warning("Gate blocked cycle for %s: %s", gap.name, reasons)
                self._result.errors.append(f"Blocked {gap.name}: {reasons}")
                continue
            self._execute_cycle(gap)

    def _execute_cycle(self, gap: Gap) -> None:
        """Run a single generation loop cycle and record the outcome."""
        logger.info("Running improvement cycle for gap: %s", gap.name)
        try:
            cycle: CycleResult = run_cycle(
                repo_path=REPO_PATH,
                registry=self.registry,
                log_path=self.log_path,
            )
            self._result.cycles_run += 1
            self._result.gaps_addressed.append(gap.name)
            if cycle.change and cycle.change.success:
                self._result.changes_applied.append(cycle.change.file_path)
                logger.info("Change applied: %s", cycle.change.file_path)
            elif cycle.error:
                self._result.errors.append(f"{gap.name}: {cycle.error}")
        except BudgetExceededError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cycle failed for %s: %s", gap.name, exc)
            self._result.errors.append(f"{gap.name}: {exc}")

    def _build_summary(self) -> str:
        """Compose a concise improvement summary for delivery to Jesse."""
        r = self._result
        lines = [
            "🌙 **Overnight Self-Improvement Report**",
            f"Cycles run: {r.cycles_run} | Gaps addressed: {len(r.gaps_addressed)} | "
            f"Files changed: {len(r.changes_applied)}",
        ]
        if r.dimension_insights:
            lines.append("\n**Dimension Insights:**")
            for insight in r.dimension_insights.values():
                lines.append(f"  • {insight}")
        if r.changes_applied:
            lines.append("\n**Changes Applied:**")
            for fp in r.changes_applied:
                lines.append(f"  • {fp}")
        if r.errors:
            lines.append(f"\n⚠️ Errors ({len(r.errors)}): " + "; ".join(r.errors[:3]))
        return "\n".join(lines)

    def _deliver_summary(self) -> None:
        """Send the improvement summary via Discord."""
        if not self._result.summary:
            return
        try:
            success = notify(self._result.summary)
            if success:
                logger.info("Summary delivered via Discord.")
            else:
                logger.warning("Discord delivery returned False; summary not sent.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not deliver summary via Discord: %s", exc)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _format_messages(messages: list[dict[str, Any]]) -> str:
    """Format a list of message dicts into a readable string."""
    if not messages:
        return "(no messages)"
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = str(msg.get("content", ""))[:200]
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def _format_gaps(gaps: list[Gap]) -> str:
    """Format a list of Gap objects into a readable string."""
    if not gaps:
        return "(no gaps detected)"
    return "\n".join(
        f"- {g.name} (priority={g.priority:.2f}, source={g.source}): {g.reason}"
        for g in gaps
    )


def _session_budget() -> float:
    """Return the configured session budget ceiling."""
    try:
        return float(os.environ.get("ARCHI_SESSION_BUDGET", "1.0"))
    except ValueError:
        return 1.0


def run_overnight(
    registry: CapabilityRegistry | None = None,
    max_cycles: int = 5,
    log_path: Path | None = None,
) -> ImprovementResult:
    """
    Convenience entry point to launch an overnight self-improvement session.

    Args:
        registry: Optional pre-loaded CapabilityRegistry.
        max_cycles: Maximum number of generation cycles to execute.
        log_path: Optional path for operational log scanning.

    Returns:
        ImprovementResult with the session outcome.
    """
    engine = OvernightSelfImprovement(
        registry=registry,
        max_cycles=max_cycles,
        log_path=log_path,
    )
    return engine.run()