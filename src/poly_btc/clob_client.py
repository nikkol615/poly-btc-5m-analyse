"""Polymarket CLOB market-channel WebSocket client.

URL:     wss://ws-subscriptions-clob.polymarket.com/ws/market
Subs:    {assets_ids, type:"market", custom_feature_enabled:true}
Dynamic: {assets_ids, operation:"subscribe"|"unsubscribe"}.

Event shapes (observed):
  book           {market, asset_id, timestamp, hash, bids:[{price,size}], asks:[{price,size}]}
  price_change   {market, timestamp, event_type, price_changes:[{asset_id,price,size,side,hash,best_bid,best_ask}]}
  best_bid_ask   {market, asset_id, best_bid, best_ask, spread, timestamp, event_type}
  last_trade_price {market, asset_id, price, size, side, fee_rate_bps, timestamp, event_type, transaction_hash}
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import orjson
import websockets
from websockets.asyncio.client import ClientConnection, connect

from .config import settings
from .db import BatchWriter
from .log import get_logger

log = get_logger(__name__)

PING_INTERVAL = 10.0
RECONNECT_DELAY_BASE = 1.0
RECONNECT_DELAY_MAX = 30.0


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        v = float(value)
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


def _top_of_book(levels: list[dict[str, Any]], side: str) -> tuple[float | None, float | None]:
    """levels: [{price, size}]; side='bid' picks max, 'ask' picks min."""
    prices: list[tuple[float, float]] = []
    for lvl in levels:
        p = _f(lvl.get("price"))
        s = _f(lvl.get("size"))
        if p is None or s is None or s <= 0:
            continue
        prices.append((p, s))
    if not prices:
        return None, None
    if side == "bid":
        p, s = max(prices, key=lambda x: x[0])
    else:
        p, s = min(prices, key=lambda x: x[0])
    return p, s


class CLOBClient:
    def __init__(self, writer: BatchWriter) -> None:
        self._writer = writer
        self._ws: ClientConnection | None = None
        self._tokens: set[str] = set()
        self._subscribed: set[str] = set()
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def add_tokens(self, *tokens: str) -> None:
        new = [t for t in tokens if t not in self._tokens]
        for t in new:
            self._tokens.add(t)
        if self._ws is not None and new:
            unsubscribed = [t for t in new if t not in self._subscribed]
            if unsubscribed:
                await self._send({
                    "assets_ids": unsubscribed,
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                })
                self._subscribed.update(unsubscribed)

    async def remove_tokens(self, *tokens: str) -> None:
        to_drop = [t for t in tokens if t in self._subscribed]
        for t in tokens:
            self._tokens.discard(t)
            self._subscribed.discard(t)
        if self._ws is not None and to_drop:
            await self._send({"assets_ids": to_drop, "operation": "unsubscribe"})

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(orjson.dumps(msg).decode())

    async def _send_initial_sub(self) -> None:
        if not self._tokens:
            return
        tokens = list(self._tokens)
        await self._send({
            "assets_ids": tokens,
            "type": "market",
            "custom_feature_enabled": True,
        })
        self._subscribed = set(tokens)
        log.info("clob_subscribed_initial", tokens=len(tokens))

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
            try:
                raw = raw.decode()
            except UnicodeDecodeError:
                return
        if not raw or raw in ("PONG", "pong"):
            return
        try:
            data = orjson.loads(raw)
        except Exception:
            return
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if isinstance(ev, dict):
                self._dispatch(ev)

    def _dispatch(self, ev: dict[str, Any]) -> None:
        et = ev.get("event_type") or ev.get("type")
        ts = _parse_ts(ev.get("timestamp"))

        if et == "book":
            asset_id = str(ev.get("asset_id") or "") or None
            if not asset_id:
                return
            bid, bid_sz = _top_of_book(ev.get("bids") or [], "bid")
            ask, ask_sz = _top_of_book(ev.get("asks") or [], "ask")
            self._writer.add_book(ts, asset_id, "book", bid, ask, bid_sz, ask_sz)

        elif et == "price_change":
            # Skipped on purpose. Each price_change event carries best_bid/best_ask
            # but a matching `best_bid_ask` event is emitted by the server on the
            # same top-of-book transition, so persisting both is redundant.
            # Dropping price_change cuts pm_book write volume by ~60-70%.
            pass

        elif et == "best_bid_ask":
            asset_id = str(ev.get("asset_id") or "") or None
            if not asset_id:
                return
            self._writer.add_book(
                ts, asset_id, "best_bid_ask",
                _f(ev.get("best_bid")), _f(ev.get("best_ask")),
                None, None,
            )

        elif et == "last_trade_price":
            asset_id = str(ev.get("asset_id") or "") or None
            self._writer.add_trade({
                "ts": ts,
                "slug": None,
                "token_id": asset_id,
                "outcome": None,
                "side": ev.get("side"),
                "price": _f(ev.get("price")),
                "size": _f(ev.get("size")),
                "tx_hash": ev.get("transaction_hash"),
                "raw": ev,
            })

    async def _run_once(self) -> None:
        log.info("clob_connecting", url=settings.clob_ws_url, tokens=len(self._tokens))
        async with connect(settings.clob_ws_url, max_size=16 * 1024 * 1024,
                           proxy=None) as ws:
            self._ws = ws
            await self._send_initial_sub()
            ping_task = asyncio.create_task(self._ping_loop(), name="clob-ping")
            try:
                async for raw in ws:
                    await self._handle_message(raw)
            finally:
                ping_task.cancel()
                self._ws = None
                self._subscribed.clear()

    async def run_forever(self) -> None:
        delay = RECONNECT_DELAY_BASE
        while not self._stop.is_set():
            try:
                await self._run_once()
                delay = RECONNECT_DELAY_BASE
            except asyncio.CancelledError:
                raise
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("clob_disconnect", error=str(e))
            except Exception as e:
                log.exception("clob_error", error=str(e))
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
