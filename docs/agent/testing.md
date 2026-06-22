# Testing policy

## Current state

There are zero tests. This is not the goal. The goal is to have tests for
every new piece of non-trivial logic, and to add tests as you touch
existing code.

## Setting up the harness (one-time)

When you add the first test:
- `tests/` at the repo root (next to `src/`)
- `pytest` + `pytest-asyncio` in `pyproject.toml` `[project.optional-dependencies] dev`
- `conftest.py` with an async DB fixture. Use testcontainers (`testcontainers[postgres]`)
  for an ephemeral Postgres instance — do NOT point tests at the real DB
- A `pytest.ini` (or `pyproject.toml [tool.pytest.ini_options]`) entry to
  enable asyncio_mode=auto and discover tests under `tests/`

If the user pushes back on testcontainers (Docker dependency), an
alternative is a SQLite test mode — but most of the SQL uses
Postgres-specific syntax (LATERAL, jsonb_agg, DISTINCT ON, make_interval),
so SQLite will require shimming. Probably not worth it.

## What requires a test

Mandatory:
- Every function in `core.py` that decides keep/skip/PnL — needs cases for
  win, loss, skip-stale, skip-low-agreement, skip-bad-price, retry-success,
  retry-exhausted
- Every SQL change in `sql.py` — needs a fixture with synthetic data and a
  property test ("staleness is correct", "no row missing")
- Compactor — before/after backtester output equivalence test
- New `_dispatch` branches in WSS clients — needs a captured fixture
  payload

Optional but encouraged:
- Streamlit page rendering — at least an `from poly_btc.backtest import app`
  import smoke test to catch syntax breakage

## Test data

Do not commit real captured Polymarket data with PII (none currently exists,
but watch for trader addresses in `pm_trades`). Synthetic fixtures are
preferred; if real data is needed, generate a small anonymized sample.

## CI

No CI is configured. When you add tests, add a GitHub Action that runs them
on PR. Minimum: lint (ruff) + tests. Optional: a build of the Docker image.

## Anti-patterns to avoid

- Tests that just call a function and don't assert anything specific
- Tests that depend on the current state of the production DB
- Tests with no comment explaining what bug or invariant they protect
- Mocking everything until the test passes regardless of correctness
