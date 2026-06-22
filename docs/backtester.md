# Backtester

## Question it answers

> For each resolved 5-minute BTC up/down market, what would my PnL have been if
> I had entered X seconds before close on the side suggested by some signal,
> with bet size Y?

## Inputs

Sidebar controls (persisted in URL query params so reload restores them):

| Param | Type | Default | Effect |
|---|---|---|---|
| `X` | int sec | 20 | Entry offset from `window_end` |
| `Y` | float $ | 10 | Bet size |
| `N` | int | 200 | How many most-recent resolved markets to consider |
| `signal` | enum | `spot_vs_strike` | `spot_vs_strike` (BTC vs strike at t_entry) or `pm_price` (market consensus) |
| `min_ask_cents` | int 0-99 | 10 | Skip trade if chosen side's best_ask < threshold |
| `retry_enabled` | bool | off | If `min_ask_cents` not met initially, retry every R sec |
| `retry_interval_sec` | int | 2 | R |
| `min_signal_agreement_pct` | int 0-100 | 0 | Pre-entry filter: only take trades where ≥ X% of pre-entry BTC ticks confirm the chosen side |
| `max_spot_staleness_sec` | int | 15 | Drop trade if BTC spot tick at entry is older than this |
| `max_book_staleness_sec` | int | 10 | Drop trade if PM book at entry is older than this |

## Pipeline (`core.py`)

```
fetch_resolved_dataset(x_sec, n)   → raw DataFrame (1 row per market, all features)
    │  via single SQL with LATERAL joins (sql.py)
    ▼
apply_strategy(raw, x_sec, y, ...)  → (trades_df, Summary)
    │  pure, no I/O — safe to cache
    └─ _decide_side          signal → side
       _walk_retries         min_ask + retry → entry attempt seconds
       skip / keep classification + PnL math
```

`simulate()` is the convenience wrapper that does both. The UI uses the
cached-dataset path for incremental fetch (see `app.py: cached_dataset`).

## Features per market (SQL output)

Computed once per (slug, X) by a single LATERAL-heavy query:

- **Spot**: price + staleness + source at `window_start`, `t_entry`, `window_end`
- **Polymarket book**: best_bid/ask of Up and Down at the same three points
- **Per-second book history** in `[t_entry, window_end]` for both tokens — used
  by retry walk and to compute `pct_above_strike_before_entry`
- **`btc_range_before_entry`**: max - min of binance ticks in `[window_start, t_entry)`
- **`crossed_strike_before_entry`**: count of binance ticks straddling
  `sp_o.price` in `[window_start, t_entry)`
- **`pct_above_strike_before_entry`**: fraction of binance ticks at-or-above
  strike, pre-entry. The Python side derives `signal_agreement_pct` from this
  and the chosen side

## Biases the code actively guards against

These are real bugs we hit:

### 1. Look-ahead bias from sparse spot data
When BTC spot ticks were sparse (Chainlink-only era), the "latest tick before
t_entry" was often the same physical tick as the "latest before window_end" —
which made our signal LITERALLY a function of the close price. Result: 100%
winrate, $1500 PnL on Y=$10. Hilariously wrong.

**Guard**: `max_spot_staleness_sec` filter drops markets where the tick is
older than the threshold. Binance was added as a denser source.

**Future agents**: if you add a new spot-based feature, ALWAYS expose its
staleness in seconds AND apply a configurable maximum.

### 2. Post-entry features used as filters
We had `btc_range_after_entry` and `crossed_strike_count` (whole window) until
the user pointed out that those are **only knowable after the trade closed** —
so they can't actually be used as a pre-entry decision filter. They were
removed and replaced with `*_before_entry` equivalents.

**Future agents**: any feature used by `_build_trade_row` for filtering MUST
be computable from data with `ts < t_entry`. If you want a post-entry feature
for diagnostic display, put it in a separate "diagnostic" column and DO NOT
allow it to gate the trade.

### 3. Side-vs-token confusion
The token IDs in `markets.token_up` / `token_down` are large decimal strings,
e.g. `"10955796..."`. The `market` field in CLOB events is the condition_id, a
hex string starting `0x...`. Using one in place of the other silently
mis-matches and you get bid/ask values for the wrong outcome.

**Guard**: the SQL uses `r.token_up`/`r.token_down` consistently. The Python
output dict has `"token_up"`/`"token_down"` keys for both.

## Methodology gaps you should know about

The backtester displays Sharpe and Max DD, but:
- Sample sizes are tiny in practice (~50-200 markets); statistics are
  **noisy**. Don't take a positive PnL on N<500 markets seriously.
- No walk-forward / out-of-sample split exists today. If you tune parameters
  on the same data you evaluate on, you'll overfit. See [backlog.md](backlog.md).
- No slippage / fees model. Live trading will underperform backtest.

## Files

```
src/poly_btc/backtest/
  sql.py     – heavy SQL with LATERAL joins, returns raw dataset
  core.py    – pure strategy: _decide_side, _walk_retries, _build_trade_row
  app.py     – Streamlit main page: sidebar, metrics, table, bin charts
  cli.py     – `poly-btc-app` entry point launching streamlit
  pages/
    1_Inspect_market.py   – per-window detail chart
    2_DB_status.py        – DB occupancy + growth forecast
```
