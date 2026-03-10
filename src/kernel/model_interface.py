"""Minimal model caller — Archi's wire to an LLM.

call_model(prompt) → ModelResponse. Reads provider/model from env.
Tracks cost per call, enforces session budget ceiling.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import anthropic
import httpx

from src.kernel.alignment_gates import log_cost

logger = logging.getLogger(__name__)

# Cost table (USD per 1M tokens) — Archi can extend via registry metadata.
COST_PER_MILLION = {
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
    "grok-4-1-fast-reasoning":   {"input": 2.00, "output": 10.00},
}
DEFAULT_COST = {"input": 5.00, "output": 15.00}  # safe fallback
# Session budget is informational only — daily/monthly ceilings in
# alignment_gates.py are the real hard blocks.  This value controls
# the warning threshold logged after each call.
DEFAULT_SESSION_BUDGET = 5.00  # USD — matches DEFAULT_DAILY_CEILING

@dataclass
class ModelResponse:
    """Result of a single model call."""
    text: str
    tokens_in: int
    tokens_out: int
    cost_estimate: float
    model: str
    provider: str
    latency_ms: int = 0
    error: Optional[str] = None


class BudgetExceededError(Exception):
    """Raised when a call would exceed the session budget."""

_session_cost: float = 0.0

def get_session_cost() -> float:
    return _session_cost


def reset_session() -> None:
    global _session_cost
    _session_cost = 0.0
    logger.info("Session cost reset to $0.00.")

def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rates = COST_PER_MILLION.get(model, DEFAULT_COST)
    return (tokens_in * rates["input"] + tokens_out * rates["output"]) / 1_000_000


def _get_config() -> tuple[str, str]:
    """Read default provider and model from environment."""
    provider = os.environ.get("ARCHI_PROVIDER", "anthropic").lower()
    model = os.environ.get("ARCHI_MODEL", "claude-sonnet-4-6")
    return provider, model


def get_task_config(task: str) -> tuple[str, str]:
    """Read provider/model for a named task type (e.g. 'plan', 'codegen').

    Checks ARCHI_{TASK}_PROVIDER and ARCHI_{TASK}_MODEL env vars.
    Falls back to default config if task-specific vars are unset.
    """
    prefix = f"ARCHI_{task.upper()}"
    provider = os.environ.get(f"{prefix}_PROVIDER", "").lower()
    model = os.environ.get(f"{prefix}_MODEL", "")
    if provider and model:
        return provider, model
    return _get_config()

def _get_budget() -> float:
    raw = os.environ.get("ARCHI_SESSION_BUDGET")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_SESSION_BUDGET

def _call_anthropic(prompt: str, model: str, system: Optional[str]) -> ModelResponse:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "max_tokens": 4096, "messages": messages}
    if system:
        kwargs["system"] = system

    t0 = time.monotonic()
    resp = client.messages.create(**kwargs)
    latency = int((time.monotonic() - t0) * 1000)

    text = resp.content[0].text if resp.content else ""
    tokens_in = resp.usage.input_tokens
    tokens_out = resp.usage.output_tokens
    cost = _estimate_cost(model, tokens_in, tokens_out)

    return ModelResponse(
        text=text, tokens_in=tokens_in, tokens_out=tokens_out,
        cost_estimate=cost, model=model, provider="anthropic",
        latency_ms=latency,
    )


def _call_openai_compat(
    prompt: str, model: str, system: Optional[str],
    base_url: str, api_key: str, provider_name: str,
) -> ModelResponse:
    """Call an OpenAI-compatible endpoint (xAI, OpenRouter, etc.)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    t0 = time.monotonic()
    resp = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "max_tokens": 4096},
        timeout=120.0,
    )
    resp.raise_for_status()
    latency = int((time.monotonic() - t0) * 1000)

    data = resp.json()
    choice = data["choices"][0]["message"]
    usage = data.get("usage", {})
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    cost = _estimate_cost(model, tokens_in, tokens_out)

    return ModelResponse(
        text=choice.get("content", ""), tokens_in=tokens_in,
        tokens_out=tokens_out, cost_estimate=cost, model=model,
        provider=provider_name, latency_ms=latency,
    )

PROVIDER_CONFIG = {
    "xai":        {"env_key": "XAI_API_KEY",        "base_url": "https://api.x.ai/v1"},
    "openrouter": {"env_key": "OPENROUTER_API_KEY",  "base_url": "https://openrouter.ai/api/v1"},
}

def call_model(
    prompt: str,
    system: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> ModelResponse:
    """Call an LLM. Returns ModelResponse. Raises BudgetExceededError if over budget."""
    global _session_cost

    cfg_provider, cfg_model = _get_config()
    provider = (provider or cfg_provider).lower()
    model = model or cfg_model

    try:
        if provider == "anthropic":
            result = _call_anthropic(prompt, model, system)
        elif provider in PROVIDER_CONFIG:
            cfg = PROVIDER_CONFIG[provider]
            api_key = os.environ.get(cfg["env_key"], "")
            result = _call_openai_compat(
                prompt, model, system, cfg["base_url"], api_key, provider,
            )
        else:
            raise ValueError(f"Unknown provider: {provider}")
    except BudgetExceededError:
        raise
    except Exception as exc:
        logger.error("Model call failed (%s/%s): %s", provider, model, exc)
        return ModelResponse(
            text="", tokens_in=0, tokens_out=0, cost_estimate=0.0,
            model=model, provider=provider, error=str(exc),
        )

    _session_cost += result.cost_estimate
    log_cost(result.cost_estimate, detail=f"{provider}/{model}")
    logger.info(
        "Model call: %s/%s — %d in, %d out, $%.6f (session total $%.4f)",
        provider, model, result.tokens_in, result.tokens_out,
        result.cost_estimate, _session_cost,
    )

    return result
