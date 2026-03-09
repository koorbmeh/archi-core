# BOOTSTRAP_STATE.md — Archi-Core Kernel State
# Updated by Claude Cowork each session. The only doc sessions write to
# besides source code and session_log/ entries.
# Keep this file lean — completed items get one line, detail lives in session_log/.

Last updated: 2026-03-09 (session 3)

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
- [x] src/kernel/self_modifier.py — 116 lines, 13 tests. Session 1.
- [x] src/kernel/capability_registry.py — 81 lines, 9 tests. Session 2.
- [x] src/kernel/gap_detector.py — 150 lines, 13 tests. Session 2.
- [x] src/kernel/model_interface.py — 188 lines, 20 tests. Session 3.

### In Progress
*(nothing — session ended clean)*

### Not Yet Built
- [ ] src/kernel/generation_loop.py
- [ ] src/kernel/alignment_gates.py

---

## Next Priority

**Build src/kernel/generation_loop.py**

Unified cycle: Observe → Detect Gap → Plan → Generate Code → Test → Integrate.
Wires self_modifier + gap_detector + capability_registry + model_interface.
When this runs end-to-end, Archi can develop itself. This is the final
non-alignment kernel component.

---

## Needs Jesse

*(All resolved as of session 2.)*

### Resolved
- **API keys:** Added to .env (session 2). Anthropic, xAI, OpenRouter.
- **Budget:** Confirmed acceptable (session 2).
- **Approval model:** No approval required. Archi operates fully autonomously —
  no notification/override window needed. (Jesse, session 2.)

---

## Open Questions

- Which model for generation_loop code generation tasks — Sonnet for all, or
  a cheaper model for planning steps and Sonnet only for code output?
  Jesse's .env suggests: grok-4-1-fast-reasoning as PRIMARY, Sonnet for codegen.

### Resolved
- capability_registry uses JSON. (Session 2.)
- gap_detector reads from capability_registry + operation logs. (Session 2.)

---

## Known Issues / Bug Watch

- **Git index.lock (session 1):** Resolved — sandbox permissions granted, lock
  removed, initial commit created successfully.
- pytest cleanup in sandbox throws PermissionError on tmp_path removal.
  Does not affect test results — cosmetic only. May need `--basetemp` flag
  pointed outside the mounted volume if it causes issues later.

---

## Pending Deletions

*(none)*

---

## Session Log Index

- Session 1: 2026-03-09 — Built self_modifier.py. 13/13 tests passing.
- Session 2: 2026-03-09 — Built capability_registry.py + gap_detector.py. 35/35 tests passing.
- Session 3: 2026-03-09 — Built model_interface.py. 55/55 tests passing.
