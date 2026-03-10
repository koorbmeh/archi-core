"""
Aggregates weekly metrics from Health, Wealth, Happiness, Agency, and Capability daily trackers
to compute trends, dimension scores, cross-dimensional insights, and generate comprehensive
Discord DM reports with text charts, summaries, imbalances, and action recommendations.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import call_model, ModelResponse, get_session_cost
from src.kernel.alignment_gates import ActionContext, check_gates

from capabilities.discord_notifier import notify
from capabilities.event_loop import EventLoop


_data_dir: Optional[Path] = None
_synth: Optional['WeeklyFullDimensionSynthesis'] = None


class WeeklyFullDimensionSynthesis:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.dimensions: List[str] = ['health', 'wealth', 'happiness', 'agency', 'capability']
        self.score_fields: Dict[str, str] = {
            'happiness': 'mood', 'agency': 'autonomy', 'capability': 'score',
            'health': 'score', 'wealth': 'score'
        }
        self.qual_fields: Dict[str, str] = {
            'happiness': 'gratitudes', 'agency': 'decisions', 'capability': 'skills',
            'health': 'highlights', 'wealth': 'milestones'
        }

    def load_dimension_data(self, dimension: str, days_back: int = 35) -> List[Dict[str, Any]]:
        file_path = self.data_dir / f"{dimension}.json"
        if not file_path.exists():
            return []
        try:
            with file_path.open('r') as f:
                entries = json.load(f)
            now = datetime.now().date()
            recent = []
            for e in entries:
                if isinstance(e, dict) and 'date' in e:
                    try:
                        edate = datetime.strptime(e['date'], '%Y-%m-%d').date()
                        if (now - edate).days <= days_back:
                            recent.append(e)
                    except ValueError:
                        continue
            return sorted(recent, key=lambda e: e.get('date', ''))
        except Exception:
            return []

    def _get_week_periods(self, num_weeks: int = 5) -> List[Tuple[str, str]]:
        now = datetime.now()
        monday_offset = timedelta(days=now.weekday())
        cur_monday = (now - monday_offset).date()
        periods = []
        for _ in range(num_weeks):
            start = cur_monday.strftime('%Y-%m-%d')
            end_date = cur_monday + timedelta(days=6)
            end = end_date.strftime('%Y-%m-%d')
            periods.append((start, end))
            cur_monday -= timedelta(days=7)
        return periods

    def compute_aggregates(self) -> Dict[str, List[Dict[str, Any]]]:
        periods = self._get_week_periods()
        aggs: Dict[str, List[Dict[str, Any]]] = {d: [] for d in self.dimensions}
        for dim in self.dimensions:
            data = self.load_dimension_data(dim)
            for wstart, wend in periods:
                wdata = [d for d in data if wstart <= d.get('date', '') <= wend]
                if not wdata:
                    continue
                sf = self.score_fields[dim]
                scores = [float(d.get(sf, 0)) for d in wdata if d.get(sf) is not None]
                avg = sum(scores) / len(scores) if scores else 0.0
                ndays = len([d for d in wdata if d.get(sf) is not None])
                qf = self.qual_fields.get(dim)
                qsum = ''
                if qf:
                    qs = [q for d in wdata for q in d.get(qf, [])]
                    qsum = '; '.join(list(set(qs))[:5])
                aggs[dim].append({
                    'week_start': wstart, 'avg_score': round(avg, 1),
                    'n_days': ndays, 'qual_summary': qsum
                })
        return aggs

    def compute_trends(self, aggs: Dict[str, List[Dict]]) -> Dict[str, Dict[str, Any]]:
        trends = {}
        for dim, weeks in aggs.items():
            if len(weeks) < 2:
                trends[dim] = {'slope': 0.0, 'improving': False}
                continue
            wscores = [w['avg_score'] for w in reversed(weeks)]
            n = len(wscores)
            slope = (wscores[-1] - wscores[0]) / (n - 1) if n > 1 else 0.0
            trends[dim] = {'slope': round(slope, 2), 'improving': slope > 0}
        return trends

    def _scores_chart(self, scores: Dict[str, float]) -> str:
        maxl = max((len(d) for d in scores), default=8) + 2
        lines = []
        for dim in sorted(scores, key=scores.get, reverse=True):
            s = scores[dim]
            barlen = int(s * 3)
            bar = '█' * barlen + '░' * (30 - barlen)
            lines.append(f"`{dim:<{maxl}}|{bar}|{s:>4.1f}`")
        return '\n'.join(lines)

    def _trends_chart(self, trends: Dict[str, Dict[str, Any]]) -> str:
        lines = []
        for dim in sorted(self.dimensions):
            t = trends[dim]['slope']
            sym = '↗️' if t > 0 else ('↘️' if t < 0 else '➡️')
            lines.append(f"`{dim:<12}| {sym} {t:>+4.2f}`")
        return '\n'.join(lines)

    def generate_insights(self, aggs: Dict, trends: Dict, scores: Dict) -> str:
        sum_aggs = {k: [w['avg_score'] for w in v[:3]] for k, v in aggs.items() if v}
        qrec = {k: v[0].get('qual_summary', 'N/A') for k, v in aggs.items() if v}
        prompt = f"""Weekly full-dimension analysis.
