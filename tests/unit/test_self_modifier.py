"""
Tests for src/kernel/self_modifier.py

Covers: successful modification, failed-test rollback, protected-file rejection,
and exception handling during apply_change.
"""

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import git
import pytest

from src.kernel.self_modifier import (
    PROTECTED_FILES,
    ChangeResult,
    apply_change,
)


@pytest.fixture
def temp_repo(tmp_path):
    """Create a minimal git repo with a passing test."""
    repo = git.Repo.init(tmp_path)
    repo.config_writer().set_value("user", "name", "test").release()
    repo.config_writer().set_value("user", "email", "test@test").release()

    # Initial file
    hello = tmp_path / "hello.py"
    hello.write_text("MSG = 'hello'\n")

    # A test that always passes (for baseline)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").touch()
    (tests_dir / "test_baseline.py").write_text(
        "def test_baseline():\n    assert True\n"
    )

    repo.index.add(["hello.py", "tests/__init__.py", "tests/test_baseline.py"])
    repo.index.commit("initial commit")
    return tmp_path


class TestApplyChangeSuccess:
    """Happy path: change is applied and tests pass."""

    def test_creates_new_file(self, temp_repo):
        result = apply_change(
            repo_path=str(temp_repo),
            file_path="src/new_module.py",
            new_content="VALUE = 42\n",
        )
        assert result.success is True
        assert "integrated" in result.message
        assert (temp_repo / "src" / "new_module.py").read_text() == "VALUE = 42\n"

    def test_modifies_existing_file(self, temp_repo):
        result = apply_change(
            repo_path=str(temp_repo),
            file_path="hello.py",
            new_content="MSG = 'updated'\n",
        )
        assert result.success is True
        assert (temp_repo / "hello.py").read_text() == "MSG = 'updated'\n"

    def test_branch_is_cleaned_up(self, temp_repo):
        repo = git.Repo(temp_repo)
        branches_before = [b.name for b in repo.branches]
        apply_change(str(temp_repo), "hello.py", "MSG = 'v2'\n")
        branches_after = [b.name for b in repo.branches]
        # No archi/ branches should remain
        archi_branches = [b for b in branches_after if b.startswith("archi/")]
        assert archi_branches == []

    def test_returns_on_main_branch(self, temp_repo):
        repo = git.Repo(temp_repo)
        original = repo.active_branch.name
        apply_change(str(temp_repo), "hello.py", "MSG = 'v3'\n")
        assert repo.active_branch.name == original


class TestApplyChangeRollback:
    """Tests fail → change is rolled back."""

    def test_rollback_on_failing_test(self, temp_repo):
        # Write a test that will fail if a certain file exists
        failing_test = textwrap.dedent("""\
            from pathlib import Path
            def test_no_bad_file():
                assert not Path("bad_file.py").exists(), "bad_file should not exist"
        """)
        (temp_repo / "tests" / "test_no_bad.py").write_text(failing_test)
        repo = git.Repo(temp_repo)
        repo.index.add(["tests/test_no_bad.py"])
        repo.index.commit("add guard test")

        original_content = (temp_repo / "hello.py").read_text()

        # This change creates bad_file.py, which the test rejects
        result = apply_change(
            repo_path=str(temp_repo),
            file_path="bad_file.py",
            new_content="EVIL = True\n",
        )
        assert result.success is False
        assert "Rolled back" in result.message
        assert not (temp_repo / "bad_file.py").exists()

    def test_branch_cleaned_after_rollback(self, temp_repo):
        # Make tests always fail via mock
        with patch(
            "src.kernel.self_modifier._run_tests",
            return_value=(False, "MOCKED FAILURE"),
        ):
            result = apply_change(str(temp_repo), "hello.py", "MSG = 'bad'\n")
        assert result.success is False
        repo = git.Repo(temp_repo)
        archi_branches = [b.name for b in repo.branches if b.name.startswith("archi/")]
        assert archi_branches == []

    def test_original_content_preserved_after_rollback(self, temp_repo):
        original = (temp_repo / "hello.py").read_text()
        with patch(
            "src.kernel.self_modifier._run_tests",
            return_value=(False, "MOCKED FAILURE"),
        ):
            apply_change(str(temp_repo), "hello.py", "MSG = 'should not stick'\n")
        assert (temp_repo / "hello.py").read_text() == original


