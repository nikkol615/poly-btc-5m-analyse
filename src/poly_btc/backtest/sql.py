"""Single-shot SQL that pulls everything the backtester needs per market.

For each resolved market we collect, via LATERAL joins:
  - BTC spot at the entry instant (t_entry = window_end - X seconds)
  - Polymarket best_bid / best_ask for Up + Down tokens at three timestamps:
        window_start (open), t_entry (entry), window_end (close)

Designed so the only varying input is X seconds (entry offset) and N (limit).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from ..db import get_conn


_SQL = """
WITH resolved AS (
    SELECT slug, window_start, window_end, strike, close_price, resolved_outcome,
           token_up, token_down,
           window_end - make_interval(secs => %(x_sec)s) AS t_entry
    FROM markets
    WHERE close_price IS NOT NULL
      AND resolved_outcome IS NOT NULL
    ORDER BY window_end DESC
    LIMIT %(n)s
)
SELECT
    r.slug,
    r.window_start,
    r.window_end,
    r.t_entry,
    r.strike,
    r.close_price,
    r.resolved_outcome,
    r.token_up,
    r.token_down,
    spot_e.price AS spot_at_entry,
    -- Open snapshot
    up_o.best_bid AS up_bid_open,  up_o.best_ask AS up_ask_open,
    dn_o.best_bid AS dn_bid_open,  dn_o.best_ask AS dn_ask_open,
    -- Entry snapshot
    up_e.best_bid AS up_bid_entry, up_e.best_ask AS up_ask_entry,
    dn_e.best_bid AS dn_bid_entry, dn_e.best_ask AS dn_ask_entry,
    -- Close snapshot
    up_c.best_bid AS up_bid_close, up_c.best_ask AS up_ask_close,
    dn_c.best_bid AS dn_bid_close, dn_c.best_ask AS dn_ask_close
FROM resolved r
LEFT JOIN LATERAL (
    SELECT price FROM btc_spot
    WHERE source = 'chainlink' AND ts <= r.t_entry
    ORDER BY ts DESC LIMIT 1
) spot_e ON true
LEFT JOIN LATERAL (
    SELECT best_bid, best_ask FROM pm_book
    WHERE token_id = r.token_up AND ts <= r.window_start
      AND best_ask IS NOT NULL AND best_bid IS NOT NULL
    ORDER BY ts DESC LIMIT 1
) up_o ON true
LEFT JOIN LATERAL (
    SELECT best_bid, best_ask FROM pm_book
    WHERE token_id = r.token_down AND ts <= r.window_start
      AND best_ask IS NOT NULL AND best_bid IS NOT NULL
    ORDER BY ts DESC LIMIT 1
) dn_o ON true
LEFT JOIN LATERAL (
    SELECT best_bid, best_ask FROM pm_book
    WHERE token_id = r.token_up AND ts <= r.t_entry
      AND best_ask IS NOT NULL AND best_bid IS NOT NULL
    ORDER BY ts DESC LIMIT 1
) up_e ON true
LEFT JOIN LATERAL (
    SELECT best_bid, best_ask FROM pm_book
    WHERE token_id = r.token_down AND ts <= r.t_entry
      AND best_ask IS NOT NULL AND best_bid IS NOT NULL
    ORDER BY ts DESC LIMIT 1
) dn_e ON true
LEFT JOIN LATERAL (
    SELECT best_bid, best_ask FROM pm_book
    WHERE token_id = r.token_up AND ts <= r.window_end
      AND best_ask IS NOT NULL AND best_bid IS NOT NULL
    ORDER BY ts DESC LIMIT 1
) up_c ON true
LEFT JOIN LATERAL (
    SELECT best_bid, best_ask FROM pm_book
    WHERE token_id = r.token_down AND ts <= r.window_end
      AND best_ask IS NOT NULL AND best_bid IS NOT NULL
    ORDER BY ts DESC LIMIT 1
) dn_c ON true
ORDER BY r.window_end DESC;
"""


_NUMERIC_COLS = (
    "strike", "close_price", "spot_at_entry",
    "up_bid_open", "up_ask_open", "dn_bid_open", "dn_ask_open",
    "up_bid_entry", "up_ask_entry", "dn_bid_entry", "dn_ask_entry",
    "up_bid_close", "up_ask_close", "dn_bid_close", "dn_ask_close",
)


async def fetch_resolved_dataset(x_sec: int, n: int) -> pd.DataFrame:
    """Returns one DataFrame row per resolved market, ordered newest first."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SQL, {"x_sec": x_sec, "n": n})
            rows: list[dict[str, Any]] = await cur.fetchall()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
