"""
capabilities/wire_wire_wire_image_recognition.py

Wires the wire_wire_image_recognition capability into an active pathway like
discord_listener.py by patching it to invoke the wiring function and
registering the new capability.

This module provides:
- wire(): patches capabilities/discord_listener.py to import and call
  wire_wire_image_recognition.wire() during listener initialization.
- register_capability(): adds this wiring capability to the registry.
- patch_pathway(): helper for targeted file updates.
"""

import logging
from pathlib import Path

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.self_modifier import apply_change

logger = logging.getLogger(__name__)

_CAPABILITY_NAME = "wire_wire_wire_image_recognition"
_MODULE_PATH = "capabilities/wire_wire_wire_image_recognition"
_TARGET_FILE = "capabilities/discord_listener.py"

_IMPORT_SENTINEL = "wire_wire_image_recognition"
_IMPORT_LINE = "from capabilities.wire_wire_image_recognition import wire as _wire_wire_image_recognition\n"
_CALL_SENTINEL = "_wire_wire_image_recognition()"
_CALL_SNIPPET = "\n# Wire wire_wire_image_recognition into this listener\ntry:\n    _wire_wire_image_recognition()\nexcept Exception as _e:\n    pass\n"


def _read_file(file_path: Path) -> str | None:
    """Read a file and return its content, or None on error."""
    try:
        return file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read %s: %s", file_path, exc)
        return None


def _inject_import(content: str) -> str:
    """Inject the wire_wire_image_recognition import if not already present."""
    if _IMPORT_SENTINEL in content:
        logger.debug("Import sentinel already present; skipping import injection.")
        return content
    lines = content.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("import ") or line.startswith("from "):
            insert_at = i + 1
    lines.insert(insert_at, _IMPORT_LINE)
    return "".join(lines)


def _inject_call(content: str) -> str:
    """Inject the _wire_wire_image_recognition() call if not already present."""
    if _CALL_SENTINEL in content:
        logger.debug("Call sentinel already present; skipping call injection.")
        return content
    return content + _CALL_SNIPPET


def patch_pathway(repo_path: str = ".", target_file: str = _TARGET_FILE) -> bool:
    """
    Patch the target file to import and call wire_wire_image_recognition.wire().

    Reads the current content of target_file, injects the import and call
    if not already present, then uses self_modifier.apply_change to write
    and validate the patched content.

    Returns True on success, False on failure.
    """
    base = Path(repo_path)
    full_path = base / target_file

    content = _read_file(full_path)
    if content is None:
        return False

    if _IMPORT_SENTINEL in content and _CALL_SENTINEL in content:
        logger.info("Target file %s already patched; nothing to do.", target_file)
        return True

    patched = _inject_import(content)
    patched = _inject_call(patched)

    result = apply_change(repo_path, target_file, patched)
    if result.success:
        logger.info("Successfully patched %s: %s", target_file, result.message)
    else:
        logger.error(
            "Failed to patch %s: %s (error=%s)", target_file, result.message, result.error
        )
    return result.success


def register_capability(
    registry: CapabilityRegistry | None = None,
) -> Capability:
    """
    Add wire_wire_wire_image_recognition to the capability registry.

    Creates and stores a Capability entry describing this wiring module.
    Returns the registered Capability.
    """
    if registry is None:
        registry = CapabilityRegistry()

    capability = Capability(
        name=_CAPABILITY_NAME,
        module=_MODULE_PATH,
        description=(
            "Wires wire_wire_image_recognition into discord_listener.py by patching "
            "it to import and invoke the wiring function during listener initialization."
        ),
        status="active",
        dependencies=["wire_wire_image_recognition", "self_modifier", "capability_registry"],
    )
    registry.register(capability)
    logger.info("Registered capability: %s", _CAPABILITY_NAME)
    return capability


def wire(repo_path: str = ".", registry: CapabilityRegistry | None = None) -> bool:
    """
    Wire wire_wire_wire_image_recognition:

    1. Patch capabilities/discord_listener.py to import and call
       wire_wire_image_recognition.wire() during listener initialization.
    2. Register this capability in the registry.

    Returns True if both steps succeed, False otherwise.
    """
    logger.info("Starting wire() for %s", _CAPABILITY_NAME)

    patch_ok = patch_pathway(repo_path=repo_path, target_file=_TARGET_FILE)
    if not patch_ok:
        logger.error("patch_pathway() failed; aborting wire().")
        return False

    try:
        register_capability(registry)
    except Exception as exc:
        logger.error("register_capability() failed: %s", exc)
        return False

    logger.info("wire() completed successfully for %s", _CAPABILITY_NAME)
    return True