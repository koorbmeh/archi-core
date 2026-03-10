"""
Integrates google_sheets_analyzer into the Discord message processing pipeline.
Enables Jesse to request analysis of specific Google Sheets via chat commands
like 'analyze sheet <sheet_id> <range>'.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

import gspread
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import BudgetExceededError, ModelResponse, call_model

from capabilities.conversational_memory import store_message
from capabilities.discord_notifier import notify
from capabilities.google_sheets_analyzer import get_gspread_client


pending_messages: Optional[asyncio.Queue[Dict[str, Any]]] = None


def initialize(
    registry: Optional[CapabilityRegistry] = None,
) -> Capability:
    global pending_messages
    pending_messages = asyncio.Queue()
    cap = register_capability(registry)
    return cap


def register_capability(
    registry: Optional[CapabilityRegistry] = None,
) -> Capability:
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name="wire_google_sheets_analyzer",
        module="capabilities.wire_google_sheets_analyzer",
        description=(
            "Integrates google_sheets_analyzer into the Discord message processing "
            "pipeline enabling Jesse to request analysis of specific Google Sheets "
            "via chat commands."
        ),
        dependencies=["google_sheets_analyzer"],
    )
    registry.add(cap)
    return cap


def receive_message(
    content: str,
    user_id: str,
    *,
    attachment_urls: List[str] | None = None,
) -> None:
    global pending_messages
    if pending_messages is None:
        return
    content_lower = content.lower().strip()
    if not content_lower.startswith("analyze sheet "):
        return
    pending_messages.put_nowait(
        {
            "content": content,
            "user_id": user_id,
            "attachments": attachment_urls or [],
        }
    )
    store_message(user_id, content)


async def process_one(
    repo_path: str,
    registry: CapabilityRegistry,
) -> bool:
    global pending_messages
    if pending_messages is None:
        return False
    try:
        msg = pending_messages.get_nowait()
    except asyncio.QueueEmpty:
        return False
    try:
        await _handle_message(msg)
    finally:
        pending_messages.task_done()
    return True


async def process_pending(
    repo_path: str,
    registry: CapabilityRegistry,
) -> int:
    global pending_messages
    if pending_messages is None:
        return 0
    count = 0
    while True:
        try:
            msg = pending_messages.get_nowait()
        except asyncio.QueueEmpty:
            break
        try:
            await _handle_message(msg)
        finally:
            pending_messages.task_done()
        count += 1
    return count


async def _handle_message(msg: Dict[str, Any]) -> None:
    content = msg["content"]
    user_id = msg["user_id"]
    parts = re.split(r"\s+", content.strip().lower())
    if len(parts) < 4 or parts[0:2] != ["analyze", "sheet"]:
        return
    sheet_id = parts[2]
    range_str = parts[3]
    try:
        client = get_gspread_client()
        gc = client.open_by_key(sheet_id)
        worksheet = gc.sheet1
        values = worksheet.get(range_str)
        if not values:
            notify(f"No data found in sheet {sheet_id} range {range_str}")
            return
        data_str = "\n".join("\t".join(map(str, row)) for row in values)
        prompt = (
            "Analyze this Google Sheets data. Provide insights, summaries, "
            "trends, anomalies, and recommendations.\n\nData:\n" + data_str
        )
        system = (
            "You are an expert data analyst. Be concise, actionable, and structured."
        )
        resp: ModelResponse = call_model(prompt, system=system)
        if resp.error:
            raise ValueError(resp.error)
        analysis = resp.text
        notify(f"**Sheet {sheet_id} [{range_str}] Analysis:**\n{analysis}")
        store_message(user_id, analysis, role="assistant")
    except BudgetExceededError:
        err = "Analysis budget exceeded."
        notify(err)
        store_message(user_id, err, role="assistant")
    except Exception as e:
        err = f"Error analyzing {sheet_id} [{range_str}]: {str(e)}"
        notify(err)
        store_message(user_id, err, role="assistant")