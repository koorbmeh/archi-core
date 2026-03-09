"""Safe source editing with git-backed test-and-rollback."""

import logging
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import git

logger = logging.getLogger(__name__)

PROTECTED_FILES = frozenset({
    "src/kernel/alignment_gates.py",
    "config/rules.yaml",
    ".env",
})


@dataclass
class ChangeResult:
    """Outcome of a proposed change."""
    success: bool
    branch_name: str
    file_path: str
    message: str
    test_output: str = ""
    error: Optional[str] = None
    failure_type: str = "unknown"


def _resolve_protected(file_path: str, repo_root: Path) -> bool:
    """Check if a path resolves to a protected file."""
    try:
        resolved = Path(file_path).resolve()
        for protected in PROTECTED_FILES:
            if resolved == (repo_root / protected).resolve():
                return True
        # Also check the raw relative path
        rel = str(Path(file_path))
        return rel in PROTECTED_FILES
    except (ValueError, OSError):
        return False


def _run_tests(repo_root: Path) -> tuple[bool, str]:
    """Run pytest against the repo. Returns (passed, output)."""
    tests_dir = repo_root / "tests"
    if not tests_dir.exists():
        return True, "No tests/ directory — skipped."
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/", "-x", "-q", "--tb=short"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Test run timed out (120s limit)."
    except FileNotFoundError:
        return False, "pytest not found."


_ENVIRONMENT_PATTERNS = [
    "dubious ownership",
    "safe.directory",
    "permission denied",
    "index.lock",
]


def _classify_failure(error_text: str) -> str:
    """Classify an exception as environment vs unknown."""
    lower = error_text.lower()
    for pattern in _ENVIRONMENT_PATTERNS:
        if pattern in lower:
            return "environment"
    return "unknown"


def apply_change(repo_path: str, file_path: str, new_content: str) -> ChangeResult:
    """Branch → apply change → test → merge on pass / rollback on fail."""
    repo_root = Path(repo_path).resolve()
    target = repo_root / file_path

    if _resolve_protected(file_path, repo_root):
        msg = f"Refused: {file_path} is protected."
        logger.warning(msg)
        return ChangeResult(False, "", file_path, msg)
    try:
        repo = git.Repo(repo_root)
    except git.InvalidGitRepositoryError:
        msg = f"Not a git repo: {repo_root}"
        logger.error(msg)
        return ChangeResult(False, "", file_path, msg, error=msg)

    original_branch = repo.active_branch.name
    branch_name = f"archi/mod-{uuid.uuid4().hex[:8]}"

    try:
        repo.git.checkout("-b", branch_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
        repo.index.add([str(target.relative_to(repo_root))])
        repo.index.commit(f"archi: modify {file_path}")
        logger.info("Change applied to %s on %s.", file_path, branch_name)
        passed, test_output = _run_tests(repo_root)
        if passed:
            repo.git.checkout(original_branch)
            repo.git.merge(branch_name, "--no-ff",
                           "-m", f"archi: integrate {file_path}")
            repo.git.branch("-d", branch_name)
            msg = f"Change to {file_path} integrated — tests passed."
            logger.info(msg)
            return ChangeResult(True, branch_name, file_path, msg, test_output)
        repo.git.checkout(original_branch)
        repo.git.branch("-D", branch_name)
        msg = f"Rolled back {file_path} — tests failed."
        logger.warning(msg)
        return ChangeResult(False, branch_name, file_path, msg, test_output,
                            failure_type="test_failure")
    except Exception as exc:
        logger.error("Exception during apply_change: %s", exc)
        try:
            repo.git.checkout(original_branch)
            if branch_name in [b.name for b in repo.branches]:
                repo.git.branch("-D", branch_name)
        except Exception:
            logger.error("Cleanup failed after: %s", exc)
        ftype = _classify_failure(str(exc))
        return ChangeResult(False, branch_name, file_path,
                            f"Exception: {exc}", error=str(exc),
                            failure_type=ftype)
