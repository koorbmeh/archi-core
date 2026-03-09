"""
Discord Listener capability module.

Establishes a persistent Discord WebSocket listener using discord_gateway to receive
DMs from Jesse and processes them to trigger the generation loop or execute direct
instructions. Registers itself with the capability registry and integrates as a
periodic task in the event loop for persistent operation.
"""

import asyncio
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Optional

from capabilities import discord_gateway
from capabilities import event_loop as event_loop_module
from capabilities.discord_gateway import ArchiBotClient
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import call_model, BudgetExceededError
from capabilities import generation_loop

logger = logging.getLogger(__name__)

_JESSE_USER_ID: int = int(os.environ.get("JESSE_DISCORD_USER_ID", "0"))
_REPO_PATH: str = os.environ.get("REPO_PATH", ".")
_message_queue: queue.Queue = queue.Queue()

SYSTEM_PROMPT = (
    "You are Archi's message interpreter. Analyze the incoming Discord DM from Jesse "
    "and classify it as one of:\n"
    "  - TRIGGER_GENERATION: Jesse wants Archi to run the generation loop\n"
    "  - DIRECT_INSTRUCTION: Jesse is giving a specific instruction (extract the instruction)\n"
    "  - INFORMATIONAL: Jesse is sharing information, no action needed\n\n"
    "Respond with a JSON object: {\"intent\": \"<INTENT>\", \"instruction\": \"<text or empty>\"}"
)


class DiscordListener:
    """Listens for Discord DMs from Jesse and dispatches appropriate actions."""

    def __init__(
        self,
        target_user_id: int = _JESSE_USER_ID,
        repo_path: str = _REPO_PATH,
        registry: Optional[CapabilityRegistry] = None,
    ) -> None:
        self.target_user_id = target_user_id
        self.repo_path = repo_path
        self.registry = registry or CapabilityRegistry()
        self._client: Optional[ArchiBotClient] = None
        self._running = False

    def _enqueue_message(self, message_content: str) -> None:
        """Callback passed to ArchiBotClient to queue incoming messages."""
        logger.info("Discord DM received, enqueuing for processing.")
        _message_queue.put(message_content)

    def start(self) -> None:
        """Initialize the ArchiBotClient and start the gateway listener."""
        if self._running:
            logger.warning("DiscordListener is already running.")
            return

        self._client = ArchiBotClient(
            target_user_id=self.target_user_id,
            receive_fn=self._enqueue_message,
        )
        self._running = True
        discord_gateway.start_gateway()
        logger.info("DiscordListener started with target_user_id=%s", self.target_user_id)

    async def stop(self) -> None:
        """Gracefully shut down the listener."""
        self._running = False
        await discord_gateway.stop_gateway()
        logger.info("DiscordListener stopped.")

    async def process_pending_messages(self) -> None:
        """Process all messages currently in the queue."""
        while not _message_queue.empty():
            try:
                content = _message_queue.get_nowait()
            except queue.Empty:
                break
            await _handle_message(content, self.repo_path, self.registry)


async def _handle_message(
    content: str,
    repo_path: str,
    registry: CapabilityRegistry,
) -> None:
    """Interpret a message and dispatch the appropriate action."""
    logger.info("Processing message: %.80s", content)
    try:
        response = call_model(
            prompt=f"Message from Jesse:\n{content}",
            system=SYSTEM_PROMPT,
        )
        _dispatch_intent(response.text, repo_path, registry)
    except BudgetExceededError:
        logger.warning("Budget exceeded while interpreting Discord message; skipping.")
    except Exception as exc:
        logger.error("Error handling Discord message: %s", exc)


def _dispatch_intent(
    model_text: str,
    repo_path: str,
    registry: CapabilityRegistry,
) -> None:
    """Parse model response and dispatch to generation loop or log instruction."""
    import json

    try:
        data = json.loads(model_text)
    except json.JSONDecodeError:
        logger.warning("Could not parse model intent JSON; raw response: %.120s", model_text)
        return

    intent = data.get("intent", "").upper()
    instruction = data.get("instruction", "")

    if intent == "TRIGGER_GENERATION":
        logger.info("Intent=TRIGGER_GENERATION: running generation loop cycle.")
        _run_generation_cycle(repo_path, registry)
    elif intent == "DIRECT_INSTRUCTION":
        logger.info("Intent=DIRECT_INSTRUCTION: %s", instruction)
        _execute_direct_instruction(instruction, repo_path, registry)
    else:
        logger.info("Intent=INFORMATIONAL (no action): %s", instruction)


def _run_generation_cycle(repo_path: str, registry: CapabilityRegistry) -> None:
    """Synchronously trigger one generation loop cycle in a thread-safe manner."""
    try:
        result = generation_loop.run_cycle(repo_path=repo_path, registry=registry)
        logger.info("Generation cycle completed: phase=%s", result.phase_reached)
    except Exception as exc:
        logger.error("Generation cycle error: %s", exc)


def _execute_direct_instruction(
    instruction: str,
    repo_path: str,
    registry: CapabilityRegistry,
) -> None:
    """Handle a direct instruction from Jesse via the model."""
    logger.info("Executing direct instruction: %.120s", instruction)
    try:
        response = call_model(
            prompt=(
                f"Jesse has issued this direct instruction to Archi:\n{instruction}\n\n"
                "Determine if this requires a generation cycle (respond YES or NO) "
                "and explain briefly."
            ),
            system="You are Archi's instruction executor. Be concise.",
        )
        if "YES" in response.text.upper():
            _run_generation_cycle(repo_path, registry)
    except BudgetExceededError:
        logger.warning("Budget exceeded while executing direct instruction.")
    except Exception as exc:
        logger.error("Error executing direct instruction: %s", exc)


_listener_instance: Optional[DiscordListener] = None


def _get_listener() -> DiscordListener:
    """Return the module-level DiscordListener singleton."""
    global _listener_instance
    if _listener_instance is None:
        _listener_instance = DiscordListener()
    return _listener_instance


def register_capability(registry: Optional[CapabilityRegistry] = None) -> Capability:
    """Register the discord_listener capability with the capability registry."""
    reg = registry or CapabilityRegistry()
    cap = Capability(
        name="discord_listener",
        module="capabilities.discord_listener",
        description=(
            "Persistent Discord WebSocket listener that receives DMs from Jesse "
            "and triggers generation loop cycles or executes direct instructions."
        ),
        status="active",
        dependencies=["discord_gateway", "event_loop", "model_interface", "generation_loop"],
    )
    reg.register(cap)
    logger.info("discord_listener capability registered.")
    return cap


def initialize(registry: Optional[CapabilityRegistry] = None) -> bool:
    """Initialize and start the DiscordListener, register capability, and attach to event loop."""
    reg = registry or CapabilityRegistry()

    register_capability(reg)

    listener = _get_listener()
    listener.registry = reg

    if not discord_gateway.initialize():
        logger.error("discord_gateway failed to initialize; DiscordListener not started.")
        return False

    listener.start()

    loop = event_loop_module.create_event_loop()

    async def poll_messages() -> None:
        await listener.process_pending_messages()

    task = event_loop_module.PeriodicTask(
        name="discord_listener_poll",
        coro_factory=poll_messages,
        interval=5.0,
    )
    loop.register_task(task)
    logger.info("discord_listener periodic poll task registered with event loop.")
    return True