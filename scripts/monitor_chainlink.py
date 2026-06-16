"""Measure the chainlink WSS stream: batch frequency, size, freshness, coverage.

Usage: python scripts/monitor_chainlink.py [duration_sec]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import orjson
from websockets.asyncio.client import connect


async def monitor(label: str, sub: dict, duration: int = 60) -> None:
    print(f"\n--- {label} (monitoring {duration}s) ---")
    async with connect("wss://ws-live-data.polymarket.com", proxy=None) as ws:
        await ws.send(orjson.dumps({"action": "subscribe", "subscriptions": [sub]}).decode())

        batches = 0
        total_ticks = 0
        seen_seconds: set[int] = set()
        sizes: list[int] = []
        first_batch_at: float | None = None
        last_batch_at: float | None = None
        t0 = time.time()

        async def ping() -> None:
            while time.time() - t0 < duration:
                await asyncio.sleep(5)
                try:
                    await ws.send("ping")  # lowercase, matches official TS client
                except Exception:
                    return

        ping_task = asyncio.create_task(ping())

        try:
            while time.time() - t0 < duration:
                remaining = max(0.5, duration - (time.time() - t0))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if raw in ("PONG", "", b""):
                    continue
                if not isinstance(raw, str) or "payload" not in raw:
                    continue
                try:
                    msg = orjson.loads(raw)
                except Exception:
                    continue
                payload = msg.get("payload") or {}
                data = payload.get("data") if isinstance(payload, dict) else payload
                if not isinstance(data, list) or not data:
                    continue

                ticks = [it for it in data if isinstance(it, dict) and "timestamp" in it]
                if not ticks:
                    continue
                batches += 1
                sizes.append(len(ticks))
                total_ticks += len(ticks)
                for it in ticks:
                    seen_seconds.add(int(it["timestamp"]) // 1000)

                now = time.time()
                if first_batch_at is None:
                    first_batch_at = now
                last_batch_at = now
                ts_min = min(int(it["timestamp"]) for it in ticks) // 1000
                ts_max = max(int(it["timestamp"]) for it in ticks) // 1000
                latest_delay = int(now) - ts_max
                print(f"  batch #{batches}: {len(ticks)} ticks  span={ts_max-ts_min+1}s  latest_delay={latest_delay}s")
        finally:
            ping_task.cancel()

        print(f"\nSUMMARY: {batches} batches, {total_ticks} ticks ({len(seen_seconds)} unique seconds)")
        if sizes:
            print(f"  avg batch size: {sum(sizes)/len(sizes):.0f} ticks")
        if first_batch_at and last_batch_at and batches > 1:
            print(f"  avg batch interval: {(last_batch_at - first_batch_at)/(batches-1):.1f}s")
        print(f"  unique-second coverage: {len(seen_seconds)}/{duration} = {len(seen_seconds)/duration*100:.0f}%")


async def main() -> None:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    variant = sys.argv[2] if len(sys.argv) > 2 else "update_filter"

    subs = {
        "update_filter": {"topic": "crypto_prices_chainlink", "type": "update",
                          "filters": json.dumps({"symbol": "btc/usd"})},
        "wildcard_filter": {"topic": "crypto_prices_chainlink", "type": "*",
                            "filters": json.dumps({"symbol": "btc/usd"})},
        "update_nofilter": {"topic": "crypto_prices_chainlink", "type": "update"},
        "wildcard_nofilter": {"topic": "crypto_prices_chainlink", "type": "*"},
        "binance_update": {"topic": "crypto_prices", "type": "update",
                           "filters": "btcusdt"},
    }
    await monitor(f"variant={variant}", subs[variant], duration=duration)


if __name__ == "__main__":
    asyncio.run(main())
