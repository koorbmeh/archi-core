"""
Image analysis capability for Archi.

Downloads images from Discord attachment URLs, extracts text via OCR,
and uses vision-capable LLMs to identify capability gaps, summarize
conversations, or extract insights from screenshots.
"""

import base64
import io
import logging
import re
from pathlib import Path
from typing import Optional

import requests

from src.kernel.gap_detector import Gap
from src.kernel.model_interface import call_model, BudgetExceededError
from capabilities.discord_notifier import notify

logger = logging.getLogger(__name__)

# OCR backend selection — prefer easyocr, fall back to pytesseract, then skip
_OCR_BACKEND: Optional[str] = None

try:
    import easyocr
    _OCR_BACKEND = "easyocr"
    _EASYOCR_READER = None  # lazy-init to avoid slow startup
except ImportError:
    try:
        import pytesseract
        from PIL import Image
        _OCR_BACKEND = "pytesseract"
    except ImportError:
        logger.warning("No OCR backend available (easyocr or pytesseract+pillow required).")

# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────

_GAP_KEYWORDS = [
    "missing", "cannot", "can't", "unable", "not implemented",
    "no capability", "lacks", "needs", "should add", "gap",
    "todo", "fixme", "not supported",
]

_VISION_SYSTEM = (
    "You are Archi, an AI assistant that analyses images and screenshots. "
    "Identify any capability gaps, action items, conversation summaries, or "
    "technical insights visible in the image. Be concise and structured."
)


def _get_easyocr_reader():
    """Return a cached easyocr Reader instance."""
    global _EASYOCR_READER
    if _EASYOCR_READER is None:
        _EASYOCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _EASYOCR_READER


def _download_image(url: str, timeout: int = 15) -> Optional[bytes]:
    """Download raw image bytes from a URL."""
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        logger.error("Failed to download image from %s: %s", url, exc)
        return None


def _extract_text_easyocr(image_bytes: bytes) -> str:
    """Extract text from image bytes using easyocr."""
    reader = _get_easyocr_reader()
    results = reader.readtext(image_bytes, detail=0)
    return "\n".join(results)


def _extract_text_pytesseract(image_bytes: bytes) -> str:
    """Extract text from image bytes using pytesseract."""
    img = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(img)


def _ocr_image(image_bytes: bytes) -> str:
    """Run OCR on image bytes using whichever backend is available."""
    if _OCR_BACKEND == "easyocr":
        return _extract_text_easyocr(image_bytes)
    if _OCR_BACKEND == "pytesseract":
        return _extract_text_pytesseract(image_bytes)
    return ""


def _image_to_base64(image_bytes: bytes) -> str:
    """Encode image bytes as a base64 string."""
    return base64.b64encode(image_bytes).decode("utf-8")


def _build_vision_prompt(ocr_text: str, user_context: str = "") -> str:
    """Build a prompt for vision-model analysis."""
    parts = ["Please analyse this image."]
    if user_context:
        parts.append(f"User context: {user_context}")
    if ocr_text.strip():
        parts.append(f"OCR-extracted text from the image:\n```\n{ocr_text}\n```")
    parts.append(
        "Provide:\n"
        "1. A brief summary of what the image shows.\n"
        "2. Any capability gaps or missing features observed.\n"
        "3. Key insights or action items.\n"
        "Use plain, structured text."
    )
    return "\n\n".join(parts)


def _parse_gaps_from_analysis(analysis_text: str, source: str) -> list[Gap]:
    """Scan analysis output for capability gap patterns."""
    gaps: list[Gap] = []
    lines = analysis_text.splitlines()
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in _GAP_KEYWORDS):
            name = re.sub(r"[^a-z0-9_\s]", "", lower).strip()[:60]
            name = re.sub(r"\s+", "_", name) or "image_detected_gap"
            gap = Gap(
                name=name,
                source=source,
                reason=line.strip(),
                priority=0.5,
                evidence=[line.strip()],
                detail=f"Detected in image analysis from {source}",
            )
            gaps.append(gap)
    return gaps


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def analyse_image_url(
    url: str,
    user_context: str = "",
    source_label: str = "discord_attachment",
) -> dict:
    """
    Download an image, run OCR, call the LLM for analysis,
    and return a result dict with keys: url, ocr_text, analysis, gaps.
    """
    result = {"url": url, "ocr_text": "", "analysis": "", "gaps": []}

    image_bytes = _download_image(url)
    if image_bytes is None:
        logger.warning("Could not download image: %s", url)
        return result

    ocr_text = _ocr_image(image_bytes)
    result["ocr_text"] = ocr_text

    prompt = _build_vision_prompt(ocr_text, user_context)
    try:
        response = call_model(
            prompt=prompt,
            system=_VISION_SYSTEM,
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
        )
        analysis = response.text
    except BudgetExceededError:
        logger.warning("Budget exceeded during image analysis for %s", url)
        analysis = f"[Budget exceeded] OCR text: {ocr_text}"
    except Exception as exc:
        logger.error("LLM call failed during image analysis: %s", exc)
        analysis = f"[Error] {exc}"

    result["analysis"] = analysis
    result["gaps"] = _parse_gaps_from_analysis(analysis, source_label)
    return result


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
    source_label = f"discord_user_{user_id}" if user_id else "discord_attachment"
    result = analyse_image_url(url, user_context=user_context, source_label=source_label)

    logger.info(
        "Image analysis complete for %s — %d gap(s) detected.",
        url,
        len(result["gaps"]),
    )

    if result["gaps"]:
        for gap in result["gaps"]:
            logger.info("Gap detected from image: [%s] %s", gap.name, gap.reason)

    if notify_result and result["analysis"]:
        summary = result["analysis"][:800]
        gap_count = len(result["gaps"])
        message = (
            f"📷 **Image Analysis Result**\n{summary}"
            + (f"\n\n⚠️ {gap_count} potential gap(s) detected." if gap_count else "")
        )
        notify(message)

    return result


def process_multiple_attachments(
    urls: list[str],
    user_id: str = "",
    user_context: str = "",
    notify_result: bool = True,
) -> list[dict]:
    """
    Process a list of image attachment URLs from a Discord message.

    Returns a list of analysis result dicts.
    """
    results = []
    for url in urls:
        result = process_discord_attachment(
            url=url,
            user_id=user_id,
            user_context=user_context,
            notify_result=notify_result,
        )
        results.append(result)
    return results


def extract_ocr_text_from_url(url: str) -> str:
    """
    Lightweight helper: download an image and return only the OCR text.
    """
    image_bytes = _download_image(url)
    if image_bytes is None:
        return ""
    return _ocr_image(image_bytes)


def summarise_image_batch(results: list[dict]) -> str:
    """
    Produce a combined summary string from a list of analysis result dicts.
    """
    if not results:
        return "No images were analysed."

    lines = [f"Analysed {len(results)} image(s):\n"]
    all_gaps: list[Gap] = []

    for idx, res in enumerate(results, start=1):
        short_analysis = res.get("analysis", "")[:300]
        lines.append(f"**Image {idx}** ({res.get('url', 'unknown')}):")
        lines.append(short_analysis or "(no analysis)")
        all_gaps.extend(res.get("gaps", []))

    if all_gaps:
        lines.append(f"\n⚠️ Total gaps detected: {len(all_gaps)}")
        for gap in all_gaps[:5]:
            lines.append(f"  • {gap.name}: {gap.reason[:80]}")

    return "\n".join(lines)