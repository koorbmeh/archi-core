"""
capabilities/timestamped_chat_history_recall.py

Retrieves specific past messages from a user's conversation history by matching
exact timestamps. Supports both ISO 8601 string timestamps and Unix epoch floats,
with optional fuzzy/range-based matching for robustness.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from capabilities.conversational_memory import get_recent_messages

logger = logging.getLogger(__name__)

_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


def _parse_timestamp(ts: str | float) -> float | None:
    """Convert a timestamp string or float to a Unix epoch float."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        for fmt in _ISO_FORMATS:
            try:
                dt = datetime.strptime(ts, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        try:
            return float(ts)
        except ValueError:
            logger.warning("Unable to parse timestamp: %r", ts)
            return None
    return None


def _extract_ts(message: dict[str, Any]) -> float | None:
    """Extract and normalise the timestamp from a message dict."""
    raw = message.get("timestamp") or message.get("ts") or message.get("time")
    if raw is None:
        return None
    return _parse_timestamp(raw)


def recall_message(
    user_id: str,
    timestamp: str | float,
) -> dict[str, Any] | None:
    """
    Search a user's full conversation history for a message whose timestamp
    exactly matches *timestamp*.

    Parameters
    ----------
    user_id:   The user whose history is queried.
    timestamp: The target timestamp as an ISO string or Unix epoch float.

    Returns
    -------
    The matching message dict, or None if not found.
    """
    target = _parse_timestamp(timestamp)
    if target is None:
        logger.error("Invalid timestamp supplied: %r", timestamp)
        return None

    history = get_recent_messages(user_id, n=None)
    for message in history:
        msg_ts = _extract_ts(message)
        if msg_ts is not None and msg_ts == target:
            return message

    logger.debug("No exact match for timestamp %s in history of user %s", timestamp, user_id)
    return None


def recall_messages_in_range(
    user_id: str,
    start: str | float,
    end: str | float,
) -> list[dict[str, Any]]:
    """
    Return all messages whose timestamps fall within [start, end] inclusive.

    Parameters
    ----------
    user_id: The user whose history is queried.
    start:   Range start as ISO string or Unix epoch float.
    end:     Range end as ISO string or Unix epoch float.

    Returns
    -------
    A (possibly empty) list of matching message dicts, ordered as stored.
    """
    t_start = _parse_timestamp(start)
    t_end = _parse_timestamp(end)
    if t_start is None or t_end is None:
        logger.error("Invalid range timestamps: start=%r end=%r", start, end)
        return []

    if t_start > t_end:
        t_start, t_end = t_end, t_start

    history = get_recent_messages(user_id, n=None)
    results = []
    for message in history:
        msg_ts = _extract_ts(message)
        if msg_ts is not None and t_start <= msg_ts <= t_end:
            results.append(message)
    return results


def recall_nearest_message(
    user_id: str,
    timestamp: str | float,
    tolerance_seconds: float = 5.0,
) -> dict[str, Any] | None:
    """
    Return the message whose timestamp is closest to *timestamp*, provided it
    falls within *tolerance_seconds*.  Returns None when no message is within
    the tolerance window.

    Parameters
    ----------
    user_id:            The user whose history is queried.
    timestamp:          Target timestamp as ISO string or Unix epoch float.
    tolerance_seconds:  Maximum allowed deviation in seconds (default 5 s).

    Returns
    -------
    The closest matching message dict, or None.
    """
    target = _parse_timestamp(timestamp)
    if target is None:
        logger.error("Invalid timestamp supplied: %r", timestamp)
        return None

    history = get_recent_messages(user_id, n=None)
    best_msg: dict[str, Any] | None = None
    best_delta = float("inf")

    for message in history:
        msg_ts = _extract_ts(message)
        if msg_ts is None:
            continue
        delta = abs(msg_ts - target)
        if delta < best_delta:
            best_delta = delta
            best_msg = message

    if best_msg is not None and best_delta <= tolerance_seconds:
        return best_msg

    logger.debug(
        "No message within %.1fs of %s for user %s (closest delta=%.3fs)",
        tolerance_seconds, timestamp, user_id, best_delta,
    )
    return None