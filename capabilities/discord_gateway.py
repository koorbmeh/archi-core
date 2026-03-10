"""
Discord Gateway capability module for Archi.

Establishes a persistent Discord gateway WebSocket connection using discord.py
to receive MESSAGE_CREATE events for DMs from Jesse and routes them to Archi's
user_communication for processing.

Authenticates as a Discord bot using DISCORD_BOT_TOKEN, filters incoming DMs
from the configured target user ID, and dispatches message content via the
user_communication interface. The bot client is spawned as a persistent asyncio
task within the event_loop, with automatic reconnection logic and graceful
shutdown support.
"""

import asyncio
import logging
import os
from typing import Optional

import discord

from src.kernel.capability_registry import Capability, CapabilityRegistry

logger = logging.getLogger(__name__)

_CAPABILITY_NAME = "discord_gateway"
_CAPABILITY_MODULE = "capabilities.discord_gateway"
_bot_task: Optional[asyncio.Task] = None
_bot_client: Optional["ArchiBotClient"] = None


def _get_target_user_id() -> int:
    """Resolve the target Discord user ID from environment."""
    raw = os.environ.get("JESSE_DISCORD_ID", "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("JESSE_DISCORD_ID is not a valid integer: %s", raw)
    logger.warning("JESSE_DISCORD_ID not set; DM filtering will be disabled")
    return 0


def _get_bot_token() -> str:
    """Retrieve the Discord bot token from environment."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set")
    return token


class ArchiBotClient(discord.Client):
    """Discord bot client that listens for DMs from the target user."""

    def __init__(self, target_user_id: int, receive_fn, **kwargs):
        intents = discord.Intents.default()
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)
        self._target_user_id = target_user_id
        self._receive_fn = receive_fn

    async def on_ready(self):
        logger.info("Discord gateway connected as %s (id=%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        if self._target_user_id and message.author.id != self._target_user_id:
            logger.debug(
                "Ignoring DM from non-target user %s", message.author.id
            )
            return
        logger.info(
            "Received DM from %s (id=%s): %s",
            message.author,
            message.author.id,
            message.content[:80],
        )
        try:
            if asyncio.iscoroutinefunction(self._receive_fn):
                await self._receive_fn(message.content, str(message.author.id))
            else:
                self._receive_fn(message.content, str(message.author.id))
        except Exception:
            logger.exception("Error dispatching message to user_communication")

    async def on_error(self, event_method: str, *args, **kwargs):
        logger.exception("Discord client error in event '%s'", event_method)

    async def on_disconnect(self):
        logger.warning("Discord gateway disconnected; discord.py will attempt reconnect")


async def _run_bot_with_reconnect(token: str, target_user_id: int, receive_fn) -> None:
    """Run the Discord bot with automatic reconnection on failure."""
    global _bot_client
    backoff = 5.0
    max_backoff = 300.0

    while True:
        client = ArchiBotClient(target_user_id=target_user_id, receive_fn=receive_fn)
        _bot_client = client
        try:
            logger.info("Starting Discord gateway bot")
            await client.start(token, reconnect=True)
        except discord.LoginFailure:
            logger.error("Discord login failed; check DISCORD_BOT_TOKEN. Aborting gateway.")
            break
        except asyncio.CancelledError:
            logger.info("Discord gateway task cancelled; shutting down")
            await client.close()
            break
        except Exception:
            logger.exception("Discord gateway encountered an error; reconnecting in %.1fs", backoff)
            await client.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        else:
            logger.info("Discord gateway bot exited cleanly")
            break


def _default_receive_fn(content: str, user_id: str) -> None:
    """Default fallback receive function when user_communication is unavailable."""
    logger.info("[discord_gateway] Message from %s: %s", user_id, content[:200])


def _resolve_receive_fn():
    """Attempt to import discord_listener.receive_message; fall back to default."""
    try:
        import importlib
        mod = importlib.import_module("capabilities.discord_listener")
        fn = getattr(mod, "receive_message", None)
        if callable(fn):
            logger.info("Resolved discord_listener.receive_message")
            return fn
        logger.warning("discord_listener.receive_message not found; using default handler")
    except ImportError:
        logger.warning("discord_listener module not available; using default handler")
    return _default_receive_fn


def start_gateway() -> None:
    """
    Spawn the Discord gateway bot as a persistent asyncio Task.

    Should be called after the event loop is running. Safe to call multiple times;
    will not spawn duplicate tasks.
    """
    global _bot_task

    if _bot_task is not None and not _bot_task.done():
        logger.info("Discord gateway is already running")
        return

    try:
        token = _get_bot_token()
    except RuntimeError as exc:
        logger.error("Cannot start Discord gateway: %s", exc)
        return

    target_user_id = _get_target_user_id()
    receive_fn = _resolve_receive_fn()

    loop = asyncio.get_event_loop()
    _bot_task = loop.create_task(
        _run_bot_with_reconnect(token, target_user_id, receive_fn),
        name="discord_gateway_bot",
    )
    logger.info("Discord gateway task created")


async def stop_gateway() -> None:
    """Gracefully shut down the Discord gateway bot and cancel its task."""
    global _bot_task, _bot_client

    if _bot_client is not None:
        try:
            await _bot_client.close()
            logger.info("Discord gateway client closed")
        except Exception:
            logger.exception("Error closing Discord gateway client")
        _bot_client = None

    if _bot_task is not None and not _bot_task.done():
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass
        logger.info("Discord gateway task stopped")
    _bot_task = None


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Capability:
    """Register the discord_gateway capability with the capability registry."""
    if registry is None:
        registry = CapabilityRegistry()

    capability = Capability(
        name=_CAPABILITY_NAME,
        module=_CAPABILITY_MODULE,
        description=(
            "Persistent Discord gateway WebSocket connection that receives MESSAGE_CREATE "
            "events for DMs from Jesse and routes them to user_communication for processing."
        ),
        status="active",
        dependencies=["event_loop", "user_communication"],
        metadata={
            "intents": ["dm_messages", "message_content"],
            "reconnect": True,
        },
    )
    registry.register(capability)
    logger.info("Registered capability: %s", _CAPABILITY_NAME)
    return capability


def initialize() -> bool:
    """
    Initialize the Discord gateway capability.

    Registers the capability and starts the bot gateway task within the
    current running asyncio event loop.
    """
    try:
        register_capability()
    except Exception:
        logger.exception("Failed to register discord_gateway capability")
        return False

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            start_gateway()
        else:
            logger.warning(
                "No running event loop detected; call start_gateway() manually after loop starts"
            )
    except RuntimeError:
        logger.warning("Could not obtain event loop during initialize(); deferring start_gateway()")

    return True