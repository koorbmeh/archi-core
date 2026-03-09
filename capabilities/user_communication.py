"""
Module for robust user communication handling.

Provides the UserCommunication class with methods for sending messages,
receiving input, managing conversation state, output formatting, and
input parsing. Integrates with model_interface to generate responses
based on conversation history. Supports both file-based and in-memory
state persistence.
"""

import json
import logging
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from capabilities.model_interface import ModelInterface

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path("conversation_state.json")
MAX_HISTORY_LENGTH = 100
LINE_WIDTH = 80


class ConversationState:
    """Holds conversation history and metadata."""

    def __init__(self) -> None:
        self.history: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {"created_at": datetime.utcnow().isoformat()}

    def add_message(self, role: str, content: str) -> None:
        self.history.append(
            {"role": role, "content": content, "timestamp": datetime.utcnow().isoformat()}
        )
        if len(self.history) > MAX_HISTORY_LENGTH:
            self.history = self.history[-MAX_HISTORY_LENGTH:]

    def clear(self) -> None:
        self.history = []
        self.metadata["cleared_at"] = datetime.utcnow().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {"history": self.history, "metadata": self.metadata}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationState":
        state = cls()
        state.history = data.get("history", [])
        state.metadata = data.get("metadata", {})
        return state


class UserCommunication:
    """
    Handles user communication: output formatting, input parsing,
    state persistence, and model-integrated response generation.
    """

    def __init__(
        self,
        model_interface: Optional[ModelInterface] = None,
        state_file: Optional[Path] = None,
        use_file_persistence: bool = False,
    ) -> None:
        self.model_interface = model_interface or ModelInterface()
        self.state_file = state_file or DEFAULT_STATE_FILE
        self.use_file_persistence = use_file_persistence
        self.state = ConversationState()
        if use_file_persistence:
            self._load_state()

    # ------------------------------------------------------------------ #
    # Output formatting
    # ------------------------------------------------------------------ #

    def format_message(self, role: str, content: str) -> str:
        """Format a single message for display."""
        prefix = f"[{role.upper()}]"
        wrapped = textwrap.fill(content, width=LINE_WIDTH - len(prefix) - 1)
        indented = textwrap.indent(wrapped, " " * (len(prefix) + 1))
        first_line, *rest = indented.splitlines()
        formatted_first = f"{prefix} {first_line.lstrip()}"
        return "\n".join([formatted_first] + rest)

    def format_history(self) -> str:
        """Return a human-readable representation of the full conversation."""
        if not self.state.history:
            return "(no conversation history)"
        lines = [self.format_message(m["role"], m["content"]) for m in self.state.history]
        return "\n\n".join(lines)

    def send_message(self, content: str, role: str = "assistant") -> str:
        """Record an outgoing message and return its formatted form."""
        self.state.add_message(role, content)
        if self.use_file_persistence:
            self._save_state()
        formatted = self.format_message(role, content)
        logger.debug("Sent message: %s", formatted)
        return formatted

    # ------------------------------------------------------------------ #
    # Input parsing
    # ------------------------------------------------------------------ #

    def parse_input(self, raw_input: str) -> dict[str, Any]:
        """
        Parse raw user input into a structured dict.

        Returns a dict with keys: 'text', 'command', 'args'.
        Commands are prefixed with '/'.
        """
        stripped = raw_input.strip()
        if stripped.startswith("/"):
            parts = stripped[1:].split(maxsplit=1)
            command = parts[0].lower() if parts else ""
            args = parts[1] if len(parts) > 1 else ""
            return {"text": stripped, "command": command, "args": args}
        return {"text": stripped, "command": None, "args": ""}

    def receive_input(self, raw_input: str) -> dict[str, Any]:
        """
        Record a user message and return the parsed input structure.
        Handles built-in commands (/clear, /history).
        """
        parsed = self.parse_input(raw_input)
        if parsed["command"] == "clear":
            self.state.clear()
            if self.use_file_persistence:
                self._save_state()
            logger.info("Conversation history cleared by user command.")
            return {**parsed, "system_response": "Conversation history cleared."}
        if parsed["command"] == "history":
            return {**parsed, "system_response": self.format_history()}
        self.state.add_message("user", parsed["text"])
        if self.use_file_persistence:
            self._save_state()
        logger.debug("Received user input: %s", parsed["text"])
        return parsed

    # ------------------------------------------------------------------ #
    # Model-integrated response generation
    # ------------------------------------------------------------------ #

    def generate_response(self, user_input: str) -> str:
        """
        Record user input, generate a model response, record it, and
        return the formatted assistant message.
        """
        parsed = self.receive_input(user_input)
        if "system_response" in parsed:
            return parsed["system_response"]
        history_for_model = [
            {"role": m["role"], "content": m["content"]} for m in self.state.history
        ]
        response_text = self.model_interface.generate(history_for_model)
        return self.send_message(response_text, role="assistant")

    def chat(self, user_input: str) -> str:
        """
        High-level entry point: accept user text, return assistant reply string.
        Alias for generate_response.
        """
        return self.generate_response(user_input)

    # ------------------------------------------------------------------ #
    # State persistence
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        """Persist conversation state to a JSON file."""
        try:
            self.state_file.write_text(
                json.dumps(self.state.to_dict(), indent=2), encoding="utf-8"
            )
            logger.debug("Conversation state saved to %s", self.state_file)
        except OSError as exc:
            logger.error("Failed to save conversation state: %s", exc)

    def _load_state(self) -> None:
        """Load conversation state from a JSON file if it exists."""
        if not self.state_file.exists():
            logger.debug("No existing state file at %s; starting fresh.", self.state_file)
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.state = ConversationState.from_dict(data)
            logger.debug(
                "Loaded %d messages from %s", len(self.state.history), self.state_file
            )
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load conversation state: %s", exc)

    def export_state(self) -> dict[str, Any]:
        """Return a serialisable snapshot of the current conversation state."""
        return self.state.to_dict()

    def import_state(self, data: dict[str, Any]) -> None:
        """Replace the current conversation state from a serialised snapshot."""
        self.state = ConversationState.from_dict(data)
        if self.use_file_persistence:
            self._save_state()

    def reset(self) -> None:
        """Clear conversation history and optionally remove the state file."""
        self.state.clear()
        if self.use_file_persistence and self.state_file.exists():
            try:
                self.state_file.unlink()
                logger.info("State file %s removed.", self.state_file)
            except OSError as exc:
                logger.error("Could not remove state file: %s", exc)


# --------------------------------------------------------------------------- #
# Smoke-test / integration simulation
# --------------------------------------------------------------------------- #

def simulate_conversation(inputs: list[str], use_file: bool = False) -> list[str]:
    """
    Simulate a multi-turn conversation and return assistant responses.

    Useful for integration testing without external infrastructure.
    """
    comm = UserCommunication(use_file_persistence=use_file)
    responses: list[str] = []
    for user_text in inputs:
        reply = comm.chat(user_text)
        responses.append(reply)
        logger.info("User: %s\nAssistant: %s", user_text, reply)
    return responses