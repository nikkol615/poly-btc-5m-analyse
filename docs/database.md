# Database

## Engine

Postgres 17 (Alpine), running in Coolify-managed container. Schema is auto-
applied on every collector start via `db.apply_schema()` which executes
`schema.sql` (idempotent — uses `CREATE TABLE IF NOT EXISTS` and
`ALTER TABLE ... IF NOT EXISTS / IF EXISTS`).

There is no migration framework (no Alembic, no version table). See
[backlog.md](backlog.md) — this should change.

## Tables

### `markets`

One row per discovered 5-minute window.

| Column | Meaning |
|---|---|
| `slug` (PK) | `btc-updown-5m-<window_ts>` |
| `window_ts` | Unix epoch of window start (also encoded in slug) |
| `window_start`, `window_end` | TIMESTAMPTZ derived from `window_ts` (+ 300s) |
| `end_date` | From Gamma `endDate` |
| `token_up`, `token_down` | Polymarket CLOB asset IDs (large decimal strings) |
| `tick_size` | From Gamma, usually 0.01 |
| `strike`, `close_price`, `resolved_outcome` | Filled by `resolver.py` |
| `compacted_at` | Set by `compactor.py` once this market's pm_book rows are squashed |
| `raw` (JSONB) | Original Gamma payload — kept for debugging |

### `btc_spot`

Reference BTC/USD prices from multiple sources.

| Column | Meaning |
|---|---|
| `ts`, `source` (PK) | Timestamp (floored to second) and `'chainlink'`/`'binance'` |
| `price` | NUMERIC(20, 10) |

The primary key on `(ts, source)` silently dedupes — multiple Binance trades in
the same second collapse to one row.

### `pm_book`

Top-of-book over time per Polymarket asset (token).

| Column | Meaning |
|---|---|
| `id` (PK) | BIGSERIAL |
| `ts`, `token_id` | When the event happened, and for which token |
| `event_type` | `'book'`, `'best_bid_ask'`, or `'compact'` |
| `best_bid`, `best_ask` | NUMERIC(8, 6) |
| `bid_size`, `ask_size` | Only filled for `event_type='book'` |

Note: `price_change` events from Polymarket are **not persisted** since 2026-06.
They were redundant with `best_bid_ask` and made up 60-70% of write volume. See
the dispatcher in `clob_client.py`.

### `pm_trades`

Executed trades from CLOB `last_trade_price` events and RTDS `activity/trades`.

| Column | Meaning |
|---|---|
| `id` (PK) | BIGSERIAL |
| `ts`, `slug` (or `token_id`), `outcome`, `side`, `price`, `size`, `tx_hash` | self-explanatory |

The historical `raw JSONB` column was dropped in 2026-06 to save space.

## Indexes

- `idx_pm_book_token_ts (token_id, ts DESC)` — central for backtester lookups
- `idx_btc_spot_source_ts (source, ts DESC)`
- `idx_pm_trades_slug_ts`, `idx_pm_trades_token_ts`
- `idx_markets_window_ts`, `idx_markets_window_end`
- `idx_markets_compacted_pending (window_end) WHERE compacted_at IS NULL`

## Growth model

Per resolved market, written by the collector (post 2026-06 fix, without
`price_change`):

| Source | ~ bytes per market |
|---|---|
| `pm_book` (book + best_bid_ask snapshots) | 400-800 KB raw, depends on activity |
| `pm_book` after 7-day compaction | ~120 KB (1Hz × 300s × 2 tokens × ~200B) |
| `pm_trades` | ~40 KB |
| `btc_spot` (shared across windows) | trivial (~5 MB/day total) |

Steady-state per-day at 288 markets/day:
- fresh writes: ~70-150 MB/day
- after compaction kicks in (day 7+): ~10-15 MB/day steady state
- standing 7-day uncompacted buffer: ~500 MB - 1 GB

## Compaction details

`compactor.py` runs as a supervisor task. Every 10 minutes:
1. SELECT up to 20 markets with `window_end < NOW() - 7d AND compacted_at IS NULL`
2. Per market, in one transaction:
   - `INSERT INTO pm_book SELECT DISTINCT ON (date_trunc('second', ts), token_id)
      ... 'compact' AS event_type ...` for the market's tokens within
      `[window_start - 5s, window_end + 30s]`
   - `DELETE FROM pm_book WHERE token_id IN (...) AND ts BETWEEN ... AND event_type <> 'compact'`
   - `UPDATE markets SET compacted_at = NOW()`

Atomic and idempotent: if interrupted mid-pass, the next pass picks up where
this one left off because `compacted_at` is set only after the whole
transaction commits.

## Caveats and gotchas

- **`VACUUM FULL` requires ~2× table size of free disk space.** With pm_book
  often >50 GB, this is dangerous. Prefer `TRUNCATE` (instant, no bloat, no
  intermediate space) when you can afford to lose data; or rebuild table via
  `CREATE TABLE … AS SELECT` + DROP + RENAME, which still needs ~1× size free.

- **`DELETE` does not return disk to the OS.** It marks tuples dead. Plain
  `VACUUM` (not FULL) only marks the freed pages reusable. Don't `DELETE` to
  free disk space — partition + drop, or TRUNCATE.

- **`pg_wal` can grow large** under heavy write load if checkpoints are slow.
  We don't currently tune `max_wal_size`. See [backlog.md](backlog.md).

- **OID-to-table mapping** is not stable across re-creations. `SELECT relname
  FROM pg_class WHERE oid = <n>` resolves it at query time.

## See also

- [data-sources.md](data-sources.md) — what fills each table
- [backlog.md](backlog.md) — schema items that should be improved
