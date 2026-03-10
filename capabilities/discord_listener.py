"""
Discord listener — processes incoming DMs from Jesse via discord_gateway.

Intent classification routes messages to the appropriate handler:
  GAP          — Jesse describes a missing capability → log to operation_log
  RECALL       — Jesse wants to recall a past message by timestamp
  CONVERSATION — General chat → respond via model + conversational memory
  IMAGE        — Message has image attachments → vision pipeline
  TRIGGER      — Jesse asks Archi to run a generation cycle

PROTECTED FILE — Archi's generation loop must NOT rewrite this file.
"""

import asyncio
import json
import logging
import re
from collections import deque
from pathlib import Path
from typing import Optional

from src.kernel.capability_registry import CapabilityRegistry
from capabilities.discord_notifier import notify, notify_async
from capabilities.conversational_memory import store_message, get_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Message queue (fed by discord_gateway.receive_message callback)
# ---------------------------------------------------------------------------

_message_queue: deque = deque()

DEFAULT_OP_LOG = Path("data/operation_log.jsonl")


def receive_message(
    content: str,
    user_id: str,
    *,
    attachment_urls: Optional[list] = None,
) -> None:
    """Callback for discord_gateway — enqueues incoming DMs for processing."""
    _message_queue.append({
        "content": content,
        "user_id": user_id,
        "attachment_urls": attachment_urls or [],
    })
    logger.debug("Enqueued message from user %s (queue depth=%d)", user_id, len(_message_queue))


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

_GAP_PATTERNS = [
    re.compile(r"\b(need|want|missing|add|build|create)\b.*\b(capability|feature|ability|function)\b", re.I),
    re.compile(r"\bgap\b", re.I),
    re.compile(r"\byou (should|could|need to)\b.*\b(be able|learn|support)\b", re.I),
    re.compile(r"\bcan you\b.*\b(start|learn|add)\b", re.I),
]

_TRIGGER_PATTERNS = [
    re.compile(r"\b(run|trigger|start|execute)\b.*\b(cycle|generation|loop|build)\b", re.I),
    re.compile(r"\bgenerate\b", re.I),
]

_RECALL_PATTERNS = [
    re.compile(r"\brecall\b.*\b(at|around|on|from)\b", re.I),
    re.compile(r"\bwhat did (i|you) say\b.*\b(at|around|on)\b", re.I),
    re.compile(r"\bshow me.*message.*\b(at|around|on|from)\b", re.I),
    re.compile(r"\bfind.*message.*\b(at|around|on|from)\b", re.I),
    re.compile(r"\brecall message\b", re.I),
]

_TIMESTAMP_EXTRACTION = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[Z+-]\S*)?|"
    r"\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?|"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?|"
    r"(?:yesterday|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+at\s+\d{1,2}:\d{2}(?:\s*[AP]M)?)?)",
    re.I,
)


def _classify_intent(content: str, attachment_urls: list) -> str:
    """Classify user intent from message content and attachments.

    Returns one of: IMAGE, RECALL, GAP, TRIGGER, CONVERSATION.
    """
    if attachment_urls:
        return "IMAGE"

    text = content.strip()

    # Check recall intent first (most specific)
    if any(p.search(text) for p in _RECALL_PATTERNS):
        return "RECALL"

    # Check for gap/capability request
    if any(p.search(text) for p in _GAP_PATTERNS):
        return "GAP"

    # Check for generation trigger
    if any(p.search(text) for p in _TRIGGER_PATTERNS):
        return "TRIGGER"

    return "CONVERSATION"


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------

async def _handle_gap(user_id: str, content: str) -> None:
    """Log a capability gap signaled by Jesse to the operation log."""
    # Derive a slug for the gap name
    words = re.findall(r"[a-z]+", content.lower())
    meaningful = [w for w in words if len(w) > 3 and w not in {
        "need", "want", "that", "this", "should", "could", "would",
        "have", "able", "capability", "feature", "archi", "please",
        "like", "some", "with", "from", "about", "what", "your",
    }]
    gap_name = "_".join(meaningful[:4]) if meaningful else "jesse_requested_capability"

    entry = {
        "event": "gap_signal_from_discord",
        "success": False,
        "missing_capability": gap_name,
        "detail": content[:500],
    }
    try:
        DEFAULT_OP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEFAULT_OP_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("Logged gap from Jesse: %s", gap_name)
    except OSError as exc:
        logger.error("Failed to write gap to operation log: %s", exc)

    # Store in memory and respond
    store_message(user_id, content, role="user")
    await notify_async(
        f"Got it — I've logged **{gap_name}** as a capability gap. "
        f"I'll work on building it in my next generation cycle."
    )
    store_message(user_id, f"Logged gap: {gap_name}", role="assistant")


