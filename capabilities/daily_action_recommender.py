"""Daily Action Recommender.

Fetches latest data from daily trackers (Health, Wealth, Happiness, Agency, Capability)
stored in data/ JSONL files, analyzes underperformance using model_interface LLM calls
to generate 1-2 prioritized actions per dimension, and sends a concise daily DM summary
via discord_notifier with expected impacts and follow-up integration.
"""

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from src.kernel.model_interface import BudgetExceededError, call_model
from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.discord_notifier import notify_async
from capabilities.event_loop import EventLoop, PeriodicTask


class DailyActionRecommender:
    def __init__(self, data_dir: Path = Path("data")):
        self.data_dir = data_dir
        self.state_file = data_dir / "daily_action_recommender_state.json"

    def initialize(
        self,
        registry: Optional[CapabilityRegistry] = None,
        event_loop: Optional[EventLoop] = None,
    ) -> None:
        if registry:
            self.register_capability(registry)
        if event_loop:
            self.integrate_with_event_loop(event_loop)

    def register_capability(
        self, registry: CapabilityRegistry
    ) -> Capability:
        cap = Capability(
            name="daily_action_recommender",
            module="capabilities.daily_action_recommender",
            description=(
                "Generates 1-2 prioritized actions per dimension from daily tracker data"
                " and sends daily Discord summary."
            ),
            dependencies=[
                "model_interface",
                "discord_notifier",
                "event_loop",
                "capability_registry",
            ],
        )
        registry.register(cap)
        return cap

    def integrate_with_event_loop(self, loop: EventLoop) -> None:
        def coro_factory() -> "asyncio.coroutine":
            return self.daily_recommendation_coro()

        task = PeriodicTask(
            name="daily_action_recommendations",
            coro_factory=coro_factory,
            interval=86400.0,  # 24 hours
        )
        loop.add_periodic_task(task)

    def load_latest_entry(self, dim: str) -> Optional[Dict[str, any]]:
        p = self.data_dir / f"daily_{dim}.jsonl"
        if not p.exists() or p.stat().st_size == 0:
            return None
        lines = p.read_text(encoding="utf-8").splitlines()
        try:
            return json.loads(lines[-1])
        except (json.JSONDecodeError, IndexError):
            return None

    def _load_state(self) -> Dict[str, str]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_processed_date": None}

    def _save_state(self, state: Dict[str, str]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )

    async def daily_recommendation_coro(self) -> None:
        dims = ["health", "wealth", "happiness", "agency", "capability"]
        data: Dict[str, Dict[str, any]] = {}
        latest_dates: List[str] = []
        for dim in dims:
            entry = self.load_latest_entry(dim)
            if entry:
                data[dim] = entry
                latest_dates.append(entry.get("date", ""))
        if not data or not latest_dates:
            return
        max_date = max(latest_dates)
        state = self._load_state()
        if state["last_processed_date"] == max_date:
            return
        prompt = self._build_prompt(data)
        system_prompt = self._get_system_prompt()
        try:
            resp = call_model(prompt, system=system_prompt)
            summary = (
                f"**Daily Actions ({date.today().isoformat()})**\n"
                f"{resp.text}\n\n*Sent via Archi*"
            )
        except BudgetExceededError:
            actions = self._get_heuristic_actions(data)
            summary = f"**Heuristic Actions ({date.today().isoformat()})**\n\n"
            for dim, acts in actions.items():
                summary += f"## {dim.capitalize()}\n" + "\n".join(f"- {act}" for act in acts) + "\n\n"
            summary += "*Budget fallback. LLM next time.*"
        await notify_async(summary)
        state["last_processed_date"] = max_date
        self._save_state(state)

    def _build_prompt(self, data: Dict[str, Dict[str, any]]) -> str:
        sections = []
        for dim, entry in data.items():
            sections.append(
                f"**{dim.upper()}**\n{json.dumps(entry, indent=2)}"
            )
        return (
            "Latest daily tracker data. Identify underperformance (score/mood/autonomy"
            " <7/10) and recommend 1-2 actions.\n\n"
            + "\n\n".join(sections)
        )

    def _get_system_prompt(self) -> str:
        return (
            "You are a life coach. For dimensions with score <7/10 (mood for Happiness,"
            " autonomy for Agency, score/hours for others), recommend 1-2 concrete,"
            " prioritized, daily actions with expected impact in (parentheses). Use"
            " markdown ## Dimension\n- Action (impact)\n. Omit good dimensions. If all"
            " >=7: 'All dimensions strong today - maintain momentum!' Keep concise."
        )

    def _get_heuristic_actions(
        self, data: Dict[str, Dict[str, any]]
    ) -> Dict[str, List[str]]:
        actions: Dict[str, List[str]] = {}
        score_map = {
            "happiness": "mood",
            "agency": "autonomy",
            "capability": "score",
            "health": "score",
            "wealth": "score",
        }
        for dim, entry in data.items():
            score_key = score_map.get(dim)
            if score_key:
                score = entry.get(score_key)
                if isinstance(score, (int, float)) and score < 7:
                    actions[dim] = [
                        f"Improve {dim.title()} (current {score}/10): reflect on notes"
                        " and pick 1 key fix today. (Expected: +1-2 score boost)"
                    ]
        return actions


# Module-level singleton and async wrapper for periodic_registry
_default_recommender: Optional[DailyActionRecommender] = None


def _get_recommender() -> DailyActionRecommender:
    global _default_recommender
    if _default_recommender is None:
        _default_recommender = DailyActionRecommender()
    return _default_recommender


async def daily_recommendation_coro() -> None:
    """Module-level wrapper so periodic_registry can resolve this coroutine."""
    recommender = _get_recommender()
    await recommender.daily_recommendation_coro()