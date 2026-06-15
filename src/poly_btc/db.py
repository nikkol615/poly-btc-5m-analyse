from __future__ import annotations

import asyncio
import importlib.resources as resources
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .config import settings
from .log import get_logger

log = get_logger(__name__)

_pool: AsyncConnectionPool | None = None


def _schema_sql() -> str:
    return resources.files("poly_btc").joinpath("schema.sql").read_text()


async def init_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await _pool.open()
        await _pool.wait()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_conn():
    pool = await init_pool()
    async with pool.connection() as conn:
        yield conn


async def apply_schema() -> None:
    sql = _schema_sql()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
        await conn.commit()
    log.info("schema_applied")


# ----- Writers -----

async def upsert_market(m: dict[str, Any]) -> None:
    """Insert or update a market row (idempotent)."""
    sql = """
    INSERT INTO markets (
        slug, market_id, condition_id, question_id, question,
        window_ts, window_start, window_end, end_date,
        token_up, token_down, tick_size, raw, discovered_at, last_seen_at
    ) VALUES (
        %(slug)s, %(market_id)s, %(condition_id)s, %(question_id)s, %(question)s,
        %(window_ts)s, %(window_start)s, %(window_end)s, %(end_date)s,
        %(token_up)s, %(token_down)s, %(tick_size)s, %(raw)s, NOW(), NOW()
    )
    ON CONFLICT (slug) DO UPDATE SET
        last_seen_at = NOW(),
        end_date = EXCLUDED.end_date,
        tick_size = COALESCE(markets.tick_size, EXCLUDED.tick_size);
    """
    payload = {**m, "raw": Jsonb(m["raw"])}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, payload)
        await conn.commit()


async def insert_btc_spot_many(rows: Sequence[tuple[datetime, str, float]]) -> None:
    if not rows:
        return
    sql = """
    INSERT INTO btc_spot (ts, source, price) VALUES (%s, %s, %s)
    ON CONFLICT (ts, source) DO NOTHING;
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
        await conn.commit()


async def insert_pm_book_many(
    rows: Sequence[tuple[datetime, str, str, float | None, float | None, float | None, float | None]],
) -> None:
    if not rows:
        return
    sql = """
    INSERT INTO pm_book (ts, token_id, event_type, best_bid, best_ask, bid_size, ask_size)
    VALUES (%s, %s, %s, %s, %s, %s, %s);
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
        await conn.commit()


async def insert_pm_trades_many(rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = """
    INSERT INTO pm_trades (ts, slug, token_id, outcome, side, price, size, tx_hash, raw)
    VALUES (%(ts)s, %(slug)s, %(token_id)s, %(outcome)s, %(side)s, %(price)s, %(size)s, %(tx_hash)s, %(raw)s);
    """
    payload = [{**r, "raw": Jsonb(r.get("raw"))} for r in rows]
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, payload)
        await conn.commit()


# ----- Batch writer -----

class BatchWriter:
    """Buffers rows and flushes to DB at fixed interval or when buffer is full."""

    def __init__(self, flush_interval: float = 1.0, max_buffer: int = 500) -> None:
        self.flush_interval = flush_interval
        self.max_buffer = max_buffer
        self._spot: list[tuple[datetime, str, float]] = []
        self._book: list[tuple[datetime, str, str, float | None, float | None, float | None, float | None]] = []
        self._trades: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False

    def add_spot(self, ts: datetime, source: str, price: float) -> None:
        self._spot.append((ts, source, price))

    def add_book(
        self,
        ts: datetime,
        token_id: str,
        event_type: str,
        best_bid: float | None,
        best_ask: float | None,
        bid_size: float | None,
        ask_size: float | None,
    ) -> None:
        self._book.append((ts, token_id, event_type, best_bid, best_ask, bid_size, ask_size))

    def add_trade(self, row: dict[str, Any]) -> None:
        self._trades.append(row)

    async def _flush(self) -> None:
        async with self._lock:
            spot, book, trades = self._spot, self._book, self._trades
            self._spot, self._book, self._trades = [], [], []
        try:
            await asyncio.gather(
                insert_btc_spot_many(spot),
                insert_pm_book_many(book),
                insert_pm_trades_many(trades),
            )
            if spot or book or trades:
                log.debug("flushed", spot=len(spot), book=len(book), trades=len(trades))
        except Exception as e:
            log.exception("flush_failed", error=str(e))

    async def _runner(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.flush_interval)
            await self._flush()
        await self._flush()  # final

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._runner(), name="batch-writer")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            await self._task


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
