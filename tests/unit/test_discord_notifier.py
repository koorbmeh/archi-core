"""Tests for capabilities/discord_notifier.py — session lifecycle."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest


# --- Helpers ---

def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- Session-per-call safety ---

class TestSessionPerCall:
    """Each send_message / _get_or_create_dm_channel must create a fresh session."""

    def test_send_message_creates_fresh_session(self):
        """send_message should not reuse a cached session across calls."""
        from capabilities.discord_notifier import DiscordNotifier

        notifier = DiscordNotifier(bot_token="fake", target_user_id="123")
        notifier._dm_channel_id = "chan_1"

        sessions_created = []

        class _FakeResponse:
            status = 200
            headers = {}
            async def text(self): return ""
            async def json(self): return {}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _FakeSession:
            def __init__(self, **kw):
                sessions_created.append(self)
            def post(self, *a, **kw):
                return _FakeResponse()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass

        with patch("capabilities.discord_notifier.aiohttp.ClientSession", _FakeSession):
            _run_async(notifier.send_message("hello"))
            _run_async(notifier.send_message("world"))

        assert len(sessions_created) == 2, \
            f"Expected 2 fresh sessions, got {len(sessions_created)}"

    def test_send_message_across_different_loops(self):
        """Calling send_message from separate event loops must not raise."""
        from capabilities.discord_notifier import DiscordNotifier

        notifier = DiscordNotifier(bot_token="fake", target_user_id="123")
        notifier._dm_channel_id = "chan_1"

        class _FakeResponse:
            status = 200
            headers = {}
            async def text(self): return ""
            async def json(self): return {}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _FakeSession:
            def __init__(self, **kw): pass
            def post(self, *a, **kw):
                return _FakeResponse()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("capabilities.discord_notifier.aiohttp.ClientSession", _FakeSession):
            # First call on loop A
            result1 = _run_async(notifier.send_message("msg1"))
            # Second call on loop B (loop A is now closed)
            result2 = _run_async(notifier.send_message("msg2"))

        assert result1 is True
        assert result2 is True

    def test_dm_channel_creates_fresh_session(self):
        """_get_or_create_dm_channel should also use a fresh session."""
        from capabilities.discord_notifier import DiscordNotifier

        notifier = DiscordNotifier(bot_token="fake", target_user_id="123")

        sessions_created = []

        class _FakeResponse:
            status = 200
            headers = {}
            async def text(self): return ""
            async def json(self): return {"id": "chan_99"}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _FakeSession:
            def __init__(self, **kw):
                sessions_created.append(self)
            def post(self, *a, **kw):
                return _FakeResponse()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("capabilities.discord_notifier.aiohttp.ClientSession", _FakeSession):
            _run_async(notifier._get_or_create_dm_channel())

        assert len(sessions_created) == 1


class TestNotifySync:
    """The synchronous notify() wrapper."""

    def test_notify_returns_false_when_not_configured(self):
        from capabilities.discord_notifier import notify
        with patch("capabilities.discord_notifier._notifier_instance", None):
            assert notify("hello") is False

    def test_notify_calls_asyncio_run_outside_loop(self):
        """When no loop is running, notify uses asyncio.run()."""
        from capabilities.discord_notifier import notify

        mock_notifier = MagicMock()
        mock_notifier.send_message = AsyncMock(return_value=True)

        with patch("capabilities.discord_notifier._notifier_instance", mock_notifier):
            result = notify("test message")

        assert result is True
        mock_notifier.send_message.assert_called_once_with("test message")

    def test_notify_schedules_inside_running_loop(self):
        """When called from a running loop, notify uses ensure_future."""
        from capabilities.discord_notifier import notify

        mock_notifier = MagicMock()
        mock_notifier.send_message = AsyncMock(return_value=True)

        async def _inner():
            with patch("capabilities.discord_notifier._notifier_instance", mock_notifier):
                return notify("from async context")

        result = _run_async(_inner())
        assert result is True


class TestCloseIsNoop:
    """close() should be a no-op now that sessions are per-call."""

    def test_close_does_not_raise(self):
        from capabilities.discord_notifier import DiscordNotifier
        notifier = DiscordNotifier(bot_token="fake", target_user_id="123")
        # Should not raise
        _run_async(notifier.close())
