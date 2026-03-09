"""Tests for src/kernel/capability_registry.py"""

import json
from pathlib import Path

import pytest

from src.kernel.capability_registry import Capability, CapabilityRegistry


@pytest.fixture
def reg(tmp_path):
    return CapabilityRegistry(path=tmp_path / "registry.json")


@pytest.fixture
def sample_cap():
    return Capability(
        name="self_modifier",
        module="src/kernel/self_modifier.py",
        description="Safe source editing with test-and-rollback.",
    )


class TestRegisterAndRetrieve:
    def test_register_and_get(self, reg, sample_cap):
        reg.register(sample_cap)
        assert reg.get("self_modifier") == sample_cap

    def test_has(self, reg, sample_cap):
        assert not reg.has("self_modifier")
        reg.register(sample_cap)
        assert reg.has("self_modifier")

    def test_names(self, reg, sample_cap):
        reg.register(sample_cap)
        assert "self_modifier" in reg.names()

    def test_list_all(self, reg, sample_cap):
        reg.register(sample_cap)
        assert len(reg.list_all()) == 1

    def test_list_active_filters(self, reg):
        active = Capability("a", "a.py", "active cap", status="active")
        failed = Capability("b", "b.py", "failed cap", status="failed")
        reg.register(active)
        reg.register(failed)
        assert len(reg.list_active()) == 1
        assert reg.list_active()[0].name == "a"


class TestRemove:
    def test_remove_existing(self, reg, sample_cap):
        reg.register(sample_cap)
        assert reg.remove("self_modifier") is True
        assert not reg.has("self_modifier")

    def test_remove_nonexistent(self, reg):
        assert reg.remove("nope") is False


class TestPersistence:
    def test_round_trip(self, tmp_path, sample_cap):
        path = tmp_path / "registry.json"
        reg1 = CapabilityRegistry(path=path)
        reg1.register(sample_cap)
        reg2 = CapabilityRegistry(path=path)
        assert reg2.has("self_modifier")
        assert reg2.get("self_modifier").description == sample_cap.description

    def test_corrupt_file_handled(self, tmp_path):
        path = tmp_path / "registry.json"
        path.write_text("NOT JSON", encoding="utf-8")
        reg = CapabilityRegistry(path=path)
        assert len(reg.list_all()) == 0
