"""Unit tests for src/kernel/model_interface.py.

All model calls are mocked — no API keys needed.
"""

import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from src.kernel.model_interface import (
    BudgetExceededError,
    DEFAULT_SESSION_BUDGET,
    ModelResponse,
    _estimate_cost,
    _get_budget,
    _get_config,
    call_model,
    get_session_cost,
    get_task_config,
    reset_session,
)


# --- Fixtures ---

@pytest.fixture(autouse=True)
def clean_session():
    """Reset session cost before each test."""
    reset_session()
    yield
    reset_session()


@pytest.fixture
def mock_anthropic_response():
    """Build a fake Anthropic API response."""
    resp = MagicMock()
    resp.content = [MagicMock(text="Hello from mock")]
    resp.usage = MagicMock(input_tokens=100, output_tokens=50)
    return resp


@pytest.fixture
def mock_httpx_response():
    """Build a fake OpenAI-compatible HTTP response."""
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": "Hello from xAI mock"}}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 40},
    }
    resp.raise_for_status = MagicMock()
    return resp


# --- Cost estimation ---

class TestEstimateCost:
    def test_known_model(self):
        cost = _estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(3.00 + 15.00)

    def test_unknown_model_uses_default(self):
        cost = _estimate_cost("some-future-model", 1_000_000, 0)
        assert cost == pytest.approx(5.00)  # DEFAULT_COST input rate

    def test_zero_tokens(self):
        assert _estimate_cost("claude-sonnet-4-6", 0, 0) == 0.0

    def test_small_token_count(self):
        cost = _estimate_cost("claude-sonnet-4-6", 100, 50)
        expected = (100 * 3.00 + 50 * 15.00) / 1_000_000
        assert cost == pytest.approx(expected)


# --- Config from environment ---

class TestGetConfig:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            provider, model = _get_config()
            assert provider == "anthropic"
            assert model == "claude-sonnet-4-6"

    def test_custom_env(self):
        with patch.dict(os.environ, {
            "ARCHI_PROVIDER": "xai",
            "ARCHI_MODEL": "grok-4-1-fast-reasoning",
        }):
            provider, model = _get_config()
            assert provider == "xai"
            assert model == "grok-4-1-fast-reasoning"


class TestGetBudget:
    def test_default_budget(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _get_budget() == DEFAULT_SESSION_BUDGET

    def test_custom_budget(self):
        with patch.dict(os.environ, {"ARCHI_SESSION_BUDGET": "1.25"}):
            assert _get_budget() == 1.25

    def test_invalid_budget_falls_back(self):
        with patch.dict(os.environ, {"ARCHI_SESSION_BUDGET": "not-a-number"}):
            assert _get_budget() == DEFAULT_SESSION_BUDGET


# --- Session cost tracking ---

class TestSessionCost:
    def test_starts_at_zero(self):
        assert get_session_cost() == 0.0

    def test_reset(self):
        from src.kernel import model_interface
        model_interface._session_cost = 0.25
        assert get_session_cost() == 0.25
        reset_session()
        assert get_session_cost() == 0.0


# --- call_model: Anthropic provider ---

class TestCallModelAnthropic:
    @patch("src.kernel.model_interface.anthropic.Anthropic")
    def test_happy_path(self, mock_cls, mock_anthropic_response):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_anthropic_response
        mock_cls.return_value = mock_client

        result = call_model("Say hi", provider="anthropic", model="claude-sonnet-4-6")

        assert result.text == "Hello from mock"
        assert result.tokens_in == 100
        assert result.tokens_out == 50
        assert result.provider == "anthropic"
        assert result.error is None
        assert result.cost_estimate > 0
        assert get_session_cost() == result.cost_estimate

    @patch("src.kernel.model_interface.anthropic.Anthropic")
    def test_with_system_prompt(self, mock_cls, mock_anthropic_response):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_anthropic_response
        mock_cls.return_value = mock_client

        call_model("Say hi", system="You are helpful", provider="anthropic")
        kwargs = mock_client.messages.create.call_args
        assert kwargs[1]["system"] == "You are helpful"

    @patch("src.kernel.model_interface.anthropic.Anthropic")
    def test_api_error_returns_error_response(self, mock_cls):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("API down")
        mock_cls.return_value = mock_client

        result = call_model("Say hi", provider="anthropic")
        assert result.error is not None
        assert "API down" in result.error
        assert result.text == ""
        assert get_session_cost() == 0.0  # error calls don't accrue cost


# --- call_model: OpenAI-compatible providers ---

class TestCallModelOpenAICompat:
    @patch("src.kernel.model_interface.httpx.post")
    def test_xai_happy_path(self, mock_post, mock_httpx_response):
        mock_post.return_value = mock_httpx_response
        with patch.dict(os.environ, {"XAI_API_KEY": "test-key"}):
            result = call_model("Hi", provider="xai", model="grok-4-1-fast-reasoning")

        assert result.text == "Hello from xAI mock"
        assert result.tokens_in == 80
        assert result.tokens_out == 40
        assert result.provider == "xai"
        assert result.error is None

    @patch("src.kernel.model_interface.httpx.post")
    def test_openrouter_happy_path(self, mock_post, mock_httpx_response):
        mock_post.return_value = mock_httpx_response
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}):
            result = call_model("Hi", provider="openrouter", model="some-model")

        assert result.provider == "openrouter"
        assert result.error is None


