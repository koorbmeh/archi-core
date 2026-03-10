"""
Prompt cacher capability.

Provides a caching mechanism for LLM prompts and responses to reduce API costs
by storing and reusing results for identical prompts (including system, provider,
model).

Cache uses SQLite at `data/prompt_cache.db` with lazy TTL expiration.

Usage::

    from capabilities.prompt_cacher import cached_call_model, register_prompt_cacher, get_cache

    # Register capability
    cap = register_prompt_cacher(registry)

    # Use cached calls
    resp = cached_call_model("Hello, world!")

    # Integration example with generation_loop
    from src.kernel.generation_loop import run_cycle
    result = run_cycle(
        repo_path=".",
        registry=registry,
        log_path=log_path,
        plan_fn=cached_call_model,
        generate_fn=cached_call_model
    )

Cache is automatically initialized on first use.
"""

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import BudgetExceededError, ModelResponse, call_model


class PromptCache:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(cache_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    ttl INTEGER DEFAULT 3600
                )
            """)
            self._conn.commit()

    def _compute_key(
        self,
        prompt: str,
        system: Optional[str],
        provider: Optional[str],
        model: Optional[str],
    ) -> str:
        combined = f"{prompt}|{system or ''}|{provider or ''}|{model or ''}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def get(
        self,
        prompt: str,
        system: Optional[str],
        provider: Optional[str],
        model: Optional[str],
    ) -> Optional[ModelResponse]:
        key = self._compute_key(prompt, system, provider, model)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT response_json, timestamp, ttl FROM cache WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
            if row:
                resp_json, ts, ttl = row
                if time.time() - ts < ttl:
                    data = json.loads(resp_json)
                    return ModelResponse(**data)
        return None

    def store(
        self,
        prompt: str,
        system: Optional[str],
        provider: Optional[str],
        model: Optional[str],
        resp: ModelResponse,
        ttl: int = 3600,
    ) -> None:
        key = self._compute_key(prompt, system, provider, model)
        resp_json = json.dumps(resp.__dict__)
        ts = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO cache (key, response_json, timestamp, ttl)
                VALUES (?, ?, ?, ?)
                """,
                (key, resp_json, ts, ttl),
            )
            self._conn.commit()


_cache: Optional[PromptCache] = None


def get_cache(cache_path: Optional[Path] = None) -> PromptCache:
    global _cache
    if _cache is None:
        _cache = PromptCache(cache_path or Path("data") / "prompt_cache.db")
    return _cache


def cached_call_model(
    prompt: str,
    system: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    ttl_seconds: int = 3600,
) -> ModelResponse:
    cache = get_cache()
    resp = cache.get(prompt, system, provider, model)
    if resp is not None:
        return resp
    try:
        resp = call_model(prompt, system=system, provider=provider, model=model)
    except BudgetExceededError:
        raise
    cache.store(prompt, system, provider, model, resp, ttl_seconds)
    return resp


def register_prompt_cacher(registry: CapabilityRegistry) -> Capability:
    return registry.register_capability(
        Capability(
            name="prompt_cacher",
            module="capabilities.prompt_cacher",
            description="Caching mechanism for LLM prompts and responses to reduce API costs.",
            dependencies=["model_interface"],
        )
    )