"""Compress `pm_book` history for markets older than COMPACT_AGE_DAYS.

For each eligible market the worker:
  1. SELECTs all surviving rows (book / price_change / best_bid_ask) for both of
     its tokens within [window_start - 5s, window_end + 30s].
  2. Reduces them to one row per second per token (latest non-null best_bid /
     best_ask within that second) via `DISTINCT ON (date_trunc('second', ts))`.
  3. INSERTs those 1Hz rows back with `event_type = 'compact'`.
  4. DELETEs the original non-compact rows.
  5. Stamps `markets.compacted_at = NOW()`.

Steps 3-5 run in a single transaction per market — atomic, idempotent (filter
on `compacted_at IS NULL`), and safe to interrupt: a half-done market simply
has no `compacted_at` stamp and gets picked up on the next pass.

The backtester SQL filters by `best_ask IS NOT NULL` without restricting
`event_type`, so the swap is transparent — compact rows are picked up the same
way as live `best_bid_ask` rows.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .db import get_conn
from .log import get_logger

log = get_logger(__name__)

COMPACT_AGE_DAYS = 7
LOOP_INTERVAL_SEC = 600          # check every 10 min
BATCH_SIZE = 20                  # markets per pass
PAUSE_BETWEEN_MARKETS_SEC = 0.5  # don't hog the DB


_FETCH_SQL = """
SELECT slug, token_up, token_down, window_start, window_end
FROM markets
WHERE compacted_at IS NULL
  AND window_end < NOW() - make_interval(days => %(age_days)s)
ORDER BY window_end ASC
LIMIT %(batch)s;
"""

_INSERT_COMPACT_SQL = """
INSERT INTO pm_book (ts, token_id, event_type, best_bid, best_ask, bid_size, ask_size)
SELECT DISTINCT ON (date_trunc('second', ts), token_id)
       date_trunc('second', ts)        AS ts,
       token_id,
       'compact'                        AS event_type,
       best_bid, best_ask,
       NULL, NULL
FROM pm_book
WHERE token_id = ANY(%(tokens)s::text[])
  AND ts >= %(t_lo)s AND ts <= %(t_hi)s
  AND event_type <> 'compact'
  AND best_bid IS NOT NULL AND best_ask IS NOT NULL
ORDER BY date_trunc('second', ts), token_id, ts DESC;
"""

_DELETE_ORIGINALS_SQL = """
DELETE FROM pm_book
WHERE token_id = ANY(%(tokens)s::text[])
  AND ts >= %(t_lo)s AND ts <= %(t_hi)s
  AND event_type <> 'compact';
"""

_STAMP_SQL = "UPDATE markets SET compacted_at = NOW() WHERE slug = %(slug)s;"


async def _compact_one(slug: str, tokens: list[str],
                       t_lo: Any, t_hi: Any) -> tuple[int, int]:
    """One market, one transaction. Returns (inserted_compact_rows, deleted_rows)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_INSERT_COMPACT_SQL,
                              {"tokens": tokens, "t_lo": t_lo, "t_hi": t_hi})
            inserted = cur.rowcount or 0
            await cur.execute(_DELETE_ORIGINALS_SQL,
                              {"tokens": tokens, "t_lo": t_lo, "t_hi": t_hi})
            deleted = cur.rowcount or 0
            await cur.execute(_STAMP_SQL, {"slug": slug})
        await conn.commit()
    return inserted, deleted


async def compact_once() -> tuple[int, int, int]:
    """One pass. Returns (markets_processed, total_inserted, total_deleted)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_FETCH_SQL,
                              {"age_days": COMPACT_AGE_DAYS, "batch": BATCH_SIZE})
            markets = await cur.fetchall()

    if not markets:
        return 0, 0, 0

    n, total_in, total_del = 0, 0, 0
    from datetime import timedelta
    for m in markets:
        tokens = [m["token_up"], m["token_down"]]
        t_lo = m["window_start"] - timedelta(seconds=5)
        t_hi = m["window_end"] + timedelta(seconds=30)
        try:
            ins, dele = await _compact_one(m["slug"], tokens, t_lo, t_hi)
            n += 1
            total_in += ins
            total_del += dele
            log.debug("compacted_market", slug=m["slug"],
                      inserted=ins, deleted=dele, freed_rows=dele - ins)
        except Exception as e:
            log.exception("compact_market_failed", slug=m["slug"], error=str(e))
        await asyncio.sleep(PAUSE_BETWEEN_MARKETS_SEC)

    if n:
        log.info("compactor_pass_done",
                 markets=n, inserted=total_in, deleted=total_del,
                 freed_rows=total_del - total_in)
    return n, total_in, total_del


async def run_forever() -> None:
    log.info("compactor_started",
             age_days=COMPACT_AGE_DAYS,
             interval_sec=LOOP_INTERVAL_SEC,
             batch=BATCH_SIZE)
    while True:
        try:
            n, ins, dele = await compact_once()
            # If we got a full batch, immediately try another pass — there is
            # likely a backlog (e.g., catching up on history).
            if n >= BATCH_SIZE:
                continue
        except Exception as e:
            log.exception("compactor_loop_error", error=str(e))
        await asyncio.sleep(LOOP_INTERVAL_SEC)