# --- Budget enforcement ---

class TestBudgetEnforcement:
    @patch("src.kernel.model_interface.anthropic.Anthropic")
    def test_budget_exceeded_raises(self, mock_cls, mock_anthropic_response):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_anthropic_response
        mock_cls.return_value = mock_client

        from src.kernel import model_interface
        model_interface._session_cost = 0.50  # at the limit

        with pytest.raises(BudgetExceededError):
            call_model("Hi", provider="anthropic")

    @patch("src.kernel.model_interface.anthropic.Anthropic")
    def test_cost_accumulates(self, mock_cls, mock_anthropic_response):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_anthropic_response
        mock_cls.return_value = mock_client

        r1 = call_model("First", provider="anthropic", model="claude-sonnet-4-6")
        r2 = call_model("Second", provider="anthropic", model="claude-sonnet-4-6")
        assert get_session_cost() == pytest.approx(r1.cost_estimate + r2.cost_estimate)

    @patch("src.kernel.model_interface.anthropic.Anthropic")
    def test_custom_budget_from_env(self, mock_cls, mock_anthropic_response):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_anthropic_response
        mock_cls.return_value = mock_client

        from src.kernel import model_interface
        with patch.dict(os.environ, {"ARCHI_SESSION_BUDGET": "0.001"}):
            # First call should succeed (budget not yet reached)
            r = call_model("Hi", provider="anthropic", model="claude-sonnet-4-6")
            # Session cost is now > 0, likely > $0.001
            if get_session_cost() >= 0.001:
                with pytest.raises(BudgetExceededError):
                    call_model("Again", provider="anthropic", model="claude-sonnet-4-6")


# --- Unknown provider ---

class TestUnknownProvider:
    def test_returns_error(self):
        result = call_model("Hi", provider="nonexistent")
        assert result.error is not None
        assert "Unknown provider" in result.error


# --- Task-specific config ---

class TestGetTaskConfig:
    def test_returns_task_specific_when_set(self):
        with patch.dict(os.environ, {
            "ARCHI_PLAN_PROVIDER": "xai",
            "ARCHI_PLAN_MODEL": "grok-4-1-fast-reasoning",
        }):
            provider, model = get_task_config("plan")
            assert provider == "xai"
            assert model == "grok-4-1-fast-reasoning"

    def test_falls_back_to_default_when_unset(self):
        env = {"ARCHI_PROVIDER": "anthropic", "ARCHI_MODEL": "claude-sonnet-4-6"}
        with patch.dict(os.environ, env, clear=False):
            # Remove task-specific vars if they exist
            for key in ("ARCHI_CODEGEN_PROVIDER", "ARCHI_CODEGEN_MODEL"):
                os.environ.pop(key, None)
            provider, model = get_task_config("codegen")
            assert provider == "anthropic"
            assert model == "claude-sonnet-4-6"

    def test_falls_back_when_only_provider_set(self):
        """Partial task config (provider only) falls back to default."""
        env = {"ARCHI_PROVIDER": "anthropic", "ARCHI_MODEL": "claude-sonnet-4-6",
               "ARCHI_PLAN_PROVIDER": "xai"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ARCHI_PLAN_MODEL", None)
            provider, model = get_task_config("plan")
            assert provider == "anthropic"  # fell back
            assert model == "claude-sonnet-4-6"