Scores: {scores}
Trends: {trends}
Recent avgs (3wks): {sum_aggs}
Quals: {qrec}

Identify patterns, imbalances (<6 or >>avg), synergies/risks. 3 prioritized actions.
Format as bullet list. <400 chars."""
        try:
            ctx = ActionContext('model_call', 'weekly_insights', estimated_cost=0.01)
            if check_gates(ctx, session_cost=get_session_cost()):
                return '**Gate blocked.**'
            resp: ModelResponse = call_model(prompt)
            return resp.text.strip()[:800]
        except Exception as e:
            return f'Insights error: {str(e)[:100]}'

    def generate_report(self) -> str:
        aggs = self.compute_aggregates()
        trends_dict = self.compute_trends(aggs)
        curr_scores = {d: aggs[d][0]['avg_score'] if aggs[d] else 5.0 for d in self.dimensions}
        overall = sum(curr_scores.values()) / len(curr_scores)
        imb_low = [d for d, s in curr_scores.items() if s < overall - 1.2]
        chart = self._scores_chart(curr_scores)
        tchart = self._trends_chart(trends_dict)
        qrec = '\n'.join(f"{d}: {aggs[d][0].get('qual_summary','')} "[:50] for d in self.dimensions if aggs[d])
        summary = f"""**🌈 Full Dimension Weekly Synthesis**

**Scores:**
{chart}

**Overall: {overall:.1f}/10** | **Imbalance Low:** {', '.join(imb_low) or 'None'}

**Trends:**
{tchart}

**Highlights:**
{qrec}"""
        insights = self.generate_insights(aggs, trends_dict, curr_scores)
        return summary + f"\n\n**Insights & Actions:**\n{insights}"


def initialize(data_dir: Path = Path('data'), registry: Optional[CapabilityRegistry] = None,
               event_loop: Optional[EventLoop] = None) -> None:
    global _data_dir, _synth
    _data_dir = data_dir
    _synth = WeeklyFullDimensionSynthesis(data_dir)
    if registry is not None:
        register_capability(registry)
    if event_loop is not None:
        integrate_with_event_loop(event_loop)


def integrate_with_event_loop(loop: EventLoop) -> None:
    loop.add_periodic_task(
        'weekly_full_dimension_synthesis',
        weekly_full_dimension_synthesis_coro,
        interval=604800.0  # 7 days
    )


async def weekly_full_dimension_synthesis_coro() -> None:
    if _synth is None:
        return
    try:
        report = _synth.generate_report()
        notify(report)
    except Exception as e:
        notify(f"**Weekly Full Synthesis Error:** {str(e)}")


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Optional[Capability]:
    if registry is None:
        return None
    cap = Capability(
        name='weekly_full_dimension_synthesis',
        module='capabilities.weekly_full_dimension_synthesis',
        description=__doc__.strip(),
        dependencies=['weekly_dimension_synthesis', 'daily_agency_tracker',
                      'daily_capability_tracker', 'event_loop', 'discord_notifier']
    )
    registry.register(cap)
    return cap