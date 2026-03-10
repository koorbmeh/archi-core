"""
Discord Listener capability module.

Receives DMs from Jesse via the discord_gateway bot and processes them
to trigger Archi's generation loop or execute direct instructions.

The listener registers a receive callback with discord_gateway so that
incoming DMs are enqueued. A periodic poll drains the queue and dispatches
each message through a lightweight intent classifier (via call_model)
before routing to the appropriate action.
"""

import json
import logging
import os
import queue
from typing import Optional

from capabilities.discord_notifier import notify as discord_notify
from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.generation_loop import CycleResult, run_cycle
from src.kernel.model_interface import call_model, BudgetExceededError

logger = logging.getLogger(__name__)

_JESSE_DISCORD_ID: str = os.environ.get("JESSE_DISCORD_ID", "0")
_message_queue: queue.Queue = queue.Queue()

INTENT_SYSTEM = (
    "You are Archi's message interpreter. Analyze the incoming Discord DM from Jesse "
    "and classify it as one of:\n"
    "  - TRIGGER_GENERATION: Jesse wants Archi to run the generation loop\n"
    "  - DIRECT_INSTRUCTION: Jesse is giving a specific instruction (extract it)\n"
    "  - INFORMATIONAL: Jesse is sharing information, no action needed\n\n"
    'Respond with ONLY a JSON object: {"intent": "<INTENT>", "instruction": "<text or empty>"}'
)


def receive_message(content: str, user_id: str) -> None:
    """Callback for discord_gateway — enqueues incoming DMs for processing.

    This function is passed to discord_gateway as the receive_fn so that
    incoming messages land in our queue instead of just being logged.
    """
    logger.info("Discord DM received from %s, enqueuing.", user_id)
    _message_queue.put({"content": content, "user_id": user_id})


async def process_pending(
    repo_path: str,
    registry: CapabilityRegistry,
) -> int:
    """Drain the message queue and process each message.

    Returns the number of messages processed.
    """
    processed = 0
    while not _message_queue.empty():
        try:
            msg = _message_queue.get_nowait()
        except queue.Empty:
            break
        await _handle_message(msg["content"], repo_path, registry)
        processed += 1
    return processed


async def _handle_message(
    content: str,
    repo_path: str,
    registry: CapabilityRegistry,
) -> None:
    """Classify a message and dispatch the appropriate action."""
    logger.info("Processing message: %.80s", content)
    try:
        response = call_model(
            prompt=f"Message from Jesse:\n{content}",
            system=INTENT_SYSTEM,
        )
    except BudgetExceededError:
        logger.warning("Budget exceeded while classifying message; skipping.")
        discord_notify("Budget limit reached — can't process your message right now.")
        return
    except Exception as exc:
        logger.error("Error classifying message: %s", exc)
        return

    try:
        data = json.loads(response.text.strip())
    except json.JSONDecodeError:
        logger.warning("Could not parse intent JSON: %.120s", response.text)
        discord_notify("I received your message but couldn't parse my own response. Try again?")
        return

    intent = data.get("intent", "").upper()
    instruction = data.get("instruction", "")

    if intent == "TRIGGER_GENERATION":
        logger.info("Intent=TRIGGER_GENERATION: running generation loop.")
        discord_notify("Running generation loop cycle now.")
        result = run_cycle(repo_path=repo_path, registry=registry)
        _notify_cycle_outcome(result)
    elif intent == "DIRECT_INSTRUCTION":
        logger.info("Intent=DIRECT_INSTRUCTION: %s", instruction)
        discord_notify(f"Acknowledged instruction: {instruction[:200]}")
        # Direct instructions trigger a cycle — future: richer dispatch
        result = run_cycle(repo_path=repo_path, registry=registry)
        _notify_cycle_outcome(result)
    else:
        logger.info("Intent=INFORMATIONAL (no action needed).")
        discord_notify("Got it. No action needed on my end.")


def _notify_cycle_outcome(result: CycleResult) -> None:
    """Send a Discord summary of a triggered cycle result."""
    if result.capability_registered and result.gap:
        discord_notify(f"Done — integrated {result.gap.name}.")
    elif result.error:
        discord_notify(f"Cycle failed at {result.phase_reached}: {result.error[:200]}")
    elif result.phase_reached == "observe":
        discord_notify("No gaps detected — nothing to do.")
