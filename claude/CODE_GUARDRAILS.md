# CODE_GUARDRAILS.md — What Not to Break
# Read once per session before touching code.
# Append new conventions at the bottom as they're established.

---

## Protected Files (Never Modify)

- src/kernel/alignment_gates.py
- config/rules.yaml
- .env (never read or write — credentials only, always gitignored)

---

## Before Changing Any File

1. Read it first. Understand existing patterns before touching anything.
2. Grep for references before removing any function, class, or import.
3. Run pytest tests/ before and after. Zero new failures is the bar.
4. Changes touching multiple modules — trace dependencies two levels deep.

---

## Code Conventions

- Imports: stdlib → third-party → local. One per line. No unused imports.
- Error handling: raise in library code, log + return None in handlers that
  must not crash. Never silently swallow exceptions.
- Logging: logger = logging.getLogger(__name__). No print() for operations.
- Log levels: DEBUG=flow tracing, INFO=normal events, WARNING=recoverable,
  ERROR=needs attention.
- No hardcoded paths. Use pathlib.Path. No backslash literals.
- No secrets in source. Credentials in .env only. New secrets → .env.example
  with placeholder and comment.
- snake_case for files/functions/variables. PascalCase for classes.
  UPPER_SNAKE for constants.
- Functions under ~40 lines. Longer usually means doing two things.

---

## Conciseness

Adding 30 lines? Find 30 to remove elsewhere. Kernel files stay under 200
lines. A codebase readable in one session is a feature.

---

## Testing

- Unit tests for all kernel logic. Location: tests/unit/
- Integration tests: tests/integration/
- Must run without API keys. Mock all model calls in unit tests.
- Minimum coverage: happy path, failure/rollback path, boundary conditions.

---

## Automated Session Rules

- Never delete files — log to BOOTSTRAP_STATE.md → PENDING_DELETIONS
- Never use interactive confirmation or AskUserQuestion tool
- Never commit with stale git lock — check .git/index.lock (0 bytes = stale)
  If stale: log in BOOTSTRAP_STATE.md and skip git this session

---

## Protected File Exception Protocol

`alignment_gates.py` may be modified by Jesse (via Cowork) under two conditions:
(1) the change is a bug fix that does not weaken any constraint, and
(2) the reason is documented here.

Exception 1: 2026-03-09 — Windows path separator normalization.
`str(Path())` produces backslashes on Windows, breaking protected file
comparisons against forward-slash strings in PROTECTED_FILES.
Fix: `.replace("\\", "/")` on line 101 (check_protected_file) and
line 150 (check_scope). Same root cause, same fix.

---

## Established Conventions

- Session logs are append-only: new dated file each session, never edit old ones. 2026-03-09
- BOOTSTRAP_STATE.md stays lean: completed items get one line. Detail in session_log/. 2026-03-09
- Kernel modules stay under 200 lines: split when exceeded. 2026-03-09
- Every model call logs tokens used and estimated cost. 2026-03-09
