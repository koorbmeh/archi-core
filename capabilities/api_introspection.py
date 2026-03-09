"""
Module for introspecting available functions, signatures, and docstrings
in registered capabilities and modules before generating dependent code.

Uses Python's inspect module to dynamically scan modules listed in the
capability_registry and extract callable members with their signatures.
Provides caching to avoid repeated introspection during planning and
code generation phases.
"""

import inspect
import logging
from functools import lru_cache
from typing import Any

from capabilities import capability_registry

logger = logging.getLogger(__name__)


def _extract_callable_info(name: str, obj: Any) -> dict:
    """Extract signature and docstring information from a callable object."""
    info = {
        "name": name,
        "qualname": getattr(obj, "__qualname__", name),
        "docstring": inspect.getdoc(obj) or "",
        "signature": None,
        "parameters": {},
        "return_annotation": None,
        "is_coroutine": inspect.iscoroutinefunction(obj),
        "is_class": inspect.isclass(obj),
    }
    try:
        sig = inspect.signature(obj)
        info["signature"] = str(sig)
        info["parameters"] = {
            param_name: {
                "kind": str(param.kind),
                "default": (
                    None
                    if param.default is inspect.Parameter.empty
                    else repr(param.default)
                ),
                "annotation": (
                    None
                    if param.annotation is inspect.Parameter.empty
                    else str(param.annotation)
                ),
            }
            for param_name, param in sig.parameters.items()
        }
        info["return_annotation"] = (
            None
            if sig.return_annotation is inspect.Parameter.empty
            else str(sig.return_annotation)
        )
    except (ValueError, TypeError) as exc:
        logger.debug("Could not extract signature for %s: %s", name, exc)
    return info


def _scan_module(module: Any) -> list[dict]:
    """Scan a module and return a list of callable member info dicts."""
    members = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if callable(obj) or inspect.isclass(obj):
            try:
                source_module = getattr(obj, "__module__", None)
                module_name = getattr(module, "__name__", None)
                if source_module and module_name and not source_module.startswith(module_name):
                    continue
            except Exception:
                pass
            info = _extract_callable_info(name, obj)
            members.append(info)
    return members


def _load_module_for_capability(capability_name: str) -> Any | None:
    """Retrieve the module object associated with a capability name."""
    registry = capability_registry.get_registry()
    entry = registry.get(capability_name)
    if entry is None:
        logger.warning("Capability '%s' not found in registry.", capability_name)
        return None
    module = getattr(entry, "module", None)
    if module is None:
        module_path = getattr(entry, "module_path", None) or (
            entry if isinstance(entry, str) else None
        )
        if module_path:
            import importlib
            try:
                module = importlib.import_module(module_path)
            except ImportError as exc:
                logger.error(
                    "Could not import module '%s' for capability '%s': %s",
                    module_path,
                    capability_name,
                    exc,
                )
                return None
    return module


@lru_cache(maxsize=128)
def get_api(capability_name: str) -> list[dict]:
    """
    Return a structured list of available endpoints for a named capability.

    Each entry in the list is a dict with keys: name, qualname, docstring,
    signature, parameters, return_annotation, is_coroutine, is_class.

    Args:
        capability_name: The registered name of the capability to introspect.

    Returns:
        A list of callable info dicts, or an empty list if not found.
    """
    logger.debug("Introspecting capability: %s", capability_name)
    module = _load_module_for_capability(capability_name)
    if module is None:
        return []
    return _scan_module(module)


def get_global_api() -> dict[str, list[dict]]:
    """
    Return introspection data for all registered capabilities.

    Returns:
        A dict mapping each capability name to its list of callable info dicts.
    """
    registry = capability_registry.get_registry()
    result = {}
    for capability_name in registry:
        result[capability_name] = get_api(capability_name)
    return result


def invalidate_cache(capability_name: str | None = None) -> None:
    """
    Invalidate cached introspection data.

    Args:
        capability_name: If provided, invalidate only this capability's cache.
                         If None, invalidate the entire cache.
    """
    if capability_name is not None:
        logger.debug("Invalidating cache for capability: %s", capability_name)
        cache_info = get_api.cache_info()
        logger.debug("Cache info before invalidation: %s", cache_info)
    get_api.cache_clear()
    logger.debug("API introspection cache cleared.")


def summarize_api(capability_name: str) -> str:
    """
    Return a human-readable summary of a capability's public API.

    Args:
        capability_name: The registered name of the capability.

    Returns:
        A formatted string listing functions, signatures, and docstrings.
    """
    endpoints = get_api(capability_name)
    if not endpoints:
        return f"No API information available for capability '{capability_name}'."
    lines = [f"API for capability: {capability_name}", "=" * 60]
    for ep in endpoints:
        kind = "class" if ep["is_coroutine"] else ("async def" if ep["is_coroutine"] else "def")
        if ep["is_class"]:
            kind = "class"
        elif ep["is_coroutine"]:
            kind = "async def"
        else:
            kind = "def"
        sig = ep["signature"] or "(unknown)"
        lines.append(f"\n{kind} {ep['name']}{sig}")
        if ep["return_annotation"]:
            lines.append(f"  -> {ep['return_annotation']}")
        if ep["docstring"]:
            for doc_line in ep["docstring"].splitlines():
                lines.append(f"    {doc_line}")
    return "\n".join(lines)


def get_capability_names() -> list[str]:
    """
    Return a list of all registered capability names.

    Returns:
        A list of capability name strings from the registry.
    """
    registry = capability_registry.get_registry()
    return list(registry.keys())


def find_callable(
    capability_name: str,
    callable_name: str,
) -> dict | None:
    """
    Find a specific callable by name within a capability's API.

    Args:
        capability_name: The registered name of the capability.
        callable_name: The name of the callable to find.

    Returns:
        The callable info dict if found, otherwise None.
    """
    endpoints = get_api(capability_name)
    for ep in endpoints:
        if ep["name"] == callable_name:
            return ep
    logger.debug(
        "Callable '%s' not found in capability '%s'.",
        callable_name,
        capability_name,
    )
    return None