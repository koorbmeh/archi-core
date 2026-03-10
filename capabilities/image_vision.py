"""
capabilities/image_vision.py

Provides vision model integration to analyze and describe images from Discord
messages, generating contextual responses for Jesse. Downloads image URLs,
base64-encodes them, and queries vision-capable models (Claude-3.5-sonnet via
the Anthropic SDK) for detailed visual analysis. Integrates with Discord
workflows via discord_listener attachment URLs, combines vision output with OCR
from image_analysis, and sends responses via discord_notifier.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import anthropic
import requests

from capabilities.image_analysis import (
    analyse_image_url,
    extract_ocr_text_from_url,
    summarise_image_batch,
)
from capabilities.discord_notifier import notify
from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import call_model, BudgetExceededError

logger = logging.getLogger(__name__)

VISION_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1024
_CAPABILITY_NAME = "image_vision"


def _get_anthropic_client() -> anthropic.Anthropic:
    """Create and return an Anthropic client using ANTHROPIC_API_KEY."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set in the environment.")
    return anthropic.Anthropic(api_key=api_key)


def download_image_as_base64(url: str) -> tuple[str, str]:
    """
    Download an image from a URL and return (base64_data, media_type).

    Raises requests.HTTPError on download failure.
    """
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "image/png").split(";")[0].strip()
    b64_data = base64.standard_b64encode(response.content).decode("utf-8")
    return b64_data, content_type


def query_vision_model(
    image_url: str,
    user_context: str = "",
    ocr_text: str = "",
) -> str:
    """
    Query the Claude vision model with a base64-encoded image.

    Returns the model's text description, or an error message on failure.
    """
    prompt_parts = ["Please analyze this image in detail."]
    if user_context:
        prompt_parts.append(f"User context: {user_context}")
    if ocr_text:
        prompt_parts.append(f"OCR text already extracted from image: {ocr_text}")
    prompt_parts.append("Provide a thorough, contextual description useful for Jesse.")

    try:
        b64_data, media_type = download_image_as_base64(image_url)
    except Exception as exc:
        logger.warning("Failed to download image for vision query: %s", exc)
        return f"[Vision: Could not download image — {exc}]"

    client = _get_anthropic_client()
    try:
        message = client.messages.create(
            model=VISION_MODEL,
            max_tokens=DEFAULT_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_data,
                            },
                        },
                        {"type": "text", "text": "\n".join(prompt_parts)},
                    ],
                }
            ],
        )
        return message.content[0].text if message.content else "[Vision: Empty response]"
    except anthropic.APIError as exc:
        logger.error("Anthropic vision API error: %s", exc)
        return f"[Vision API error: {exc}]"


def analyse_image_with_vision(
    url: str,
    user_context: str = "",
    use_ocr: bool = True,
) -> dict[str, Any]:
    """
    Full vision pipeline: OCR + Claude vision analysis for a single image URL.

    Returns a dict with keys: url, ocr_text, vision_description, combined_summary.
    """
    ocr_text = ""
    if use_ocr:
        try:
            ocr_text = extract_ocr_text_from_url(url)
        except Exception as exc:
            logger.warning("OCR extraction failed for %s: %s", url, exc)

    vision_description = query_vision_model(url, user_context=user_context, ocr_text=ocr_text)

    combined_parts = []
    if vision_description:
        combined_parts.append(f"Vision: {vision_description}")
    if ocr_text:
        combined_parts.append(f"OCR text: {ocr_text}")
    combined_summary = "\n\n".join(combined_parts)

    return {
        "url": url,
        "ocr_text": ocr_text,
        "vision_description": vision_description,
        "combined_summary": combined_summary,
    }


def process_discord_image_message(
    attachment_urls: list[str],
    user_context: str = "",
    notify_result: bool = True,
) -> list[dict[str, Any]]:
    """
    Process a list of image attachment URLs from a Discord message.

    Runs vision + OCR analysis on each, optionally sends a Discord notification.
    Returns a list of result dicts.
    """
    results = []
    for url in attachment_urls:
        logger.info("Processing image attachment via vision: %s", url)
        try:
            result = analyse_image_with_vision(url, user_context=user_context)
            results.append(result)
        except Exception as exc:
            logger.error("Vision analysis failed for %s: %s", url, exc)
            results.append({"url": url, "error": str(exc), "combined_summary": ""})

    if notify_result and results:
        summary = _build_notification_summary(results)
        try:
            notify(summary)
        except Exception as exc:
            logger.error("Failed to send Discord notification for vision results: %s", exc)

    return results


def _build_notification_summary(results: list[dict[str, Any]]) -> str:
    """Build a human-readable summary string from vision result dicts."""
    lines = [f"🖼️ Analyzed {len(results)} image(s):"]
    for i, r in enumerate(results, 1):
        if r.get("error"):
            lines.append(f"[Image {i}] Error: {r['error']}")
        else:
            vision_desc = r.get("vision_description", "")
            preview = vision_desc[:300] + "…" if len(vision_desc) > 300 else vision_desc
            lines.append(f"[Image {i}] {preview}")
    return "\n".join(lines)


def generate_contextual_response(
    vision_results: list[dict[str, Any]],
    user_message: str = "",
) -> str:
    """
    Use the text model to generate a final contextual response for Jesse
    combining vision analysis results and the user's message.
    """
    summaries = [r.get("combined_summary", "") for r in vision_results if r.get("combined_summary")]
    combined = "\n\n---\n\n".join(summaries)

    prompt = (
        f"Jesse sent the following image(s). Vision analysis results:\n\n{combined}\n\n"
        f"User message (if any): {user_message}\n\n"
        "Respond helpfully and contextually as Archi."
    )
    try:
        response = call_model(prompt, system="You are Archi, Jesse's AI assistant.")
        return response.text
    except BudgetExceededError:
        logger.warning("Budget exceeded when generating contextual response.")
        return combined or "[Vision analysis complete — budget exceeded for further response.]"
    except Exception as exc:
        logger.error("Model call failed for contextual response: %s", exc)
        return combined or f"[Error generating response: {exc}]"


def handle_discord_vision_request(
    attachment_urls: list[str],
    user_message: str = "",
    user_context: str = "",
) -> str:
    """
    Top-level handler: process images, generate response, notify Jesse via Discord.

    Returns the final response text.
    """
    if not attachment_urls:
        return "[image_vision] No attachment URLs provided."

    results = process_discord_image_message(
        attachment_urls,
        user_context=user_context or user_message,
        notify_result=False,
    )

    response_text = generate_contextual_response(results, user_message=user_message)

    try:
        notify(response_text)
    except Exception as exc:
        logger.error("Failed to notify Jesse with vision response: %s", exc)

    return response_text


def register_capability(
    registry: CapabilityRegistry | None = None,
) -> Capability:
    """Register the image_vision capability with the capability registry."""
    if registry is None:
        registry = CapabilityRegistry()

    cap = Capability(
        name=_CAPABILITY_NAME,
        module="capabilities.image_vision",
        description=(
            "Vision model integration to analyze and describe images from Discord "
            "messages using Claude-3.5-sonnet. Combines OCR and vision analysis to "
            "generate contextual responses for Jesse."
        ),
        status="active",
        dependencies=["image_analysis", "discord_notifier", "model_interface"],
        metadata={"vision_model": VISION_MODEL},
    )
    registry.register(cap)
    logger.info("Registered capability: %s", _CAPABILITY_NAME)
    return cap


# Auto-register on import
try:
    register_capability()
except Exception as _reg_exc:
    logger.debug("Auto-registration of image_vision skipped: %s", _reg_exc)