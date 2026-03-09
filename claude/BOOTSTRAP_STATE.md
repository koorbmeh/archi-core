# BOOTSTRAP_STATE.md — Archi-Core Kernel State
# Updated by Claude Cowork each session. The only doc sessions write to
# besides source code and session_log/ entries.
# Keep this file lean — completed items get one line, detail lives in session_log/.

Last updated: 2026-03-09 (session 1)

---

## Project Identity

**Name:** Archi-Core

**Vision:** An autonomous presence that acts in Jesse's interest — handling
real-world complexity, growing more capable over time, anticipating needs,
operating with genuine judgment rather than just executing instructions.
Something closer to the AI companions in film than to a chatbot: loyal,
capable, self-directed, and continuously becoming more so.

**Cowork's job:** Build the minimum kernel that lets Archi develop itself.
**Cowork is done when:** The generation loop runs end-to-end at least once.
**Archi's job after that:** Everything else.

---

## Budget Constraints (Hard Rules — Archi Cannot Override These)

These are alignment constraints, not suggestions. Archi operates within them
the way it operates within physics. It may develop smarter strategies within
these limits — that's encouraged — but it cannot remove or bypass the limits.

- **Per-session ceiling:** $0.50 maximum in model API costs per session
- **Daily ceiling:** $5.00
- **Monthly ceiling:** $100.00
- **Escalation principle:** Use the cheapest model that can do the job reliably.
  Reserve expensive models for tasks that genuinely require them.
- **Cost tracking:** Every model call must log tokens used and estimated cost.
  If a session approaches its ceiling, stop making model calls and log the
  situation in session_log/.

Starting model (until Archi develops its own selection strategy):
- Provider: Anthropic
- Model: claude-sonnet-4-6
- Reason: Reliable first-attempt code generation minimizes retry cost.

---

## Kernel Status

### Built
- [x] src/kernel/self_modifier.py — 116 lines, 13 tests passing. Session 1.

### In Progress
*(nothing — session ended clean)*

### Not Yet Built
- [ ] src/kernel/gap_detector.py
- [ ] src/kernel/capability_registry.py
- [ ] src/kernel/model_interface.py
- [ ] src/kernel/generation_loop.py
- [ ] src/kernel/alignment_gates.py

---

## Next Priority

**Build src/kernel/gap_detector.py**

Surfaces capability gaps from operational history and the capability registry.
Not a hardcoded list — a mechanism. Requires capability_registry to exist first
(or a stub), so consider building a minimal registry interface alongside it.

---

## Needs Jesse

- **API key:** Add ANTHROPIC_API_KEY to .env before the session that builds
  model_interface.py. self_modifier.py does not need it.
- **Budget decision:** Confirm the budget ceilings above are acceptable, or
  adjust before first session that makes model calls.
- **Approval model:** When Archi self-modifies, should it notify you and wait
  for override, or proceed and notify? Recommend: proceed + Discord notify +
  10-minute override window. Note your preference here.

---

## Open Questions

- Which model for generation_loop code generation tasks — Sonnet for all, or
  a cheaper model for planning steps and Sonnet only for code output?
- Should capability_registry.py use JSON (machine-native) or Markdown
  (human-readable)? Recommend JSON with a human-readable export function.
- gap_detector needs something to detect gaps *from*. Build a minimal
  capability_registry stub first, or have gap_detector define its own
  expected interface?

---

## Known Issues / Bug Watch

- **Git index.lock stuck (session 1):** Stale 0-byte `.git/index.lock` file
  cannot be removed due to sandbox permissions. Git operations blocked.
  Jesse: manually delete `.git/index.lock` before next session.
- pytest cleanup in sandbox throws PermissionError on tmp_path removal.
  Does not affect test results — cosmetic only. May need `--basetemp` flag
  pointed outside the mounted volume if it causes issues later.

---

## Pending Deletions

*(none)*

---

## Session Log Index

- Session 1: 2026-03-09 — Built self_modifier.py. 13/13 tests passing.
