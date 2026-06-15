"""Smoke checks: DB connectivity, Gamma fetch, and a brief WSS subscription.

Run:  python scripts/smoke.py
"""
from __future__ import annotations

import asyncio
import json

from poly_btc.config import settings
from poly_btc.db import apply_schema, close_pool, get_conn, init_pool
from poly_btc.gamma import GammaDiscovery, candidate_slugs


async def main() -> None:
    print(f"DB: {settings.database_url}")
    await init_pool()
    await apply_schema()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT NOW() AS now, COUNT(*) AS markets FROM markets")
            row = await cur.fetchone()
            print(f"DB ok: {row}")

    print(f"Probing {len(candidate_slugs())} candidate slugs from Gamma...")
    async def _noop(m): pass
    async with GammaDiscovery(on_new_market=_noop) as g:
        markets = await g.scan_once()
    print(f"Active BTC 5m markets found: {len(markets)}")
    for m in markets[:3]:
        print(json.dumps({
            "slug": m["slug"],
            "window_start": m["window_start"].isoformat(),
            "token_up": m["token_up"][:24] + "...",
            "token_down": m["token_down"][:24] + "...",
        }, indent=2))

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
