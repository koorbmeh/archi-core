"""
capabilities/wire_wire_image_recognition.py

Patches an active pathway (discord_listener.py or run.py) to import and invoke
wire_image_recognition.wire(), enabling image_recognition integration during
runtime initialization. Uses self_modifier.apply_change to apply the patch
safely with testing, and registers the capability in the registry.
"""

import logging
from pathlib import Path

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.self_modifier import apply_change
from src.kernel.alignment_gates import ActionContext, check_gates

from capabilities.wire_image_recognition import wire as image_recognition_wire

logger = logging.getLogger(__name__)

_CAPABILITY_NAME = "wire_wire_image_recognition"
_CAPABILITY_MODULE = "capabilities.wire_wire_image_recognition"
_CAPABILITY_DESCRIPTION = (
    "Patches discord_listener.py or run.py to import and invoke "
    "wire_image_recognition.wire() at module load, enabling image_recognition "
    "integration during runtime initialization."
)

_PATCH_IMPORT = "from capabilities.wire_image_recognition import wire as _wire_image_recognition\n"
_PATCH_CALL = "_wire_image_recognition()\n"
_PATCH_MARKER = "# wire_wire_image_recognition patched"


def _read_file(file_path: Path) -> str | None:
    """Read a file and return its contents, or None on failure."""
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to read %s: %s", file_path, exc)
        return None


def _is_already_patched(content: str) -> bool:
    """Return True if the patch marker is present in the file content."""
    return _PATCH_MARKER in content


def _build_patched_content(original: str) -> str:
    """Inject the import and wire() call after the last top-level import block."""
    lines = original.splitlines(keepends=True)
    last_import_idx = 0

    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            last_import_idx = idx

    inject_lines = [
        "\n",
        f"{_PATCH_IMPORT}",
        f"{_PATCH_CALL}",
        f"{_PATCH_MARKER}\n",
        "\n",
    ]

    patched = lines[: last_import_idx + 1] + inject_lines + lines[last_import_idx + 1 :]
    return "".join(patched)


def _check_alignment_gates(target: str) -> bool:
    """Run alignment gates for the patch action. Returns True if all pass."""
    ctx = ActionContext(
        action_type="file_write",
        target=target,
        estimated_cost=0.0,
        metadata={"reason": "wire image_recognition into active pathway"},
    )
    failures = check_gates(ctx)
    if failures:
        for gate_result in failures:
            logger.warning("Gate failed [%s]: %s", gate_result.gate, gate_result.reason)
        return False
    return True


def patch_pathway(
    repo_path: str = ".",
    target_file: str = "capabilities/discord_listener.py",
) -> bool:
    """
    Patch the target file to import and call wire_image_recognition.wire().

    Returns True on success, False on failure or if already patched.
    """
    repo = Path(repo_path)
    full_path = repo / target_file

    content = _read_file(full_path)
    if content is None:
        return False

    if _is_already_patched(content):
        logger.info("Target %s is already patched; skipping.", target_file)
        return True

    if not _check_alignment_gates(target_file):
        logger.error("Alignment gate check failed for %s", target_file)
        return False

    patched_content = _build_patched_content(content)
    result = apply_change(repo_path, target_file, patched_content)

    if result.success:
        logger.info("Successfully patched %s: %s", target_file, result.message)
    else:
        logger.error(
            "Failed to patch %s: %s (error=%s)", target_file, result.message, result.error
        )

    return result.success


def wire(repo_path: str = ".", registry: CapabilityRegistry | None = None) -> bool:
    """
    Wire wire_wire_image_recognition:
      1. Patch discord_listener.py to invoke wire_image_recognition.wire().
      2. Invoke wire_image_recognition.wire() directly for the current runtime.
      3. Register this capability.
    """
    success = patch_pathway(repo_path=repo_path, target_file="capabilities/discord_listener.py")
    if not success:
        logger.warning("Pathway patch did not fully succeed; attempting wire anyway.")

    runtime_wired = image_recognition_wire(repo_path=repo_path, registry=registry)
    if not runtime_wired:
        logger.warning("wire_image_recognition.wire() returned False.")

    register_capability(registry=registry)
    return success and runtime_wired


def register_capability(
    registry: CapabilityRegistry | None = None,
) -> Capability:
    """Add wire_wire_image_recognition to the capability registry."""
    if registry is None:
        registry = CapabilityRegistry()

    capability = Capability(
        name=_CAPABILITY_NAME,
        module=_CAPABILITY_MODULE,
        description=_CAPABILITY_DESCRIPTION,
        status="active",
        dependencies=["wire_image_recognition", "self_modifier", "discord_listener"],
    )

    registry.register(capability)
    logger.info("Registered capability: %s", _CAPABILITY_NAME)
    return capability