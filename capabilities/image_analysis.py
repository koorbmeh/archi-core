"""
capabilities/image_analysis.py

Comprehensive image analysis for Discord attachments, including OCR text extraction,
vision-based description, and detection/logging of capability gaps from screenshots
of errors, conversations, or logs.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.gap_detector import Gap
from src.kernel.model_interface import call_model, BudgetExceededError

from capabilities.image_ocr import extract_text
from capabilities.image_vision import analyse_image_with_vision
from capabilities.discord_notifier import notify
from capabilities.conversational_memory import store_message, get_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gap indicator patterns to scan for in OCR / vision text
# ---------------------------------------------------------------------------
_GAP_PATTERNS = [
    re.compile(r"missing capability", re.IGNORECASE),
    re.compile(r"not (yet )?implemented", re.IGNORECASE),
    re.compile(r"capability gap", re.IGNORECASE),
    re.compile(r"TODO|FIXME|HACK", re.IGNORECASE),
    re.compile(r"AttributeError|ImportError|ModuleNotFoundError", re.IGNORECASE),
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"error:|exception:|fatal:", re.IGNORECASE),
    re.compile(r"failed to (load|import|find|execute)", re.IGNORECASE),
]

_SYSTEM_PROMPT = (
    "You are Archi's image analysis module. "
    "Scan the provided image description and OCR text for capability gaps, errors, "
    "missing features, or actionable insights. "
    "Return a concise JSON object with keys: "
    "'summary' (str), 'gaps' (list of str), 'errors' (list of str), 'priority' (float 0-1)."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _scan_text_for_gaps(text: str) -> list[str]:
    """Return a list of matching gap-indicator snippets found in *text*."""
    found: list[str] = []
    for pattern in _GAP_PATTERNS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 40)
            found.append(text[start:end].strip())
    return found


def _build_analysis_prompt(ocr_text: str, vision_description: str, user_context: str) -> str:
    parts = ["## Image Analysis Request\n"]
    if user_context:
        parts.append(f"**User context:** {user_context}\n")
    if ocr_text:
        parts.append(f"**OCR text extracted from image:**\n```\n{ocr_text[:3000]}\n```\n")
    if vision_description:
        parts.append(f"**Vision model description:**\n{vision_description[:2000]}\n")
    parts.append(
        "\nIdentify any capability gaps, errors, or missing features. "
        "Respond with the JSON structure described in the system prompt."
    )
    return "\n".join(parts)


def _create_gap_from_indicators(
    indicators: list[str],
    source_label: str,
    ocr_text: str,
    vision_description: str,
    priority: float,
) -> Gap:
    """Build a Gap instance from detected indicators."""
    name = f"visual_gap_{source_label}"
    reason = f"Gap indicators detected in image from {source_label}"
    evidence = indicators[:10]
    detail = f"OCR snippet: {ocr_text[:300]}\nVision: {vision_description[:300]}"
    return Gap(
        name=name,
        source=source_label,
        reason=reason,
        priority=priority,
        evidence=evidence,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_image_url(
    url: str,
    user_context: str = "",
    source_label: str = "discord_attachment",
) -> dict:
    """Download an image, run OCR, call the LLM for analysis,
    detect capability gaps, and return a structured result dict.
    """
    result: dict[str, Any] = {
        "url": url,
        "source_label": source_label,
        "ocr_text": "",
        "vision_description": "",
        "llm_analysis": {},
        "gaps": [],
        "errors": [],
        "gap_objects": [],
        "priority": 0.0,
        "summary": "",
        "error": None,
    }

    # Step 1: OCR
    try:
        ocr_text = extract_text(url)
        result["ocr_text"] = ocr_text
    except Exception as exc:
        logger.warning("OCR failed for %s: %s", url, exc)
        ocr_text = ""
        result["errors"].append(f"OCR error: {exc}")

    # Step 2: Vision description
    try:
        vision_data = analyse_image_with_vision(url, user_context=user_context, use_ocr=False)
        vision_description = vision_data.get("description", "") or vision_data.get("analysis", "")
        result["vision_description"] = vision_description
    except Exception as exc:
        logger.warning("Vision analysis failed for %s: %s", url, exc)
        vision_description = ""
        result["errors"].append(f"Vision error: {exc}")

    # Step 3: LLM gap scan
    prompt = _build_analysis_prompt(ocr_text, vision_description, user_context)
    try:
        response = call_model(prompt, system=_SYSTEM_PROMPT)
        import json
        raw = response.text.strip()
        # Try to extract JSON block if wrapped in markdown
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            llm_analysis = json.loads(json_match.group())
        else:
            llm_analysis = {"summary": raw, "gaps": [], "errors": [], "priority": 0.0}
        result["llm_analysis"] = llm_analysis
        result["summary"] = llm_analysis.get("summary", "")
        result["gaps"] = llm_analysis.get("gaps", [])
        result["priority"] = float(llm_analysis.get("priority", 0.0))
    except BudgetExceededError:
        logger.warning("Budget exceeded during image LLM analysis.")
        result["errors"].append("Budget exceeded")
    except Exception as exc:
        logger.warning("LLM analysis failed: %s", exc)
        result["errors"].append(f"LLM error: {exc}")

    # Step 4: Pattern-based gap detection on combined text
    combined_text = f"{ocr_text}\n{vision_description}"
    pattern_indicators = _scan_text_for_gaps(combined_text)
    all_gap_texts = list(result["gaps"]) + pattern_indicators

    if all_gap_texts:
        priority = result["priority"] or min(0.8, 0.3 + 0.1 * len(all_gap_texts))
        gap_obj = _create_gap_from_indicators(
            all_gap_texts, source_label, ocr_text, vision_description, priority
        )
        result["gap_objects"].append(gap_obj)
        logger.info("Gap detected from image %s: %s", url, gap_obj.name)

    return result


def extract_ocr_text_from_url(url: str) -> str:
    """Lightweight helper: download an image and return only the OCR text."""
    try:
        return extract_text(url)
    except Exception as exc:
        logger.warning("extract_ocr_text_from_url failed for %s: %s", url, exc)
        return ""


def process_discord_attachment(
    url: str,
    user_id: str = "",
    user_context: str = "",
    notify_result: bool = True,
) -> dict:
    """Full pipeline for a Discord image attachment: download → OCR → analyse → notify."""
    conv_context = ""
    if user_id:
        try:
            conv_context = get_context(user_id)
        except Exception:
            pass

    combined_context = "\n".join(filter(None, [conv_context, user_context]))
    result = analyse_image_url(url, user_context=combined_context, source_label="discord_attachment")

    # Store in conversational memory
    if user_id and result.get("summary"):
        try:
            store_message(user_id, f"[Image analysis] {result['summary']}", role="assistant")
        except Exception as exc:
            logger.warning("Failed to store image analysis in memory: %s", exc)

    # Notify gaps
    if notify_result:
        gap_objects: list[Gap] = result.get("gap_objects", [])
        for gap in gap_objects:
            try:
                notify(
                    f"🔍 **Capability gap detected** from image analysis\n"
                    f"**Name:** {gap.name}\n"
                    f"**Reason:** {gap.reason}\n"
                    f"**Priority:** {gap.priority:.2f}\n"
                    f"**Evidence:** {'; '.join(gap.evidence[:3])}"
                )
            except Exception as exc:
                logger.warning("Discord notification failed: %s", exc)

        if result.get("summary"):
            try:
                notify(f"🖼️ **Image Analysis**\n{result['summary']}")
            except Exception as exc:
                logger.warning("Discord notify summary failed: %s", exc)

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
        try:
            res = process_discord_attachment(
                url,
                user_id=user_id,
                user_context=user_context,
                notify_result=False,  # batch notify at end
            )
            results.append(res)
        except Exception as exc:
            logger.error("Failed to process attachment %s: %s", url, exc)
            results.append({"url": url, "error": str(exc), "summary": "", "gaps": [], "gap_objects": []})

    if notify_result and results:
        summary_text = summarise_image_batch(results)
        try:
            notify(f"🖼️ **Batch Image Analysis ({len(results)} image(s))**\n{summary_text}")
        except Exception as exc:
            logger.warning("Batch notify failed: %s", exc)

    return results


def summarise_image_batch(results: list[dict]) -> str:
    """Produce a combined summary string from a list of analysis result dicts."""
    if not results:
        return "No images analysed."

    lines: list[str] = []
    total_gaps = 0
    for i, res in enumerate(results, 1):
        summary = res.get("summary", "").strip() or "(no summary)"
        gap_count = len(res.get("gap_objects", []))
        total_gaps += gap_count
        error = res.get("error", "")
        status = f"⚠️ Error: {error}" if error else f"✅ {summary}"
        gap_note = f" | {gap_count} gap(s) detected" if gap_count else ""
        lines.append(f"{i}. {status}{gap_note}")

    lines.append(f"\n**Total gaps detected: {total_gaps}**")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

def register_capability(registry: CapabilityRegistry | None = None) -> Capability:
    """Register the image_analysis capability with the capability registry."""
    cap = Capability(
        name="image_analysis",
        module="capabilities.image_analysis",
        description=(
            "Comprehensive image analysis for Discord attachments: OCR, vision description, "
            "gap detection from screenshots of errors and logs."
        ),
        status="active",
        dependencies=["image_ocr", "image_vision", "model_interface", "discord_notifier", "conversational_memory"],
        metadata={"version": "1.0.0"},
    )
    if registry is not None:
        try:
            registry.register(cap)
            logger.info("image_analysis capability registered.")
        except Exception as exc:
            logger.warning("Could not register image_analysis capability: %s", exc)
    return cap