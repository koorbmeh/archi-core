"""
capabilities/image_ocr.py

Dedicated capability for performing optical character recognition (OCR) on images
from URLs to extract readable text. Provides functions for single-image extraction,
batch processing for Discord attachments, and optional language support.

Integrates with image_analysis and image_vision as a callable OCR utility layer.
"""

import io
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Optional OCR backend imports — gracefully degrade if neither is installed
try:
    import pytesseract
    from PIL import Image as PILImage

    _TESSERACT_AVAILABLE = True
except ImportError:
    _TESSERACT_AVAILABLE = False

try:
    import easyocr

    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False

_EASYOCR_READER: Optional[object] = None

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_easyocr_reader(languages: list[str]) -> object:
    """Return a cached EasyOCR reader for the given language list."""
    global _EASYOCR_READER
    if _EASYOCR_READER is None and _EASYOCR_AVAILABLE:
        _EASYOCR_READER = easyocr.Reader(languages, gpu=False, verbose=False)
    return _EASYOCR_READER


def _download_image_bytes(url: str, timeout: int = 15) -> bytes:
    """Download image bytes from a URL. Raises requests.RequestException on failure."""
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def _bytes_to_pil(image_bytes: bytes):
    """Convert raw bytes to a PIL Image object."""
    if not _TESSERACT_AVAILABLE:
        raise ImportError("Pillow is required for image preprocessing.")
    return PILImage.open(io.BytesIO(image_bytes)).convert("RGB")


def _preprocess_image(pil_image) -> object:
    """Apply basic preprocessing to improve OCR accuracy."""
    # Convert to grayscale for better OCR results
    return pil_image.convert("L")


# ---------------------------------------------------------------------------
# Core OCR functions
# ---------------------------------------------------------------------------


def extract_text_tesseract(
    image_bytes: bytes, languages: str = "eng"
) -> str:
    """Run pytesseract OCR on image bytes. Returns extracted text or empty string."""
    if not _TESSERACT_AVAILABLE:
        logger.warning("pytesseract/Pillow not available; returning empty string.")
        return ""
    try:
        pil_image = _bytes_to_pil(image_bytes)
        preprocessed = _preprocess_image(pil_image)
        text = pytesseract.image_to_string(preprocessed, lang=languages)
        return text.strip()
    except Exception as exc:
        logger.error("pytesseract OCR failed: %s", exc)
        return ""


def extract_text_easyocr(
    image_bytes: bytes, languages: list[str] | None = None
) -> str:
    """Run EasyOCR on image bytes. Returns extracted text or empty string."""
    if not _EASYOCR_AVAILABLE:
        logger.warning("easyocr not available; returning empty string.")
        return ""
    if languages is None:
        languages = ["en"]
    try:
        reader = _get_easyocr_reader(languages)
        results = reader.readtext(image_bytes, detail=0, paragraph=True)
        return "\n".join(results).strip()
    except Exception as exc:
        logger.error("EasyOCR failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_text(url: str, languages: list[str] | None = None) -> str:
    """
    Download an image from a URL and extract all readable text via OCR.

    Tries pytesseract first, then falls back to EasyOCR.
    Returns the extracted text string, or an empty string on failure.

    Args:
        url: Public URL of the image.
        languages: List of language codes (e.g. ['en', 'fr']). Defaults to English.

    Returns:
        Extracted text as a plain string.
    """
    if languages is None:
        languages = ["en"]

    try:
        image_bytes = _download_image_bytes(url)
    except Exception as exc:
        logger.error("Failed to download image from %s: %s", url, exc)
        return ""

    if _TESSERACT_AVAILABLE:
        lang_str = "+".join(languages)
        text = extract_text_tesseract(image_bytes, languages=lang_str)
        if text:
            return text

    if _EASYOCR_AVAILABLE:
        text = extract_text_easyocr(image_bytes, languages=languages)
        if text:
            return text

    logger.warning(
        "No OCR backend produced output for URL: %s. "
        "Install pytesseract+Pillow or easyocr.",
        url,
    )
    return ""


def extract_text_batch(
    urls: list[str], languages: list[str] | None = None
) -> list[dict]:
    """
    Extract text from multiple image URLs.

    Args:
        urls: List of image URLs to process.
        languages: Optional list of language codes.

    Returns:
        List of dicts with keys 'url', 'text', and 'success'.
    """
    results = []
    for url in urls:
        try:
            text = extract_text(url, languages=languages)
            results.append({"url": url, "text": text, "success": True})
        except Exception as exc:
            logger.error("Batch OCR failed for %s: %s", url, exc)
            results.append({"url": url, "text": "", "success": False})
    return results


def process_discord_attachments(
    attachment_urls: list[str],
    languages: list[str] | None = None,
) -> list[dict]:
    """
    OCR-process a list of Discord attachment image URLs.

    Args:
        attachment_urls: URLs of Discord image attachments.
        languages: Optional language codes list.

    Returns:
        List of result dicts with 'url', 'text', and 'success' keys.
    """
    if not attachment_urls:
        logger.debug("No attachment URLs provided to process_discord_attachments.")
        return []
    return extract_text_batch(attachment_urls, languages=languages)


def ocr_summary(results: list[dict]) -> str:
    """
    Combine OCR results from a batch into a single summary string.

    Args:
        results: List of dicts as returned by extract_text_batch.

    Returns:
        Formatted string with each image's extracted text.
    """
    if not results:
        return ""
    parts = []
    for idx, result in enumerate(results, start=1):
        text = result.get("text", "").strip()
        status = "OK" if result.get("success") else "FAILED"
        header = f"[Image {idx} — {status}]"
        body = text if text else "(no text detected)"
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


def check_ocr_backends() -> dict:
    """
    Return a dict reporting availability of OCR backends.

    Returns:
        Dict with keys 'pytesseract' and 'easyocr' mapped to booleans.
    """
    return {
        "pytesseract": _TESSERACT_AVAILABLE,
        "easyocr": _EASYOCR_AVAILABLE,
    }