class TestProtectedFiles:
    """Protected files are rejected before any git operation."""

    @pytest.mark.parametrize("protected", list(PROTECTED_FILES))
    def test_refuses_protected_file(self, temp_repo, protected):
        result = apply_change(str(temp_repo), protected, "HACKED\n")
        assert result.success is False
        assert "protected" in result.message.lower()

    def test_protected_file_not_modified(self, temp_repo):
        # Create the file first so we can check it stays unchanged
        target = temp_repo / ".env"
        target.write_text("SECRET=original\n")
        result = apply_change(str(temp_repo), ".env", "SECRET=hacked\n")
        assert result.success is False
        assert target.read_text() == "SECRET=original\n"


class TestEdgeCases:
    """Invalid repo, exceptions, boundary conditions."""

    def test_invalid_repo_path(self, tmp_path):
        not_a_repo = tmp_path / "empty"
        not_a_repo.mkdir()
        result = apply_change(str(not_a_repo), "file.py", "x = 1\n")
        assert result.success is False
        assert "git repo" in result.message.lower() or result.error

    def test_exception_during_change(self, temp_repo):
        with patch(
            "src.kernel.self_modifier.git.Repo"
        ) as mock_repo_cls:
            mock_repo = mock_repo_cls.return_value
            mock_repo.active_branch.name = "main"
            mock_repo.git.checkout.side_effect = git.GitCommandError("checkout", "fail")
            mock_repo.branches = []
            result = apply_change(str(temp_repo), "file.py", "x = 1\n")
        assert result.success is False
        assert result.error is not None


class TestFailureClassification:
    """ChangeResult.failure_type classifies errors correctly."""

    def test_dubious_ownership_is_environment(self, temp_repo):
        with patch("src.kernel.self_modifier.git.Repo") as mock_repo_cls:
            mock_repo = mock_repo_cls.return_value
            mock_repo.active_branch.name = "main"
            mock_repo.git.checkout.side_effect = git.GitCommandError(
                "checkout", "fatal: detected dubious ownership in repository"
            )
            mock_repo.branches = []
            result = apply_change(str(temp_repo), "file.py", "x = 1\n")
        assert result.failure_type == "environment"

    def test_safe_directory_is_environment(self, temp_repo):
        with patch("src.kernel.self_modifier.git.Repo") as mock_repo_cls:
            mock_repo = mock_repo_cls.return_value
            mock_repo.active_branch.name = "main"
            mock_repo.git.checkout.side_effect = git.GitCommandError(
                "checkout", "call: git config --global --add safe.directory"
            )
            mock_repo.branches = []
            result = apply_change(str(temp_repo), "file.py", "x = 1\n")
        assert result.failure_type == "environment"

    def test_test_failure_classified(self, temp_repo):
        with patch(
            "src.kernel.self_modifier._run_tests",
            return_value=(False, "FAILED test_x.py"),
        ):
            result = apply_change(str(temp_repo), "hello.py", "MSG = 'bad'\n")
        assert result.failure_type == "test_failure"

    def test_unknown_exception_classified(self, temp_repo):
        with patch("src.kernel.self_modifier.git.Repo") as mock_repo_cls:
            mock_repo = mock_repo_cls.return_value
            mock_repo.active_branch.name = "main"
            mock_repo.git.checkout.side_effect = RuntimeError("something weird")
            mock_repo.branches = []
            result = apply_change(str(temp_repo), "file.py", "x = 1\n")
        assert result.failure_type == "unknown"

    def test_success_has_default_failure_type(self, temp_repo):
        result = apply_change(str(temp_repo), "hello.py", "MSG = 'ok'\n")
        assert result.success is True
        assert result.failure_type == "unknown"
