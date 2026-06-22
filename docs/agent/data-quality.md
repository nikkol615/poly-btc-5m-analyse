# Data quality invariants

Rules that the collector and backtester must preserve. Violations almost
always cause silent correctness bugs (not loud crashes), which is the
worst kind.

## Time

- All timestamps in the DB are TIMESTAMPTZ in UTC. Never store local time.
- Polymarket sends timestamps in **milliseconds** (sometimes nanoseconds —
  see `_parse_ts` heuristic in `rtds_client.py`). Always normalize.
- Binance sends `T` (trade time) in milliseconds. Floor to second before
  insert.
- The `markets.window_ts` is a Unix epoch seconds value divisible by 300.
  Don't store other granularities.

## btc_spot

- `(ts, source)` is the primary key. Two ticks at the same second from the
  same source collapse — that's intentional dedup.
- `source` is enumerated: currently `'chainlink'` or `'binance'`. Add a new
  source only if you also update the backtester preference logic.
- `price` should never be negative or zero. (No constraint enforces this —
  add one if you have a chance.)

## pm_book

- `event_type` is one of `{'book', 'best_bid_ask', 'compact'}`. `'price_change'`
  was removed in 2026-06; if you see one in the table, that's pre-fix data.
- For event_type `'book'`, `bid_size` and `ask_size` may be populated.
  For `'best_bid_ask'` and `'compact'`, they are NULL by design.
- `best_bid` and `best_ask` are between 0 and 1 (inclusive). Anything outside
  is a parsing bug.
- The `(token_id, ts)` index is load-bearing — backtester is unusable
  without it.

## markets

- `slug` is the primary key and is deterministic from `window_ts`. Always
  compute as `f"btc-updown-5m-{window_ts}"`.
- `token_up` and `token_down` are decimal-string asset IDs (e.g.
  `"10955796..."`). They are NOT condition_ids. Confusing them is a
  serialization bug — see `pitfalls.md`.
- `outcomes` from Gamma is `["Up", "Down"]`. The order matters; use it to
  match token_ids correctly.
- `compacted_at` is set ONLY by `compactor.py` and ONLY after the squash
  transaction commits. Setting it manually anywhere else risks data loss
  on next compactor pass (it skips markets with `compacted_at IS NOT NULL`).

## Backtester features

- Any feature used to decide whether to enter must be computable from data
  with `ts < t_entry`. Post-entry features go in separate columns and must
  not flow into `_build_trade_row` skip logic.
- Every "fetch latest before T" must return staleness. Every staleness
  must have a max threshold.
- `signal_agreement_pct` of `None` means "no pre-entry data" — treat
  appropriately (currently: don't filter, let the trade through with
  None). If you change this, document why.

## Resolver invariants

- `strike` is meant to be the BTC price AT `window_start`. We currently
  accept the nearest tick within `[-5s, +180s]` due to Chainlink sparsity
  — see `backlog.md` for the fix.
- `close_price` is the nearest tick to `window_end` within
  `[0s, +grace=30s]`. The "nearest" semantic (ABS distance) matters: don't
  switch to "latest before" without re-checking results.
- `resolved_outcome` is computed as `Up if close >= strike else Down`. Do
  NOT call Gamma to fetch the official outcome — the official one uses
  Chainlink's specific aggregation and may disagree with our nearest-tick
  approximation by ~$1-2.

## Compactor invariants

- It only ever touches markets with `window_end < NOW() - 7 days`.
- Within one transaction it INSERTs `compact` rows BEFORE DELETEing the
  originals.
- Backtester treats `'compact'` rows as equivalent to `'best_bid_ask'` for
  query purposes (no event_type filter, just non-null best_bid/ask).
- If you add a new event_type to `pm_book`, decide explicitly whether the
  compactor processes it and whether the backtester reads it. Document the
  choice.
