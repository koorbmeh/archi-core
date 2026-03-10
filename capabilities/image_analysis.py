"""
Comprehensive image analysis capability for Archi.

Provides OCR text extraction, vision model interpretation, and automatic
detection/logging of capability gaps from screenshots or visual content
such as conversation logs. Integrates image_ocr, image_vision, gap_detector,
capability_registry, and discord_notifier to form a complete analysis pipeline.
"""

import logging
import re
from pathlib import Path
from typing import Any

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.gap_detector import Gap, detect_gaps
from src.kernel.model_interface import call_model, BudgetExceededError

from capabilities.image_ocr import extract_text
from capabilities.image_vision import analyse_image_with_vision
from capabilities.discord_notifier import notify

logger = logging.getLogger(__name__)

# Patterns that suggest a capability gap in extracted text
_GAP_PATTERNS = [
    re.compile(r"unregistered module[:\s]+(\w+)", re.IGNORECASE),
    re.compile(r"capability not found[:\s]+(\w+)", re.IGNORECASE),
    re.compile(r"missing capability[:\s]+(\w+)", re.IGNORECASE),
    re.compile(r"ModuleNotFoundError[:\s]+(\w+)", re.IGNORECASE),
    re.compile(r"no module named[:\s]+'?(\w+)'?", re.IGNORECASE),
    re.compile(r"ImportError[:\s]+(\w+)", re.IGNORECASE),
    re.compile(r"AttributeError.*'(\w+)' object has no attribute", re.IGNORECASE),
    re.compile(r"ERROR.*capability[:\s]+(\w+)", re.IGNORECASE),
]

_ANALYSIS_SYSTEM_PROMPT = (
    "You are Archi's image analysis engine. "
    "Given OCR-extracted text and vision analysis from an image, "
    "identify any error messages, missing modules, capability gaps, "
    "or operational issues. Be concise. Return a brief summary and "
    "a JSON-like list of detected gap names if any."
)


def _build_registry() -> CapabilityRegistry:
    """Instantiate the default capability registry."""
    return CapabilityRegistry()


def _extract_gap_names_from_text(text: str) -> list[str]:
    """Scan text for known gap indicator patterns; return unique gap names."""
    found: list[str] = []
    for pattern in _GAP_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if name and name not in found:
                found.append(name)
    return found


def _build_analysis_prompt(ocr_text: str, vision_summary: str, user_context: str) -> str:
    """Compose the LLM prompt from OCR text, vision summary, and user context."""
    parts = ["## Image Analysis Request\n"]
    if user_context:
        parts.append(f"**User context:** {user_context}\n")
    if ocr_text:
        parts.append(f"**OCR extracted text:**\n```\n{ocr_text[:3000]}\n```\n")
    if vision_summary:
        parts.append(f"**Vision model summary:**\n{vision_summary[:2000]}\n")
    parts.append(
        "Identify capability gaps, errors, or missing modules visible in the image. "
        "List detected gap names as: GAPS: gap1, gap2, ..."
    )
    return "\n".join(parts)


def _parse_gaps_from_llm(response_text: str, evidence: list[str], source: str) -> list[Gap]:
    """Extract Gap objects from LLM response text."""
    gaps: list[Gap] = []
    gap_line_match = re.search(r"GAPS:\s*(.+)", response_text, re.IGNORECASE)
    if not gap_line_match:
        return gaps
    raw_names = gap_line_match.group(1).strip()
    names = [n.strip() for n in raw_names.split(",") if n.strip() and n.strip().lower() != "none"]
    for name in names:
        gap = Gap(
            name=name,
            source=source,
            reason=f"Detected via image analysis of {source}",
            priority=0.7,
            evidence=evidence[:5],
            detail=response_text[:500],
        )
        gaps.append(gap)
    return gaps


