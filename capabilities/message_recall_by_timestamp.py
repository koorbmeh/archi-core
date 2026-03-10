"""
capabilities/message_recall_by_timestamp.py

Retrieves specific messages from a user's conversation history using
human-readable timestamps like '9:48 PM' by parsing and matching against
stored timestamps.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from capabilities import timestamped_chat_history_recall

logger = logging.getLogger(__name__)

_CAPABILITY_NAME = "message_recall_by_timestamp"
_CAPABILITY_MODULE = "capabilities.message_recall_by_timestamp"
_CAPABILITY_DESCRIPTION = (
    "Retrieves specific messages from a user's conversation history using "
    "human-readable timestamps like '9:48 PM'."
)
_TOLERANCE_SECONDS = 60.0
_TIME_FORMAT = "%I:%M %p"

__all__ = ["recall_by_timestamp_str", "register_capability"]


def _parse_timestamp_str(timestamp_str: str) -> float | None:
    """Parse a human-readable time string into a Unix timestamp anchored to today."""
    try:
        now = datetime.now()
        parsed_time = datetime.strptime(timestamp_str.strip(), _TIME_FORMAT)
        anchored = parsed_time.replace(
            year=now.year,
            month=now.month,
            day=now.day,
        )
        return anchored.timestamp()
    except ValueError:
        logger.warning("Failed to parse timestamp string: %r", timestamp_str)
        return None


def _fuzzy_fallback(
    user_id: str,
    timestamp_str: str,
    n: int = 50,
) -> dict[str, Any] | None:
    """Search recent history for a message whose stored timestamp fuzzy-matches the input."""
    from capabilities.conversational_memory import get_recent_messages

    recent = get_recent_messages(user_id, n=n)
    normalized_input = timestamp_str.strip().lower()

    for msg in reversed(recent):
        raw_ts = msg.get("timestamp")
        if raw_ts is None:
            continue
        try:
            ts_float = float(raw_ts)
            dt = datetime.fromtimestamp(ts_float)
            formatted = dt.strftime(_TIME_FORMAT).lower()
            if formatted == normalized_input:
                return msg
        except (ValueError, TypeError, OSError):
            continue

    return None


def recall_by_timestamp_str(
    user_id: str,
    timestamp_str: str,
) -> dict[str, Any] | None:
    """
    Retrieve a message from a user's conversation history by a human-readable
    timestamp string (e.g. '9:48 PM').

    Parses the timestamp, anchors it to today, and calls
    timestamped_chat_history_recall.recall_nearest_message with a 60-second
    tolerance.  Falls back to fuzzy matching against recent history if no
    result is found.

    Args:
        user_id: The user whose history is searched.
        timestamp_str: A human-readable time string in '%-I:%M %p' format.

    Returns:
        A message dict if found, otherwise None.
    """
    unix_ts = _parse_timestamp_str(timestamp_str)
    if unix_ts is None:
        logger.error("Cannot recall message: unparseable timestamp %r", timestamp_str)
        return None

    result = timestamped_chat_history_recall.recall_nearest_message(
        user_id, unix_ts, tolerance_seconds=_TOLERANCE_SECONDS
    )

    if result is not None:
        logger.debug("Exact match found for %r (user=%s)", timestamp_str, user_id)
        return result

    logger.debug(
        "No exact match for %r; falling back to fuzzy search (user=%s)",
        timestamp_str,
        user_id,
    )
    return _fuzzy_fallback(user_id, timestamp_str)


def register_capability(registry=None) -> Any | None:
    """
    Register this capability with a CapabilityRegistry if one is provided.

    Args:
        registry: An optional CapabilityRegistry instance.

    Returns:
        The registered Capability object, or None if no registry was supplied.
    """
    if registry is None:
        return None

    from src.kernel.capability_registry import Capability

    cap = Capability(
        name=_CAPABILITY_NAME,
        module=_CAPABILITY_MODULE,
        description=_CAPABILITY_DESCRIPTION,
        status="active",
        dependencies=["timestamped_chat_history_recall"],
    )
    registry.register(cap)
    logger.info("Registered capability: %s", _CAPABILITY_NAME)
    return cap