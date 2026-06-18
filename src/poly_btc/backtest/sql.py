"""Single-shot SQL that pulls everything the backtester needs per market.

For each resolved market we collect, via LATERAL joins:
  - BTC spot at three timestamps: window_start (strike), t_entry, window_end
    (closing print). Each is the most recent tick <= the target time from any
    source (chainlink OR binance). We also return the staleness in seconds and
    which source was used so the backtester can filter / annotate.
  - Polymarket best_bid / best_ask for Up + Down tokens at the same three points,
    plus the entry-snapshot staleness for each book lookup.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from ..config import settings

_SQL = """
WITH resolved AS (
    SELECT slug, window_start, window_end, strike, close_price, resolved_outcome,
           token_up, token_down,
           window_end - make_interval(secs => %(x_sec)s) AS t_entry
    FROM markets
    WHERE close_price IS NOT NULL
      AND resolved_outcome IS NOT NULL
      AND (cardinality(%(slugs)s::text[]) = 0 OR slug = ANY(%(slugs)s))
    ORDER BY window_end DESC
    LIMIT %(n)s
)
SELECT
    r.slug,
    r.window_start,
    r.window_end,
    r.t_entry,
    r.strike                                              AS resolver_strike,
    r.close_price                                         AS resolver_close,
    r.resolved_outcome,
    -- Spot at open / entry / close (any source, most recent <= target)
    sp_o.price                                            AS strike_btc,
    sp_o.source                                           AS strike_src,
    EXTRACT(EPOCH FROM (r.window_start - sp_o.ts))::int   AS strike_staleness_s,
    sp_e.price                                            AS spot_at_entry,
    sp_e.source                                           AS spot_at_entry_src,
    EXTRACT(EPOCH FROM (r.t_entry - sp_e.ts))::int        AS spot_at_entry_staleness_s,
    sp_c.price                                            AS close_btc,
    sp_c.source                                           AS close_src,
    EXTRACT(EPOCH FROM (r.window_end - sp_c.ts))::int     AS close_staleness_s,
    -- Open book snapshots
    up_o.best_bid AS up_bid_open,  up_o.best_ask AS up_ask_open,
    dn_o.best_bid AS dn_bid_open,  dn_o.best_ask AS dn_ask_open,
    -- Entry book snapshots (with staleness)
    up_e.best_bid AS up_bid_entry, up_e.best_ask AS up_ask_entry,
    EXTRACT(EPOCH FROM (r.t_entry - up_e.ts))::int        AS up_book_staleness_s,
    dn_e.best_bid AS dn_bid_entry, dn_e.best_ask AS dn_ask_entry,
    EXTRACT(EPOCH FROM (r.t_entry - dn_e.ts))::int        AS dn_book_staleness_s,
    -- Close book snapshots
    up_c.best_bid AS up_bid_close, up_c.best_ask AS up_ask_close,
    dn_c.best_bid AS dn_bid_close, dn_c.best_ask AS dn_ask_close,
    -- Per-second history of best_ask in [t_entry, window_end] for retry logic.
    -- Subsampled: one row per second (latest book within that second).
    up_h.history AS up_entry_history,
    dn_h.history AS dn_entry_history,
    -- BTC range in [window_start, t_entry); volatility known at decision time
    rng_pre.rng AS btc_range_before_entry,
    -- How many times BTC crossed sp_o.price in [window_start, t_entry); pre-entry
    cross_pre.n AS crossed_strike_before_entry,
    -- Percentage of pre-entry ticks at or above strike. Python derives
    -- signal_agreement: if side==Up use pct_above; if side==Down use 100 - pct_above.
    pct_above_pre.pct AS pct_above_strike_before_entry
FROM resolved r
-- BTC spot lookups (any source)
LEFT JOIN LATERAL (SELECT ts, source, price FROM btc_spot
                   WHERE ts <= r.window_start ORDER BY ts DESC LIMIT 1) sp_o ON true
LEFT JOIN LATERAL (SELECT ts, source, price FROM btc_spot
                   WHERE ts <= r.t_entry ORDER BY ts DESC LIMIT 1) sp_e ON true
LEFT JOIN LATERAL (SELECT ts, source, price FROM btc_spot
                   WHERE ts <= r.window_end ORDER BY ts DESC LIMIT 1) sp_c ON true
