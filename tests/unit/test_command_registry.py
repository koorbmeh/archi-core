"""Tests for src/kernel/command_registry.py — on-demand Discord commands."""

import json
from pathlib import Path

import pytest

from src.kernel.command_registry import (
    CommandEntry,
    list_commands_text,
    load_registry,
    match_command,
    register,
    resolve_function,
    save_registry,
)


# --- load / save / register ---

class TestLoadSave:
    def test_empty_file_returns_empty(self, tmp_path):
        assert load_registry(tmp_path / "nope.json") == []

    def test_round_trip(self, tmp_path):
        path = tmp_path / "cmd.json"
        entries = [
            CommandEntry(command="scan", module="capabilities.scanner",
                         function="scan_now", description="Scan things"),
            CommandEntry(command="report", module="capabilities.reporter",
                         function="generate", description="Make report",
                         is_async=False),
        ]
        save_registry(entries, path)
        loaded = load_registry(path)
        assert len(loaded) == 2
        assert loaded[0].command == "scan"
        assert loaded[1].is_async is False

    def test_register_adds_entry(self, tmp_path):
        path = tmp_path / "cmd.json"
        entry = register("test cmd", "mod", "fn", description="A test", path=path)
        assert entry.command == "test cmd"
        loaded = load_registry(path)
        assert len(loaded) == 1

    def test_register_updates_existing(self, tmp_path):
        path = tmp_path / "cmd.json"
        register("x", "mod1", "fn1", path=path)
        register("x", "mod2", "fn2", path=path)
        loaded = load_registry(path)
        assert len(loaded) == 1
        assert loaded[0].module == "mod2"

    def test_invalid_json_returns_empty(self, tmp_path):
        path = tmp_path / "cmd.json"
        path.write_text("{bad", encoding="utf-8")
        assert load_registry(path) == []


# --- match_command ---

class TestMatchCommand:
    def setup_method(self):
        self.entries = [
            CommandEntry(command="scan craigslist", module="m", function="f"),
            CommandEntry(command="scan", module="m2", function="f2"),
            CommandEntry(command="help", module="m3", function="f3"),
        ]

    def test_exact_match(self):
        result = match_command("!help", self.entries)
        assert result is not None
        entry, args = result
        assert entry.command == "help"
        assert args == ""

    def test_prefix_match_with_args(self):
        result = match_command("!scan craigslist dogs", self.entries)
        assert result is not None
        entry, args = result
        # Longest prefix wins: "scan craigslist" over "scan"
        assert entry.command == "scan craigslist"
        assert args == "dogs"

    def test_shorter_prefix(self):
        result = match_command("!scan something", self.entries)
        assert result is not None
        entry, args = result
        assert entry.command == "scan"
        assert args == "something"

    def test_no_match(self):
        assert match_command("!unknown", self.entries) is None

    def test_not_command(self):
        assert match_command("hello", self.entries) is None

    def test_case_insensitive(self):
        result = match_command("!HELP", self.entries)
        assert result is not None
        assert result[0].command == "help"


# --- resolve_function ---

class TestResolveFunction:
    def test_resolves_simple_function(self):
        entry = CommandEntry(command="test", module="json", function="dumps")
        fn = resolve_function(entry)
        assert fn is not None
        assert callable(fn)

    def test_bad_module_returns_none(self):
        entry = CommandEntry(command="test", module="nonexistent_xyz", function="fn")
        assert resolve_function(entry) is None

    def test_bad_attr_returns_none(self):
        entry = CommandEntry(command="test", module="json", function="nonexistent_xyz")
        assert resolve_function(entry) is None

    def test_factory_pattern(self):
        """Resolve 'loads' via a dotted path — json.loads is a real function."""
        entry = CommandEntry(command="test", module="json", function="loads")
        fn = resolve_function(entry)
        assert fn is not None


# --- list_commands_text ---

class TestListCommandsText:
    def test_no_commands(self, tmp_path):
        # Monkey-patch the default path temporarily
        import src.kernel.command_registry as mod
        orig = mod.DEFAULT_REGISTRY_PATH
        mod.DEFAULT_REGISTRY_PATH = tmp_path / "empty.json"
        try:
            text = list_commands_text()
            assert "No commands" in text
        finally:
            mod.DEFAULT_REGISTRY_PATH = orig

    def test_lists_commands(self, tmp_path):
        import src.kernel.command_registry as mod
        orig = mod.DEFAULT_REGISTRY_PATH
        path = tmp_path / "cmd.json"
        register("scan", "m", "f", description="Scan stuff", path=path)
        register("help", "m2", "f2", description="Show help", path=path)
        mod.DEFAULT_REGISTRY_PATH = path
        try:
            text = list_commands_text()
            assert "!help" in text
            assert "!scan" in text
            assert "Scan stuff" in text
        finally:
            mod.DEFAULT_REGISTRY_PATH = orig
