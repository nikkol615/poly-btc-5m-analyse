# Backlog and technical debt

This is an honest list of things that are wrong in this codebase. The point is
to NOT normalize these decisions: every entry exists because we cut a corner
that should be fixed before it bites again.

Ordered roughly by pain/value.

## P0 — operational risk

### No backups, no monitoring, no alerts
Disk filled up silently and took down the whole system. Nothing notified
anyone. Postgres has no scheduled `pg_dump`. There is no metric/alert pipeline.
Fix: enable Coolify backups (to S3, not local disk), add a cron that pushes
`df -h` and DB row counts to a Telegram/Discord webhook, and set a Coolify
notification on "container restart count > N within 5 min".

### Single point of failure
One VPS, one Postgres instance, one collector process. Any of them goes down
and we lose data. There's no replication, no standby, no rolling deploy. For
the current scale this is fine; for any "real" use it isn't.

### No tests
**Zero unit tests, zero integration tests.** Bugs (look-ahead bias, side/token
confusion, raw-vs-current mismatch) were caught only by user inspection. Every
non-trivial PR should add tests; today there's no test harness to extend.

## P1 — correctness

### Schema migrations done by raw SQL on every boot
`schema.sql` is re-run from scratch on every collector start. No version
tracking, no rollback, no audit trail. Adding a column means editing the file
and praying. Should adopt Alembic (or Atlas / sqlx-migrate / your pick) and
have a `migrations/` directory with timestamped, ordered up/down scripts.

### `BatchWriter` has no upper bound
`db.BatchWriter` accumulates rows in memory and flushes every 1s. If Postgres
stalls (recovery, lock, network) the buffer grows unbounded → OOM. Should cap
at N rows and apply backpressure (drop with structured log, or block the
producer).

### `compactor` was added without verifying data correctness
The compactor squashes pm_book into 1Hz `compact` rows. There is no test that
running it preserves backtester output. We should have a "before/after"
verification: run backtest on a market, run compactor, run backtest again,
assert results are within tolerance. Add this before turning compactor on in
production by default — currently it just runs.

### `apply_strategy` is O(M × X) per market for retries
For each market we Python-loop through `up_entry_history` / `dn_entry_history`
JSON arrays. With X=60 and R=1 that's 60 attempts per market. At N=1000
markets, ~60K Python iterations per backtest run. Tolerable now, won't scale
beyond ~10K markets in the table. Move the retry walk to SQL with a window
function returning the first qualifying row.

### Strike capture in `resolver.py` has a 180s lookahead
We accept a btc_spot tick up to 180 seconds after `window_start` as the strike
because Chainlink ticks are sparse. This is a pragmatic hack that introduces
up to 3 minutes of skew. Now that Binance is collected at 1Hz, we should use
Binance for strike when within 5 seconds of `window_start` and fall back only
when nothing else is available.

## P2 — UX and consistency

### URL-state management is verbose and manual
`app.py` has ~20 lines of `_qp_int / _qp_bool / _qp_str` plus a per-widget
read + write-back. This should be a small helper module or use a third-party
binding (streamlit-url-state, etc).

### Bin edges and chart colors are hardcoded
`_bin_retry`, `_bin_cross`, `_bin_range`, `_bin_agree` all hardcode bin
boundaries in the page module. Refactor into named constants in `core.py` (or
a small `bins.py`) and let the UI optionally override.

### `cached_dataset` cache survives only per browser session
Closing the tab drops the cache. Pickling to disk (one file per (slug, X))
would survive process restarts and let the user iterate quickly across days.

### Streamlit `app.py` has no error boundaries
A DB connection failure shows a raw Python stack trace to the user. Wrap the
top-level call in try/except and render a friendly "DB unavailable: <reason>,
retry in N seconds" message.

## P3 — observability / hygiene

### Logs are unstructured beyond `structlog` defaults
We log free-form `info` events but don't have a stable schema. A dashboard
(Grafana / OpenSearch) would help, but first decide what fields are
contracts (`event`, `slug`, `token_id`, `latency_ms`).

### No linter or formatter in CI
Code is fine right now but drift will set in. Add `ruff` (or `black + ruff`)
and a GitHub Action that runs them on PR.

### SQL is embedded in Python strings
No syntax highlighting, easy to forget to escape a `%` (real bug we hit
in 2026-06). Could move to `.sql` files loaded via `importlib.resources`, or
adopt a tiny ORM like `pugsql`. Keep raw psycopg though — we have enough
custom CTE / LATERAL logic that a heavy ORM would just get in the way.

### `pm_trades.raw` was dropped in 2026-06 — irreversible
We can't get back the original JSONB payloads of past trades. If you ever
need them for a new feature, you'll have to wait for new trades to arrive
post-fix. Document this assumption explicitly anywhere it matters.

## P4 — strategy / research

### No walk-forward / out-of-sample tooling
The backtester has only one mode: "run on the last N resolved markets". Any
parameter you tune on this same data is overfit. We need:
- A `train/test` split by time range
- A cross-validation harness running the simulation across rolling windows
- Confidence bands or bootstrap CIs on PnL

### No slippage or fee model
The backtester assumes immediate fill at `best_ask` with zero fees. Polymarket
has `takerBaseFee` from the market metadata. Should plumb this into the PnL
math, plus a configurable slippage assumption.

### Live trading bot is unbuilt
Discussed conceptually (see chat history) but not designed in detail. When
this lands it will need: order signing, fill reconciliation, kill switch, risk
limits — none of which exist today.

## Cross-cutting

### Same Docker image used for two unrelated roles
`poly-btc-collector` and `poly-btc-app` ship from the same `Dockerfile` with
different CMD. This means the app image carries asyncio/websockets that it
doesn't need, and the collector image carries Streamlit. Either split into
two images, or accept the modest size overhead and document the choice.

### `apply_schema()` is called by the collector but the app assumes the schema
exists
If the app starts first against an empty DB, it crashes. Move schema apply
into a separate one-shot job, or have both call it idempotently at startup.
