# Data sources

This codebase pulls from four upstream feeds. Each has a quirk worth
documenting so future changes don't trip over them.

## Polymarket Gamma REST — market discovery

- URL: `https://gamma-api.polymarket.com/markets?slug=btc-updown-5m-<window_ts>`
- We poll one slug at a time, fanning out N candidate slugs around the current
  5-minute boundary (`PROBE_BACK=2`, `PROBE_FORWARD=24` in `gamma.py`)
- The endpoint returns a list with at most one element per slug
- `outcomes`, `clobTokenIds` come back as JSON-encoded strings — must
  `json.loads` them

## Polymarket CLOB WS — order book

- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe with `{"assets_ids": [...], "type": "market", "custom_feature_enabled": true}`
- Dynamic subscribe/unsubscribe: `{"assets_ids": [...], "operation": "subscribe"|"unsubscribe"}`
- Heartbeat: send text `"PING"` every ~10s. Server may respond with `"PONG"`
  (silently dropped by the client)
- Event types observed: `book`, `price_change`, `best_bid_ask`,
  `last_trade_price`, `tick_size_change`, plus optionally `new_market` and
  `market_resolved` if custom features are on
- **Quirk**: each `price_change` event already carries `best_bid` and
  `best_ask` for the affected asset(s). We dropped persistence of these in
  2026-06 because the same top-of-book transition fires a separate
  `best_bid_ask` event that is redundant with the price_change-embedded
  bid/ask. Persisting only `best_bid_ask` cut write volume by ~60-70%
- For `price_change` events, the asset ID is **inside** each item of the
  `price_changes` array, not at the top level. Top-level `market` is the
  condition_id (hex). Confusing the two was a real bug — see
  [agent/pitfalls.md](agent/pitfalls.md)

## Polymarket RTDS — activity & crypto prices

- URL: `wss://ws-live-data.polymarket.com`
- Generic envelope: `{"payload": {"data": [...]}}` — the topic/type is NOT
  echoed back in the envelope, so the client must dispatch on payload shape
- Topics we use:
  - `activity` / `trades` filtered by `market_slug` — pushes live trades
  - `crypto_prices_chainlink` filtered by `btc/usd` — **snapshot-only**
- **CRITICAL QUIRK for `crypto_prices_chainlink`**: after the initial
  subscription you receive ONE snapshot of the last ~60s of ticks, then the
  server sends nothing else. Sending pings does not unstick it. There is no
  documented way to make it stream. We work around this in
  `chainlink_snapshot.py` by closing and re-opening the connection every 45s.
  Each fresh subscribe yields a new ~60s snapshot. Overlapping ticks are
  de-duplicated by the `(ts, source)` primary key on `btc_spot`
- Heartbeat: official TS client sends text `"ping"` (lowercase) every 5s. The
  case matters only in some places; we send `"PING"` historically but have not
  observed differences

## Binance Spot WS — backup BTC/USD

- URL: `wss://stream.binance.com:9443/ws/btcusdt@trade`
- One message per trade, with `T` (trade time, ms) and `p` (price string)
- We floor the trade timestamp to seconds and write to `btc_spot` with
  `source='binance'`. Multiple trades per second collapse to one row via PK
- Reliable 1 Hz coverage, basically zero ops cost
- Used as primary spot reference for backtester features (`btc_range`,
  `crossed_strike`, `signal_agreement`) because Chainlink is too sparse
- Note: Polymarket itself resolves via Chainlink, so for `strike` and
  `close_price` exactness we prefer Chainlink when fresh, else Binance

## Why we don't currently use any auth

All four endpoints serve public read-only data for the BTC market. If/when we
add live trading we'll need CLOB auth credentials (key, secret, passphrase) and
EIP-712 signing keys. None of that is in scope today.
