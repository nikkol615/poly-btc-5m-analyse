"""Polymarket Real-Time Data Socket (RTDS) client.

Connects to wss://ws-live-data.polymarket.com and subscribes to:
  - crypto_prices_chainlink for BTC/USD spot (used as signal + resolution proxy)
  - activity/trades per market_slug for executed Polymarket trades (backup)

Subscriptions can be added/removed without reconnecting.
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

PING_INTERVAL = 5.0  # seconds
RECONNECT_DELAY_BASE = 1.0
RECONNECT_DELAY_MAX = 30.0


def _sub_chainlink_btc() -> dict[str, Any]:
    return {
        "topic": "crypto_prices_chainlink",
        "type": "update",
        "filters": json.dumps({"symbol": "btc/usd"}),
    }


def _sub_activity_trades(slug: str) -> dict[str, Any]:
    return {
        "topic": "activity",
        "type": "trades",
        "filters": json.dumps({"market_slug": slug}),
    }


def _parse_ts(value: Any) -> datetime:
    """Parse ISO string or numeric (s / ms / us) timestamp to aware UTC."""
    if isinstance(value, (int, float)):
        v = float(value)
        # Heuristic: choose unit by magnitude
        if v > 1e16:
            v /= 1e6
        elif v > 1e13:
            v /= 1e3
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class RTDSClient:
    def __init__(self, writer: BatchWriter) -> None:
        self._writer = writer
        self._ws: ClientConnection | None = None
        self._slugs: set[str] = set()
        self._subscribed_slugs: set[str] = set()
        self._chainlink_subscribed = False
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def add_slug(self, slug: str) -> None:
        self._slugs.add(slug)
        if self._ws is not None and slug not in self._subscribed_slugs:
            await self._send({"action": "subscribe", "subscriptions": [_sub_activity_trades(slug)]})
            self._subscribed_slugs.add(slug)
            log.debug("rtds_subscribed_slug", slug=slug)

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
        subs: list[dict[str, Any]] = [_sub_chainlink_btc()]
        for slug in self._slugs:
            subs.append(_sub_activity_trades(slug))
        await self._send({"action": "subscribe", "subscriptions": subs})
        self._chainlink_subscribed = True
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

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        if raw == "PONG":
            return
        try:
            env = orjson.loads(raw)
        except Exception:
            return
        # The TS client filters by `"payload"` substring; envelope is {topic,type,timestamp,payload,connection_id}.
        payload = env.get("payload") if isinstance(env, dict) else None
        if payload is None:
            return
        topic = env.get("topic")
        ts = _parse_ts(env.get("timestamp"))

        if topic == "crypto_prices_chainlink":
            self._handle_chainlink(ts, payload)
        elif topic == "activity":
            self._handle_activity(ts, env.get("type"), payload)

    def _handle_chainlink(self, ts: datetime, payload: Any) -> None:
        # Payload shape varies; tolerate dict or list
        items = payload if isinstance(payload, list) else [payload]
        for it in items:
            if not isinstance(it, dict):
                continue
            sym = (it.get("symbol") or "").lower()
            if sym and sym != "btc/usd":
                continue
            price = it.get("value") or it.get("price")
            ts_field = it.get("timestamp")
            row_ts = _parse_ts(ts_field) if ts_field is not None else ts
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            self._writer.add_spot(row_ts, "chainlink", price_f)

    def _handle_activity(self, ts: datetime, mtype: str | None, payload: Any) -> None:
        items = payload if isinstance(payload, list) else [payload]
        for it in items:
            if not isinstance(it, dict):
                continue
            row_ts = _parse_ts(it.get("timestamp") or it.get("created_at") or ts)
            price = it.get("price")
            size = it.get("size")
            try:
                price_f = float(price) if price is not None else None
                size_f = float(size) if size is not None else None
            except (TypeError, ValueError):
                continue
            self._writer.add_trade({
                "ts": row_ts,
                "slug": it.get("market_slug") or it.get("slug"),
                "token_id": str(it.get("asset_id") or it.get("token_id") or "") or None,
                "outcome": it.get("outcome"),
                "side": it.get("side") or it.get("trader_side"),
                "price": price_f,
                "size": size_f,
                "tx_hash": it.get("transaction_hash") or it.get("tx_hash"),
                "raw": it,
            })

    async def _run_once(self) -> None:
        log.info("rtds_connecting", url=settings.rtds_ws_url)
        async with connect(settings.rtds_ws_url, max_size=8 * 1024 * 1024) as ws:
            self._ws = ws
            await self._send_initial_subs()
            ping_task = asyncio.create_task(self._ping_loop(), name="rtds-ping")
            try:
                async for raw in ws:
                    await self._handle_message(raw)
            finally:
                ping_task.cancel()
                self._ws = None
                self._chainlink_subscribed = False
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
