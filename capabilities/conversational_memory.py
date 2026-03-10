"""
Persistent per-user conversation history storage and retrieval.

Stores conversation histories in a JSON file keyed by user_id, with methods
to append messages, retrieve recent context, and generate truncated context
for LLM prompts. Thread-safe via threading.Lock for use in async environments.

Integration hooks:
  - store_message(user_id, content, role): callable from discord_listener pipeline
  - get_context(user_id): returns formatted context string for response generation
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_HISTORY_PATH = Path("data") / "conversation_history.json"
_DEFAULT_MAX_MESSAGES = 50
_DEFAULT_CONTEXT_MESSAGES = 10
_DEFAULT_MAX_CONTEXT_CHARS = 4000

_memory_instance: Optional["ConversationalMemory"] = None
_instance_lock = threading.Lock()


class ConversationalMemory:
    """Manages persistent per-user conversation histories with thread-safe access."""

    def __init__(
        self,
        history_path: Path | None = None,
        max_messages_per_user: int = _DEFAULT_MAX_MESSAGES,
        context_window: int = _DEFAULT_CONTEXT_MESSAGES,
        max_context_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        self._path = history_path or _DEFAULT_HISTORY_PATH
        self._max_messages = max_messages_per_user
        self._context_window = context_window
        self._max_context_chars = max_context_chars
        self._lock = threading.Lock()
        self._histories: dict[str, list[dict]] = {}
        self._ensure_data_dir()
        self._load()

    def _ensure_data_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        if not self._path.exists():
            self._histories = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                self._histories = json.load(fh)
            logger.debug("Loaded conversation history from %s", self._path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load conversation history: %s", exc)
            self._histories = {}

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._histories, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("Failed to save conversation history: %s", exc)

    def store_message(self, user_id: str, content: str, role: str = "user") -> None:
        """Append a message to the user's history and persist to disk."""
        if role not in ("user", "assistant", "system"):
            logger.warning("Unknown role '%s'; defaulting to 'user'", role)
            role = "user"
        entry = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
        }
        with self._lock:
            history = self._histories.setdefault(user_id, [])
            history.append(entry)
            if len(history) > self._max_messages:
                self._histories[user_id] = history[-self._max_messages :]
            self._save()

    def get_recent_messages(self, user_id: str, n: int | None = None) -> list[dict]:
        """Return the most recent N messages for the user (thread-safe copy)."""
        count = n if n is not None else self._context_window
        with self._lock:
            history = self._histories.get(user_id, [])
            return list(history[-count:])

    def get_context(self, user_id: str) -> str:
        """Return a formatted, truncated context string for LLM prompt injection."""
        messages = self.get_recent_messages(user_id)
        if not messages:
            return ""
        lines: list[str] = []
        for msg in messages:
            role_label = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            lines.append(f"{role_label}: {content}")
        full_context = "\n".join(lines)
        if len(full_context) <= self._max_context_chars:
            return full_context
        truncated = full_context[-self._max_context_chars :]
        newline_pos = truncated.find("\n")
        if newline_pos != -1:
            truncated = truncated[newline_pos + 1 :]
        return "[...earlier context omitted...]\n" + truncated

    def clear_history(self, user_id: str) -> None:
        """Remove all stored messages for a user."""
        with self._lock:
            if user_id in self._histories:
                del self._histories[user_id]
                self._save()

    def get_history_stats(self, user_id: str) -> dict:
        """Return metadata about a user's stored history."""
        with self._lock:
            history = self._histories.get(user_id, [])
            if not history:
                return {"user_id": user_id, "message_count": 0, "oldest_ts": None, "newest_ts": None}
            timestamps = [m.get("timestamp", 0.0) for m in history]
            return {
                "user_id": user_id,
                "message_count": len(history),
                "oldest_ts": min(timestamps),
                "newest_ts": max(timestamps),
            }

    def list_users(self) -> list[str]:
        """Return the list of user_ids with stored histories."""
        with self._lock:
            return list(self._histories.keys())


def _get_instance() -> ConversationalMemory:
    """Return or create the module-level singleton ConversationalMemory."""
    global _memory_instance
    with _instance_lock:
        if _memory_instance is None:
            history_path_env = os.environ.get("ARCHI_COMMS_FALLBACK_PATH")
            if history_path_env:
                base = Path(history_path_env).parent / "conversation_history.json"
            else:
                base = _DEFAULT_HISTORY_PATH
            _memory_instance = ConversationalMemory(history_path=base)
    return _memory_instance


def store_message(user_id: str, content: str, role: str = "user") -> None:
    """Module-level integration hook: store a message in conversation history."""
    _get_instance().store_message(user_id, content, role)


def get_context(user_id: str) -> str:
    """Module-level integration hook: retrieve formatted context for a user."""
    return _get_instance().get_context(user_id)


def get_recent_messages(user_id: str, n: int | None = None) -> list[dict]:
    """Module-level helper: retrieve recent raw messages for a user."""
    return _get_instance().get_recent_messages(user_id, n)


def clear_history(user_id: str) -> None:
    """Module-level helper: clear all history for a user."""
    _get_instance().clear_history(user_id)


def get_history_stats(user_id: str) -> dict:
    """Module-level helper: return stats about a user's history."""
    return _get_instance().get_history_stats(user_id)


def initialize(
    history_path: Path | None = None,
    max_messages_per_user: int = _DEFAULT_MAX_MESSAGES,
    context_window: int = _DEFAULT_CONTEXT_MESSAGES,
    max_context_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
) -> ConversationalMemory:
    """
    Explicitly initialise (or reinitialise) the module-level singleton.

    Useful for tests or when a custom storage path is required before the
    first call to store_message / get_context.
    """
    global _memory_instance
    with _instance_lock:
        _memory_instance = ConversationalMemory(
            history_path=history_path,
            max_messages_per_user=max_messages_per_user,
            context_window=context_window,
            max_context_chars=max_context_chars,
        )
    return _memory_instance