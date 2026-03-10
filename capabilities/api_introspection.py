"""
Module for introspecting available functions, signatures, and docstrings
in registered capabilities and kernel modules.

Converts file-path-based module references from the capability registry
into importable module paths, then uses Python's inspect module to extract
callable members with their signatures. Provides a summary format suitable
for injection into planner and codegen prompts so that generated code calls
real interfaces rather than hallucinated ones.
"""

import importlib
import inspect
import logging
from pathlib import PurePosixPath
from typing import Any, Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core introspection helpers
# ---------------------------------------------------------------------------

def _extract_callable_info(name: str, obj: Any) -> dict:
    """Extract signature and docstring information from a callable object."""
    info: dict[str, Any] = {
        "name": name,
        "qualname": getattr(obj, "__qualname__", name),
        "docstring": inspect.getdoc(obj) or "",
        "signature": None,
        "is_coroutine": inspect.iscoroutinefunction(obj),
        "is_class": inspect.isclass(obj),
    }
    try:
        sig = inspect.signature(obj)
        info["signature"] = str(sig)
    except (ValueError, TypeError) as exc:
        logger.debug("Could not extract signature for %s: %s", name, exc)
    return info


def _scan_module(module: Any) -> list[dict]:
    """Scan a module and return info dicts for its public callables."""
    members = []
    module_name = getattr(module, "__name__", "")
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if not (callable(obj) or inspect.isclass(obj)):
            continue
        # Skip re-exports from other packages
        source_module = getattr(obj, "__module__", None)
        if source_module and module_name and not source_module.startswith(module_name):
            continue
        members.append(_extract_callable_info(name, obj))
    return members


def _file_path_to_module(file_path: str) -> str:
    """Convert a file path like 'capabilities/foo.py' to 'capabilities.foo'."""
    p = PurePosixPath(file_path.replace("\\", "/"))
    # Strip .py extension and convert slashes to dots
    parts = list(p.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _load_module(file_path: str) -> Optional[Any]:
    """Import a module given a file-path-style reference."""
    module_path = _file_path_to_module(file_path)
    try:
        return importlib.import_module(module_path)
    except Exception as exc:
        logger.debug("Could not import '%s': %s", module_path, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_api(capability_name: str, registry: Optional[CapabilityRegistry] = None) -> list[dict]:
    """
    Return a structured list of public callables for a named capability.

    Each entry is a dict with keys: name, qualname, docstring, signature,
    is_coroutine, is_class.

    Args:
        capability_name: The registered name of the capability to introspect.
        registry: Optional registry instance. Creates a default one if omitted.

    Returns:
        A list of callable info dicts, or an empty list if not found.
    """
    reg = registry or CapabilityRegistry()
    cap = reg.get(capability_name)
    if cap is None:
        logger.warning("Capability '%s' not found in registry.", capability_name)
        return []
    module = _load_module(cap.module)
    if module is None:
        return []
    return _scan_module(module)


def get_module_api(module_path: str) -> list[dict]:
    """
    Introspect a module by its dotted import path (e.g. 'src.kernel.gap_detector').

    Args:
        module_path: Dotted Python import path.

    Returns:
        A list of callable info dicts, or an empty list if import fails.
    """
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        logger.debug("Could not import '%s': %s", module_path, exc)
        return []
    return _scan_module(module)


def summarize_capability(capability_name: str,
                         registry: Optional[CapabilityRegistry] = None) -> str:
    """
    Return a compact, human-readable summary of a capability's public API.

    Args:
        capability_name: The registered name of the capability.
        registry: Optional registry instance.

    Returns:
        A formatted string listing functions, signatures, and first-line docs.
    """
    endpoints = get_api(capability_name, registry)
    if not endpoints:
        return ""
    return _format_endpoints(capability_name, endpoints)


def summarize_module(module_path: str) -> str:
    """
    Return a compact summary of a module's public API by dotted import path.

    Args:
        module_path: Dotted Python import path.

    Returns:
        A formatted string, or empty string if module can't be imported.
    """
    endpoints = get_module_api(module_path)
    if not endpoints:
        return ""
    return _format_endpoints(module_path, endpoints)


def summarize_all(registry: Optional[CapabilityRegistry] = None) -> str:
    """
    Return a combined API summary for all registered capabilities.

    Args:
        registry: Optional registry instance.

    Returns:
        A single string with all capability APIs, separated by blank lines.
    """
    reg = registry or CapabilityRegistry()
    sections = []
    for cap in reg.list_active():
        summary = summarize_capability(cap.name, reg)
        if summary:
            sections.append(summary)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Kernel module scanning
# ---------------------------------------------------------------------------

# Kernel modules that generated code may need to interact with.
KERNEL_MODULES = [
    "src.kernel.capability_registry",
    "src.kernel.gap_detector",
    "src.kernel.model_interface",
    "src.kernel.self_modifier",
    "src.kernel.alignment_gates",
]


def summarize_kernel() -> str:
    """
    Return API summaries for all kernel modules that generated code
    might need to call.

    Returns:
        A single string with kernel module APIs.
    """
    sections = []
    for mod_path in KERNEL_MODULES:
        summary = summarize_module(mod_path)
        if summary:
            sections.append(summary)
    return "\n\n".join(sections)


def _scan_env_vars() -> str:
    """Scan capability modules for os.environ references and list known env vars.

    Rather than trying to parse source, we maintain a curated list of
    environment variables that Archi's capabilities use. This ensures
    generated code references the correct env var names.
    """
    env_vars = {
        "DISCORD_BOT_TOKEN": "Discord bot authentication token",
        "JESSE_DISCORD_ID": "Jesse's Discord user ID for DM targeting",
        "ARCHI_LOG_LEVEL": "Logging level override (default: INFO)",
    }
    # Also scan .env file for additional keys if it exists
    try:
        from pathlib import Path
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                if key and key not in env_vars:
                    env_vars[key] = "(from .env)"
    except Exception:
        pass
    if not env_vars:
        return ""
    lines = ["## Environment variables"]
    for key, desc in sorted(env_vars.items()):
        lines.append(f"  {key}  # {desc}")
    return "\n".join(lines)


def build_api_context(registry: Optional[CapabilityRegistry] = None) -> str:
    """
    Build the full API context string for injection into planner/codegen prompts.

    Combines kernel module APIs, registered capability APIs, and known
    environment variables into a single block that shows Archi (and the
    models it calls) what real functions exist, their exact signatures,
    and what they do.

    Args:
        registry: Optional registry instance.

    Returns:
        A formatted string ready for prompt injection.
    """
    parts = []
    kernel = summarize_kernel()
    if kernel:
        parts.append("# Kernel modules\n" + kernel)
    caps = summarize_all(registry)
    if caps:
        parts.append("# Registered capabilities\n" + caps)
    env = _scan_env_vars()
    if env:
        parts.append("# Environment\n" + env)
    if not parts:
        return ""
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Internal formatting
# ---------------------------------------------------------------------------

def _format_endpoints(label: str, endpoints: list[dict]) -> str:
    """Format a list of endpoint dicts into a compact readable block."""
    lines = [f"## {label}"]
    for ep in endpoints:
        if ep["is_class"]:
            kind = "class"
        elif ep["is_coroutine"]:
            kind = "async def"
        else:
            kind = "def"
        sig = ep["signature"] or "(…)"
        lines.append(f"  {kind} {ep['name']}{sig}")
        # Include only the first line of the docstring to keep it compact
        if ep["docstring"]:
            first_line = ep["docstring"].splitlines()[0].strip()
            if first_line:
                lines.append(f"    # {first_line}")
    return "\n".join(lines)
