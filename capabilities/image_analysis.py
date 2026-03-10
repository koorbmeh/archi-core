"""
capabilities/image_analysis.py

Comprehensive image analysis module providing OCR text extraction, vision-based
description, and detection of Archi capability gaps from screenshots or visual
error logs in Discord attachments.

Integrates with image_ocr, image_vision, gap_detector, discord_notifier, and
conversational_memory to process Discord attachments automatically and report
results or newly detected gaps back to the user.
"""

import logging
import re
from typing import Any

from capabilities import image_ocr
from capabilities import image_vision
from capabilities import discord_notifier
from capabilities import conversational_memory
from src.kernel import gap_detector as gd
from src.kernel.capability_registry import CapabilityRegistry
from src.kernel.model_interface import call_model, BudgetExceededError

logger = logging.getLogger(__name__)

_GAP_PATTERNS = [
    r"capability\s+['\"]?(\w+)['\"]?\s+not\s+(found|registered|available)",
    r"unregistered\s+capability[:\s]+(\w+)",
    r"missing\s+capability[:\s]+(\w+)",
    r"no\s+module\s+named\s+['\"]capabilities\.(\w+)['\"]",
    r"AttributeError.*capabilities\.(\w+)",
    r"ImportError.*capabilities\.(\w+)",
    r"KeyError[:\s]+['\"]?capabilities[./](\w+)['\"]?",
    r"capability\s+gap[:\s]+(\w+)",
]

_ANALYSIS_SYSTEM_PROMPT = (
    "You are Archi's image analysis engine. Examine the provided OCR text and "
    "image description. Identify: (1) any error messages or stack traces, "
    "(2) references to missing or broken capabilities, (3) the overall context "
    "of what the image shows. Be concise and structured."
)


def _build_analysis_prompt(ocr_text: str, vision_description: str, user_context: str) -> str:
    parts = []
    if user_context:
        parts.append(f"User context: {user_context}")
    if ocr_text.strip():
        parts.append(f"OCR extracted text:\n{ocr_text}")
    if vision_description.strip():
        parts.append(f"Vision model description:\n{vision_description}")
    parts.append(
        "Based on the above, provide a structured analysis. "
        "Note any error messages, missing capabilities, or actionable findings."
    )
    return "\n\n".join(parts)


def _scan_for_gap_patterns(text: str) -> list[str]:
    """Scan text for patterns that suggest missing capabilities."""
    found: list[str] = []
    combined = text.lower()
    for pattern in _GAP_PATTERNS:
        for match in re.finditer(pattern, combined, re.IGNORECASE):
            candidate = match.group(1) if match.lastindex else match.group(0)
            if candidate and candidate not in found:
                found.append(candidate)
    return found


def _detect_gaps_from_text(text: str, source_label: str) -> list[gd.Gap]:
    """Create Gap objects from patterns found in text."""
    candidates = _scan_for_gap_patterns(text)
    gaps: list[gd.Gap] = []
    for name in candidates:
        gap = gd.Gap(
            name=name,
            source=source_label,
            reason=f"Referenced as missing/unavailable in image analysis of {source_label}",
            priority=0.7,
            evidence=[text[:500]],
        )
        gaps.append(gap)
    return gaps


def extract_ocr_text_from_url(url: str) -> str:
    """Lightweight helper: download an image and return only the OCR text."""
    try:
        return image_ocr.extract_text(url)
    except Exception as exc:
        logger.warning("OCR extraction failed for %s: %s", url, exc)
        return ""


