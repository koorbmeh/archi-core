"""
Discord listener — processes incoming DMs from Jesse via discord_gateway.

Intent classification routes messages to the appropriate handler:
  COMMAND      — Jesse sends !command → dispatch to command_registry handler
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
from src.kernel.command_registry import (
    load_registry as load_commands,
    match_command,
    resolve_function as resolve_command_fn,
)
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

_PREREQ_CONFIRM_PATTERNS = [
    re.compile(r"^\s*(done|ready|installed|set\s*up|confirmed|go\s*ahead|all\s*set)\s*[.!]?\s*$", re.I),
    re.compile(r"\b(done|ready|installed|set\s*up|confirmed)\b.*\bprereq", re.I),
    re.compile(r"\bprereq.*\b(done|ready|installed|set\s*up|confirmed)\b", re.I),
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


def _has_pending_prerequisites() -> bool:
    """Check if there are any prerequisite_pending entries without confirmation."""
    if not DEFAULT_OP_LOG.exists():
        return False
    pending: set[str] = set()
    confirmed: set[str] = set()
    try:
        for line in DEFAULT_OP_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = entry.get("event", "")
            cap = entry.get("missing_capability", "")
            if evt == "prerequisite_pending" and cap:
                pending.add(cap)
                confirmed.discard(cap)
            elif evt == "prerequisite_confirmed" and cap:
                confirmed.add(cap)
    except OSError:
        return False
    return bool(pending - confirmed)


def _classify_intent(content: str, attachment_urls: list) -> str:
    """Classify user intent from message content and attachments.

    Returns one of: COMMAND, IMAGE, RECALL, GAP, TRIGGER, PREREQ_CONFIRM, CONVERSATION.
    """
    if attachment_urls:
        return "IMAGE"

    text = content.strip()

    # Check for !command patterns first (highest priority for explicit commands)
    if text.startswith("!"):
        commands = load_commands()
        if match_command(text, commands) is not None:
            return "COMMAND"

    # Check recall intent first (most specific)
    if any(p.search(text) for p in _RECALL_PATTERNS):
        return "RECALL"

    # Check for prerequisite confirmation (only if there are pending prereqs)
    if any(p.search(text) for p in _PREREQ_CONFIRM_PATTERNS):
        if _has_pending_prerequisites():
            return "PREREQ_CONFIRM"

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


async def _handle_prereq_confirm(user_id: str, content: str) -> None:
    """Jesse confirmed that prerequisites are in place — unblock pending gaps."""
    store_message(user_id, content, role="user")

    # Find all pending (unconfirmed) prerequisite gaps and confirm them
    confirmed_gaps: list[str] = []
    if DEFAULT_OP_LOG.exists():
        pending: dict[str, str] = {}  # gap_name → detail
        confirmed_set: set[str] = set()
        try:
            for line in DEFAULT_OP_LOG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evt = entry.get("event", "")
                cap = entry.get("missing_capability", "")
                if evt == "prerequisite_pending" and cap:
                    pending[cap] = entry.get("detail", "")
                    confirmed_set.discard(cap)
                elif evt == "prerequisite_confirmed" and cap:
                    confirmed_set.add(cap)
        except OSError:
            pass

        waiting = set(pending.keys()) - confirmed_set
        for gap_name in waiting:
            entry = {
                "event": "prerequisite_confirmed",
                "success": True,
                "missing_capability": gap_name,
                "detail": f"Jesse confirmed prerequisites are ready.",
            }
            try:
                with DEFAULT_OP_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
                confirmed_gaps.append(gap_name)
                logger.info("Prerequisite confirmed for gap: %s", gap_name)
            except OSError as exc:
                logger.error("Failed to log prerequisite confirmation: %s", exc)

    if confirmed_gaps:
        names = ", ".join(f"**{g}**" for g in confirmed_gaps)
        reply = (
            f"Got it — prerequisites confirmed for {names}. "
            f"I'll build {'them' if len(confirmed_gaps) > 1 else 'it'} "
            f"in the next generation cycle."
        )
    else:
        reply = "I don't have any pending prerequisites right now, but noted!"

    await notify_async(reply)
    store_message(user_id, reply, role="assistant")


async def _handle_command(user_id: str, content: str) -> None:
    """Dispatch a !command to the appropriate registered handler."""
    store_message(user_id, content, role="user")

    commands = load_commands()
    result = match_command(content, commands)
    if result is None:
        await notify_async("I didn't recognize that command. Try `!help` to see available commands.")
        return

    entry, args = result
    fn = resolve_command_fn(entry)
    if fn is None:
        await notify_async(
            f"The command **!{entry.command}** is registered but I couldn't load "
            f"its handler ({entry.module}.{entry.function}). This may need a fix."
        )
        return

    await notify_async(f"Running **!{entry.command}**...")
    try:
        if entry.is_async:
            if args:
                await fn(args)
            else:
                await fn()
        else:
            loop = asyncio.get_event_loop()
            if args:
                await loop.run_in_executor(None, fn, args)
            else:
                await loop.run_in_executor(None, fn)
    except TypeError:
        # Handler doesn't accept args — call without
        try:
            if entry.is_async:
                await fn()
            else:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, fn)
        except Exception as exc:
            logger.exception("Command !%s failed: %s", entry.command, exc)
            await notify_async(f"Command **!{entry.command}** failed: {exc}")
            return
    except Exception as exc:
        logger.exception("Command !%s failed: %s", entry.command, exc)
        await notify_async(f"Command **!{entry.command}** failed: {exc}")
        return

    store_message(user_id, f"Ran command: !{entry.command}", role="assistant")


async def _handle_trigger(user_id: str, content: str) -> None:
    """Acknowledge a generation cycle trigger request."""
    store_message(user_id, content, role="user")
    await notify_async("Starting a generation cycle now — I'll let you know what I build.")
    store_message(user_id, "Triggering generation cycle.", role="assistant")
    # The actual cycle runs in the daemon's generation task — we just acknowledge here.
    # A more sophisticated version could signal the daemon to run immediately.


PROFILE_PATH = Path("data/personal_profile.json")


def _load_jesse_profile() -> str:
    """Load Jesse's personal profile JSON as a string for prompt injection.

    Returns an empty string if the profile does not exist or can't be read.
    """
    try:
        if PROFILE_PATH.exists():
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            return json.dumps(data, indent=2)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not load Jesse's profile: %s", exc)
    return ""


_CONVERSATION_SYSTEM = (
    "You are Archi, Jesse's personal AI assistant. Be concise and helpful.\n\n"
    "CRITICAL RULES:\n"
    "1. NEVER search the web for information about Jesse. You know Jesse ONLY "
    "through what he tells you directly in conversation and from his profile "
    "below. If you don't know something about Jesse, ask him — do not guess "
    "or fabricate details.\n"
    "2. NEVER start your response with '[build]' or any build notification "
    "prefix. Those are internal system messages.\n"
    "3. If Jesse asks you to research him or look him up, explain that you "
    "learn about him through conversation, not web searches, and ask him to "
    "share the details directly.\n"
    "4. Do not confidently assert facts about Jesse (employer, location, "
    "skills, etc.) unless they appear in his profile or he told you in this "
    "conversation."
)


async def _handle_conversation(user_id: str, content: str) -> None:
    """Handle general conversation using the model + conversational memory."""
    store_message(user_id, content, role="user")

    # Build context from conversation history
    context = get_context(user_id)

    # Load Jesse's profile for grounded responses
    profile_text = _load_jesse_profile()

    try:
        from src.kernel.model_interface import call_model

        prompt = (
            "You are Archi, Jesse's personal AI assistant. You are helpful, concise, "
            "and oriented toward Jesse's wellbeing across six dimensions: Health, Wealth, "
            "Happiness, Agency, Capability, and Synthesis.\n\n"
        )
        if profile_text:
            prompt += f"Jesse's profile (verified, self-reported):\n{profile_text}\n\n"
        if context:
            prompt += f"Recent conversation context:\n{context}\n\n"
        prompt += f"Jesse says: {content}\n\nRespond helpfully and concisely."

        response = call_model(
            prompt,
            system=_CONVERSATION_SYSTEM,
        )
        reply = response.text.strip()
    except Exception as exc:
        logger.warning("Model call failed for conversation: %s", exc)
        reply = "I'm here but having trouble generating a response right now. Let me try again later."

    # Guard: never let a conversation reply start with a build notification prefix
    if reply.startswith("[build]"):
        reply = reply[len("[build]"):].strip()

    await notify_async(reply)
    store_message(user_id, reply, role="assistant")

    # Write-through: extract profile-worthy facts from this exchange
    try:
        from capabilities.personal_profile_manager import get_manager
        mgr = get_manager()
        mgr.update_profile(content, reply)
    except Exception as exc:
        logger.debug("Profile write-through failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Message processing (called by ArchiDaemon's message task)
# ---------------------------------------------------------------------------

# Intents that mutate shared state (operation log, registry) and need the
# build lock.  Everything else (CONVERSATION, IMAGE, RECALL) runs lock-free.
_STATE_MUTATING_INTENTS = {"GAP", "PREREQ_CONFIRM", "TRIGGER"}


async def process_one(
    repo_path: str,
    registry: CapabilityRegistry,
    *,
    build_lock: Optional["asyncio.Lock"] = None,
) -> bool:
    """Dequeue and process a single message. Returns True if a message was handled.

    *build_lock*, when provided, is acquired only for intents that mutate
    shared state (GAP, PREREQ_CONFIRM, TRIGGER).  Conversations and other
    read-only intents run without any lock so Jesse gets fast replies even
    during a build cycle.
    """
    if not _message_queue:
        return False

    msg = _message_queue.popleft()
    content: str = msg["content"]
    user_id: str = msg["user_id"]
    attachment_urls: list = msg.get("attachment_urls", [])

    intent = _classify_intent(content, attachment_urls)
    logger.info("Processing message from user=%s intent=%s: %s", user_id, intent, content[:80])

    try:
        if intent == "COMMAND":
            await _handle_command(user_id, content)
        elif intent == "IMAGE":
            await _handle_image(user_id, content, attachment_urls)
        elif intent == "RECALL":
            await _handle_recall(user_id, content)
        elif intent == "GAP":
            if build_lock:
                async with build_lock:
                    await _handle_gap(user_id, content)
            else:
                await _handle_gap(user_id, content)
        elif intent == "PREREQ_CONFIRM":
            if build_lock:
                async with build_lock:
                    await _handle_prereq_confirm(user_id, content)
            else:
                await _handle_prereq_confirm(user_id, content)
        elif intent == "TRIGGER":
            if build_lock:
                async with build_lock:
                    await _handle_trigger(user_id, content)
            else:
                await _handle_trigger(user_id, content)
        else:
            await _handle_conversation(user_id, content)
    except Exception as exc:
        logger.exception("Error handling %s intent from user %s: %s", intent, user_id, exc)

    return True


async def process_pending(
    repo_path: str,
    registry: CapabilityRegistry,
    *,
    build_lock: Optional["asyncio.Lock"] = None,
) -> int:
    """Drain the message queue and process each message.

    *build_lock* is forwarded to ``process_one`` — only state-mutating
    intents acquire it.  Conversations run concurrently with build cycles.
    """
    count = 0
    while _message_queue:
        processed = await process_one(
            repo_path, registry, build_lock=build_lock,
        )
        if processed:
            count += 1
    return count
