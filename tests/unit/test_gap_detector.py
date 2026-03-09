"""Tests for src/kernel/gap_detector.py"""

import json
from pathlib import Path

import pytest

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.gap_detector import (
    KERNEL_COMPONENTS,
    Gap,
    detect_gaps,
    detect_operational_gaps,
    detect_registry_gaps,
    detect_structural_gaps,
)


@pytest.fixture
def empty_reg(tmp_path):
    return CapabilityRegistry(path=tmp_path / "reg.json")


@pytest.fixture
def full_reg(tmp_path):
    """Registry with all kernel components registered."""
    reg = CapabilityRegistry(path=tmp_path / "reg.json")
    for name, module in KERNEL_COMPONENTS.items():
        reg.register(Capability(name=name, module=module, description=f"{name} cap"))
    return reg


class TestStructuralGaps:
    def test_empty_registry_finds_all_kernel_components(self, empty_reg):
        gaps = detect_structural_gaps(empty_reg)
        gap_names = {g.name for g in gaps}
        assert gap_names == set(KERNEL_COMPONENTS.keys())

    def test_full_registry_finds_no_gaps(self, full_reg):
        assert detect_structural_gaps(full_reg) == []

    def test_partial_registry(self, empty_reg):
        empty_reg.register(Capability("self_modifier", "sm.py", "desc"))
        gaps = detect_structural_gaps(empty_reg)
        gap_names = {g.name for g in gaps}
        assert "self_modifier" not in gap_names
        assert "generation_loop" in gap_names

    def test_structural_gaps_have_high_priority(self, empty_reg):
        gaps = detect_structural_gaps(empty_reg)
        assert all(g.priority >= 0.8 for g in gaps)


class TestRegistryGaps:
    def test_failed_capability_detected(self, empty_reg):
        empty_reg.register(Capability("broken", "b.py", "broke", status="failed"))
        gaps = detect_registry_gaps(empty_reg)
        assert any(g.name == "broken" and g.source == "registry" for g in gaps)

    def test_unmet_dependency_detected(self, empty_reg):
        cap = Capability("a", "a.py", "needs b", dependencies=["b"])
        empty_reg.register(cap)
        gaps = detect_registry_gaps(empty_reg)
        assert any(g.name == "b" for g in gaps)

    def test_met_dependency_no_gap(self, empty_reg):
        empty_reg.register(Capability("b", "b.py", "dep"))
        empty_reg.register(Capability("a", "a.py", "needs b", dependencies=["b"]))
        gaps = detect_registry_gaps(empty_reg)
        dep_gaps = [g for g in gaps if g.name == "b"]
        assert dep_gaps == []


class TestOperationalGaps:
    def test_no_log_file(self, tmp_path):
        assert detect_operational_gaps(tmp_path / "nope.jsonl") == []

    def test_parses_failures(self, tmp_path):
        log = tmp_path / "ops.jsonl"
        entries = [
            {"event": "web_search", "success": False, "missing_capability": "web_tools"},
            {"event": "web_search", "success": False, "missing_capability": "web_tools"},
            {"event": "code_gen", "success": True},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        gaps = detect_operational_gaps(log)
        assert len(gaps) == 1
        assert gaps[0].name == "web_tools"
        assert len(gaps[0].evidence) == 2

    def test_priority_scales_with_frequency(self, tmp_path):
        log = tmp_path / "ops.jsonl"
        entries = [
            {"event": f"fail_{i}", "success": False, "missing_capability": "x"}
            for i in range(10)
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        gaps = detect_operational_gaps(log)
        assert gaps[0].priority > 0.9

    def test_corrupt_lines_skipped(self, tmp_path):
        log = tmp_path / "ops.jsonl"
        log.write_text("NOT JSON\n{\"success\": false, \"missing_capability\": \"x\", \"event\": \"e\"}\n",
                        encoding="utf-8")
        gaps = detect_operational_gaps(log)
        assert len(gaps) == 1


class TestDetectGaps:
    def test_deduplicates_across_sources(self, empty_reg, tmp_path):
        # "model_interface" will appear as structural gap AND operational gap
        log = tmp_path / "ops.jsonl"
        log.write_text(json.dumps({
            "event": "call_model", "success": False,
            "missing_capability": "model_interface",
        }), encoding="utf-8")
        gaps = detect_gaps(empty_reg, log_path=log)
        mi_gaps = [g for g in gaps if g.name == "model_interface"]
        assert len(mi_gaps) == 1  # deduplicated
        assert mi_gaps[0].source == "structural"  # higher source wins
        assert len(mi_gaps[0].evidence) >= 2  # evidence merged

    def test_sorted_by_priority_descending(self, empty_reg):
        gaps = detect_gaps(empty_reg)
        priorities = [g.priority for g in gaps]
        assert priorities == sorted(priorities, reverse=True)

    def test_filters_out_active_operational_gaps(self, tmp_path):
        """Operational gaps for already-active capabilities are not surfaced."""
        reg = CapabilityRegistry(path=tmp_path / "reg.json")
        # Register all kernel components + user_communication as active
        for name, module in KERNEL_COMPONENTS.items():
            reg.register(Capability(name=name, module=module, description=f"{name} cap"))
        reg.register(Capability("user_communication", "caps/uc.py", "comms", status="active"))
        log = tmp_path / "ops.jsonl"
        entries = [
            {"event": "fail", "success": False, "missing_capability": "user_communication"},
            {"event": "fail", "success": False, "missing_capability": "push_notification"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        gaps = detect_gaps(reg, log_path=log)
        gap_names = {g.name for g in gaps}
        assert "user_communication" not in gap_names
        assert "push_notification" in gap_names


class TestEnvironmentGaps:
    """Environment gaps (env_ prefix) get special treatment."""

    def test_env_gap_has_environment_source(self, tmp_path):
        log = tmp_path / "ops.jsonl"
        entry = {"event": "integrate_failed", "success": False,
                 "missing_capability": "env_dubious_ownership"}
        log.write_text(json.dumps(entry), encoding="utf-8")
        gaps = detect_operational_gaps(log)
        assert len(gaps) == 1
        assert gaps[0].source == "environment"
        assert gaps[0].name == "env_dubious_ownership"

    def test_env_gap_has_priority_1(self, tmp_path):
        log = tmp_path / "ops.jsonl"
        entry = {"event": "integrate_failed", "success": False,
                 "missing_capability": "env_git_safe_directory"}
        log.write_text(json.dumps(entry), encoding="utf-8")
        gaps = detect_operational_gaps(log)
        assert gaps[0].priority == 1.0

    def test_env_gap_priority_not_boosted_beyond_1(self, tmp_path):
        log = tmp_path / "ops.jsonl"
        entries = [
            {"event": f"fail_{i}", "success": False,
             "missing_capability": "env_permission_denied"}
            for i in range(10)
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        gaps = detect_operational_gaps(log)
        assert gaps[0].priority == 1.0

    def test_env_gap_outranks_capability_gap(self, empty_reg, tmp_path):
        log = tmp_path / "ops.jsonl"
        entries = [
            {"event": "fail", "success": False,
             "missing_capability": "env_dubious_ownership"},
            {"event": "fail", "success": False,
             "missing_capability": "user_communication"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        gaps = detect_gaps(empty_reg, log_path=log)
        env_gaps = [g for g in gaps if g.name.startswith("env_")]
        cap_gaps = [g for g in gaps if g.name == "user_communication"]
        assert env_gaps[0].priority >= cap_gaps[0].priority
