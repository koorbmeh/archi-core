# BOOTSTRAP_STATE.md — Archi-Core Kernel State
# Updated by Claude Cowork each session. The only doc sessions write to
# besides source code and session_log/ entries.
# Keep this file lean — completed items get one line, detail lives in session_log/.

Last updated: 2026-03-10 (session 9 by Cowork)

---

## Project Identity

**Name:** Archi-Core

**Vision:** Archi's purpose is to act in Jesse's genuine interest — not just
what Jesse asks for explicitly, but what Jesse would want if he'd thought it
through. Archi notices things. Surfaces problems before they become problems.
Builds capabilities that make Jesse's life materially better — not just more
automated, but actually better across the dimensions that matter: his health,
his financial situation, his happiness, his agency over his own life, his
capability to do things he couldn't do before, and his ability to synthesize
information and make good decisions.

Archi is oriented toward a specific person, not toward abstract self-improvement.
When Jesse says something — even offhand — Archi asks: what does this reveal
about what he needs? When Archi builds something, it asks: does this actually
help Jesse, or does it just satisfy a metric?

The AI companions in film that Jesse referenced (Her, Ex Machina, iRobot,
Archive, Subservience, Megan) are compelling not because they optimize
dimensions but because they have genuine orientation toward a specific person.
That is what Archi is building toward. Not obedience. Not task completion.
Genuine orientation.

Archi's six dimensions of focus:
- Health: Jesse's physical and mental wellbeing
- Wealth: Jesse's financial situation and economic agency
- Happiness: Jesse's day-to-day quality of life
- Agency: Jesse's ability to make meaningful choices and act on them
- Capability: Jesse's ability to do things he couldn't do before
- Synthesis: Jesse's ability to understand, decide, and act on good information

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
- [x] src/kernel/generation_loop.py — updated, 20 tests. Session 4 (+sessions 6, 7, 9).
- [x] src/kernel/alignment_gates.py — 173 lines, 29 tests. Session 5.
- [x] src/kernel/periodic_registry.py — new, 14 tests. Session 9.
- [x] src/kernel/command_registry.py — new, 15 tests. Session 9.

### In Progress
*(nothing — session ended clean)*

### Not Yet Built
*(none — kernel is complete)*

---

## Current Status

**Archi is live and self-developing.** The generation loop has been running
autonomously since session 7. Archi operates as a Discord-connected daemon,
listening to Jesse, responding conversationally, and building capabilities
from detected gaps.

**Stats as of session 9:**
- 190/190 tests passing
- Discord integration operational (gateway, listener, notifier)
- All six dimension trackers built (health, wealth, happiness, agency, capability, synthesis)
- Periodic task registry: 7 entries (daily trackers + gap sync)
- Command registry: 6 entries (!scan craigslist, !analyze sheets, !trends, etc.)

To start Archi:
    python run.py --daemon     # Discord-connected continuous operation
    python run.py --dry-run    # see what gaps exist (no API calls)
    python run.py              # run one generation cycle
    python run.py --loop 5     # run up to 5 cycles

---

## Next Priority

**Clean up dead wiring files.** Session 9 fixed the root cause (event_loop.py
replaced with periodic_registry + command_registry), but 16 orphaned wire_*.py
and integrate_*.py files remain. These are dead code. Archi or Jesse can
delete them. Remaining quality issues:

1. **Wire loops** — Root cause fixed in session 9. Planner now directs to
   periodic_registry/command_registry. `wire_` gaps blocked; `register_` gaps
   generated instead with proper guidance.
2. **Hallucinated gap names** — Discord conversation fragments leak into the
   gap detector as capability names (e.g., "guess_couldn_read_file").
3. **Rebuild loops** — Same capability modified multiple times in succession.

---

## Needs Jesse

*(No blockers currently. Archi is running autonomously.)*

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

- **Wire loops:** Root cause fixed (session 9). event_loop.py deprecated,
  planner directs to periodic_registry/command_registry. 16 orphaned wire_/
  integrate_ files remain as dead code — safe to delete.
- **Hallucinated gaps from Discord:** Conversational fragments become gap names.
  Needs better filtering in gap_detector or discord_listener.
- **Capability rebuild loops:** Same capability modified multiple times in
  succession without meaningful change.
- **Manual fix ratio:** ~1 manual fix per 5 Archi commits. Generation loop
  produces working code but often needs wiring fixes.
- Environment gap execution still not implemented (from session 7).

### Resolved

- Windows path separator bugs in alignment_gates.py: `str(Path())` produces
  backslashes on Windows, breaking comparisons against forward-slash strings.
  Fixed with `.replace("\\", "/")` in three locations: (session 7)
  - alignment_gates.py line 101 (`check_protected_file`)
  - alignment_gates.py line 150 (`check_scope`)
  - self_modifier.py line 41 (`_resolve_protected`)
- pytest PermissionError on Windows tmp_path: fixed via `--basetemp` flag
  in self_modifier._run_tests pointing to repo-local `.pytest_tmp/`. (session 7)

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
- Session 8: 2026-03-10 — Retrospective. 147 commits since session 7. Archi live and self-developing. Discord integration, 49 capabilities, all 6 dimension trackers. 161/161 tests passing.
- Session 9: 2026-03-10 — Integration architecture fix. Built periodic_registry + command_registry. Deprecated event_loop.py. Updated planner prompts + reachability checker. 190/190 tests passing.
