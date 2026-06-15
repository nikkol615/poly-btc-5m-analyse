"""Polymarket CLOB market-channel WebSocket client.

URL:     wss://ws-subscriptions-clob.polymarket.com/ws/market
Subs:    {assets_ids, type:"market", custom_feature_enabled:true}
Dynamic: send {assets_ids, operation:"subscribe"|"unsubscribe"} to change set.
Events:  book, price_change, last_trade_price, best_bid_ask, tick_size_change, new_market, market_resolved
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
        if v > 1e16:
            v /= 1e6
        elif v > 1e13:
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


class _Book:
    """Per-token order book; tracks price -> size for bids and asks."""

    __slots__ = ("bids", "asks")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def reset(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = {float(b["price"]): float(b["size"]) for b in bids if _f(b.get("size"))}
        self.asks = {float(a["price"]): float(a["size"]) for a in asks if _f(a.get("size"))}

    def apply_change(self, side: str, price: float, size: float) -> None:
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def best(self) -> tuple[float | None, float | None, float | None, float | None]:
        best_bid = max(self.bids) if self.bids else None
        best_ask = min(self.asks) if self.asks else None
        bid_sz = self.bids.get(best_bid) if best_bid is not None else None
        ask_sz = self.asks.get(best_ask) if best_ask is not None else None
        return best_bid, best_ask, bid_sz, ask_sz


class CLOBClient:
    def __init__(self, writer: BatchWriter) -> None:
        self._writer = writer
        self._ws: ClientConnection | None = None
        self._tokens: set[str] = set()
        self._subscribed: set[str] = set()
        self._books: dict[str, _Book] = {}
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def add_tokens(self, *tokens: str) -> None:
        new = [t for t in tokens if t not in self._tokens]
        for t in new:
            self._tokens.add(t)
            self._books.setdefault(t, _Book())
        if self._ws is not None and new:
            unsubscribed = [t for t in new if t not in self._subscribed]
            if unsubscribed:
                await self._send({
                    "assets_ids": unsubscribed,
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                })
                self._subscribed.update(unsubscribed)
                log.debug("clob_subscribed_tokens", count=len(unsubscribed))

    async def remove_tokens(self, *tokens: str) -> None:
        to_drop = [t for t in tokens if t in self._subscribed]
        for t in tokens:
            self._tokens.discard(t)
            self._books.pop(t, None)
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

    def _emit_book_snapshot(self, ts: datetime, token_id: str, event_type: str) -> None:
        book = self._books.get(token_id)
        if book is None:
            return
        bid, ask, bid_sz, ask_sz = book.best()
        self._writer.add_book(ts, token_id, event_type, bid, ask, bid_sz, ask_sz)

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        if raw in ("PONG", "pong"):
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
        asset_id = str(ev.get("asset_id") or ev.get("market") or "") or None

        if et == "book" and asset_id:
            book = self._books.setdefault(asset_id, _Book())
            book.reset(ev.get("bids") or [], ev.get("asks") or [])
            self._emit_book_snapshot(ts, asset_id, "book")

        elif et == "price_change" and asset_id:
            book = self._books.setdefault(asset_id, _Book())
            for ch in ev.get("changes") or []:
                p = _f(ch.get("price"))
                s = _f(ch.get("size"))
                side = ch.get("side") or ""
                if p is None or s is None or not side:
                    continue
                book.apply_change(side, p, s)
            self._emit_book_snapshot(ts, asset_id, "price_change")

        elif et == "best_bid_ask" and asset_id:
            self._writer.add_book(
                ts, asset_id, "best_bid_ask",
                _f(ev.get("best_bid")), _f(ev.get("best_ask")),
                _f(ev.get("best_bid_size")), _f(ev.get("best_ask_size")),
            )

        elif et == "last_trade_price" and asset_id:
            self._writer.add_trade({
                "ts": ts,
                "slug": None,
                "token_id": asset_id,
                "outcome": None,
                "side": ev.get("side"),
                "price": _f(ev.get("price")),
                "size": _f(ev.get("size")),
                "tx_hash": None,
                "raw": ev,
            })

    async def _run_once(self) -> None:
        log.info("clob_connecting", url=settings.clob_ws_url, tokens=len(self._tokens))
        async with connect(settings.clob_ws_url, max_size=16 * 1024 * 1024) as ws:
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