-- PM book lookups
LEFT JOIN LATERAL (SELECT ts, best_bid, best_ask FROM pm_book
                   WHERE token_id = r.token_up AND ts <= r.window_start
                     AND best_ask IS NOT NULL AND best_bid IS NOT NULL
                   ORDER BY ts DESC LIMIT 1) up_o ON true
LEFT JOIN LATERAL (SELECT ts, best_bid, best_ask FROM pm_book
                   WHERE token_id = r.token_down AND ts <= r.window_start
                     AND best_ask IS NOT NULL AND best_bid IS NOT NULL
                   ORDER BY ts DESC LIMIT 1) dn_o ON true
LEFT JOIN LATERAL (SELECT ts, best_bid, best_ask FROM pm_book
                   WHERE token_id = r.token_up AND ts <= r.t_entry
                     AND best_ask IS NOT NULL AND best_bid IS NOT NULL
                   ORDER BY ts DESC LIMIT 1) up_e ON true
LEFT JOIN LATERAL (SELECT ts, best_bid, best_ask FROM pm_book
                   WHERE token_id = r.token_down AND ts <= r.t_entry
                     AND best_ask IS NOT NULL AND best_bid IS NOT NULL
                   ORDER BY ts DESC LIMIT 1) dn_e ON true
LEFT JOIN LATERAL (SELECT ts, best_bid, best_ask FROM pm_book
                   WHERE token_id = r.token_up AND ts <= r.window_end
                     AND best_ask IS NOT NULL AND best_bid IS NOT NULL
                   ORDER BY ts DESC LIMIT 1) up_c ON true
LEFT JOIN LATERAL (SELECT ts, best_bid, best_ask FROM pm_book
                   WHERE token_id = r.token_down AND ts <= r.window_end
                     AND best_ask IS NOT NULL AND best_bid IS NOT NULL
                   ORDER BY ts DESC LIMIT 1) dn_c ON true
-- Per-second history arrays for retry walk
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
               'sec',  EXTRACT(EPOCH FROM (sec_ts - r.t_entry))::int,
               'bid',  best_bid,
               'ask',  best_ask
           ) ORDER BY sec_ts) AS history
    FROM (
        SELECT DISTINCT ON (date_trunc('second', b.ts))
               date_trunc('second', b.ts) AS sec_ts,
               b.best_bid, b.best_ask
        FROM pm_book b
        WHERE b.token_id = r.token_up
          AND b.ts >= r.t_entry
          AND b.ts <= r.window_end
          AND b.best_ask IS NOT NULL AND b.best_bid IS NOT NULL
        ORDER BY date_trunc('second', b.ts), b.ts DESC
    ) s
) up_h ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
               'sec',  EXTRACT(EPOCH FROM (sec_ts - r.t_entry))::int,
               'bid',  best_bid,
               'ask',  best_ask
           ) ORDER BY sec_ts) AS history
    FROM (
        SELECT DISTINCT ON (date_trunc('second', b.ts))
               date_trunc('second', b.ts) AS sec_ts,
               b.best_bid, b.best_ask
        FROM pm_book b
        WHERE b.token_id = r.token_down
          AND b.ts >= r.t_entry
          AND b.ts <= r.window_end
          AND b.best_ask IS NOT NULL AND b.best_bid IS NOT NULL
        ORDER BY date_trunc('second', b.ts), b.ts DESC
    ) s
) dn_h ON true
-- BTC spot range (max-min) within [window_start, t_entry) — known when we decide.
-- Binance-only (dense + consistent 1Hz) to avoid source-mixing noise: chainlink
-- and binance can differ by $5-30 which would inflate the range.
LEFT JOIN LATERAL (
    SELECT (MAX(price) - MIN(price))::numeric AS rng
    FROM btc_spot
    WHERE source = 'binance'
      AND ts >= r.window_start AND ts < r.t_entry
) rng_pre ON true
-- Strike crossings in [window_start, t_entry) — pre-entry only, actionable.
LEFT JOIN LATERAL (
    SELECT COUNT(*)::int AS n
    FROM (
        SELECT (price >= sp_o.price) AS above,
               LAG(price >= sp_o.price) OVER (ORDER BY ts) AS prev_above
        FROM btc_spot
        WHERE source = 'binance'
          AND ts >= r.window_start AND ts < r.t_entry
    ) t
    WHERE prev_above IS NOT NULL
      AND above IS DISTINCT FROM prev_above
) cross_pre ON true
-- Pre-entry fraction of ticks at-or-above strike (sp_o.price).
LEFT JOIN LATERAL (
    SELECT (COUNT(*) FILTER (WHERE price >= sp_o.price))::float
           / NULLIF(COUNT(*), 0) * 100 AS pct
    FROM btc_spot
    WHERE source = 'binance'
      AND ts >= r.window_start AND ts < r.t_entry
) pct_above_pre ON true
ORDER BY r.window_end DESC;
"""

_NUMERIC_COLS = (
    "resolver_strike", "resolver_close",
    "strike_btc", "spot_at_entry", "close_btc",
    "up_bid_open", "up_ask_open", "dn_bid_open", "dn_ask_open",
    "up_bid_entry", "up_ask_entry", "dn_bid_entry", "dn_ask_entry",
    "up_bid_close", "up_ask_close", "dn_bid_close", "dn_ask_close",
    "btc_range_before_entry",
    "pct_above_strike_before_entry",
)


_DETAIL_MARKET = """
SELECT slug, window_start, window_end, strike, close_price, resolved_outcome,
       token_up, token_down
