# Code conventions (target state — not necessarily current state)

What we want new code to look like. Where existing code violates these,
prefer to follow this doc — don't propagate the existing patterns.

## Language and stack

- Python 3.12+
- Async I/O via `asyncio` + `httpx` / `websockets` / `psycopg[binary,pool]`
- Streamlit for UI (one main `app.py` + `pages/`)
- Postgres 17, no ORM — write SQL with `psycopg` named placeholders

## Style

- **Type hints on every function signature.** Existing code complies.
- **Docstring on every module and every non-trivial function.** State what
  the function does, what its inputs mean, and any non-obvious invariants.
  Existing code is uneven here — fix as you touch.
- **Imports**: stdlib first, then third-party, then local, separated by
  blank lines. Existing code mostly does this.
- **Names**: snake_case for funcs/vars, PascalCase for classes, UPPER for
  constants. Internal helpers prefixed `_`.
- **No emojis in code, docs or commit messages.** Already a rule, follow it.
- **Logging**: `structlog`-style with a stable event name as first arg.
  `log.info("market_discovered", slug=..., window_start=...)`. Existing
  code does this — keep it.
- **No comments restating what the code does.** Comments should explain
  WHY (a workaround for X, a counter-intuitive invariant, etc.). Existing
  code has a few `# this updates X` style comments — prune as you touch.

## SQL

- Use named placeholders: `%(slug)s`. Never f-strings into SQL.
- Multi-line SQL: tabular alignment, capital keywords, one column per line
  for SELECT lists with > 3 columns.
- **`%` in SQL comments must be escaped as `%%`** when passing to `psycopg`
  with named placeholders — that's a real bug we hit (see `pitfalls.md`).
- For per-row aggregation across many groups, prefer one big SQL with
  LATERAL joins over N round-trips. The existing backtester SQL is the
  reference pattern.
- Index hint: queries hitting `pm_book` should ALWAYS go through
  `(token_id, ts)`. Without that index, a query against 100M rows will
  cripple the server.

## Async

- All I/O is async. No `requests`, no sync DB calls inside async functions.
- Use a single `BatchWriter` per process for DB writes — don't open
  short-lived connections per event.
- Long-running tasks use a supervisor pattern (see `main.py`): each task
  is named, started by `asyncio.create_task(..., name=...)`, restarted on
  exception with exponential backoff.
- **Always use `asyncio.run()` only at the top entry point.** Inside the
  app, work with the existing loop.

## Streamlit specifics

- Page scripts live in `src/poly_btc/backtest/pages/`. Numbered prefix
  (`1_X.py`, `2_Y.py`) sets sidebar order.
- Streamlit reruns whole script on every widget change. Cache expensive
  computations via `@st.cache_data(ttl=...)`.
- The DB connection pool is process-shared; opening a new
  `psycopg.AsyncConnection` per Streamlit rerun is fine for short queries.
  Use one-shot connections, not the global pool (pools choke on Streamlit's
  changing event loops).
- Widget state persists across reruns but NOT across browser reload. URL
  query params do (see `app.py: _qp_int` etc.). Bind every meaningful
  control to a URL param.

## Tests (when they exist)

Until a test framework lands:
- The first agent to add tests picks the layout: probably `tests/` next to
  `src/`, `pytest` + `pytest-asyncio`, fixtures for DB via testcontainers
  or a local Postgres.
- New behavior must come with tests once the framework is in place.

Anti-pattern to avoid: writing a test that just exercises the code path
without asserting on outputs. A test that always passes is worse than no
test.

## Commits and PRs

- Conventional-style messages: `feat:`, `fix:`, `chore:`, `docs:`, etc.
- One concept per commit. Mixed-concern commits should be split.
- Always update `backlog.md` if you introduce a known limitation or
  remove one.
- For destructive operations (DROP COLUMN, TRUNCATE, drop index): require
  user confirmation in conversation BEFORE running.
