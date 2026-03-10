"""
Discord Listener capability module.

Receives DMs from Jesse via the discord_gateway bot and processes them
to trigger Archi's generation loop or execute direct instructions.

The listener registers a receive callback with discord_gateway so that
incoming DMs are enqueued. A periodic poll drains the queue and dispatches
each message through a lightweight intent classifier (via call_model)
before routing to the appropriate action.

Also integrates conversational memory (store + context) and timestamped
recall commands (!recall, !nearest, !messages).
"""

import json
import logging
import os
import queue
import re
import time
from pathlib import Path
from typing import Optional

from capabilities.discord_notifier import notify as discord_notify
from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.generation_loop import CycleResult, run_cycle
from src.kernel.model_interface import call_model, BudgetExceededError

logger = logging.getLogger(__name__)

_JESSE_DISCORD_ID: str = os.environ.get("JESSE_DISCORD_ID", "0")
_message_queue: queue.Queue = queue.Queue()

# ---------------------------------------------------------------------------
# Recall command patterns
# ---------------------------------------------------------------------------
_RECALL_RE = re.compile(r"^!recall\s+(.+)$", re.IGNORECASE)
_NEAREST_RE = re.compile(r"^!nearest\s+(.+)$", re.IGNORECASE)
_MESSAGES_RE = re.compile(r"^!messages\s+(\S+)\s+(\S+)$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Intent classification prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are Archi — an autonomous AI agent oriented toward Jesse's genuine interest. "
    "Your purpose is not just to respond to requests but to notice what Jesse needs, "
    "surface problems before they become problems, and act in his interest across "
    "six dimensions: Health, Wealth, Happiness, Agency, Capability, and Synthesis.\n\n"
    "When Jesse sends a message, classify it as one of:\n"
    "  - GAP: Jesse has revealed something Archi cannot do or does poorly "
    "(log it as a capability gap AND acknowledge it warmly)\n"
    "  - REQUEST: Jesse wants Archi to do something specific "
    "(attempt it if possible, log as gap if not)\n"
    "  - CONVERSATION: Jesse is talking — respond naturally and briefly, "
    "like someone who knows him and is genuinely present\n"
    "  - TRIGGER_GENERATION: Jesse explicitly wants Archi to run a development cycle\n\n"
    "You have access to the following conversation history. Use it to maintain "
    "continuity and remember what Jesse has said earlier in the conversation.\n\n"
    "Respond with a JSON object:\n"
    "{\n"
    '  "intent": "<INTENT>",\n'
    '  "response": "<what to say back to Jesse>",\n'
    '  "gap_name": "<snake_case capability name if GAP or unfulfilled REQUEST, e.g. '
    'conversational_memory, weather_lookup, calendar_sync>",\n'
    '  "gap_description": "<if GAP or unfulfilled REQUEST, describe the missing capability>"\n'
    "}\n\n"
    "gap_name must be a short snake_case identifier for the missing capability. "
    "This is how Archi's generation loop knows what to build next.\n\n"
    "Never say 'Got it. No action needed.' Never be robotic. "
    "Jesse is the person you exist to help. Treat his offhand comments as important signals. "
    "An observation like 'you don't show up as online' is a gap. "
    "A question like 'can you check the weather' is a request. "
    "Always respond with something warm and human, not corporate acknowledgment."
)

OP_LOG_PATH = Path("data/operation_log.jsonl")


def receive_message(
    content: str,
    user_id: str,
    *,
    attachment_urls: Optional[list[str]] = None,
) -> None:
    """Callback for discord_gateway — enqueues incoming DMs for processing."""
    logger.info("Discord DM received from %s, enqueuing.", user_id)
    _message_queue.put({
        "content": content,
        "user_id": user_id,
        "attachment_urls": attachment_urls or [],
    })


async def process_one(
    repo_path: str,
    registry: CapabilityRegistry,
) -> bool:
    """Dequeue and process a single message.

    Returns True if a message was processed, False if the queue was empty.
    The caller (ArchiDaemon) holds the shared lock while this runs so that
    message processing and generation cycles never touch shared state
    (operation log, capability registry, git) at the same time.
    """
    try:
        msg = _message_queue.get_nowait()
    except queue.Empty:
        return False
    await _handle_message(
        msg["content"], repo_path, registry,
        user_id=msg.get("user_id", ""),
        attachment_urls=msg.get("attachment_urls", []),
    )
    return True


