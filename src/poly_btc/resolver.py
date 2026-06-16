"""Fill `markets.strike`, `close_price`, and `resolved_outcome` from collected ticks.

Polymarket BTC-updown 5m markets resolve via the Chainlink BTC/USD data stream:
  - strike     = first chainlink tick at-or-after window_start
  - close      = last chainlink tick at-or-before window_end
  - outcome    = "Up" if close >= strike else "Down"

We expose a small tolerance (LATE_GRACE_SEC) on the close side because feeds can
publish ticks slightly after the boundary. Resolution waits for that grace
period to elapse before recording the close.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .db import get_conn
from .log import get_logger

log = get_logger(__name__)

LOOP_INTERVAL_SEC = 5.0
# Wait this long past window_end before snapping close_price (lets late ticks land).
LATE_GRACE_SEC = 30
# How far before window_start the tick may be (small slack for clock skew).
STRIKE_LOOKBACK_SEC = 5
# Max seconds AFTER window_start we'll accept as the strike tick. Chainlink WS
# delivers in bursts so the nearest tick may be 1–3 minutes off the boundary.
STRIKE_LOOKAHEAD_SEC = 180


_STRIKE_SQL = """
UPDATE markets m
SET strike = sub.price
FROM (
    SELECT DISTINCT ON (m.slug) m.slug, s.price
    FROM markets m
    JOIN btc_spot s ON s.source = 'chainlink'
                   AND s.ts >= m.window_start - make_interval(secs => %(lookback)s)
                   AND s.ts <= m.window_start + make_interval(secs => %(lookahead)s)
    WHERE m.strike IS NULL
      AND m.window_start <= NOW()
    ORDER BY m.slug, s.ts ASC
) sub
WHERE m.slug = sub.slug
RETURNING m.slug;
"""

_CLOSE_SQL = """
UPDATE markets m
SET close_price = sub.price,
    resolved_outcome = CASE WHEN sub.price >= m.strike THEN 'Up' ELSE 'Down' END
FROM (
    SELECT DISTINCT ON (m.slug) m.slug, s.price
    FROM markets m
    JOIN btc_spot s ON s.source = 'chainlink'
                   AND s.ts >= m.window_start
                   AND s.ts <= m.window_end + make_interval(secs => %(grace)s)
    WHERE m.close_price IS NULL
      AND m.strike IS NOT NULL
      AND m.window_end <= NOW() - make_interval(secs => %(grace)s)
    ORDER BY m.slug, ABS(EXTRACT(EPOCH FROM (s.ts - m.window_end)))
) sub
WHERE m.slug = sub.slug
RETURNING m.slug, m.strike, m.close_price, m.resolved_outcome;
"""


async def resolve_once() -> tuple[int, int]:
    """One pass: returns (strike_filled, close_filled)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_STRIKE_SQL, {"lookback": STRIKE_LOOKBACK_SEC,
                                             "lookahead": STRIKE_LOOKAHEAD_SEC})
            strikes = await cur.fetchall()
            await cur.execute(_CLOSE_SQL, {"grace": LATE_GRACE_SEC})
            closes = await cur.fetchall()
        await conn.commit()

    if strikes:
        log.info("strikes_filled", count=len(strikes),
                 slugs=[r["slug"] for r in strikes[:5]])
    if closes:
        log.info("closes_filled", count=len(closes),
                 sample=[{
                     "slug": r["slug"],
                     "strike": float(r["strike"]) if r["strike"] is not None else None,
                     "close": float(r["close_price"]) if r["close_price"] is not None else None,
                     "outcome": r["resolved_outcome"],
                 } for r in closes[:3]])
    return len(strikes), len(closes)


async def run_forever() -> None:
    while True:
        try:
            await resolve_once()
        except Exception as e:
            log.exception("resolver_error", error=str(e))
        await asyncio.sleep(LOOP_INTERVAL_SEC)
