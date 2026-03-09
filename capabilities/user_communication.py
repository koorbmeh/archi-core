"""
Module for user communication capabilities.

Provides functions for sending messages to the user and receiving input,
enabling direct interaction outside of model-generated responses.
Registers itself with the capability_registry upon import to make
functions available system-wide.
"""

import sys
import textwrap
from datetime import datetime
from typing import Optional


_REGISTERED = False
_MESSAGE_PREFIX = "[Archi]"
_LINE_WIDTH = 80


def send_message(
    text: str,
    prefix: Optional[str] = None,
    timestamp: bool = False,
    width: int = _LINE_WIDTH,
) -> bool:
    """
    Send a formatted message to the user via stdout.

    Args:
        text: The message text to send.
        prefix: Optional prefix label. Defaults to module-level prefix.
        timestamp: Whether to include a timestamp in the output.
        width: Maximum line width for wrapping. Defaults to 80.

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception as exc:
            print(
                f"{_MESSAGE_PREFIX} [ERROR] Failed to convert message to string: {exc}",
                file=sys.stderr,
            )
            return False

    effective_prefix = prefix if prefix is not None else _MESSAGE_PREFIX

    header_parts = [effective_prefix]
    if timestamp:
        header_parts.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    header = " ".join(header_parts)

    indent = " " * (len(header) + 1)
    wrapped = textwrap.fill(
        text,
        width=width,
        initial_indent=f"{header} ",
        subsequent_indent=indent,
    )

    try:
        print(wrapped, flush=True)
        return True
    except OSError as exc:
        print(
            f"{_MESSAGE_PREFIX} [ERROR] Failed to write message to stdout: {exc}",
            file=sys.stderr,
        )
        return False


def receive_input(
    prompt: Optional[str] = None,
    strip_whitespace: bool = True,
) -> Optional[str]:
    """
    Receive input from the user via stdin.

    Args:
        prompt: Optional prompt to display before reading input.
        strip_whitespace: Whether to strip leading/trailing whitespace from input.

    Returns:
        The user's input string, or None if input could not be read.
    """
    formatted_prompt = ""
    if prompt is not None:
        formatted_prompt = f"{_MESSAGE_PREFIX} {prompt} "

    try:
        user_input = input(formatted_prompt)
        if strip_whitespace:
            user_input = user_input.strip()
        return user_input
    except EOFError:
        send_message(
            "Input stream closed (EOF). No further input available.",
            prefix=f"{_MESSAGE_PREFIX} [WARNING]",
        )
        return None
    except KeyboardInterrupt:
        send_message(
            "Input interrupted by user (KeyboardInterrupt).",
            prefix=f"{_MESSAGE_PREFIX} [WARNING]",
        )
        return None
    except OSError as exc:
        print(
            f"{_MESSAGE_PREFIX} [ERROR] Failed to read input from stdin: {exc}",
            file=sys.stderr,
        )
        return None


def _build_capability_entry() -> dict:
    """
    Build the capability registry entry for this module.

    Returns:
        A dictionary describing the module's capabilities.
    """
    return {
        "module": "capabilities.user_communication",
        "description": (
            "Direct user communication outside of model-generated responses. "
            "Provides send_message() for formatted stdout output and "
            "receive_input() for stdin reading."
        ),
        "functions": {
            "send_message": send_message,
            "receive_input": receive_input,
        },
    }


def _register_with_capability_registry() -> None:
    """
    Attempt to register this module with the capability_registry.

    Silently skips registration if the registry is unavailable, ensuring
    the module remains functional as a standalone import.
    """
    global _REGISTERED

    if _REGISTERED:
        return

    try:
        from capabilities import capability_registry  # type: ignore

        entry = _build_capability_entry()
        capability_registry.register(
            name="user_communication",
            **entry,
        )
        _REGISTERED = True
    except ImportError:
        pass
    except Exception as exc:
        print(
            f"{_MESSAGE_PREFIX} [WARNING] Could not register with capability_registry: {exc}",
            file=sys.stderr,
        )


_register_with_capability_registry()