FROM markets WHERE slug = %s
"""

_DETAIL_SPOT = """
SELECT ts, source, price
FROM btc_spot
WHERE ts >= %(t0)s AND ts <= %(t1)s
ORDER BY ts
"""

_DETAIL_BOOK = """
SELECT DISTINCT ON (date_trunc('second', ts))
       date_trunc('second', ts) AS ts,
       best_bid, best_ask
FROM pm_book
WHERE token_id = %(token)s
  AND ts >= %(t0)s AND ts <= %(t1)s
  AND best_ask IS NOT NULL AND best_bid IS NOT NULL
ORDER BY date_trunc('second', ts), ts DESC
"""


async def fetch_market_detail(slug: str) -> dict:
    """Pull all BTC spot ticks + per-second PM book (Up & Down) for one market.

    Returns dict with keys: market (dict|None), spot (DataFrame), up_book (DataFrame),
    dn_book (DataFrame). Empty market means slug not found.
    """
    async with await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row
    ) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DETAIL_MARKET, (slug,))
            market = await cur.fetchone()
            if not market:
                return {"market": None, "spot": pd.DataFrame(),
                        "up_book": pd.DataFrame(), "dn_book": pd.DataFrame()}
            # Pad ±10s for a bit of context around the window
            from datetime import timedelta
            t0 = market["window_start"] - timedelta(seconds=10)
            t1 = market["window_end"] + timedelta(seconds=30)

            await cur.execute(_DETAIL_SPOT, {"t0": t0, "t1": t1})
            spot_rows = await cur.fetchall()
            await cur.execute(_DETAIL_BOOK, {"token": market["token_up"], "t0": t0, "t1": t1})
            up_rows = await cur.fetchall()
            await cur.execute(_DETAIL_BOOK, {"token": market["token_down"], "t0": t0, "t1": t1})
            dn_rows = await cur.fetchall()

    def _df(rows):
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        for c in df.columns:
            if c in ("price", "best_bid", "best_ask"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    return {"market": market, "spot": _df(spot_rows),
            "up_book": _df(up_rows), "dn_book": _df(dn_rows)}


async def fetch_resolved_slugs(n: int) -> list[str]:
    """Cheap query: just the slugs of the N most recent fully-resolved markets."""
    sql = (
        "SELECT slug FROM markets "
        "WHERE close_price IS NOT NULL AND resolved_outcome IS NOT NULL "
        "ORDER BY window_end DESC LIMIT %s"
    )
    async with await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row
    ) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (n,))
            return [r["slug"] for r in await cur.fetchall()]


async def fetch_resolved_dataset(
    x_sec: int, n: int, slugs: list[str] | None = None,
) -> pd.DataFrame:
    """One DataFrame row per resolved market, newest first.

    If `slugs` is provided, fetches ONLY those markets (n becomes an upper bound).
    Used by the incremental cache to top up only missing entries.
    """
    slugs_arg = slugs or []
    async with await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row
    ) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _SQL, {"x_sec": x_sec, "n": n if not slugs_arg else len(slugs_arg),
                       "slugs": slugs_arg},
            )
            rows: list[dict[str, Any]] = await cur.fetchall()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
