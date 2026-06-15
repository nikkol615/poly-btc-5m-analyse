"""Dump raw RTDS + CLOB messages for one current BTC 5m market."""
from __future__ import annotations

import asyncio
import json

import orjson
from websockets.asyncio.client import connect

from poly_btc.config import settings
from poly_btc.gamma import GammaDiscovery


async def dump_rtds(slug: str, n: int = 8) -> None:
    print(f"\n=== RTDS messages (first {n}) ===")
    async with connect(settings.rtds_ws_url, proxy=None) as ws:
        sub = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "update",
                 "filters": json.dumps({"symbol": "btc/usd"})},
                {"topic": "activity", "type": "trades",
                 "filters": json.dumps({"market_slug": slug})},
            ],
        }
        await ws.send(orjson.dumps(sub).decode())
        count = 0
        async for raw in ws:
            if raw == "PONG":
                continue
            try:
                msg = orjson.loads(raw)
            except Exception:
                print("non-json:", raw[:200]); continue
            print(json.dumps(msg, indent=2, default=str)[:1200])
            print("---")
            count += 1
            if count >= n:
                break


async def dump_clob(tokens: list[str], n: int = 8) -> None:
    print(f"\n=== CLOB messages (first {n}) ===")
    async with connect(settings.clob_ws_url, proxy=None) as ws:
        await ws.send(orjson.dumps({
            "assets_ids": tokens, "type": "market", "custom_feature_enabled": True
        }).decode())
        count = 0
        async for raw in ws:
            if raw in ("PONG", "pong"):
                continue
            try:
                msg = orjson.loads(raw)
            except Exception:
                print("non-json:", raw[:200]); continue
            print(json.dumps(msg, indent=2, default=str)[:1500])
            print("---")
            count += 1
            if count >= n:
                break


async def main() -> None:
    async def _noop(m): pass
    async with GammaDiscovery(on_new_market=_noop) as g:
        markets = await g.scan_once()
    target = max(markets, key=lambda m: m["window_ts"] if m["window_start"].timestamp() > 0 else 0)
    # Pick a market that's currently open (window_start <= now < window_end)
    import time
    now = time.time()
    live = [m for m in markets if m["window_start"].timestamp() <= now < m["window_end"].timestamp()]
    target = live[0] if live else markets[0]
    print(f"Target: {target['slug']}  tokens=[Up:{target['token_up'][:16]}..., Down:{target['token_down'][:16]}...]")

    await asyncio.gather(
        dump_rtds(target["slug"]),
        dump_clob([target["token_up"], target["token_down"]]),
    )


if __name__ == "__main__":
    asyncio.run(main())
