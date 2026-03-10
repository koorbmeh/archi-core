"""Implements secure Google Sheets access for bills/expenses analysis.
Parsing data into structured expense records, performing categorization/totals/trends/recommendations,
and notifying via Discord DMs on command or periodically. Compatible with daily_wealth_tracker.
"""
import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

import gspread

from src.kernel.model_interface import call_model

from capabilities.discord_notifier import notify
from capabilities.event_loop import EventLoop
from capabilities.google_sheets_analyzer import get_gspread_client
from capabilities.personal_profile_manager import get_manager as get_profile_manager


task_queue: asyncio.Queue | None = None
periodic_sheets: List[Dict[str, str]] = []


def initialize() -> None:
    global task_queue, periodic_sheets
    task_queue = asyncio.Queue()
    try:
        manager = get_profile_manager()
        profile = getattr(manager, 'profile', getattr(manager, 'data', {}))
        periodic_sheets = profile.get('bills_sheets', [])
    except Exception:
        periodic_sheets = []


def infer_category(description: str) -> str:
    desc_lower = description.lower()
    if any(w in desc_lower for w in ['food', 'restaurant', 'grocery', 'mcdonald', 'starbucks']):
        return 'food'
    if any(w in desc_lower for w in ['gas', 'fuel', 'shell', 'chevron', 'exxon']):
        return 'transport'
    if any(w in desc_lower for w in ['amazon', 'walmart', 'target']):
        return 'shopping'
    if any(w in desc_lower for w in ['netflix', 'spotify', 'subscription']):
        return 'subscriptions'
    return 'other'


def parse_expenses(values: List[list]) -> List[Dict[str, Any]]:
    if not values or len(values) < 2:
        return []
    header = [str(h).strip().lower() for h in values[0]]
    date_col = next((i for i, h in enumerate(header) if 'date' in h), None)
    if date_col is None:
        raise ValueError('No date column found')
    amt_col = next((i for i, h in enumerate(header) if any(k in h for k in ['amount', 'outflow', 'debit', 'charge'])), None)
    if amt_col is None:
        raise ValueError('No amount column found')
    desc_col = next((i for i, h in enumerate(header) if any(k in h for k in ['description', 'payee', 'merchant'])), desc_col=None)
    cat_col = next((i for i, h in enumerate(header) if 'category' in h), None)
    expenses = []
    for row in values[1:]:
        if len(row) <= max(date_col, amt_col, desc_col or 0, cat_col or 0):
            continue
        try:
            date_str = str(row[date_col]).strip()
            amt_str = str(row[amt_col]).replace('$', '').replace(',', '').strip()
            amount = abs(float(amt_str)) if amt_str else 0.0
            desc = str(row[desc_col] if desc_col is not None else '').strip()
            cat = str(row[cat_col]).strip() if cat_col is not None and len(row) > cat_col else None
            if amount > 0:
                expenses.append({'date': date_str, 'amount': amount, 'description': desc, 'category': cat})
        except (ValueError, IndexError, TypeError):
            continue
    return expenses


def compute_analysis(expenses: List[Dict[str, Any]]) -> Dict[str, Any]:
    for exp in expenses:
        if exp['category'] is None:
            exp['category'] = infer_category(exp['description'])
    total = sum(e['amount'] for e in expenses)
    cat_totals = defaultdict(float)
    daily_totals = defaultdict(float)
    dates = []
    for e in expenses:
        cat_totals[e['category']] += e['amount']
        try:
            dt = datetime.strptime(e['date'], '%Y-%m-%d')
            day_str = dt.date().isoformat()
            daily_totals[day_str] += e['amount']
            dates.append(dt.date())
        except ValueError:
            pass
    avg_daily = total / len(daily_totals) if daily_totals else 0
    trend = 'no data'
    if len(set(dates)) >= 14:
        recent = sorted(set(dates))[-14:]
        last7_avg = sum(daily_totals[d.isoformat()] for d in recent[-7:]) / 7
        prev7_avg = sum(daily_totals[d.isoformat()] for d in recent[-14:-7]) / 7
        if last7_avg > prev7_avg * 1.1:
            trend = 'increasing'
        elif last7_avg < prev7_avg * 0.9:
            trend = 'decreasing'
        else:
            trend = 'stable'
    summary_lines = [f'Total: ${total:.2f}', f'Daily avg: ${avg_daily:.2f} ({trend})', 'By category:']
    for cat, amt in sorted(cat_totals.items(), key=lambda kv: kv[1], reverse=True):
        summary_lines.append(f'  {cat}: ${amt:.2f}')
    summary = '\n'.join(summary_lines)
    prompt = f'Analyze bills:\n{summary}\nGive 3 short recommendations to cut costs.'
    try:
        resp = call_model(prompt)
        recs = resp.text.strip()
    except Exception:
        recs = 'Track uncategorized spends. Review top categories.'
    return {'summary': summary, 'recommendations': recs, 'expenses': expenses}


def analyze_bills(sheet_id: str, range_str: str, user_id: str) -> str:
    try:
        client = get_gspread_client()
        sh = client.open_by_key(sheet_id)
        ws = sh.get_worksheet(0)
        values = ws.get(range_str or 'A1:Z1000')
        expenses = parse_expenses(values)
        analysis = compute_analysis(expenses)
        top_exp = '\n'.join(f"{e['description'][:50]}: ${e['amount']:.2f} ({e['category']})"
                            for e in sorted(expenses, key=lambda e: e['amount'], reverse=True)[:5])
        full_text = f"""Bills analysis ({user_id}, {sheet_id}:{range_str}):
{analysis['summary']}

Recommendations:
{analysis['recommendations']}

Top 5:
{top_exp}

# TODO: feed expenses to daily_wealth_tracker"""
        notify(full_text)
        return full_text
    except Exception as e:
        err_msg = f"Bills analysis failed for {sheet_id}: {str(e)}"
        notify(err_msg)
        return err_msg


async def process_queue_coro() -> None:
    processed = 0
    while True:
        try:
            task = task_queue.get_nowait()  # type: ignore
            sheet_id, range_str, user_id = task
            analyze_bills(sheet_id, range_str, user_id)
            task_queue.task_done()  # type: ignore
            processed += 1
        except asyncio.QueueEmpty:
            break
        except Exception as e:
            notify(f"Bill task error: {e}")
    if processed:
        notify(f"Completed {processed} bills analyses.")


async def periodic_analysis_coro() -> None:
    if not periodic_sheets:
        return
    summaries = []
    for config in periodic_sheets:
        summ = analyze_bills(config['sheet_id'], config.get('range_str', 'A1:Z1000'), 'periodic')
        summaries.append(summ)
    combined = '\n\n----\n\n'.join(summaries)
    notify(f"Periodic bills summary:\n{combined}")


def receive_message(content: str, user_id: str, attachment_urls: List[str] | None = None) -> None:
    if not content.startswith('!analyze_bills '):
        return
    parts = content[14:].strip().split(maxsplit=1)
    if len(parts) < 1:
        notify("Usage: !analyze_bills SHEET_ID [RANGE]")
        return
    sheet_id = parts[0]
    range_str = parts[1] if len(parts) > 1 else None
    if task_queue is None:
        notify("Analyzer not initialized.")
        return
    task_queue.put_nowait((sheet_id, range_str, user_id))
    notify(f"Enqueued bills analysis for {sheet_id}.")


def integrate_with_event_loop(loop: EventLoop) -> None:
    loop.add_periodic_task('bills.process_queue', process_queue_coro, 30.0)
    if periodic_sheets:
        loop.add_periodic_task('bills.periodic', periodic_analysis_coro, 86400.0)