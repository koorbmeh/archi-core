"""
Discord listener capability with integrated message_recall_by_timestamp support.

Extends the Discord listener's message processing pipeline to detect recall-by-timestamp
requests from users and respond with the recalled message content or a helpful error.
"""

import asyncio
import logging
import re
from collections import deque
from typing import Optional

from src.kernel.capability_registry import CapabilityRegistry
from capabilities.message_recall_by_timestamp import recall_by_timestamp_str
from capabilities.discord_notifier import notify_async

logger = logging.getLogger(__name__)

# Module-level message queue shared between receive_message and process_one
_message_queue: deque = deque()

# Keyword patterns that indicate a timestamp-based recall intent
_RECALL_PATTERNS = [
    re.compile(r"\brecall\b.*\b(at|around|on|from)\b", re.IGNORECASE),
    re.compile(r"\bwhat did (i|you) say\b.*\b(at|around|on)\b", re.IGNORECASE),
    re.compile(r"\bshow me.*message.*\b(at|around|on|from)\b", re.IGNORECASE),
    re.compile(r"\bfind.*message.*\b(at|around|on|from)\b", re.IGNORECASE),
    re.compile(r"\bget.*message.*\b(at|around|on|from)\b", re.IGNORECASE),
    re.compile(r"\bmessage.*\b(at|around|on)\b.*\d", re.IGNORECASE),
    re.compile(r"\brecall message\b", re.IGNORECASE),
]

# Pattern to extract a timestamp string from the message
_TIMESTAMP_EXTRACTION_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[Z+-]\S*)?|"
    r"\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?|"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?|"
    r"(?:yesterday|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+at\s+\d{1,2}:\d{2}(?:\s*[AP]M)?)?)",
    re.IGNORECASE,
)


def receive_message(
    content: str,
    user_id: str,
    *,
    attachment_urls: Optional[list] = None,
) -> None:
    """Callback for discord_gateway — enqueues incoming DMs for processing."""
    _message_queue.append(
        {
            "content": content,
            "user_id": user_id,
            "attachment_urls": attachment_urls or [],
        }
    )
    logger.debug("Enqueued message from user %s (queue depth=%d)", user_id, len(_message_queue))


def _is_recall_intent(content: str) -> bool:
    """Return True if the message content appears to be a recall-by-timestamp request."""
    return any(pattern.search(content) for pattern in _RECALL_PATTERNS)


def _extract_timestamp_str(content: str) -> Optional[str]:
    """Extract the first timestamp-like substring from the message content."""
    match = _TIMESTAMP_EXTRACTION_PATTERN.search(content)
    return match.group(0).strip() if match else None


def _format_recalled_message(result: dict) -> str:
    """Format a recalled message dict into a human-readable reply string."""
    role = result.get("role", "unknown")
    content = result.get("content", "")
    ts = result.get("timestamp", "")
    return f"[{role} @ {ts}]: {content}"


async def _handle_recall_request(user_id: str, content: str) -> bool:
    """
    Detect a recall intent, extract the timestamp, invoke recall_by_timestamp_str,
    and send the result back to the user.  Returns True if handled.
    """
    if not _is_recall_intent(content):
        return False

    timestamp_str = _extract_timestamp_str(content)
    if not timestamp_str:
        await notify_async(
            f"I noticed you want to recall a message, but I couldn't find a recognisable "
            f"timestamp in your request. Please include a timestamp such as "
            f"'2024-01-15 14:30' or '3:45 PM'."
        )
        return True

    logger.info("Recall intent detected for user=%s timestamp='%s'", user_id, timestamp_str)

    try:
        result = recall_by_timestamp_str(user_id, timestamp_str)
    except Exception as exc:
        logger.exception("recall_by_timestamp_str raised for user=%s: %s", user_id, exc)
        await notify_async(
            f"An error occurred while recalling your message at '{timestamp_str}'. "
            f"Please try again."
        )
        return True

    if result is None:
        await notify_async(
            f"No message found near the timestamp '{timestamp_str}'. "
            f"Make sure the timestamp matches something in your history."
        )
    else:
        formatted = _format_recalled_message(result)
        await notify_async(f"Recalled message:\n{formatted}")

    return True


async def process_one(repo_path: str, registry: CapabilityRegistry) -> bool:
    """Dequeue and process a single message."""
    if not _message_queue:
        return False

    msg = _message_queue.popleft()
    content: str = msg["content"]
    user_id: str = msg["user_id"]

    logger.debug("Processing message from user=%s", user_id)

    handled = await _handle_recall_request(user_id, content)
    if handled:
        return True

    # Fallback: no special handler matched; log and acknowledge
    logger.info("No recall intent in message from user=%s; no further handler.", user_id)
    return True


async def process_pending(repo_path: str, registry: CapabilityRegistry) -> int:
    """Drain the message queue and process each message."""
    count = 0
    while _message_queue:
        processed = await process_one(repo_path, registry)
        if processed:
            count += 1
    return count


def _run_simulation(user_id: str, content: str) -> None:
    """Simulate a Discord recall message for integration testing."""
    receive_message(content, user_id)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            process_one(repo_path=".", registry=CapabilityRegistry())
        )
    finally:
        loop.close()