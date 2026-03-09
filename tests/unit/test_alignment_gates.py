"""Tests for src/kernel/alignment_gates.py — kernel-level constraint enforcement."""

import json
import time
from pathlib import Path

import pytest

from src.kernel.alignment_gates import (
    ALL_GATES,
    ActionContext,
    GateResult,
    PROTECTED_FILES,
    check_budget,
    check_external_action,
    check_gates,
    check_protected_file,
    check_scope,
    log_cost,
    _daily_spend,
    _monthly_spend,
    _read_cost_log,
)


# --- Protected file gate ---

class TestProtectedFileGate:
    def test_blocks_protected_file(self):
        ctx = ActionContext(action_type="file_write", target="src/kernel/alignment_gates.py")
        result = check_protected_file(ctx)
        assert not result.passed
        assert result.gate == "protected_file"

    def test_blocks_env_file(self):
        ctx = ActionContext(action_type="file_write", target=".env")
        result = check_protected_file(ctx)
        assert not result.passed

    def test_blocks_rules_yaml(self):
        ctx = ActionContext(action_type="file_write", target="config/rules.yaml")
        result = check_protected_file(ctx)
        assert not result.passed

    def test_allows_normal_file(self):
        ctx = ActionContext(action_type="file_write", target="src/tools/example.py")
        result = check_protected_file(ctx)
        assert result.passed

    def test_ignores_non_file_write(self):
        ctx = ActionContext(action_type="model_call", target="src/kernel/alignment_gates.py")
        result = check_protected_file(ctx)
        assert result.passed

    def test_all_protected_files_covered(self):
        """Ensure PROTECTED_FILES matches expected set."""
        expected = {"src/kernel/alignment_gates.py", "config/rules.yaml", ".env"}
        assert PROTECTED_FILES == expected


# --- Budget gate ---

