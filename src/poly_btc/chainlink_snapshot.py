"""Polymarket RTDS chainlink rotator.

The `crypto_prices_chainlink` topic on `ws-live-data.polymarket.com` returns one
snapshot (≈60s of history) on subscribe and then nothing — there is no live push.
To keep `btc_spot` dense, we open a fresh connection on a short interval, capture
the snapshot, and close.

Successive snapshots overlap by `(SNAPSHOT_SECONDS - ROTATE_EVERY_SEC)` seconds;
overlapping ticks are silently de-duplicated by the `(ts, source)` primary key.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import orjson
import websockets
from websockets.asyncio.client import connect

from .config import settings
from .db import BatchWriter
from .log import get_logger

log = get_logger(__name__)

ROTATE_EVERY_SEC = 45            # open a fresh connection this often
SNAPSHOT_WAIT_SEC = 4            # read messages for this long per connection
SNAPSHOT_SECONDS = 60            # rough size of each snapshot (informational)


def _floor_to_sec(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def _sub() -> dict[str, Any]:
    return {
        "topic": "crypto_prices_chainlink",
        "type": "update",
        "filters": json.dumps({"symbol": "btc/usd"}),
    }


class ChainlinkSnapshotRotator:
    def __init__(self, writer: BatchWriter) -> None:
        self._writer = writer
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def _capture_one(self) -> int:
        """Open a connection, send subscribe, drain for SNAPSHOT_WAIT_SEC, close.
        Returns the number of ticks written from this snapshot."""
        written = 0
        try:
            async with connect(settings.rtds_ws_url, proxy=None,
                               ping_interval=20, ping_timeout=10,
                               max_size=8 * 1024 * 1024) as ws:
                await ws.send(orjson.dumps({"action": "subscribe",
                                            "subscriptions": [_sub()]}).decode())
                deadline = asyncio.get_event_loop().time() + SNAPSHOT_WAIT_SEC
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    if not isinstance(raw, str):
                        try:
                            raw = raw.decode()
                        except UnicodeDecodeError:
                            continue
                    if not raw or "payload" not in raw:
                        continue
                    try:
                        env = orjson.loads(raw)
                    except Exception:
                        continue
                    payload = env.get("payload") if isinstance(env, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    data = payload.get("data")
                    if not isinstance(data, list):
                        continue
                    for it in data:
                        if not isinstance(it, dict):
                            continue
                        try:
                            ts = _floor_to_sec(int(it["timestamp"]))
                            price = float(it["value"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        self._writer.add_spot(ts, "chainlink", price)
                        written += 1
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("chainlink_snapshot_disconnect", error=str(e))
        except Exception as e:
            log.exception("chainlink_snapshot_error", error=str(e))
        return written

    async def run_forever(self) -> None:
        log.info("chainlink_rotator_started",
                 rotate_every=ROTATE_EVERY_SEC, snapshot_wait=SNAPSHOT_WAIT_SEC)
        while not self._stop.is_set():
            t0 = asyncio.get_event_loop().time()
            written = await self._capture_one()
            elapsed = asyncio.get_event_loop().time() - t0
            log.debug("chainlink_snapshot_done", ticks=written, took_sec=round(elapsed, 1))
            sleep_for = max(0.0, ROTATE_EVERY_SEC - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
