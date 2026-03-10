"""
Weekly synthesis capability that aggregates data from daily Health, Wealth, and Happiness trackers,
computes metrics like averages, trends, variances, and correlations, generates a formatted Discord DM
report with ASCII charts, insights, recommendations, and anomaly flags.
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List

import statistics
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import call_model, get_task_config
from capabilities.discord_notifier import notify_async
from capabilities.event_loop import EventLoop, PeriodicTask


class WeeklyDimensionSynthesis:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def _load_data(self, filename: str, score_key: str) -> Dict[str, float]:
        path = self.data_dir / filename
        data: Dict[str, float] = {}
        if path.exists():
            try:
                for line in path.read_text(encoding='utf-8').splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    d = entry.get('date')
                    s = entry.get(score_key)
                    if d and s is not None:
                        data[d] = float(s)
            except Exception:
                pass
        return data

    async def weekly_report_coro(self) -> None:
        today = date.today()
        days_back = 28
        dates = [(today - timedelta(days=i)).isoformat() for i in range(days_back)]
        health_data = self._load_data('daily_health_entries.jsonl', 'health_score')
        wealth_data = self._load_data('daily_wealth_entries.jsonl', 'wealth_score')
        happiness_data = self._load_data('daily_happiness_entries.jsonl', 'mood')

        def compute_metrics(data_dict: Dict[str, float], target_dates: List[str]) -> Dict[str, float]:
            values = [data_dict.get(d) for d in target_dates if data_dict.get(d) is not None]
            n = len(values)
            if n == 0:
                return {'avg': 0.0, 'trend': 0.0, 'var': 0.0, 'std': 0.0, 'n': 0}
            avg = statistics.mean(values)
            var = statistics.variance(values)
            std = statistics.stdev(values) if n > 1 else 0.0
            trend = self._simple_trend(values)
            return {'avg': avg, 'trend': trend, 'var': var, 'std': std, 'n': n}

        h_metrics = compute_metrics(health_data, dates)
        w_metrics = compute_metrics(wealth_data, dates)
        hap_metrics = compute_metrics(happiness_data, dates)

        common_dates = [d for d in dates if d in {health_data, wealth_data, happiness_data}]
        corrs = {'health_wealth': 0.0, 'health_happiness': 0.0, 'wealth_happiness': 0.0}
        if common_dates:
            h_vals = [health_data[d] for d in common_dates]
            w_vals = [wealth_data[d] for d in common_dates]
            hap_vals = [happiness_data[d] for d in common_dates]
            corrs['health_wealth'] = self._correlation(h_vals, w_vals)
            corrs['health_happiness'] = self._correlation(h_vals, hap_vals)
            corrs['wealth_happiness'] = self._correlation(w_vals, hap_vals)

        summary_data = {
            'health': h_metrics, 'wealth': w_metrics, 'happiness': hap_metrics,
            'correlations': corrs, 'period_days': days_back, 'common_days': len(common_dates)
        }

        def get_weekly_avgs(data_dict: Dict[str, float], target_dates: List[str]) -> List[float]:
            weekly = []
            for wk in range(4):
                wk_dates = target_dates[wk * 7 : (wk + 1) * 7]
                wk_vals = [data_dict.get(d) for d in wk_dates if data_dict.get(d) is not None]
                weekly.append(statistics.mean(wk_vals) if wk_vals else 0.0)
            return weekly

        h_weekly = get_weekly_avgs(health_data, dates)
        w_weekly = get_weekly_avgs(wealth_data, dates)
        hap_weekly = get_weekly_avgs(happiness_data, dates)

        def ascii_chart(weekly_vals: List[float], lbl: str, cur_date: date) -> str:
            bars = []
            for v in weekly_vals:
                bar_len = int((v / 10.0) * 20)
                bar = '█' * bar_len + '░' * (20 - bar_len)
                bars.append(bar)
            avgs_str = ', '.join(f'{x:.1f}' for x in weekly_vals)
            return f'**{lbl}:**\n{"  ".join(bars)}\n({avgs_str})\n'

        charts = (
            ascii_chart(h_weekly, 'Health', today) +
            ascii_chart(w_weekly, 'Wealth', today) +
            ascii_chart(hap_weekly, 'Happiness', today)
        )

        provider, model_name = get_task_config('plan')
        prompt = (
            'Analyze Health-Wealth-Happiness data for insights, anomalies, recommendations.\n\n'
            f'Data: ```json\n{json.dumps(summary_data, indent=2)}\n```\n\n'
            'Output:\n- 2-3 key insights\n- 2-3 actionable recommendations\n- Balance/anomaly flags\n\nConcise.'
        )
        try:
            resp = call_model(prompt, provider=provider, model=model_name)
            insights = resp.text.strip()
        except Exception as exc:
            insights = f'Analysis error: {exc}'

        metrics_str = (
            f'**Health:** avg {h_metrics["avg"]:.1f} | trend {h_metrics["trend"]:.2f} | std {h_metrics["std"]:.1f} (n={h_metrics["n"]})\n'
            f'**Wealth:** avg {w_metrics["avg"]:.1f} | trend {w_metrics["trend"]:.2f} | std {w_metrics["std"]:.1f} (n={w_metrics["n"]})\n'
            f'**Happiness:** avg {hap_metrics["avg"]:.1f} | trend {hap_metrics["trend"]:.2f} | std {hap_metrics["std"]:.1f} (n={hap_metrics["n"]})\n'
            f'**Correlations:** H-W:{corrs["health_wealth"]:.2f} H-Hap:{corrs["health_happiness"]:.2f} W-Hap:{corrs["wealth_happiness"]:.2f}'
        )

        report = (
            f'# 🏥💰😊 Weekly Synthesis Report ({today.isoformat()})\n\n'
            f'{charts}\n\n'
            f'{metrics_str}\n\n'
            f'**Insights, Recs, Flags:**\n```\n{insights}\n```\n\n'
            f'*Generated: {datetime.datetime.now().isoformat()}*'
        )
        await notify_async(report)

    @staticmethod
    def _simple_trend(values: List[float]) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        x = list(range(n))
        sum_x, sum_y, sum_xy, sum_x2 = 0.0, 0.0, 0.0, 0.0
        for xi, yi in zip(x, values):
            sum_x += xi
            sum_y += yi
            sum_xy += xi * yi
            sum_x2 += xi * xi
        denom = n * sum_x2 - sum_x ** 2
        return (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0.0

    @staticmethod
    def _correlation(a: List[float], b: List[float]) -> float:
        n = len(a)
        if n < 2 or len(b) != n:
            return 0.0
        mean_a = statistics.mean(a)
        mean_b = statistics.mean(b)
        cov = statistics.covariance(a, b)
        std_a = statistics.stdev(a)
        std_b = statistics.stdev(b)
        return cov / (std_a * std_b) if std_a > 0 and std_b > 0 else 0.0


_instance: WeeklyDimensionSynthesis | None = None


def initialize(
    data_dir: Path = Path('data'),
    registry: CapabilityRegistry | None = None,
    event_loop: EventLoop | None = None
) -> None:
    global _instance
    _instance = WeeklyDimensionSynthesis(data_dir)
    if registry is not None:
        register_capability(registry)
    if event_loop is not None:
        integrate_with_event_loop(event_loop)


def register_capability(registry: CapabilityRegistry | None = None) -> Capability | None:
    if registry is None:
        return None
    cap = Capability(
        name='weekly_dimension_synthesis',
        module='capabilities.weekly_dimension_synthesis',
        description=(
            'Aggregates data from daily Health, Wealth, and Happiness trackers, computes averages, trends, '
            'variances, correlations over past 4 weeks, generates Discord DM with ASCII charts, LLM insights, '
            'recommendations, anomaly flags.'
        ),
        dependencies=[
            'daily_health_tracker',
            'daily_wealth_tracker',
            'daily_happiness_tracker',
            'discord_notifier',
            'model_interface'
        ]
    )
    registry.register(cap)
    return cap


def integrate_with_event_loop(loop: EventLoop) -> None:
    global _instance
    if _instance is None:
        raise ValueError('Call initialize() first.')
    coro_factory = lambda: _instance.weekly_report_coro()
    task = PeriodicTask('weekly_dimension_synthesis_report', coro_factory, 604800.0)
    loop.periodic_tasks.append(task)