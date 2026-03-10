"""
Periodically analyzes historical data from daily trackers (Health, Wealth, Happiness,
Agency, Capability) to compute trends like 7-day averages, growth rates, and correlations,
generates text-based charts and insights, sends Discord DM summaries, and provides
dimension_trends.json for daily_action_recommender integration.
"""

import json
from pathlib import Path
from typing import Dict, List, Any
from src.kernel.model_interface import call_model
from src.kernel.capability_registry import Capability, CapabilityRegistry
from capabilities.event_loop import EventLoop, PeriodicTask
from capabilities.discord_notifier import notify_async

_analyzer: DimensionTrendAnalyzer | None = None

class DimensionTrendAnalyzer:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    @staticmethod
    def pearson_correlation(x: List[float], y: List[float]) -> float:
        n = len(x)
        if n < 2:
            return 0.0
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        den_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
        den_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5
        return num / (den_x * den_y) if den_x > 0 and den_y > 0 else 0.0

    @staticmethod
    def generate_ascii_chart(values: List[float], width: int = 50) -> str:
        if not values:
            return "No data"
        values = values[-width:]
        if not values:
            return ""
        min_v, max_v = min(values), max(values)
        if min_v == max_v:
            return "█" * len(values)
        chars = " ▁▂▃▄▅▆▇█"
        norm = [int(((v - min_v) / (max_v - min_v)) * (len(chars) - 1)) for v in values]
        return "".join(chars[i] for i in norm)

    def load_tracker_data(self, tracker: str) -> List[Dict[str, Any]]:
        p = self.data_dir / f"daily_{tracker}.json"
        return json.loads(p.read_text()) if p.exists() else []

    def _extract_score(self, entry: Dict[str, Any], dim: str) -> float:
        fields = {
            "happiness": "mood",
            "agency": "autonomy",
            "capability": "score",
            "health": "score",
            "wealth": "score",
        }
        return float(entry.get(fields.get(dim, "score"), 0))

    def compute_single_trend(self, data: List[Dict[str, Any]], dim: str) -> Dict[str, Any]:
        if len(data) < 2:
            return {"avg7": 0.0, "growth": 0.0, "chart": ""}
        data = sorted(data, key=lambda e: e.get("date", "0000-00-00"))
        scores: List[float] = [self._extract_score(e, dim) for e in data]
        n7 = min(7, len(scores))
        avg7 = sum(scores[-n7:]) / n7
        growth = 0.0
        if len(scores) >= 14:
            prev_avg = sum(scores[-14 : -7]) / 7
            growth = (avg7 - prev_avg) / 7
        chart = self.generate_ascii_chart(scores)
        return {"avg7": round(avg7, 2), "growth": round(growth, 3), "chart": chart}

    def compute_dimension_trends(self) -> Dict[str, Any]:
        trackers = ["health", "wealth", "happiness", "agency", "capability"]
        data_dict = {t: self.load_tracker_data(t) for t in trackers}
        active = [t for t, d in data_dict.items() if d]
        trends = {t: self.compute_single_trend(data_dict[t], t) for t in active}
        trends["correlations"] = self.compute_correlations(data_dict)
        return trends

    def compute_correlations(self, data_dict: Dict[str, List[Dict]]) -> Dict[str, float]:
        dims = list(data_dict.keys())
        corrs: Dict[str, float] = {}
        for i, d1 in enumerate(dims):
            for d2 in dims[i + 1 :]:
                if not data_dict[d1] or not data_dict[d2]:
                    continue
                rec1 = data_dict[d1][-60:]
                rec2 = data_dict[d2][-60:]
                dates1 = {e["date"]: self._extract_score(e, d1) for e in rec1}
                common_dates = set(dates1) & set(e["date"] for e in rec2)
                paired_x, paired_y = [], []
                for date in common_dates:
                    paired_y.append(self._extract_score(next(e for e in rec2 if e["date"] == date), d2))
                    paired_x.append(dates1[date])
                if len(paired_x) >= 3:
                    corrs[f"{d1}-{d2}"] = round(self.pearson_correlation(paired_x, paired_y), 3)
        return corrs

    def generate_insights(self, trends: Dict[str, Any]) -> str:
        prompt = f"""Analyze these life dimension trends. Focus on declines/improvements,
strong correlations (|r| > 0.5), and 1-2 prioritized suggestions. Concise, <150 words.

{trends}"""
        try:
            resp = call_model(prompt)
            return resp.text.strip()
        except Exception:
            return "Model unavailable."

    def format_summary(self, trends: Dict[str, Any], insights: str) -> str:
        lines = ["**Daily Dimension Trends**\n"]
        for dim, t in trends.items():
            if dim == "correlations":
                lines.append("\n**Correlations:**")
                for pair, r in t.items():
                    lines.append(f"{pair}: {r}")
                continue
            lines.extend([f"\n{dim.upper()}:", f"7d avg: {t['avg7']} | growth/wk: {t['growth']}", t["chart"]])
        lines.append(f"\n**Insights:**\n{insights}")
        return "\n".join(lines)

    async def analyze_coro(self) -> None:
        trends = self.compute_dimension_trends()
        (self.data_dir / "dimension_trends.json").write_text(json.dumps(trends, indent=2))
        insights = self.generate_insights(trends)
        summary = self.format_summary(trends, insights)
        await notify_async(summary)

    def integrate_with_event_loop(self, loop: EventLoop) -> None:
        def factory() -> Any:
            return self.analyze_coro()
        task_daily = PeriodicTask("dimension_trends_daily", factory, 86400.0)
        task_weekly = PeriodicTask("dimension_trends_weekly", factory, 604800.0)
        loop.add_periodic_task(task_daily)
        loop.add_periodic_task(task_weekly)

def initialize(
    data_dir: Path = Path("data"),
    registry: CapabilityRegistry | None = None,
    event_loop: EventLoop | None = None,
) -> None:
    global _analyzer
    if _analyzer is None:
        _analyzer = DimensionTrendAnalyzer(data_dir)
    if registry is not None:
        register_capability(registry)
    if event_loop is not None:
        _analyzer.integrate_with_event_loop(event_loop)

def register_capability(registry: CapabilityRegistry | None = None) -> Capability | None:
    if registry is None:
        return None
    cap = Capability(
        name="dimension_trend_analyzer",
        module="capabilities.dimension_trend_analyzer",
        description="Periodically analyzes historical data from daily trackers to compute trends, charts, insights; notifies via Discord; stores trends.json for recommender.",
        dependencies=[
            "daily_health_tracker",
            "daily_wealth_tracker",
            "daily_happiness_tracker",
            "daily_agency_tracker",
            "daily_capability_tracker",
            "daily_action_recommender",
            "discord_notifier",
            "event_loop",
            "model_interface",
        ],
    )
    registry.add(cap)
    return cap