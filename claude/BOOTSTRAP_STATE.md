# BOOTSTRAP_STATE.md — Archi-Core Kernel State
# Updated by Claude Cowork each session. The only doc sessions write to
# besides source code and session_log/ entries.
# Keep this file lean — completed items get one line, detail lives in session_log/.

Last updated: 2026-03-09 (session 7)

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

### Built — ALL KERNEL COMPONENTS COMPLETE
- [x] src/kernel/self_modifier.py — 138 lines, 18 tests. Session 1 (+session 7).
- [x] src/kernel/capability_registry.py — 81 lines, 9 tests. Session 2.
- [x] src/kernel/gap_detector.py — 155 lines, 17 tests. Session 2 (+session 7).
- [x] src/kernel/model_interface.py — 204 lines, 23 tests. Session 3 (+session 6).
- [x] src/kernel/generation_loop.py — 230 lines, 20 tests. Session 4 (+sessions 6, 7).
- [x] src/kernel/alignment_gates.py — 173 lines, 29 tests. Session 5.

### In Progress
*(nothing — session ended clean)*

### Not Yet Built
*(none — kernel is complete)*

---

## Next Priority

**Ready for first live run.**

All six kernel components are built and wired. run.py is the entry point.
Alignment gates are wired as pre-flight checks in the generation loop.
Cost logging is wired into model_interface after each call. Two-model
routing reads from ARCHI_PLAN_PROVIDER/MODEL and ARCHI_CODEGEN_PROVIDER/MODEL.

To start Archi:
    python run.py --dry-run    # see what gaps exist (no API calls)
    python run.py              # run one generation cycle
    python run.py --loop 5     # run up to 5 cycles

---

## Needs Jesse

**First live run:** `python run.py` with API keys in .env. The first gap
Archi will detect is `user_communication` — seeded via operation_log.jsonl.

**Comms fallback:** If Archi cannot reach its communication target (Discord
down, bad token, etc.), it should fall back to appending messages to
ARCHI_COMMS_FALLBACK_PATH (default: `data/archi_messages.txt`). This is
Jesse's last resort for seeing what Archi is trying to say.

*(All previous items resolved as of session 2.)*

### Resolved
- **API keys:** Added to .env (session 2). Anthropic, xAI, OpenRouter.
- **Budget:** Confirmed acceptable (session 2).
- **Approval model:** No approval required. Archi operates fully autonomously —
  no notification/override window needed. (Jesse, session 2.)

---

## Open Questions

*(none)*

### Resolved
- **Model routing (session 6):** Planning/orchestration/gap analysis → Grok 4.1
  Fast Reasoning (xai). Code generation/file writing → Claude Sonnet 4.6
  (anthropic). Cost tracking required on both. Archi to develop smarter routing
  over time. Already supported: generation_loop accepts separate plan_fn and
  generate_fn callables. Env vars: ARCHI_PLAN_PROVIDER/MODEL, ARCHI_CODEGEN_PROVIDER/MODEL.
- capability_registry uses JSON. (Session 2.)
- gap_detector reads from capability_registry + operation logs. (Session 2.)

---

## Known Issues / Bug Watch

- pytest cleanup in sandbox throws PermissionError on tmp_path removal.
  Does not affect test results — cosmetic only. May need `--basetemp` flag
  pointed outside the mounted volume if it causes issues later.
- Environment gaps are now classified and surfaced (session 7), but Archi
  cannot yet *execute* environment repairs. The mechanism detects and logs
  env_ gaps at priority 1.0 — Archi still needs to build a capability that
  can act on them (e.g., running git config, fixing permissions). This is
  the next gap after user_communication is working.

---

## Pending Deletions

*(none)*

---

## Session Log Index

- Session 1: 2026-03-09 — Built self_modifier.py. 13/13 tests passing.
- Session 2: 2026-03-09 — Built capability_registry.py + gap_detector.py. 35/35 tests passing.
- Session 3: 2026-03-09 — Built model_interface.py. 55/55 tests passing.
- Session 4: 2026-03-09 — Built generation_loop.py. 74/74 tests passing.
- Session 5: 2026-03-09 — Built alignment_gates.py. 103/103 tests passing. KERNEL COMPLETE.
- Session 6: 2026-03-09 — Built run.py entry point. Wired gates + cost logging into loop. 106/106 tests passing.
- Session 7: 2026-03-09 — Failure classification mechanism. Environment vs test vs unknown failures across self_modifier, generation_loop, gap_detector. Windows path fix in alignment_gates.py (Jesse-approved exception). 116/116 tests passing.
