"""Extracts text from PDF files via URL download and parsing.

Enables processing of PDF attachments in Discord messages by providing
core extraction functions and Discord message enqueue/process hooks.
"""

import asyncio
import io
import logging
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import pypdf
from capabilities.conversational_memory import store_message
from src.kernel.capability_registry import CapabilityRegistry

pending_queue: Optional[asyncio.Queue] = None


def _ensure_queue() -> asyncio.Queue:
    global pending_queue
    if pending_queue is None:
        pending_queue = asyncio.Queue()
    return pending_queue


def extract_text(url: str) -> str:
    """Extract text from a PDF at the given URL."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            pdf_bytes = resp.read()
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        texts = []
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ''
            texts.append(f"\n--- Page {i} ---\n{text.strip()}")
        return ''.join(texts).strip()
    except Exception as e:
        return f"[PDF Extraction Error: {str(e)}]"


def process_discord_attachments(attachment_urls: List[str]) -> List[Dict[str, Any]]:
    """Filter PDF attachments and extract text from each."""
    pdf_urls = [u for u in attachment_urls if u.lower().endswith('.pdf')]
    results = []
    for url in pdf_urls:
        text = extract_text(url)
        success = not text.startswith("[PDF Extraction Error")
        results.append({"url": url, "text": text, "success": success})
    return results


def receive_message(
    content: str,
    user_id: str,
    *,
    attachment_urls: Optional[List[str]] = None,
) -> None:
    """Enqueue a Discord message for PDF text extraction processing."""
    _ensure_queue().put_nowait((user_id, content, attachment_urls or []))


async def process_one(
    repo_path: str,
    registry: CapabilityRegistry,
) -> bool:
    """Process one pending message: extract PDF texts, augment, store to memory."""
    queue = _ensure_queue()
    try:
        item: Tuple[str, str, List[str]] = await asyncio.wait_for(
            queue.get(), timeout=0.1
        )
        queue.task_done()
        user_id, orig_content, att_urls = item
        results = process_discord_attachments(att_urls)
        if results:
            extracts = []
            for res in results:
                status = "✅" if res["success"] else "❌"
                trunc_text = res["text"][:2000] + "..." if len(res["text"]) > 2000 else res["text"]
                extracts.append(f"{status} {res['url']}\n{trunc_text}")
            aug_content = f"{orig_content}\n\n📄 PDF Extracts:\n" + "\n\n".join(extracts)
        else:
            aug_content = orig_content
        store_message(user_id, aug_content)
        return True
    except asyncio.TimeoutError:
        return False
    except Exception as e:
        logging.error(f"PDF extractor process_one error: {e}")
        return False


async def process_pending(
    repo_path: str,
    registry: CapabilityRegistry,
) -> int:
    """Drain and process all pending messages."""
    count = 0
    while await process_one(repo_path, registry):
        count += 1
    return count