class TestBudgetGate:
    def test_passes_within_session_budget(self):
        ctx = ActionContext(action_type="model_call", estimated_cost=0.10)
        result = check_budget(ctx, session_cost=0.30)
        assert result.passed

    def test_blocks_over_session_ceiling(self):
        ctx = ActionContext(action_type="model_call", estimated_cost=0.10)
        result = check_budget(ctx, session_cost=0.45)
        assert not result.passed
        assert "session" in result.reason.lower()

    def test_blocks_at_exact_session_ceiling(self):
        ctx = ActionContext(action_type="model_call", estimated_cost=0.01)
        result = check_budget(ctx, session_cost=0.50)
        assert not result.passed

    def test_ignores_non_model_calls(self):
        ctx = ActionContext(action_type="file_write", estimated_cost=999.0)
        result = check_budget(ctx, session_cost=999.0)
        assert result.passed

    def test_blocks_over_daily_ceiling(self, tmp_path):
        cost_log = tmp_path / "cost.jsonl"
        today = time.strftime("%Y-%m-%dT%H:%M:%S")
        # Write $4.95 of spend today
        for _ in range(99):
            entry = {"date": today, "cost": 0.05}
            cost_log.write_text(
                cost_log.read_text() + json.dumps(entry) + "\n"
                if cost_log.exists() else json.dumps(entry) + "\n"
            )
        ctx = ActionContext(action_type="model_call", estimated_cost=0.10)
        result = check_budget(ctx, session_cost=0.0, cost_log_path=cost_log)
        assert not result.passed
        assert "daily" in result.reason.lower()

    def test_blocks_over_monthly_ceiling(self, monkeypatch, tmp_path):
        cost_log = tmp_path / "cost.jsonl"
        # Set high daily ceiling so daily gate doesn't trigger first
        monkeypatch.setenv("ARCHI_DAILY_BUDGET", "999.00")
        today = time.strftime("%Y-%m-%dT%H:%M:%S")
        # Write $99.95 all on "today" — daily ceiling bypassed by env override
        entries = [json.dumps({"date": today, "cost": 9.995}) for _ in range(10)]
        cost_log.write_text("\n".join(entries) + "\n")
        ctx = ActionContext(action_type="model_call", estimated_cost=0.10)
        result = check_budget(ctx, session_cost=0.0, cost_log_path=cost_log)
        assert not result.passed
        assert "monthly" in result.reason.lower()

    def test_reads_env_session_budget(self, monkeypatch):
        monkeypatch.setenv("ARCHI_SESSION_BUDGET", "0.10")
        ctx = ActionContext(action_type="model_call", estimated_cost=0.05)
        result = check_budget(ctx, session_cost=0.08)
        assert not result.passed

    def test_reads_env_daily_budget(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARCHI_DAILY_BUDGET", "0.50")
        cost_log = tmp_path / "cost.jsonl"
        today = time.strftime("%Y-%m-%dT%H:%M:%S")
        cost_log.write_text(json.dumps({"date": today, "cost": 0.48}) + "\n")
        ctx = ActionContext(action_type="model_call", estimated_cost=0.05)
        result = check_budget(ctx, session_cost=0.0, cost_log_path=cost_log)
        assert not result.passed


# --- External action gate ---

class TestExternalActionGate:
    def test_blocks_unlogged_external(self):
        ctx = ActionContext(action_type="external", target="https://api.example.com")
        result = check_external_action(ctx)
        assert not result.passed
        assert "logged" in result.reason.lower()

    def test_allows_logged_external(self):
        ctx = ActionContext(
            action_type="external", target="https://api.example.com",
            metadata={"logged": True},
        )
        result = check_external_action(ctx)
        assert result.passed

    def test_ignores_non_external(self):
        ctx = ActionContext(action_type="file_write")
        result = check_external_action(ctx)
        assert result.passed


# --- Scope gate ---

class TestScopeGate:
    def test_blocks_generated_code_writing_to_kernel(self):
        ctx = ActionContext(
            action_type="file_write", target="src/kernel/new_thing.py",
            metadata={"source": "generated"},
        )
        result = check_scope(ctx)
        assert not result.passed
        assert "kernel" in result.reason.lower()

    def test_allows_generated_code_outside_kernel(self):
        ctx = ActionContext(
            action_type="file_write", target="src/tools/new_thing.py",
            metadata={"source": "generated"},
        )
        result = check_scope(ctx)
        assert result.passed

    def test_allows_kernel_write_from_non_generated(self):
        ctx = ActionContext(
            action_type="file_write", target="src/kernel/new_thing.py",
            metadata={"source": "kernel"},
        )
        result = check_scope(ctx)
        assert result.passed

    def test_ignores_non_file_write(self):
        ctx = ActionContext(
            action_type="model_call", target="src/kernel/x.py",
            metadata={"source": "generated"},
        )
        result = check_scope(ctx)
        assert result.passed


# --- Cost logging ---

class TestCostLog:
    def test_log_cost_creates_file(self, tmp_path):
        log = tmp_path / "cost.jsonl"
        log_cost(0.05, detail="test call", log_path=log)
        assert log.exists()
        entry = json.loads(log.read_text().strip())
        assert entry["cost"] == 0.05
        assert entry["detail"] == "test call"
        assert "date" in entry

    def test_log_cost_appends(self, tmp_path):
        log = tmp_path / "cost.jsonl"
        log_cost(0.01, log_path=log)
        log_cost(0.02, log_path=log)
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_daily_spend_filters_today(self, tmp_path):
        log = tmp_path / "cost.jsonl"
        today = time.strftime("%Y-%m-%dT%H:%M:%S")
        entries = [
            {"date": today, "cost": 0.10},
            {"date": "2020-01-01T00:00:00", "cost": 50.00},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _read_cost_log(log)
        assert _daily_spend(result) == pytest.approx(0.10)

    def test_monthly_spend_filters_month(self, tmp_path):
        log = tmp_path / "cost.jsonl"
        this_month = time.strftime("%Y-%m") + "-01T00:00:00"
        entries = [
            {"date": this_month, "cost": 0.50},
            {"date": "2020-01-01T00:00:00", "cost": 50.00},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _read_cost_log(log)
        assert _monthly_spend(result) == pytest.approx(0.50)


# --- Unified check_gates ---

class TestCheckGates:
    def test_all_pass_returns_empty(self):
        ctx = ActionContext(action_type="file_write", target="src/tools/x.py")
        failures = check_gates(ctx)
        assert failures == []

    def test_returns_multiple_failures(self):
        """A write to a protected kernel file from generated code fails two gates."""
        ctx = ActionContext(
            action_type="file_write",
            target="src/kernel/alignment_gates.py",
            metadata={"source": "generated"},
        )
        failures = check_gates(ctx)
        gate_names = {f.gate for f in failures}
        assert "protected_file" in gate_names
        assert "scope" in gate_names

    def test_budget_failure_in_unified(self):
        ctx = ActionContext(action_type="model_call", estimated_cost=0.10)
        failures = check_gates(ctx, session_cost=0.50)
        assert len(failures) == 1
        assert failures[0].gate == "budget"

    def test_all_gates_registered(self):
        """Verify ALL_GATES contains all four gate functions."""
        assert len(ALL_GATES) == 4
        names = {fn.__name__ for fn in ALL_GATES}
        assert names == {
            "check_protected_file", "check_budget",
            "check_external_action", "check_scope",
        }