async def _handle_recall(user_id: str, content: str) -> None:
    """Handle a recall-by-timestamp request."""
    ts_match = _TIMESTAMP_EXTRACTION.search(content)
    if not ts_match:
        await notify_async(
            "I noticed you want to recall a message, but I couldn't find a "
            "recognizable timestamp. Please include something like '3:45 PM' "
            "or '2024-01-15 14:30'."
        )
        return

    timestamp_str = ts_match.group(0).strip()
    logger.info("Recall intent for user=%s timestamp='%s'", user_id, timestamp_str)

    try:
        from capabilities.message_recall_by_timestamp import recall_by_timestamp_str
        result = recall_by_timestamp_str(user_id, timestamp_str)
    except ImportError:
        # Fallback to basic timestamped recall
        try:
            from capabilities.timestamped_chat_history_recall import recall_nearest_message
            result = recall_nearest_message(user_id, timestamp_str, tolerance_seconds=300)
        except Exception as exc:
            logger.warning("Recall fallback failed: %s", exc)
            result = None
    except Exception as exc:
        logger.exception("recall_by_timestamp_str failed: %s", exc)
        await notify_async(f"An error occurred recalling your message at '{timestamp_str}'. Please try again.")
        return

    if result is None:
        await notify_async(f"No message found near the timestamp '{timestamp_str}'.")
    else:
        role = result.get("role", "unknown")
        msg_content = result.get("content", "")
        ts = result.get("timestamp", "")
        await notify_async(f"Recalled message:\n[{role} @ {ts}]: {msg_content}")


async def _handle_image(user_id: str, content: str, attachment_urls: list) -> None:
    """Run the vision pipeline on image attachments, then notify from async context."""
    store_message(user_id, f"[image: {len(attachment_urls)} attachment(s)] {content}", role="user")

    loop = asyncio.get_event_loop()

    # Run the vision pipeline in a thread executor (it does blocking HTTP calls)
    # with notify_result=False so we handle notification from this async context
    try:
        from capabilities.image_vision import handle_discord_vision_request

        # The vision handler does blocking I/O, so run in executor
        # We use a wrapper that suppresses its internal notify call
        from capabilities.image_analysis import process_multiple_attachments
        results = await loop.run_in_executor(
            None,
            lambda: process_multiple_attachments(
                attachment_urls,
                user_id=user_id,
                user_context=content,
                notify_result=False,  # We'll notify from async context
            ),
        )

        # Build a response from the results
        from capabilities.image_vision import generate_contextual_response
        response_text = await loop.run_in_executor(
            None,
            lambda: generate_contextual_response(
                [{"combined_summary": r.get("summary", "") or r.get("vision_description", "")}
                 for r in results],
                user_message=content,
            ),
        )

        # Notify from async context (safe for ensure_future)
        if response_text:
            await notify_async(response_text)
            store_message(user_id, response_text, role="assistant")

    except ImportError as exc:
        logger.warning("Image analysis not fully available: %s", exc)
        await notify_async("I received your image but my vision capabilities aren't fully wired yet.")
    except Exception as exc:
        logger.exception("Image handling failed: %s", exc)
        await notify_async("I had trouble analyzing your image. I'll look into what went wrong.")


async def _handle_trigger(user_id: str, content: str) -> None:
    """Acknowledge a generation cycle trigger request."""
    store_message(user_id, content, role="user")
    await notify_async("Starting a generation cycle now — I'll let you know what I build.")
    store_message(user_id, "Triggering generation cycle.", role="assistant")
    # The actual cycle runs in the daemon's generation task — we just acknowledge here.
    # A more sophisticated version could signal the daemon to run immediately.


async def _handle_conversation(user_id: str, content: str) -> None:
    """Handle general conversation using the model + conversational memory."""
    store_message(user_id, content, role="user")

    # Build context from conversation history
    context = get_context(user_id)

    try:
        from src.kernel.model_interface import call_model

        prompt = (
            f"You are Archi, Jesse's personal AI assistant. You are helpful, concise, "
            f"and oriented toward Jesse's wellbeing across six dimensions: Health, Wealth, "
            f"Happiness, Agency, Capability, and Synthesis.\n\n"
        )
        if context:
            prompt += f"Recent conversation context:\n{context}\n\n"
        prompt += f"Jesse says: {content}\n\nRespond helpfully and concisely."

        response = call_model(
            prompt,
            system="You are Archi, Jesse's AI assistant. Be concise and helpful.",
        )
        reply = response.text.strip()
    except Exception as exc:
        logger.warning("Model call failed for conversation: %s", exc)
        reply = "I'm here but having trouble generating a response right now. Let me try again later."

    await notify_async(reply)
    store_message(user_id, reply, role="assistant")


# ---------------------------------------------------------------------------
# Message processing (called by ArchiDaemon's message task)
# ---------------------------------------------------------------------------

async def process_one(repo_path: str, registry: CapabilityRegistry) -> bool:
    """Dequeue and process a single message. Returns True if a message was handled."""
    if not _message_queue:
        return False

    msg = _message_queue.popleft()
    content: str = msg["content"]
    user_id: str = msg["user_id"]
    attachment_urls: list = msg.get("attachment_urls", [])

    intent = _classify_intent(content, attachment_urls)
    logger.info("Processing message from user=%s intent=%s: %s", user_id, intent, content[:80])

    try:
        if intent == "IMAGE":
            await _handle_image(user_id, content, attachment_urls)
        elif intent == "RECALL":
            await _handle_recall(user_id, content)
        elif intent == "GAP":
            await _handle_gap(user_id, content)
        elif intent == "TRIGGER":
            await _handle_trigger(user_id, content)
        else:
            await _handle_conversation(user_id, content)
    except Exception as exc:
        logger.exception("Error handling %s intent from user %s: %s", intent, user_id, exc)

    return True


async def process_pending(repo_path: str, registry: CapabilityRegistry) -> int:
    """Drain the message queue and process each message."""
    count = 0
    while _message_queue:
        processed = await process_one(repo_path, registry)
        if processed:
            count += 1
    return count
