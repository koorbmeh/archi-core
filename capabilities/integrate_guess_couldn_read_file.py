"""
Integrates the guess_couldn_read_file capability into Discord message processing
to handle PDF attachments and file readability queries from Jesse.
"""

import asyncio
import aiohttp
import pathlib
import tempfile
from typing import Dict, List, Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry

from capabilities.guess_couldn_read_file import guess_couldn_read_file
from capabilities.discord_notifier import notify


_message_queue: Optional[asyncio.Queue] = None


def initialize(registry: Optional[CapabilityRegistry] = None) -> Optional[Capability]:
    global _message_queue
    if _message_queue is None:
        _message_queue = asyncio.Queue()

    cap = Capability(
        name='wire_guess_couldn_read_file',
        module=__name__,
        description='Integrates guess_couldn_read_file into Discord message processing to handle PDF attachments and file readability queries from Jesse.',
        dependencies=['guess_couldn_read_file', 'discord_notifier']
    )
    if registry is not None:
        registry.add(cap)
    return cap


def receive_message(content: str, user_id: str, *, attachment_urls: List[str] | None = None) -> None:
    global _message_queue
    if _message_queue is None:
        return

    lower_content = content.lower()
    keywords = ['read this file', 'could you read', 'can you read this file', 'file readability']
    is_query = any(kw in lower_content for kw in keywords)
    has_pdf = bool(attachment_urls and any(url.lower().endswith('.pdf') for url in attachment_urls))

    if has_pdf or is_query:
        target_url = None
        if attachment_urls:
            for url in attachment_urls:
                if url.lower().endswith('.pdf'):
                    target_url = url
                    break
            if target_url is None:
                target_url = attachment_urls[0]
        if target_url:
            _message_queue.put_nowait(('check_file', {'url': target_url, 'content': content, 'user_id': user_id}))


async def process_one(repo_path: str, registry: CapabilityRegistry) -> bool:
    global _message_queue
    if _message_queue is None or _message_queue.empty():
        return False

    try:
        item = await _message_queue.get_nowait()
    except asyncio.QueueEmpty:
        return False

    action, data = item
    if action == 'check_file':
        result_msg = await _download_and_check_pdf(**data)
        notify(result_msg)

    _message_queue.task_done()
    return True


async def process_pending(repo_path: str, registry: CapabilityRegistry) -> int:
    count = 0
    while await process_one(repo_path, registry):
        count += 1
    return count


async def _download_and_check_pdf(url: str, content: str, user_id: str) -> str:
    tmp_path: Optional[pathlib.Path] = None
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                file_bytes = await response.read()

        suffix = pathlib.Path(url).suffix or '.file'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            tmp_path = pathlib.Path(tmp_file.name)
            tmp_path.write_bytes(file_bytes)

        result = guess_couldn_read_file(tmp_path)
        file_type = 'PDF' if suffix.lower() == '.pdf' else 'file'
        prefix = f'Readability check for your attached {file_type}: '
        context = f"\n(Context: {content[:200]}...)" if content.strip() else ''
        return prefix + result + context
    except Exception as exc:
        return f'Error checking file readability from {url}: {str(exc)[:300]}'
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()