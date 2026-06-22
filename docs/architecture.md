# Architecture

## What the project does

Collects Polymarket BTC 5-minute Up/Down market data plus reference BTC/USD
spot, and provides a Streamlit-based backtester for strategies of the form
"enter X seconds before window close, on the side suggested by some signal".

## Two deployables

```
poly-btc-collector   (long-running)   writes to Postgres
poly-btc-app         (Streamlit)      reads from Postgres
```

Same Docker image, different command. Both speak to the same Postgres database.

## Data flow

```
                 ┌─── Gamma REST ──────┐
                 │  market discovery   │
                 ▼
        ┌───────────────────┐
        │  Postgres        │
        │   markets        │ ←──── resolver (joins spot)
        │   btc_spot       │
        │   pm_book        │ ←──── compactor (7-day 1Hz squash)
        │   pm_trades      │
        └─────────▲─────────┘
                  │
   ┌──────────────┼──────────────┬──────────────┐
   │              │              │              │
RTDS WSS    Chainlink WSS    Binance WSS    CLOB WSS
(activity)  (snapshot rot.)  (btcusdt@trade) (book/best_bid_ask)
```

## Processes inside `poly-btc-collector`

All run as asyncio tasks under one supervisor (`src/poly_btc/main.py`):

| Task | Module | Purpose |
|---|---|---|
| `discovery` | `gamma.py` | Probes `gamma-api.polymarket.com` every 30s for new `btc-updown-5m-*` markets |
| `rtds` | `rtds_client.py` | `wss://ws-live-data.polymarket.com` — `activity/trades` per slug |
| `clob` | `clob_client.py` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` — book/best_bid_ask per token id |
| `binance` | `binance_client.py` | `wss://stream.binance.com:9443/ws/btcusdt@trade` — 1Hz spot |
| `chainlink-rotator` | `chainlink_snapshot.py` | Rotates a new RTDS connection every 45s to fetch the 60-sec history snapshot (the topic does not push live updates — see [data-sources.md](data-sources.md)) |
| `resolver` | `resolver.py` | Joins `markets` with `btc_spot` to fill `strike`, `close_price`, `resolved_outcome` |
| `compactor` | `compactor.py` | After 7 days, replaces full pm_book rows for a market with 1Hz `compact` snapshots |

All websocket clients share a single `BatchWriter` (`db.py`) that buffers
inserts and flushes every 1s.

## Streamlit app structure

```
src/poly_btc/backtest/
  app.py                          main "Backtester" page
  cli.py                          launcher: `poly-btc-app`
  core.py                         apply_strategy() — pure, signal+filters
  sql.py                          fetch_resolved_dataset(), fetch_market_detail()
  pages/
    1_Inspect_market.py           per-window deep dive
    2_DB_status.py                size + growth forecast
```

Streamlit's multi-page mode auto-discovers `pages/` siblings to `app.py`.

State (filter values, selected slug) lives in `st.query_params` so a browser
reload restores the view; cache of fetched market data lives in
`st.session_state` keyed by `(slug, x_sec)`.

## Local config

`.env` at repo root with at minimum `DATABASE_URL`. On the server this is set
via Coolify environment variables. See [deployment.md](deployment.md).
