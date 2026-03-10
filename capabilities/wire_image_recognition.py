"""
capabilities/wire_image_recognition.py

Integrates image_recognition into discord_listener by patching its receive_message
function to automatically process image attachments using
image_recognition.process_discord_image.

Reads the current discord_listener.py source, generates updated code that imports
image_recognition and calls process_discord_image(attachment_urls, user_id) within
receive_message when attachment_urls is present.  Uses self_modifier.apply_change to
safely apply the patch (branch → test → merge), registers the new capability, and
notifies via discord_notifier.
"""

import logging
import pathlib
import textwrap

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.self_modifier import apply_change

from capabilities.discord_notifier import notify

logger = logging.getLogger(__name__)

_CAPABILITY_NAME = "wire_image_recognition"
_LISTENER_PATH = "capabilities/discord_listener.py"

# ---------------------------------------------------------------------------
# Source-level patch helpers
# ---------------------------------------------------------------------------

def _read_listener_source(repo_path: pathlib.Path) -> str:
    """Return the current source of discord_listener.py."""
    full = repo_path / _LISTENER_PATH
    return full.read_text(encoding="utf-8")


def _ensure_import(source: str) -> str:
    """Inject 'import capabilities.image_recognition as image_recognition' if absent."""
    marker = "import capabilities.image_recognition"
    if marker in source:
        return source

    import_line = "import capabilities.image_recognition as image_recognition\n"
    # Insert after the last stdlib/third-party import block by finding the first
    # blank line after an 'import' statement near the top.
    lines = source.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            insert_at = i + 1
    lines.insert(insert_at, import_line)
    return "".join(lines)


def _inject_image_processing(source: str) -> str:
    """
    Insert a call to image_recognition.process_discord_image inside
    receive_message, guarded by 'if attachment_urls:'.
    """
    trigger = "if attachment_urls:"
    if "process_discord_image" in source:
        logger.info("process_discord_image already present; skipping injection.")
        return source

    injection = textwrap.dedent("""\
        if attachment_urls:
            try:
                image_recognition.process_discord_image(attachment_urls, user_id)
            except Exception as _img_err:  # noqa: BLE001
                logger.warning("image_recognition failed: %s", _img_err)
    """)

    # Locate the body of receive_message and insert before the _queue.put / enqueue line.
    lines = source.splitlines(keepends=True)
    func_found = False
    inject_index = None

    for i, line in enumerate(lines):
        if "def receive_message(" in line:
            func_found = True
        if func_found and ("_queue" in line or "queue" in line.lower()) and inject_index is None:
            inject_index = i
            break

    if inject_index is None:
        logger.warning("Could not locate insertion point in receive_message; appending before end of function.")
        # Fallback: append injection just before the first return or end of function
        for i, line in enumerate(lines):
            if func_found and line.strip().startswith("return"):
                inject_index = i
                break

    if inject_index is None:
        raise RuntimeError("Failed to find injection point in discord_listener.py")

    # Determine indentation from the target line
    target_line = lines[inject_index]
    indent = len(target_line) - len(target_line.lstrip())
    indented_injection = textwrap.indent(injection, " " * indent)
    lines.insert(inject_index, indented_injection)
    return "".join(lines)


def _patch_source(source: str) -> str:
    """Apply both transformations to the source."""
    source = _ensure_import(source)
    source = _inject_image_processing(source)
    return source


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def _register_capability(registry: CapabilityRegistry) -> None:
    """Register wire_image_recognition in the capability registry."""
    cap = Capability(
        name=_CAPABILITY_NAME,
        module=f"capabilities.{_CAPABILITY_NAME}",
        description=(
            "Wires image_recognition into discord_listener so that image "
            "attachments are automatically processed via "
            "image_recognition.process_discord_image."
        ),
        status="active",
        dependencies=["image_recognition", "discord_listener", "self_modifier"],
    )
    registry.register(cap)
    logger.info("Capability '%s' registered.", _CAPABILITY_NAME)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def wire(repo_path: str = ".", registry: CapabilityRegistry | None = None) -> bool:
    """
    Patch discord_listener.py to integrate image_recognition, register the
    capability, and notify via discord_notifier.

    Returns True on success, False on failure.
    """
    repo = pathlib.Path(repo_path).resolve()

    # 1. Read current source
    try:
        original = _read_listener_source(repo)
    except FileNotFoundError:
        logger.error("discord_listener.py not found at %s", repo / _LISTENER_PATH)
        return False

    # 2. Generate patched source
    try:
        patched = _patch_source(original)
    except RuntimeError as exc:
        logger.error("Patch generation failed: %s", exc)
        notify(f"[wire_image_recognition] Patch generation failed: {exc}")
        return False

    if patched == original:
        logger.info("discord_listener.py already wired; nothing to do.")
        notify("[wire_image_recognition] discord_listener already wired with image_recognition.")
        _maybe_register(registry)
        return True

    # 3. Apply via self_modifier (branch → test → merge)
    result = apply_change(str(repo), _LISTENER_PATH, patched)

    if not result.success:
        msg = f"[wire_image_recognition] Patch failed: {result.message} | error={result.error}"
        logger.error(msg)
        notify(msg)
        return False

    logger.info("Patch applied successfully: %s", result.message)

    # 4. Register capability
    _maybe_register(registry)

    # 5. Notify
    notify(
        "[wire_image_recognition] Successfully wired image_recognition into "
        "discord_listener. Image attachments will now be processed automatically."
    )
    return True


def _maybe_register(registry: CapabilityRegistry | None) -> None:
    """Register if a registry was supplied."""
    if registry is not None:
        try:
            _register_capability(registry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not register capability: %s", exc)


def register_capability(registry: CapabilityRegistry | None = None) -> Capability:
    """Register wire_image_recognition with the capability registry."""
    if registry is None:
        registry = CapabilityRegistry()
    cap = Capability(
        name=_CAPABILITY_NAME,
        module=f"capabilities.{_CAPABILITY_NAME}",
        description=(
            "Wires image_recognition into discord_listener so that image "
            "attachments are automatically processed via "
            "image_recognition.process_discord_image."
        ),
        status="active",
        dependencies=["image_recognition", "discord_listener", "self_modifier"],
    )
    registry.register(cap)
    return cap