# SESSION_STARTER — Paste this to begin every Claude Cowork session.

Read these files in order:
1. claude/META_BOOTSTRAP.md
2. claude/BOOTSTRAP_STATE.md
3. claude/CODE_GUARDRAILS.md
4. The most recent file in claude/session_log/ (skip on first session)

Before writing any code, state your answers to:
1. What capability (not feature) am I building toward this session?
2. How will Archi discover, invoke, and extend this without human help?
3. Does this belong in the kernel, or should it emerge from the kernel later?

If you cannot answer all three, reread META_BOOTSTRAP.md.

Then work. Priorities:
1. Advance the kernel — whatever BOOTSTRAP_STATE.md says is next
2. Fix bugs blocking kernel work
3. Do not build features — if you catch yourself doing it, stop and log it

Wrap up:
1. Write claude/session_log/YYYY-MM-DD.md — what was built, what failed, why
2. Update BOOTSTRAP_STATE.md — current status, next priority, anything for Jesse
3. Commit all changes
