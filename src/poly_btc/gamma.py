"""Discover active BTC 5-minute Up/Down markets via Gamma REST API.

Market slugs follow the deterministic pattern `btc-updown-5m-{window_ts}` where
`window_ts` is Unix epoch seconds divisible by 300 (5-minute boundary). We probe
a small range around the current time to cover already-published future markets.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import orjson

from .config import settings
from .db import upsert_market
from .log import get_logger

log = get_logger(__name__)

WINDOW_SEC = 300
SLUG_PREFIX = "btc-updown-5m-"
# How many windows to probe behind/ahead of "now". Markets are typically created
# many hours in advance, so a small forward window is enough.
PROBE_BACK = 2
PROBE_FORWARD = 24  # 2 hours ahead


def current_window_ts(now: float | None = None) -> int:
    t = int(now if now is not None else time.time())
    return t - (t % WINDOW_SEC)


def candidate_slugs(now: float | None = None) -> list[str]:
    base = current_window_ts(now)
    return [
        f"{SLUG_PREFIX}{base + i * WINDOW_SEC}"
        for i in range(-PROBE_BACK, PROBE_FORWARD + 1)
    ]


def _parse_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    slug = raw.get("slug", "")
    if not slug.startswith(SLUG_PREFIX):
        return None
    try:
        window_ts = int(slug[len(SLUG_PREFIX):])
    except ValueError:
        return None

    token_ids = raw.get("clobTokenIds")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    outcomes = raw.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if not token_ids or not outcomes or len(token_ids) != 2:
        return None

    # Outcomes order: ["Up", "Down"] per Gamma; token_ids matches that order.
    idx_up = outcomes.index("Up") if "Up" in outcomes else 0
    idx_down = outcomes.index("Down") if "Down" in outcomes else 1

    window_start = datetime.fromtimestamp(window_ts, tz=timezone.utc)
    window_end = datetime.fromtimestamp(window_ts + WINDOW_SEC, tz=timezone.utc)
    end_iso = raw.get("endDate")
    end_date = (
        datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        if end_iso else window_end
    )

    return {
        "slug": slug,
        "market_id": str(raw.get("id")) if raw.get("id") is not None else None,
        "condition_id": raw.get("conditionId"),
        "question_id": raw.get("questionID"),
        "question": raw.get("question"),
        "window_ts": window_ts,
        "window_start": window_start,
        "window_end": window_end,
        "end_date": end_date,
        "token_up": str(token_ids[idx_up]),
        "token_down": str(token_ids[idx_down]),
        "tick_size": raw.get("orderPriceMinTickSize"),
        "raw": raw,
    }


class GammaDiscovery:
    def __init__(self, on_new_market) -> None:
        """on_new_market: async callable invoked with parsed market dict the first time
        we see a given slug in this process. Use it to push subscriptions."""
        self._client: httpx.AsyncClient | None = None
        self._seen: set[str] = set()
        self._on_new = on_new_market

    async def __aenter__(self) -> "GammaDiscovery":
        self._client = httpx.AsyncClient(
            base_url=settings.gamma_api_url,
            timeout=httpx.Timeout(10.0),
            headers={"accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _fetch_slug(self, slug: str) -> dict[str, Any] | None:
        assert self._client is not None
        try:
            r = await self._client.get("/markets", params={"slug": slug})
            r.raise_for_status()
            data = orjson.loads(r.content)
            if isinstance(data, list) and data:
                return data[0]
            return None
        except Exception as e:
            log.warning("gamma_fetch_failed", slug=slug, error=str(e))
            return None

    async def scan_once(self) -> list[dict[str, Any]]:
        slugs = candidate_slugs()
        results = await asyncio.gather(*(self._fetch_slug(s) for s in slugs))
        markets: list[dict[str, Any]] = []
        for raw in results:
            if raw is None:
                continue
            parsed = _parse_market(raw)
            if parsed is None:
                continue
            await upsert_market(parsed)
            if parsed["slug"] not in self._seen:
                self._seen.add(parsed["slug"])
                await self._on_new(parsed)
                log.info(
                    "market_discovered",
                    slug=parsed["slug"],
                    window_start=parsed["window_start"].isoformat(),
                )
            markets.append(parsed)
        return markets

    async def run_forever(self) -> None:
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                log.exception("discovery_loop_error", error=str(e))
            await asyncio.sleep(settings.discovery_interval_sec)
