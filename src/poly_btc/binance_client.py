"""Binance spot trade WSS — reliable 1Hz BTC/USDT source.

URL:    wss://stream.binance.com:9443/ws/btcusdt@trade
Frame:  {"e":"trade","E":..,"s":"BTCUSDT","t":..,"p":"<price>","q":..,"T":<trade_ms>,...}

We floor trade timestamps to seconds so the (ts, source) primary key naturally
de-duplicates the many trades-per-second into one tick per second per source.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import orjson
import websockets
from websockets.asyncio.client import connect

from .db import BatchWriter
from .log import get_logger

log = get_logger(__name__)

URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
RECONNECT_DELAY_BASE = 1.0
RECONNECT_DELAY_MAX = 30.0


def _floor_to_sec(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


class BinanceSpotClient:
    def __init__(self, writer: BatchWriter) -> None:
        self._writer = writer
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def _run_once(self) -> None:
        log.info("binance_connecting", url=URL)
        async with connect(URL, proxy=None, ping_interval=20, ping_timeout=10) as ws:
            log.info("binance_subscribed")
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    msg = orjson.loads(raw)
                except Exception:
                    continue
                if msg.get("e") != "trade":
                    continue
                ts_ms = msg.get("T")
                price = msg.get("p")
                if ts_ms is None or price is None:
                    continue
                try:
                    self._writer.add_spot(_floor_to_sec(int(ts_ms)), "binance", float(price))
                except (TypeError, ValueError):
                    continue

    async def run_forever(self) -> None:
        delay = RECONNECT_DELAY_BASE
        while not self._stop.is_set():
            try:
                await self._run_once()
                delay = RECONNECT_DELAY_BASE
            except asyncio.CancelledError:
                raise
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("binance_disconnect", error=str(e))
            except Exception as e:
                log.exception("binance_error", error=str(e))
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
