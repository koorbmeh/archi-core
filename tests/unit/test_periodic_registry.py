"""Tests for src/kernel/periodic_registry.py — periodic task scheduling."""

import asyncio
import json
from pathlib import Path
from typing import Optional

import pytest

from src.kernel.periodic_registry import (
    PeriodicEntry,
    load_registry,
    register,
    resolve_coroutine,
    run_periodic,
    save_registry,
)


# --- load / save / register ---

class TestLoadSave:
    def test_empty_file_returns_empty(self, tmp_path):
        assert load_registry(tmp_path / "nope.json") == []

    def test_round_trip(self, tmp_path):
        path = tmp_path / "reg.json"
        entries = [
            PeriodicEntry(name="a", module="mod_a", coroutine="coro_a",
                          interval_seconds=60, enabled=True),
            PeriodicEntry(name="b", module="mod_b", coroutine="coro_b",
                          interval_seconds=3600, enabled=False),
        ]
        save_registry(entries, path)
        loaded = load_registry(path)
        assert len(loaded) == 2
        assert loaded[0].name == "a"
        assert loaded[0].interval_seconds == 60
        assert loaded[1].enabled is False

    def test_register_adds_entry(self, tmp_path):
        path = tmp_path / "reg.json"
        entry = register("test_task", "capabilities.test", "my_coro",
                         interval_seconds=120, path=path)
        assert entry.name == "test_task"
        loaded = load_registry(path)
        assert len(loaded) == 1
        assert loaded[0].module == "capabilities.test"

    def test_register_updates_existing(self, tmp_path):
        path = tmp_path / "reg.json"
        register("x", "mod1", "coro1", interval_seconds=60, path=path)
        register("x", "mod2", "coro2", interval_seconds=120, path=path)
        loaded = load_registry(path)
        assert len(loaded) == 1
        assert loaded[0].module == "mod2"
        assert loaded[0].interval_seconds == 120

    def test_invalid_json_returns_empty(self, tmp_path):
        path = tmp_path / "reg.json"
        path.write_text("not json!", encoding="utf-8")
        assert load_registry(path) == []

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "reg.json"
        save_registry([], path)
        assert path.exists()


# --- resolve_coroutine ---

class TestResolveCoroutine:
    def test_resolves_asyncio_module(self):
        """Resolve asyncio.sleep as a known coroutine function."""
        entry = PeriodicEntry(
            name="test", module="asyncio", coroutine="sleep",
            interval_seconds=1,
        )
        fn = resolve_coroutine(entry)
        assert fn is not None
        assert asyncio.iscoroutinefunction(fn)

    def test_bad_module_returns_none(self):
        entry = PeriodicEntry(
            name="test", module="nonexistent_module_xyz", coroutine="coro",
            interval_seconds=1,
        )
        assert resolve_coroutine(entry) is None

    def test_bad_attr_returns_none(self):
        entry = PeriodicEntry(
            name="test", module="asyncio", coroutine="nonexistent_attr_xyz",
            interval_seconds=1,
        )
        assert resolve_coroutine(entry) is None

    def test_non_coroutine_returns_none(self):
        """json.dumps is a regular function, not a coroutine."""
        entry = PeriodicEntry(
            name="test", module="json", coroutine="dumps",
            interval_seconds=1,
        )
        assert resolve_coroutine(entry) is None


# --- run_periodic ---

class TestRunPeriodic:
    def test_runs_and_sleeps(self):
        """run_periodic calls the coroutine then sleeps."""
        call_count = 0

        async def my_coro():
            nonlocal call_count
            call_count += 1

        entry = PeriodicEntry(
            name="test", module="test", coroutine="test",
            interval_seconds=0,  # no sleep for testing
        )

        async def run_briefly():
            task = asyncio.create_task(run_periodic(entry, my_coro))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_briefly())
        assert call_count >= 1

    def test_exception_does_not_crash(self):
        """An exception in the coroutine doesn't kill the loop."""
        call_count = 0

        async def failing_coro():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        entry = PeriodicEntry(
            name="test", module="test", coroutine="test",
            interval_seconds=0,
        )

        async def run_briefly():
            task = asyncio.create_task(run_periodic(entry, failing_coro))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_briefly())
        assert call_count >= 1  # kept running despite exception
