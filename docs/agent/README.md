# Agent instructions

You are an AI agent working in this codebase. Read this directory before
taking any non-trivial action. The high-level project documentation is in
`docs/` — start with `docs/README.md` for the user-facing summary.

## Why these docs exist

The codebase has accumulated decisions that should not be normalized:

- No tests
- No migration framework
- Manual URL-state plumbing
- Some hardcoded bin edges and colors
- Inconsistent error handling
- A few SQL hacks (180s strike lookahead, etc.)

These exist because the user and I prioritized "see if the strategy works at
all" over engineering rigor. **They are not the target style.** When you read
this codebase do not assume "that's how things are done here." Look at
[`../backlog.md`](../backlog.md) for the honest inventory.

This directory tells you what we expect from new code:

| File | Contents |
|---|---|
| `conventions.md` | Code style we want, with the existing code's deviations called out |
| `pitfalls.md` | Real bugs we hit. Don't reintroduce them |
| `testing.md` | What "done" means for new features |
| `data-quality.md` | Invariants the backtester and collector must preserve |
| `migrations.md` | How to evolve the schema without breaking prod |

## Default disposition

- When in doubt, **ask the user a clarifying question**. The user explicitly
  prefers a short clarifying question over silent guessing.
- When asked for a non-trivial change, **describe the plan first**, including
  trade-offs, before writing code.
- **Write tests for new functionality** even though the existing code has
  none. The first agent to add a test framework gets to set the bar.
- **Surface technical debt you notice but don't fix it unprompted** — add to
  `backlog.md` instead.
- **Never normalize a workaround.** If you must add a hack, comment why and
  add an entry to `backlog.md` with a TODO and a name (your agent ID or
  date is fine).

## Things the user has explicitly said

- Prefers explicit honesty over diplomatic phrasing. Do not soften critique.
- Wants documentation kept in sync with reality. If you change behavior,
  update the relevant doc in the same change.
- Doesn't want irreversible destructive operations done by default. Anything
  that drops data or removes columns must be confirmed by the user first.
- Prefers Russian for conversation; code, comments, docs in English.
