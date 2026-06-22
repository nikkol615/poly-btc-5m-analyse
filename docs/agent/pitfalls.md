# Pitfalls — real bugs hit in this codebase

If you find yourself about to do one of these things, stop.

## 1. Using "latest tick before T" without measuring staleness

**The bug**: BTC spot data was sparse. The query "latest btc_spot tick where
ts <= t_entry" returned the SAME physical tick as "latest btc_spot tick where
ts <= window_end". So our "signal at entry" was literally the close price.
Backtest showed 100% winrate, +$1500 PnL on $10 bets.

**Why it's seductive**: the SQL looks innocent. There's no obvious red flag.

**Prevention**:
- ANY feature that does a lateral `SELECT ... WHERE ts <= some_target ORDER BY ts DESC LIMIT 1` must ALSO return `EXTRACT(EPOCH FROM (target - row.ts)) AS staleness`
- The backtester must filter on a configurable max staleness
- A test should construct a sparse-data scenario and assert the affected
  trades are dropped (no such test exists yet — please add one if you touch
  this area)

## 2. Mixing condition_id and asset_id

**The bug**: in CLOB `price_change` events, the top-level `market` field is
the condition_id (hex string `0x...`). The actual asset/token ID is INSIDE
each item of the `price_changes` array. Initial code took the top-level
field as the token id. Result: every row had its `token_id` set to the
condition_id, no rows had usable bid/ask data, and the backtester returned
NaN for entries.

**Prevention**:
- When parsing any Polymarket event, double check the schema by looking at
  a real captured payload, not by guessing from the docs.
- Test new dispatch logic by capturing 1-2 real events and asserting the
  parsed `token_id` matches the asset's `clobTokenIds` from Gamma.

## 3. Post-entry features used as pre-entry filters

**The bug**: `btc_range_after_entry` and `crossed_strike_count` (whole
window) were added as "features". The user immediately pointed out: you
can't use post-entry information to decide whether to enter. They were
removed.

**Prevention**:
- For every feature in `_build_trade_row`, ask: "is this value KNOWN at
  `t_entry` time in a real-time trading scenario?" If not, it must NOT
  gate the trade. Diagnostic-only features go in their own column with a
  clear name (`*_after_entry` or `*_post_trade`) and are excluded from
  filter logic by convention.

## 4. `%` in SQL comments breaks psycopg parameter binding

**The bug**: a SQL comment `-- % of pre-entry ticks above strike` caused
`psycopg.ProgrammingError: incomplete placeholder: '%'`. The `%` in the
comment was parsed as a parameter placeholder.

**Prevention**:
- Never write a bare `%` in SQL passed to `psycopg.cursor.execute` with
  named parameters. Either escape it (`%%`) or rephrase the comment
  ("percentage" instead of "%").
- Lint rule worth adding: regex search for `'.*%[^%(].*'` in SQL strings.

## 5. Streamlit page reading from a shared connection pool

**The bug**: `simulate()` used the module-level psycopg pool. Streamlit
spawns a fresh event loop per rerun; connections in the pool were bound to
the previous (now-dead) loop. Result: `server closed the connection
unexpectedly` on every refresh after the first.

**Prevention**:
- Streamlit pages use ONE-SHOT `psycopg.AsyncConnection.connect(...)` per
  query, NOT a global pool. The collector can use a pool because its event
  loop lives for the process lifetime.

## 6. Collector and app sharing one Coolify resource

**The bug**: the Coolify resource was configured to publish port 8501 with
Traefik routing, but the Dockerfile CMD was `poly-btc-collector` (no HTTP).
Traefik routed traffic into a process that doesn't listen → user got 502s
or empty pages.

**Prevention**:
- Two Coolify resources, same image, different CMD via the Coolify
  "Custom Start Command" field. Only the `poly-btc-app` resource publishes
  port 8501.
- OR, switch to the docker-compose build pack which defines both services
  in one file.

## 7. Hairpin NAT via public IP

**The bug**: `DATABASE_URL` pointed at the host's public IP. When Coolify
restarted Postgres without re-publishing the port, the container-to-host-
to-container path silently broke. Error: `server closed the connection
unexpectedly`.

**Prevention**:
- Always use the Postgres container name (or service alias) as the
  hostname in the server-side `DATABASE_URL`. The `coolify` Docker network
  has internal DNS.
- For local dev, use an SSH tunnel to the container, not the public IP.

## 8. `DELETE` to free disk space

**Anti-pattern**: deleting rows to free disk on a full filesystem. `DELETE`
writes WAL (so the disk gets EVEN MORE full short-term) and dead tuples
that only `VACUUM FULL` can reclaim. `VACUUM FULL` needs 2× the table size
free.

**Prevention**:
- If disk is full and you need to free space NOW: `TRUNCATE` (instant, no
  WAL, no bloat). If you can't lose data: `CREATE TABLE new AS SELECT ...`
  + `DROP` + `RENAME` if you have ~1× free disk.
- Partition by date for large tables that need TTL cleanup. Drop entire
  partitions instead of deleting rows.

## 9. Treating absence-of-data as zero

**Pattern to watch**: when a market has no Binance ticks (e.g. Binance was
down for that window), `signal_agreement_pct` ends up `None`. The original
code path skipped the filter "if it's None", which silently let through
trades that should have been gated. The current code is intentional about
this (`if pct_above is not None: ... else: skip`), but easy to revert
accidentally.

**Prevention**: when a feature is `None`, EITHER skip the trade OR fall
back to a defined default. Never silently pass through.

## 10. Reading docs without verifying against live data

The Polymarket and Streamlit docs are correct in spirit but missing
specifics or sometimes drifted. Several times the right answer came from
dumping live messages or reading the official client source. When in
doubt, dump the wire format yourself.