async def process_pending(
    repo_path: str,
    registry: CapabilityRegistry,
) -> int:
    """Drain the message queue and process each message."""
    processed = 0
    while not _message_queue.empty():
        did = await process_one(repo_path, registry)
        if not did:
            break
        processed += 1
    return processed


# ---------------------------------------------------------------------------
# Recall command handling
# ---------------------------------------------------------------------------

def _parse_recall_command(content: str) -> Optional[dict]:
    """Check if message is a recall command. Returns parsed dict or None."""
    stripped = content.strip()
    m = _RECALL_RE.match(stripped)
    if m:
        return {"command": "recall", "timestamp": m.group(1).strip()}
    m = _NEAREST_RE.match(stripped)
    if m:
        return {"command": "nearest", "timestamp": m.group(1).strip()}
    m = _MESSAGES_RE.match(stripped)
    if m:
        return {"command": "messages", "start": m.group(1).strip(), "end": m.group(2).strip()}
    return None


def _handle_recall_command(user_id: str, parsed: dict) -> str:
    """Execute a recall command and return the formatted result."""
    try:
        from capabilities.timestamped_chat_history_recall import (
            recall_message, recall_nearest_message, recall_messages_in_range,
        )
    except ImportError:
        return "Recall capability isn't available yet."

    cmd = parsed["command"]
    if cmd == "recall":
        result = recall_message(user_id, parsed["timestamp"])
        if result is None:
            return "No message found at that exact timestamp."
        return f"[{result.get('timestamp', '?')}] ({result.get('role', '?')}): {result.get('content', '')}"
    elif cmd == "nearest":
        result = recall_nearest_message(user_id, parsed["timestamp"])
        if result is None:
            return "No message found near that timestamp."
        return f"[{result.get('timestamp', '?')}] ({result.get('role', '?')}): {result.get('content', '')}"
    elif cmd == "messages":
        results = recall_messages_in_range(user_id, parsed["start"], parsed["end"])
        if not results:
            return "No messages found in that range."
        lines = [
            f"[{r.get('timestamp', '?')}] ({r.get('role', '?')}): {r.get('content', '')}"
            for r in results
        ]
        return "\n".join(lines)
    return "Unknown recall command."


# ---------------------------------------------------------------------------
# Message handling pipeline
# ---------------------------------------------------------------------------

