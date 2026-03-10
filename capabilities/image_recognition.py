"""
Unified image recognition capability combining OCR text extraction and vision-based
semantic interpretation for processing screenshots and conversation images from Discord.

Integrates image_ocr and image_vision to produce structured recognition results,
and provides Discord-oriented handlers that mirror existing pipeline patterns.
"""

import logging
from typing import Any

from capabilities import image_ocr
from capabilities import image_vision
from capabilities import image_analysis
from capabilities.discord_notifier import notify
from src.kernel.capability_registry import Capability, CapabilityRegistry

logger = logging.getLogger(__name__)

_CAPABILITY_NAME = "image_recognition"
_CAPABILITY_MODULE = "capabilities.image_recognition"
_CAPABILITY_DESCRIPTION = (
    "Unified image recognition combining OCR text extraction and vision-based "
    "semantic interpretation for Discord screenshots and conversation images."
)
_DEPENDENCIES = ["image_ocr", "image_vision", "image_analysis"]


def recognize_image(url: str, context: str = "") -> dict[str, Any]:
    """
    Recognise a single image by combining OCR text extraction and vision analysis.

    Returns a structured dict with keys: url, text, description, entities, error.
    """
    result: dict[str, Any] = {
        "url": url,
        "text": "",
        "description": "",
        "entities": [],
        "error": None,
    }

    try:
        ocr_text = image_ocr.extract_text(url)
        result["text"] = ocr_text or ""
    except Exception as exc:
        logger.warning("OCR extraction failed for %s: %s", url, exc)
        result["text"] = ""

    try:
        vision_result = image_vision.analyse_image_with_vision(
            url, user_context=context, use_ocr=False
        )
        result["description"] = vision_result.get("description", "") or vision_result.get("analysis", "")
        result["entities"] = _extract_entities(vision_result)
    except Exception as exc:
        logger.warning("Vision analysis failed for %s: %s", url, exc)
        result["error"] = str(exc)

    return result


def _extract_entities(vision_result: dict[str, Any]) -> list[str]:
    """Extract entity mentions from a vision result dict."""
    entities = vision_result.get("entities", [])
    if isinstance(entities, list):
        return [str(e) for e in entities]
    tags = vision_result.get("tags", [])
    if isinstance(tags, list):
        return [str(t) for t in tags]
    return []


def recognize_images_batch(urls: list[str], context: str = "") -> list[dict[str, Any]]:
    """Recognise multiple images, returning a list of structured result dicts."""
    results = []
    for url in urls:
        results.append(recognize_image(url, context=context))
    return results


def process_discord_image(urls: list[str], user_id: str) -> list[dict[str, Any]]:
    """
    Discord integration handler: recognise a list of image URLs and notify via
    discord_notifier with a summary of the results.

    Mirrors the pattern used by image_analysis.process_multiple_attachments.
    """
    if not urls:
        logger.info("process_discord_image called with empty URL list for user %s", user_id)
        return []

    context = f"Discord image from user {user_id}"
    results = recognize_images_batch(urls, context=context)

    summary_lines = []
    for idx, res in enumerate(results, start=1):
        text_snippet = (res["text"] or "")[:120].replace("\n", " ")
        desc_snippet = (res["description"] or "")[:200].replace("\n", " ")
        parts = [f"Image {idx}:"]
        if text_snippet:
            parts.append(f"OCR: {text_snippet}")
        if desc_snippet:
            parts.append(f"Vision: {desc_snippet}")
        if res.get("error"):
            parts.append(f"[error: {res['error']}]")
        summary_lines.append(" | ".join(parts))

    summary = "\n".join(summary_lines) if summary_lines else "No image results."

    try:
        notify(f"Image recognition complete for user {user_id}:\n{summary}")
    except Exception as exc:
        logger.warning("discord_notifier.notify failed: %s", exc)

    return results


def register_capability(registry: CapabilityRegistry | None = None) -> Capability:
    """Register the image_recognition capability with the capability registry."""
    if registry is None:
        registry = CapabilityRegistry()

    capability = Capability(
        name=_CAPABILITY_NAME,
        module=_CAPABILITY_MODULE,
        description=_CAPABILITY_DESCRIPTION,
        status="active",
        dependencies=_DEPENDENCIES,
        metadata={},
    )

    registry.register(capability)
    logger.info("Registered capability: %s", _CAPABILITY_NAME)
    return capability


# Auto-register on import
try:
    _registry = CapabilityRegistry()
    register_capability(_registry)
except Exception as _reg_exc:
    logger.debug("Auto-registration of %s skipped: %s", _CAPABILITY_NAME, _reg_exc)