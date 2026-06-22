# poly-btc-5m-analyse

Collector + backtester for Polymarket BTC 5-minute Up/Down markets.

📖 **Full documentation lives in [`docs/`](docs/README.md).** This README is a
quick start; for architecture, schema, deployment, known issues and agent
instructions, follow the links there.

The collector subscribes to:
- **RTDS** (`wss://ws-live-data.polymarket.com`) — Chainlink BTC/USD spot + Polymarket trades per market slug.
- **CLOB market channel** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) — order book snapshots, `best_bid_ask`, and `last_trade_price` per token id.

Discovery is via Gamma REST (`/markets?slug=btc-updown-5m-{window_ts}`), probing a sliding window around the current 5-minute boundary.

## Setup — local (venv)

```bash
cd poly-btc-5m-analyse
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env  # set DATABASE_URL to your remote DSN

python scripts/smoke.py     # verifies DB + Gamma
python -m poly_btc.main     # starts the collector
```

## Setup — Docker (server or local)

```bash
cp .env.example .env  # set DATABASE_URL (same DSN as local)
docker compose up -d --build
docker compose logs -f collector
```

The image is ~150 MB (python:3.12-slim base) and runs as non-root user `app`. No
local Postgres needed — collector connects to whatever `DATABASE_URL` you set.

Schema is auto-applied on startup (idempotent `CREATE TABLE IF NOT EXISTS`), so
the same DB can be shared between dev and prod containers safely.

## Backtester UI

Streamlit app reads the same Postgres and lets you tune strategy params live.

```bash
# local
poly-btc-app                      # http://localhost:8501

# docker (part of the same compose file)
docker compose up -d app          # http://<host>:8501
```

Controls:
- **X** — seconds before close to enter (5–60)
- **Y** — bet size in USD
- **N** — number of most recent resolved markets to backtest on
- **Signal** — `Spot vs Strike` (BTC tilt) or `PM price tilt` (consensus)

Per-trade table includes: side, win/loss, PnL, entry price in cents, spread,
strike, spot at entry, close BTC price, and PM mid prices at open/entry/close.

## Schema

- `markets`        — one row per discovered 5-min window (slug, token ids, strike when known, etc.)
- `btc_spot`       — Chainlink/Binance BTC/USD ticks
- `pm_book`        — best bid/ask snapshots per token id (event_type: `book` / `price_change` / `best_bid_ask`)
- `pm_trades`      — executed trades (from RTDS activity stream and CLOB `last_trade_price`)

## Notes & limitations

- **Strike capture**: the open price (strike) is not in the Gamma response. We rely on having Chainlink ticks at/just after `window_start` recorded in `btc_spot`. A post-processing step (TBD) will join them onto `markets.strike`.
- **History**: backfill is not available. Useful data only starts accumulating after this collector runs.
- **Reconnects**: both WSS clients reconnect with exponential backoff; subscriptions are re-sent from in-memory state on reconnect.

## Layout

```
src/poly_btc/
  config.py       Pydantic settings
  log.py          structlog setup
  schema.sql      Postgres DDL
  db.py           asyncpg pool, batch writer, insert helpers
  gamma.py        REST discovery of active 5m windows
  rtds_client.py  ws-live-data.polymarket.com client (spot + trades)
  clob_client.py  ws-subscriptions-clob.polymarket.com/ws/market client (book)
  main.py         asyncio supervisor (entrypoint: poly-btc-collector)
scripts/smoke.py  DB + Gamma sanity check
```