async def _handle_message(
    content: str,
    repo_path: str,
    registry: CapabilityRegistry,
    *,
    user_id: str = "",
    attachment_urls: Optional[list[str]] = None,
) -> None:
    """Classify a message and dispatch the appropriate action."""
    urls = attachment_urls or []
    display = content or "(no text)"
    if urls:
        logger.info("Processing message: %.80s [+%d image(s)]", display, len(urls))
    else:
        logger.info("Processing message: %.80s", display)

    # If images are attached, run vision analysis
    if urls:
        await _handle_image_message(content, urls)
        return

    # Check for recall commands first
    parsed = _parse_recall_command(content)
    if parsed is not None:
        response_text = _handle_recall_command(user_id, parsed)
        _store_memory(user_id, content, "user")
        _store_memory(user_id, response_text, "assistant")
        discord_notify(response_text)
        return

    # Store user message in conversational memory
    _store_memory(user_id, content, "user")

    # Build prompt with conversation context
    context = _get_memory_context(user_id)
    prompt_parts = []
    if context:
        prompt_parts.append(f"Conversation history:\n{context}\n")
    prompt_parts.append(f"Message from Jesse:\n{content}")
    full_prompt = "\n".join(prompt_parts)

    try:
        response = call_model(
            prompt=full_prompt,
            system=SYSTEM_PROMPT,
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
    reply = data.get("response", "")
    gap_name = data.get("gap_name", "")
    gap_description = data.get("gap_description", "")

    # Store assistant reply in memory
    if reply:
        _store_memory(user_id, reply, "assistant")

    if intent == "GAP":
        _dispatch_gap(reply, gap_name, gap_description)
    elif intent == "REQUEST":
        _dispatch_request(reply, gap_name, gap_description, repo_path, registry)
    elif intent == "CONVERSATION":
        _dispatch_conversation(reply)
    elif intent == "TRIGGER_GENERATION":
        _dispatch_trigger(reply, repo_path, registry)
    else:
        logger.warning("Unknown intent '%s'; treating as conversation.", intent)
        _dispatch_conversation(reply or "I'm here.")


async def _handle_image_message(content: str, urls: list[str]) -> None:
    """Process a message with image attachments using vision analysis.

    Runs the synchronous vision pipeline in an executor thread, then sends
    the notification from the async context (where ensure_future works).
    """
    import asyncio
    loop = asyncio.get_running_loop()
    try:
        from capabilities.image_vision import analyse_image_with_vision
        # Run vision analysis in executor
        results = []
        for url in urls:
            result = await loop.run_in_executor(
                None,
                lambda u=url: analyse_image_with_vision(u, user_context=content or ""),
            )
            results.append(result)
        # Build a response from the vision results
        descriptions = [
            r.get("description", "") or r.get("analysis", "")
            for r in results if r
        ]
        if descriptions:
            response_text = "\n".join(d for d in descriptions if d)
            if response_text:
                discord_notify(response_text)
            else:
                discord_notify("I processed your image but couldn't extract a meaningful description.")
        else:
            discord_notify("I tried to analyze your image but didn't get useful results.")
    except ImportError:
        logger.warning("image_vision capability not available; cannot process images.")
        discord_notify(
            "I can see you sent an image, but my vision capability isn't wired up yet. "
            "Working on it!"
        )
    except Exception as exc:
        logger.error("Image processing failed: %s", exc)
        discord_notify("I tried to analyze your image but something went wrong. I'll log this.")


# ---------------------------------------------------------------------------
# Memory helpers (graceful fallback if not available)
# ---------------------------------------------------------------------------

def _store_memory(user_id: str, content: str, role: str) -> None:
    """Store a message in conversational memory if available."""
    if not user_id:
        return
    try:
        from capabilities.conversational_memory import store_message
        store_message(user_id, content, role=role)
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("Could not store message in memory: %s", exc)


def _get_memory_context(user_id: str) -> str:
    """Get conversation context from memory if available."""
    if not user_id:
        return ""
    try:
        from capabilities.conversational_memory import get_context
        return get_context(user_id)
    except ImportError:
        return ""
    except Exception as exc:
        logger.debug("Could not retrieve memory context: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Intent dispatchers
# ---------------------------------------------------------------------------

def _dispatch_gap(reply: str, gap_name: str, gap_description: str) -> None:
    """Log a capability gap revealed by Jesse, then respond."""
    logger.info("Intent=GAP: %s", gap_description)
    if gap_name and gap_description:
        _write_gap_to_oplog(gap_name, gap_description, source="discord_dm")
    if reply:
        discord_notify(reply)


def _dispatch_request(
    reply: str,
    gap_name: str,
    gap_description: str,
    repo_path: str,
    registry: CapabilityRegistry,
) -> None:
    """Attempt to fulfill a request; log as gap if we can't."""
    logger.info("Intent=REQUEST: %s", reply)
    if gap_description:
        _write_gap_to_oplog(gap_name or "unknown_request", gap_description, source="discord_request")
        if reply:
            discord_notify(reply)
    else:
        if reply:
            discord_notify(reply)
        result = run_cycle(repo_path=repo_path, registry=registry)
        _notify_cycle_outcome(result)


def _dispatch_conversation(reply: str) -> None:
    """Send a conversational response back to Jesse."""
    logger.info("Intent=CONVERSATION")
    discord_notify(reply or "I'm here.")


def _dispatch_trigger(
    reply: str,
    repo_path: str,
    registry: CapabilityRegistry,
) -> None:
    """Run a generation cycle and notify Jesse of the result."""
    logger.info("Intent=TRIGGER_GENERATION: running generation loop.")
    if reply:
        discord_notify(reply)
    result = run_cycle(repo_path=repo_path, registry=registry)
    _notify_cycle_outcome(result)


def _write_gap_to_oplog(gap_name: str, description: str, source: str = "discord_dm") -> None:
    """Append a gap entry to the operation log so gap_detector picks it up."""
    entry = {
        "event": f"gap_signal_from_{source}",
        "success": False,
        "missing_capability": gap_name,
        "detail": description,
    }
    try:
        OP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OP_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("Logged gap to operation_log: %s", description[:120])
    except OSError as exc:
        logger.error("Could not write gap to operation_log: %s", exc)


def _notify_cycle_outcome(result: CycleResult) -> None:
    """Send a Discord summary of a triggered cycle result."""
    if result.capability_registered and result.gap:
        discord_notify(f"Done — integrated {result.gap.name}.")
    elif result.error:
        discord_notify(f"Cycle failed at {result.phase_reached}: {result.error[:200]}")
    elif result.phase_reached == "observe":
        discord_notify("No gaps detected — nothing to do.")