def _notify_gaps(gaps: list[Gap], source_label: str) -> None:
    """Send a Discord notification summarising detected gaps."""
    if not gaps:
        return
    names = ", ".join(g.name for g in gaps)
    message = f"🔍 **Image Analysis — Gap Detected** (source: `{source_label}`)\nGaps found: `{names}`"
    try:
        notify(message)
    except Exception as exc:
        logger.warning("discord notify failed: %s", exc)


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
        "vision_summary": "",
        "llm_analysis": "",
        "gaps": [],
        "error": None,
    }

    try:
        ocr_text = extract_text(url)
        result["ocr_text"] = ocr_text
    except Exception as exc:
        logger.warning("OCR failed for %s: %s", url, exc)
        ocr_text = ""
        result["error"] = f"OCR error: {exc}"

    try:
        vision_data = analyse_image_with_vision(url, user_context=user_context, use_ocr=False)
        vision_summary = vision_data.get("analysis", "") or vision_data.get("summary", "")
        result["vision_summary"] = vision_summary
    except Exception as exc:
        logger.warning("Vision analysis failed for %s: %s", url, exc)
        vision_summary = ""
        if not result["error"]:
            result["error"] = f"Vision error: {exc}"

    prompt = _build_analysis_prompt(ocr_text, vision_summary, user_context)
    try:
        response = call_model(prompt, system=_ANALYSIS_SYSTEM_PROMPT)
        result["llm_analysis"] = response.text
    except BudgetExceededError:
        logger.warning("Budget exceeded during image analysis LLM call.")
        result["llm_analysis"] = ""
    except Exception as exc:
        logger.warning("LLM call failed during image analysis: %s", exc)
        result["llm_analysis"] = ""

    # Collect evidence for gap objects
    evidence: list[str] = []
    if ocr_text:
        evidence.append(f"OCR: {ocr_text[:200]}")
    if vision_summary:
        evidence.append(f"Vision: {vision_summary[:200]}")

    # Pattern-match gaps directly from OCR text
    pattern_gaps = _extract_gap_names_from_text(ocr_text)

    # LLM-derived gaps
    llm_gaps = _parse_gaps_from_llm(result["llm_analysis"], evidence, source_label)
    llm_gap_names = {g.name for g in llm_gaps}

    # Merge: add pattern gaps not already found by LLM
    all_gaps = list(llm_gaps)
    for name in pattern_gaps:
        if name not in llm_gap_names:
            all_gaps.append(
                Gap(
                    name=name,
                    source=source_label,
                    reason=f"Pattern match in OCR text from {source_label}",
                    priority=0.6,
                    evidence=evidence[:5],
                )
            )

    result["gaps"] = [
        {"name": g.name, "source": g.source, "reason": g.reason, "priority": g.priority}
        for g in all_gaps
    ]

    if all_gaps:
        _notify_gaps(all_gaps, source_label)

    return result


def extract_ocr_text_from_url(url: str) -> str:
    """Lightweight helper: download an image and return only the OCR text."""
    try:
        return extract_text(url)
    except Exception as exc:
        logger.warning("OCR extraction failed for %s: %s", url, exc)
        return ""


def process_discord_attachment(
    url: str,
    user_id: str = "",
    user_context: str = "",
    notify_result: bool = True,
) -> dict:
    """
    Full pipeline for a Discord image attachment: download → OCR → analyse → notify.
    Returns the analysis result dict.
    """
    source_label = f"discord_attachment:{user_id}" if user_id else "discord_attachment"
    result = analyse_image_url(url, user_context=user_context, source_label=source_label)

    if notify_result:
        summary_parts = []
        if result.get("llm_analysis"):
            summary_parts.append(result["llm_analysis"][:400])
        elif result.get("vision_summary"):
            summary_parts.append(result["vision_summary"][:400])
        elif result.get("ocr_text"):
            summary_parts.append(f"OCR: {result['ocr_text'][:300]}")

        if summary_parts:
            header = f"📷 **Image Analysis** (from `{user_id or 'unknown'}`)\n"
            try:
                notify(header + "\n".join(summary_parts))
            except Exception as exc:
                logger.warning("Failed to notify image analysis result: %s", exc)

    return result


def process_multiple_attachments(
    urls: list[str],
    user_id: str = "",
    user_context: str = "",
    notify_result: bool = True,
) -> list[dict]:
    """Process a list of image attachment URLs from a Discord message."""
    results: list[dict] = []
    for url in urls:
        result = process_discord_attachment(
            url,
            user_id=user_id,
            user_context=user_context,
            notify_result=False,
        )
        results.append(result)

    if notify_result and results:
        summary = summarise_image_batch(results)
        if summary:
            try:
                notify(f"📷 **Batch Image Analysis** ({len(results)} images)\n{summary[:800]}")
            except Exception as exc:
                logger.warning("Batch notify failed: %s", exc)

    return results


def summarise_image_batch(results: list[dict]) -> str:
    """Produce a combined summary string from a list of analysis result dicts."""
    if not results:
        return ""
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        url = r.get("url", "unknown")
        analysis = r.get("llm_analysis") or r.get("vision_summary") or r.get("ocr_text") or ""
        gaps = r.get("gaps", [])
        gap_str = ", ".join(g["name"] for g in gaps) if gaps else "none"
        snippet = analysis[:200].replace("\n", " ")
        lines.append(f"[{i}] {url}\n  Summary: {snippet}\n  Gaps: {gap_str}")
    return "\n\n".join(lines)


def register_capability(registry: CapabilityRegistry | None = None) -> Capability:
    """Register the image_analysis capability with the capability registry."""
    if registry is None:
        registry = _build_registry()
    cap = Capability(
        name="image_analysis",
        module="capabilities.image_analysis",
        description=(
            "Comprehensive image analysis: OCR text extraction, vision model interpretation, "
            "and automatic detection/logging of capability gaps from screenshots or visual content."
        ),
        status="active",
        dependencies=["image_ocr", "image_vision", "gap_detector", "discord_notifier"],
    )
    registry.register(cap)
    logger.info("image_analysis capability registered.")
    return cap