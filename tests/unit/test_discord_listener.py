"""Tests for capabilities/discord_listener.py — conversation quality."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.kernel.model_interface import ModelResponse


# --- Helpers ---

def _make_response(text: str, error=None) -> ModelResponse:
    return ModelResponse(
        text=text, tokens_in=100, tokens_out=50,
        cost_estimate=0.001, model="test", provider="test",
        error=error,
    )


def _run_async(coro):
    """Run an async coroutine synchronously for testing."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- Fix 1: No web search in conversation system prompt ---

class TestConversationSystemPrompt:
    """The conversation handler's system prompt must forbid web search."""

    def test_system_prompt_forbids_web_search(self):
        from capabilities.discord_listener import _CONVERSATION_SYSTEM
        lower = _CONVERSATION_SYSTEM.lower()
        assert "never search the web" in lower

    def test_system_prompt_forbids_fabrication(self):
        from capabilities.discord_listener import _CONVERSATION_SYSTEM
        lower = _CONVERSATION_SYSTEM.lower()
        assert "do not guess" in lower or "fabricate" in lower

    def test_system_prompt_forbids_build_prefix(self):
        from capabilities.discord_listener import _CONVERSATION_SYSTEM
        assert "[build]" in _CONVERSATION_SYSTEM


# --- Fix 2: Profile injection ---

class TestProfileInjection:
    """The conversation handler should load Jesse's profile into the prompt."""

    def test_load_jesse_profile_returns_json(self, tmp_path):
        from capabilities.discord_listener import _load_jesse_profile
        profile = {"location": "McFarland, WI", "job_details": {"title": "Tax Specialist"}}
        profile_path = tmp_path / "personal_profile.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")

        with patch("capabilities.discord_listener.PROFILE_PATH", profile_path):
            result = _load_jesse_profile()
        assert "McFarland" in result
        assert "Tax Specialist" in result

    def test_load_jesse_profile_missing_file(self, tmp_path):
        from capabilities.discord_listener import _load_jesse_profile
        with patch("capabilities.discord_listener.PROFILE_PATH", tmp_path / "nope.json"):
            result = _load_jesse_profile()
        assert result == ""

    def test_load_jesse_profile_corrupt_file(self, tmp_path):
        from capabilities.discord_listener import _load_jesse_profile
        bad_path = tmp_path / "bad.json"
        bad_path.write_text("not json at all", encoding="utf-8")
        with patch("capabilities.discord_listener.PROFILE_PATH", bad_path):
            result = _load_jesse_profile()
        assert result == ""

    def test_profile_included_in_model_prompt(self, tmp_path):
        """When a profile exists, the model call should include it."""
        from capabilities.discord_listener import _handle_conversation

        profile = {"location": "McFarland, WI", "skills": ["accounting"]}
        profile_path = tmp_path / "personal_profile.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")

        captured_prompt = {}

        def mock_call_model(prompt, system=None):
            captured_prompt["prompt"] = prompt
            captured_prompt["system"] = system
            return _make_response("Hello Jesse!")

        with patch("capabilities.discord_listener.PROFILE_PATH", profile_path), \
             patch("capabilities.discord_listener.store_message"), \
             patch("capabilities.discord_listener.get_context", return_value=""), \
             patch("capabilities.discord_listener.notify_async", new_callable=AsyncMock), \
             patch("src.kernel.model_interface.call_model", mock_call_model):
            _run_async(_handle_conversation("user123", "Hello Archi"))

        assert "McFarland" in captured_prompt["prompt"]
        assert "self-reported" in captured_prompt["prompt"].lower()


# --- Fix 3: Build notification bleed ---

class TestNoBuildNotificationInConversation:
    """Conversation replies must never contain [build] prefixes."""

    def test_build_prefix_stripped_from_reply(self):
        """If the model somehow returns [build]..., the handler must strip it."""
        from capabilities.discord_listener import _handle_conversation

        def mock_call_model(prompt, system=None):
            return _make_response("[build] Built something — description here")

        sent_messages = []

        async def mock_notify(msg):
            sent_messages.append(msg)

        with patch("capabilities.discord_listener.store_message"), \
             patch("capabilities.discord_listener.get_context", return_value=""), \
             patch("capabilities.discord_listener.notify_async", side_effect=mock_notify), \
             patch("capabilities.discord_listener.PROFILE_PATH", Path("/nonexistent")), \
             patch("src.kernel.model_interface.call_model", mock_call_model):
            _run_async(_handle_conversation("user123", "What's up?"))

        assert len(sent_messages) == 1
        assert not sent_messages[0].startswith("[build]"), \
            f"Conversation reply must not start with [build]: {sent_messages[0]!r}"

    def test_normal_reply_passes_through(self):
        """Normal replies without [build] prefix are sent as-is."""
        from capabilities.discord_listener import _handle_conversation

        def mock_call_model(prompt, system=None):
            return _make_response("Good morning Jesse! How can I help?")

        sent_messages = []

        async def mock_notify(msg):
            sent_messages.append(msg)

        with patch("capabilities.discord_listener.store_message"), \
             patch("capabilities.discord_listener.get_context", return_value=""), \
             patch("capabilities.discord_listener.notify_async", side_effect=mock_notify), \
             patch("capabilities.discord_listener.PROFILE_PATH", Path("/nonexistent")), \
             patch("src.kernel.model_interface.call_model", mock_call_model):
            _run_async(_handle_conversation("user123", "Good morning"))

        assert len(sent_messages) == 1
        assert sent_messages[0] == "Good morning Jesse! How can I help?"
