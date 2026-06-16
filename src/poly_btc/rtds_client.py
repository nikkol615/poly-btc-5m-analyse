"""Polymarket Real-Time Data Socket (RTDS) — activity/trades subscription only.

URL:    wss://ws-live-data.polymarket.com
Subs:   {"action":"subscribe","subscriptions":[{topic:"activity",type:"trades",filters:{market_slug}}]}

NOTE: We do NOT subscribe to `crypto_prices_chainlink` here — that topic returns
one snapshot per connection and never pushes updates, so it is handled by
`ChainlinkSnapshotRotator` (which reconnects periodically).

Envelope (observed): {"payload": {"data": [...]}}
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import orjson
import websockets
from websockets.asyncio.client import ClientConnection, connect

from .config import settings
from .db import BatchWriter
from .log import get_logger

log = get_logger(__name__)

PING_INTERVAL = 5.0
RECONNECT_DELAY_BASE = 1.0
RECONNECT_DELAY_MAX = 30.0


def _sub_activity_trades(slug: str) -> dict[str, Any]:
    return {
        "topic": "activity",
        "type": "trades",
        "filters": json.dumps({"market_slug": slug}),
    }


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        v = float(value)
        # Magnitude → unit: 2026 ≈ 1.78e9 s, 1.78e12 ms, 1.78e15 us, 1.78e18 ns
        if v > 1e17:
            v /= 1e9
        elif v > 1e14:
            v /= 1e6
        elif v > 1e11:
            v /= 1e3
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(value, str):
        try:
            if value.isdigit():
                return _parse_ts(int(value))
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class RTDSClient:
    def __init__(self, writer: BatchWriter) -> None:
        self._writer = writer
        self._ws: ClientConnection | None = None
        self._slugs: set[str] = set()
        self._subscribed_slugs: set[str] = set()
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def add_slug(self, slug: str) -> None:
        self._slugs.add(slug)
        if self._ws is not None and slug not in self._subscribed_slugs:
            await self._send({"action": "subscribe",
                              "subscriptions": [_sub_activity_trades(slug)]})
            self._subscribed_slugs.add(slug)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(orjson.dumps(msg).decode())

    async def _send_initial_subs(self) -> None:
        if not self._slugs:
            return
        subs = [_sub_activity_trades(slug) for slug in self._slugs]
        await self._send({"action": "subscribe", "subscriptions": subs})
        self._subscribed_slugs = set(self._slugs)
        log.info("rtds_subscribed_initial", slugs=len(self._slugs))

    async def _ping_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(PING_INTERVAL)
                if self._ws is None:
                    return
                async with self._send_lock:
                    await self._ws.send("PING")
        except Exception:
            return

    def _dispatch_item(self, fallback_ts: datetime, it: dict[str, Any]) -> None:
        # Activity trade — has price/size and market_slug or asset_id
        if "price" in it and ("market_slug" in it or "asset_id" in it or "slug" in it):
            ts = _parse_ts(
                it.get("timestamp")
                or it.get("created_at")
                or it.get("ts")
                or fallback_ts
            )
            self._writer.add_trade({
                "ts": ts,
                "slug": it.get("market_slug") or it.get("slug"),
                "token_id": str(it.get("asset_id") or it.get("token_id") or "") or None,
                "outcome": it.get("outcome"),
                "side": it.get("side") or it.get("trader_side"),
                "price": _f(it.get("price")),
                "size": _f(it.get("size")),
                "tx_hash": it.get("transaction_hash") or it.get("tx_hash"),
                "raw": it,
            })

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode()
            except UnicodeDecodeError:
                return
        if not raw or raw in ("PONG", "pong"):
            return
        if "payload" not in raw:
            return
        try:
            env = orjson.loads(raw)
        except Exception:
            return
        payload = env.get("payload") if isinstance(env, dict) else None
        if payload is None:
            return
        ts = _parse_ts(env.get("timestamp"))

        # payload can be: list of items, or dict with `data` list, or single dict.
        items: list[Any]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            inner = payload.get("data")
            if isinstance(inner, list):
                items = inner
            else:
                items = [payload]
        else:
            return

        for it in items:
            if isinstance(it, dict):
                self._dispatch_item(ts, it)

    async def _run_once(self) -> None:
        log.info("rtds_connecting", url=settings.rtds_ws_url)
        async with connect(settings.rtds_ws_url, max_size=8 * 1024 * 1024,
                           proxy=None) as ws:
            self._ws = ws
            await self._send_initial_subs()
            ping_task = asyncio.create_task(self._ping_loop(), name="rtds-ping")
            try:
                async for raw in ws:
                    await self._handle_message(raw)
            finally:
                ping_task.cancel()
                self._ws = None
                self._subscribed_slugs.clear()

    async def run_forever(self) -> None:
        delay = RECONNECT_DELAY_BASE
        while not self._stop.is_set():
            try:
                await self._run_once()
                delay = RECONNECT_DELAY_BASE
            except asyncio.CancelledError:
                raise
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("rtds_disconnect", error=str(e))
            except Exception as e:
                log.exception("rtds_error", error=str(e))
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
