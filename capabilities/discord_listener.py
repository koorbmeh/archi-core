"""
Discord listener capability with timestamped chat history recall integration.

Processes incoming Discord DMs and supports recall commands:
  !recall <timestamp>         - Find message at exact timestamp
  !nearest <timestamp>        - Find message nearest to timestamp
  !messages <start> <end>     - List messages in a time range

Falls back to standard conversational memory and LLM response for non-commands.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import Any

from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.model_interface import BudgetExceededError, call_model

import capabilities.conversational_memory as conversational_memory
import capabilities.timestamped_chat_history_recall as recall_module

logger = logging.getLogger(__name__)

# Thread-safe message queue: entries are dicts with 'content', 'user_id', 'attachment_urls'
_message_queue: deque[dict[str, Any]] = deque()
_queue_lock = asyncio.Lock()

# Regex patterns for recall commands
_RECALL_RE = re.compile(r"^!recall\s+(.+)$", re.IGNORECASE)
_NEAREST_RE = re.compile(r"^!nearest\s+(.+)$", re.IGNORECASE)
_MESSAGES_RE = re.compile(r"^!messages\s+(\S+)\s+(\S+)$", re.IGNORECASE)


def receive_message(
    content: str,
    user_id: str,
    *,
    attachment_urls: list[str] | None = None,
) -> None:
    """Callback for discord_gateway — enqueues incoming DMs for processing."""
    entry: dict[str, Any] = {
        "content": content,
        "user_id": user_id,
        "attachment_urls": attachment_urls or [],
    }
    _message_queue.append(entry)
    logger.debug("Enqueued message from user %s: %.60s", user_id, content)


def _parse_recall_command(content: str) -> dict[str, Any] | None:
    """
    Detect and parse a recall command from message content.

    Returns a dict with keys 'command' and relevant timestamp fields,
    or None if no recall command is detected.
    """
    stripped = content.strip()

    m = _RECALL_RE.match(stripped)
    if m:
        return {"command": "recall", "timestamp": m.group(1).strip()}

    m = _NEAREST_RE.match(stripped)
    if m:
        return {"command": "nearest", "timestamp": m.group(1).strip()}

    m = _MESSAGES_RE.match(stripped)
    if m:
        return {"command": "messages", "start": m.group(1).strip(), "end": m.group(2).strip()}

    return None


def _format_message_entry(entry: dict[str, Any] | None) -> str:
    """Format a single recalled message entry for display."""
    if entry is None:
        return "No message found."
    role = entry.get("role", "unknown")
    content = entry.get("content", "")
    ts = entry.get("timestamp", "")
    return f"[{ts}] ({role}): {content}"


def _format_message_list(entries: list[dict[str, Any]]) -> str:
    """Format a list of recalled messages for display."""
    if not entries:
        return "No messages found in that range."
    lines = [_format_message_entry(e) for e in entries]
    return "\n".join(lines)


def _handle_recall_command(user_id: str, parsed: dict[str, Any]) -> str:
    """Execute the appropriate recall function and return a formatted response."""
    command = parsed["command"]

    if command == "recall":
        result = recall_module.recall_message(user_id, parsed["timestamp"])
        return _format_message_entry(result)

    if command == "nearest":
        result = recall_module.recall_nearest_message(user_id, parsed["timestamp"])
        return _format_message_entry(result)

    if command == "messages":
        results = recall_module.recall_messages_in_range(
            user_id, parsed["start"], parsed["end"]
        )
        return _format_message_list(results)

    return "Unknown recall command."


def _build_prompt(user_id: str, content: str) -> str:
    """Build a prompt for the LLM using conversational context and current message."""
    context = conversational_memory.get_context(user_id)
    parts = []
    if context:
        parts.append(f"Conversation history:\n{context}")
    parts.append(f"User: {content}")
    return "\n\n".join(parts)


async def process_one(repo_path: str, registry: CapabilityRegistry) -> bool:
    """Dequeue and process a single message."""
    async with _queue_lock:
        if not _message_queue:
            return False
        entry = _message_queue.popleft()

    user_id: str = entry["user_id"]
    content: str = entry["content"]

    logger.info("Processing message from user %s", user_id)

    # Check for recall command
    parsed = _parse_recall_command(content)
    if parsed is not None:
        response_text = _handle_recall_command(user_id, parsed)
        logger.info("Recall command '%s' handled for user %s", parsed["command"], user_id)
        conversational_memory.store_message(user_id, content, role="user")
        conversational_memory.store_message(user_id, response_text, role="assistant")
        logger.debug("Recall response for %s: %s", user_id, response_text)
        return True

    # Standard conversational flow
    conversational_memory.store_message(user_id, content, role="user")
    prompt = _build_prompt(user_id, content)

    try:
        response = call_model(prompt)
        reply = response.text
    except BudgetExceededError:
        reply = "I'm sorry, I've reached my usage limit for now. Please try again later."
        logger.warning("Budget exceeded while processing message from user %s", user_id)
    except Exception as exc:
        reply = "I encountered an error processing your message."
        logger.exception("Error calling model for user %s: %s", user_id, exc)

    conversational_memory.store_message(user_id, reply, role="assistant")
    logger.debug("LLM reply for %s: %.120s", user_id, reply)
    return True


async def process_pending(repo_path: str, registry: CapabilityRegistry) -> int:
    """Drain the message queue and process each message."""
    processed = 0
    while await process_one(repo_path, registry):
        processed += 1
    logger.info("Processed %d message(s) from queue.", processed)
    return processed