def analyse_image_url(
    url: str,
    user_context: str = "",
    source_label: str = "discord_attachment",
) -> dict:
    """
    Download an image, run OCR, call the LLM for analysis,
    detect capability gaps, and return a structured result dict.
    """
    result: dict[str, Any] = {
        "url": url,
        "source_label": source_label,
        "ocr_text": "",
        "vision_description": "",
        "llm_analysis": "",
        "detected_gaps": [],
        "error": None,
    }

    ocr_text = extract_ocr_text_from_url(url)
    result["ocr_text"] = ocr_text

    try:
        vision_data = image_vision.analyse_image_with_vision(
            url, user_context=user_context, use_ocr=False
        )
        result["vision_description"] = vision_data.get("description", "")
    except Exception as exc:
        logger.warning("Vision analysis failed for %s: %s", url, exc)
        result["vision_description"] = ""

    combined_text = f"{ocr_text}\n{result['vision_description']}"

    try:
        prompt = _build_analysis_prompt(ocr_text, result["vision_description"], user_context)
        response = call_model(prompt, system=_ANALYSIS_SYSTEM_PROMPT)
        result["llm_analysis"] = response.text
        combined_text += f"\n{response.text}"
    except BudgetExceededError:
        logger.warning("Budget exceeded during LLM analysis for %s", url)
        result["llm_analysis"] = "(Budget exceeded — LLM analysis skipped)"
    except Exception as exc:
        logger.warning("LLM analysis failed for %s: %s", url, exc)
        result["llm_analysis"] = ""

    gaps = _detect_gaps_from_text(combined_text, source_label)
    result["detected_gaps"] = [
        {"name": g.name, "reason": g.reason, "priority": g.priority}
        for g in gaps
    ]

    if gaps:
        logger.info("Detected %d gap(s) from image %s: %s", len(gaps), url, [g.name for g in gaps])

    return result


def process_discord_attachment(
    url: str,
    user_id: str = "",
    user_context: str = "",
    notify_result: bool = True,
) -> dict:
    """
    Full pipeline for a Discord image attachment: download → OCR → analyse → notify.
    Stores analysis in conversational memory and sends a Discord notification.
    """
    result = analyse_image_url(url, user_context=user_context, source_label=f"discord:{user_id or 'unknown'}")

    summary_parts = [f"**Image Analysis** for `{url}`"]
    if result["ocr_text"].strip():
        excerpt = result["ocr_text"][:300].replace("\n", " ")
        summary_parts.append(f"OCR text: {excerpt}{'...' if len(result['ocr_text']) > 300 else ''}")
    if result["llm_analysis"].strip():
        summary_parts.append(f"Analysis: {result['llm_analysis'][:400]}")
    if result["detected_gaps"]:
        gap_names = ", ".join(g["name"] for g in result["detected_gaps"])
        summary_parts.append(f"⚠️ Capability gaps detected: {gap_names}")

    summary = "\n".join(summary_parts)

    if user_id:
        try:
            conversational_memory.store_message(user_id, summary, role="assistant")
        except Exception as exc:
            logger.warning("Failed to store analysis in conversational memory: %s", exc)

    if notify_result:
        try:
            discord_notifier.notify(summary)
        except Exception as exc:
            logger.warning("Discord notification failed: %s", exc)

    return result


def process_multiple_attachments(
    urls: list[str],
    user_id: str = "",
    user_context: str = "",
    notify_result: bool = True,
) -> list[dict]:
    """Process a list of image attachment URLs from a Discord message."""
    results = []
    for url in urls:
        try:
            result = process_discord_attachment(
                url,
                user_id=user_id,
                user_context=user_context,
                notify_result=False,
            )
            results.append(result)
        except Exception as exc:
            logger.error("Failed to process attachment %s: %s", url, exc)
            results.append({"url": url, "error": str(exc), "detected_gaps": []})

    if notify_result and results:
        batch_summary = summarise_image_batch(results)
        try:
            discord_notifier.notify(batch_summary)
        except Exception as exc:
            logger.warning("Batch Discord notification failed: %s", exc)

    return results


def summarise_image_batch(results: list[dict]) -> str:
    """Produce a combined summary string from a list of analysis result dicts."""
    if not results:
        return "No images were processed."

    lines = [f"**Batch Image Analysis** — {len(results)} image(s) processed"]
    all_gaps: list[str] = []

    for i, r in enumerate(results, 1):
        url_short = r.get("url", "unknown")[-60:]
        error = r.get("error")
        if error:
            lines.append(f"{i}. `{url_short}` — ❌ Error: {error}")
            continue
        analysis_excerpt = (r.get("llm_analysis") or r.get("ocr_text") or "")[:200]
        lines.append(f"{i}. `{url_short}` — {analysis_excerpt}")
        for gap in r.get("detected_gaps", []):
            all_gaps.append(gap["name"])

    if all_gaps:
        unique_gaps = list(dict.fromkeys(all_gaps))
        lines.append(f"\n⚠️ Capability gaps detected across batch: {', '.join(unique_gaps)}")

    return "\n".join(lines)