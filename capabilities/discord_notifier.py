"""
Module for sending asynchronous notifications to Jesse via Discord direct messages.

Uses the Discord API with aiohttp to create or retrieve a DM channel and send messages.
Automatically registers itself in the capability_registry upon import with a simple
notify(text) interface.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"

_capability_registry: dict = {}

try:
    from capability_registry import capability_registry as _capability_registry
except ImportError:
    pass


class DiscordNotifier:
    """Async Discord notifier that sends direct messages to a specified user.

    Each API call creates a fresh aiohttp.ClientSession and closes it after use.
    This avoids ``RuntimeError: Event loop is closed`` when the notifier is called
    from different event-loop contexts (e.g. ``asyncio.run()`` in the sync wrapper
    vs. a long-running listener loop).
    """

    def __init__(self, bot_token: str, target_user_id: str) -> None:
        self.bot_token = bot_token
        self.target_user_id = target_user_id
        self._dm_channel_id: Optional[str] = None
        self._rate_limit_reset: float = 0.0

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
        }

    async def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        if self._rate_limit_reset > now:
            wait_time = self._rate_limit_reset - now
            logger.warning("Rate limited by Discord. Waiting %.2f seconds.", wait_time)
            await asyncio.sleep(wait_time)

    async def _handle_rate_limit_headers(self, response: aiohttp.ClientResponse) -> None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            self._rate_limit_reset = time.monotonic() + float(retry_after)

    async def _get_or_create_dm_channel(self) -> Optional[str]:
        if self._dm_channel_id:
            return self._dm_channel_id

        url = f"{DISCORD_API_BASE}/users/@me/channels"
        payload = {"recipient_id": self.target_user_id}

        await self._wait_for_rate_limit()
        try:
            async with aiohttp.ClientSession(headers=self._get_headers()) as session:
                async with session.post(url, json=payload) as response:
                    await self._handle_rate_limit_headers(response)
                    if response.status == 429:
                        logger.error("Rate limited when creating DM channel.")
                        return None
                    if response.status not in (200, 201):
                        text = await response.text()
                        logger.error(
                            "Failed to create DM channel. Status: %d, Response: %s",
                            response.status,
                            text,
                        )
                        return None
                    data = await response.json()
                    self._dm_channel_id = data.get("id")
                    logger.info("DM channel established: %s", self._dm_channel_id)
                    return self._dm_channel_id
        except aiohttp.ClientError as exc:
            logger.exception("Network error while creating DM channel: %s", exc)
            return None

    async def send_message(self, text: str) -> bool:
        """Send a direct message to the target Discord user.

        Args:
            text: The message text to send.

        Returns:
            True if the message was sent successfully, False otherwise.
        """
        if not text or not text.strip():
            logger.warning("Attempted to send an empty message. Skipping.")
            return False

        channel_id = await self._get_or_create_dm_channel()
        if not channel_id:
            logger.error("No DM channel available. Cannot send message.")
            return False

        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        payload = {"content": text}

        await self._wait_for_rate_limit()
        try:
            async with aiohttp.ClientSession(headers=self._get_headers()) as session:
                async with session.post(url, json=payload) as response:
                    await self._handle_rate_limit_headers(response)
                    if response.status == 429:
                        logger.warning("Rate limited when sending message. Will retry on next call.")
                        return False
                    if response.status not in (200, 201):
                        body = await response.text()
                        logger.error(
                            "Failed to send Discord message. Status: %d, Body: %s",
                            response.status,
                            body,
                        )
                        return False
                    logger.info("Discord message sent successfully to channel %s.", channel_id)
                    return True
        except aiohttp.ClientError as exc:
            logger.exception("Network error while sending Discord message: %s", exc)
            return False

    async def close(self) -> None:
        """No-op — sessions are now created and closed per-call."""
        pass


def _build_notifier() -> Optional[DiscordNotifier]:
    """Build a DiscordNotifier instance from environment variables."""
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    target_user_id = os.environ.get("JESSE_DISCORD_ID", "").strip()

    if not bot_token:
        logger.warning("DISCORD_BOT_TOKEN is not set. Discord notifications disabled.")
        return None
    if not target_user_id:
        logger.warning("JESSE_DISCORD_ID is not set. Discord notifications disabled.")
        return None

    return DiscordNotifier(bot_token=bot_token, target_user_id=target_user_id)


_notifier_instance: Optional[DiscordNotifier] = _build_notifier()


def notify(text: str) -> bool:
    """Send a Discord notification synchronously using the module-level notifier.

    This function provides a simple synchronous interface suitable for use
    as a registered capability.  It always uses ``asyncio.run()`` to create a
    fresh event loop, which pairs with the per-call session strategy in
    ``DiscordNotifier`` to avoid stale-loop errors.

    When called from an already-running loop (e.g. inside the Discord listener),
    the coroutine is scheduled with ``ensure_future`` and returns True
    optimistically — callers in that context should prefer ``notify_async``.

    Args:
        text: The message text to send.

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    if _notifier_instance is None:
        logger.error("DiscordNotifier is not configured. Cannot send notification.")
        return False

    # Check if there's already a running event loop (e.g. inside discord_listener).
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # We're inside an async context — schedule and return optimistically.
        asyncio.ensure_future(_notifier_instance.send_message(text))
        return True

    # No running loop — create a fresh one via asyncio.run().
    return asyncio.run(_notifier_instance.send_message(text))


async def notify_async(text: str) -> bool:
    """Send a Discord notification asynchronously using the module-level notifier.

    Args:
        text: The message text to send.

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    if _notifier_instance is None:
        logger.error("DiscordNotifier is not configured. Cannot send notification.")
        return False
    return await _notifier_instance.send_message(text)


async def shutdown() -> None:
    """Close the underlying aiohttp session. Call on process exit."""
    if _notifier_instance is not None:
        await _notifier_instance.close()


def _register_capability() -> None:
    """Register the discord_notifier capability in the capability registry."""
    if _notifier_instance is None:
        logger.warning("Skipping capability registration: DiscordNotifier not configured.")
        return

    _capability_registry["discord_notifier"] = {
        "name": "discord_notifier",
        "description": "Send a direct message notification to Jesse via Discord.",
        "notify": notify,
        "notify_async": notify_async,
        "instance": _notifier_instance,
    }
    logger.info("discord_notifier capability registered successfully.")


_register_capability()