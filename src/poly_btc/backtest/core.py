"""Pure-function backtester.

Strategy:
  At t_entry = window_end - X seconds, pick a side using `signal`:
    - SPOT_VS_STRIKE: Up if spot(t_entry) >= strike(window_start), else Down
    - PM_PRICE:       Up if pm_up_mid(t_entry) > pm_down_mid(t_entry), else Down
  Pay best_ask of that side at t_entry. PnL = (shares - Y) on win, -Y on loss.

Staleness gates:
  Markets where any input is older than the configured threshold are SKIPPED
  rather than counted as wins/losses. This is the only honest way to backtest
  against sparse BTC spot data — using a stale "spot at entry" tick can lead to
  look-ahead bias (the same tick is what feeds `close` too).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from .sql import fetch_resolved_dataset


class SignalSource(str, Enum):
    SPOT_VS_STRIKE = "spot_vs_strike"
    PM_PRICE = "pm_price"


@dataclass
class Summary:
    n_trades: int
    n_skipped_no_data: int
    n_skipped_stale: int
    n_skipped_bad_price: int
    n_skipped_price_too_low: int
    n_skipped_low_agreement: int
    total_pnl: float
    win_rate: float
    avg_entry_price: float        # cents
    avg_winning_entry: float
    avg_losing_entry: float
    sharpe: float
    max_drawdown: float


def _mid(bid, ask) -> float | None:
    if bid is None or ask is None or pd.isna(bid) or pd.isna(ask):
        return None
    return (float(bid) + float(ask)) / 2.0


def _decide_side(row: pd.Series, signal: SignalSource) -> str | None:
    if signal == SignalSource.SPOT_VS_STRIKE:
        spot = row.get("spot_at_entry")
        strike = row.get("strike_btc")
        if pd.isna(spot) or pd.isna(strike):
            return None
        return "Up" if float(spot) >= float(strike) else "Down"
    up_mid = _mid(row.get("up_bid_entry"), row.get("up_ask_entry"))
    dn_mid = _mid(row.get("dn_bid_entry"), row.get("dn_ask_entry"))
    if up_mid is None and dn_mid is None:
        return None
    if up_mid is None:
        return "Down" if dn_mid > 0.5 else "Up"
    if dn_mid is None:
        return "Up" if up_mid > 0.5 else "Down"
    return "Up" if up_mid >= dn_mid else "Down"


def _walk_retries(
    history: list[dict] | None,
    initial_ask: float | None,
    initial_bid: float | None,
    min_ask_cents: int,
    retry_enabled: bool,
    retry_interval_sec: int,
    max_attempt_sec: int,
) -> tuple[float | None, float | None, int | None]:
    """Walk retries until best_ask >= min_ask_cents/100. Returns (ask, bid, sec_offset).
    sec_offset is seconds AFTER t_entry where the qualifying snapshot was found
    (0 means initial moment). Returns (None, None, None) if never qualifies.
    """
    threshold = min_ask_cents / 100.0

    # Attempt 0: use the existing entry snapshot the SQL already returned.
    if initial_ask is not None and initial_ask >= threshold:
        return float(initial_ask), float(initial_bid) if initial_bid is not None else None, 0

    if not retry_enabled or retry_interval_sec <= 0 or not history:
        return None, None, None

    # Walk attempts at R, 2R, 3R, ... while still strictly before window_end.
    # `history` is sorted by sec (ascending). Each attempt picks the LAST history
    # entry whose sec <= attempt_sec (latest book at-or-before that moment).
    hist_pairs = [(int(h["sec"]), float(h["ask"]), float(h["bid"]) if h.get("bid") is not None else None)
                  for h in history if h.get("ask") is not None]
    if not hist_pairs:
        return None, None, None

    attempt_sec = retry_interval_sec
    while attempt_sec <= max_attempt_sec:
        # Latest history entry with sec <= attempt_sec
        snap = None
        for s, a, b in hist_pairs:
            if s <= attempt_sec:
                snap = (s, a, b)
            else:
                break
        if snap is not None:
            _, ask, bid = snap
            if ask >= threshold:
                return ask, bid, attempt_sec
        attempt_sec += retry_interval_sec

    return None, None, None


def _build_trade_row(
    row: pd.Series,
    signal: SignalSource,
    y: float,
    max_spot_staleness_sec: int,
    max_book_staleness_sec: int,
    min_ask_cents: int,
    retry_enabled: bool,
    retry_interval_sec: int,
    x_sec: int,
    min_signal_agreement_pct: float,
) -> tuple[dict | None, str]:
    """Returns (trade_row_or_none, skip_reason).
    skip_reason values: "" (kept), "no_data", "stale", "bad_price",
    "price_too_low", "low_agreement".
    """
    side = _decide_side(row, signal)
    if side is None:
        return None, "no_data"

    # Staleness checks for the inputs the strategy depends on
    strike_age = row.get("strike_staleness_s")
    spot_age = row.get("spot_at_entry_staleness_s")
    if pd.isna(strike_age) or pd.isna(spot_age):
        return None, "no_data"
    if signal == SignalSource.SPOT_VS_STRIKE and (
        int(strike_age) > max_spot_staleness_sec
        or int(spot_age) > max_spot_staleness_sec
    ):
        return None, "stale"

    # Signal-agreement filter: fraction of pre-entry binance ticks that confirm
    # the side we picked. If the pre-entry data is missing (no binance coverage
    # for this window) we DON'T filter — we let the trade through.
    pct_above = row.get("pct_above_strike_before_entry")
    agreement_pct = None
    if not pd.isna(pct_above):
        agreement_pct = (float(pct_above) if side == "Up"
                         else 100.0 - float(pct_above))
        if agreement_pct < min_signal_agreement_pct:
            return None, "low_agreement"

    if side == "Up":
        entry_ask = row.get("up_ask_entry")
        entry_bid = row.get("up_bid_entry")
        book_age = row.get("up_book_staleness_s")
        history = row.get("up_entry_history")
    else:
        entry_ask = row.get("dn_ask_entry")
        entry_bid = row.get("dn_bid_entry")
        book_age = row.get("dn_book_staleness_s")
        history = row.get("dn_entry_history")

    if entry_ask is None or pd.isna(entry_ask) or entry_ask <= 0:
        return None, "no_data"
    if pd.isna(book_age) or int(book_age) > max_book_staleness_sec:
        return None, "stale"

    # Walk min-ask + retry logic (leave 1 sec before window_end as buffer).
    max_attempt_sec = max(0, x_sec - 1)
    chosen_ask, chosen_bid, attempt_sec = _walk_retries(
        history,
        float(entry_ask),
        float(entry_bid) if entry_bid is not None and not pd.isna(entry_bid) else None,
        min_ask_cents=min_ask_cents,
        retry_enabled=retry_enabled,
        retry_interval_sec=retry_interval_sec,
        max_attempt_sec=max_attempt_sec,
    )
    if chosen_ask is None:
        return None, "price_too_low"

    entry_price = chosen_ask
    if entry_price >= 1.0:
        return None, "bad_price"

    shares = y / entry_price
    outcome = row.get("resolved_outcome")
    win = side == outcome
    pnl = (shares - y) if win else -y

    return {
        "slug": row["slug"],
        "window_end": row["window_end"],
        "side": side,
        "win": bool(win),
        "pnl_usd": round(pnl, 4),
        "entry_price_cents": round(entry_price * 100, 2),
        "entry_bid_cents": (round(chosen_bid * 100, 2)
                            if chosen_bid is not None else None),
        "spread_cents": (round((entry_price - chosen_bid) * 100, 2)
                         if chosen_bid is not None else None),
        "retry_sec": attempt_sec,  # 0 = bought immediately at t_entry
        # Pre-entry window dynamics (known at decision time, usable as filters)
        "btc_range_before_entry": (round(float(row["btc_range_before_entry"]), 2)
                                   if not pd.isna(row.get("btc_range_before_entry")) else None),
        "crossed_strike_before_entry": (int(row["crossed_strike_before_entry"])
                                        if not pd.isna(row.get("crossed_strike_before_entry")) else None),
        # % of pre-entry ticks that AGREE with the side we picked (computed above).
        "signal_agreement_pct": round(agreement_pct, 1) if agreement_pct is not None else None,
        # Signal inputs
        "strike_btc": round(float(row["strike_btc"]), 4) if not pd.isna(row.get("strike_btc")) else None,
        "spot_at_entry": (round(float(row["spot_at_entry"]), 4)
                          if not pd.isna(row.get("spot_at_entry")) else None),
        "spot_minus_strike": (round(float(row["spot_at_entry"]) - float(row["strike_btc"]), 4)
                              if not pd.isna(row.get("spot_at_entry"))
                              and not pd.isna(row.get("strike_btc")) else None),
        "close_btc": (round(float(row["close_btc"]), 4)
                      if not pd.isna(row.get("close_btc")) else None),
        "resolved_outcome": outcome,
        # Data quality
        "strike_age_s": int(strike_age),
        "spot_age_s": int(spot_age),
        "book_age_s": int(book_age),
        "strike_src": row.get("strike_src"),
        "spot_at_entry_src": row.get("spot_at_entry_src"),
        "close_src": row.get("close_src"),
        # PM mid-prices (cents) at three points
        "pm_up_open_c": (round(_mid(row.get("up_bid_open"), row.get("up_ask_open")) * 100, 2)
                         if _mid(row.get("up_bid_open"), row.get("up_ask_open")) is not None else None),
        "pm_down_open_c": (round(_mid(row.get("dn_bid_open"), row.get("dn_ask_open")) * 100, 2)
                           if _mid(row.get("dn_bid_open"), row.get("dn_ask_open")) is not None else None),
        "pm_up_entry_c": (round(_mid(row.get("up_bid_entry"), row.get("up_ask_entry")) * 100, 2)
                          if _mid(row.get("up_bid_entry"), row.get("up_ask_entry")) is not None else None),
        "pm_down_entry_c": (round(_mid(row.get("dn_bid_entry"), row.get("dn_ask_entry")) * 100, 2)
                            if _mid(row.get("dn_bid_entry"), row.get("dn_ask_entry")) is not None else None),
        "pm_up_close_c": (round(_mid(row.get("up_bid_close"), row.get("up_ask_close")) * 100, 2)
                          if _mid(row.get("up_bid_close"), row.get("up_ask_close")) is not None else None),
        "pm_down_close_c": (round(_mid(row.get("dn_bid_close"), row.get("dn_ask_close")) * 100, 2)
                            if _mid(row.get("dn_bid_close"), row.get("dn_ask_close")) is not None else None),
    }, ""


def _summarize(trades: pd.DataFrame, skip_counts: dict[str, int]) -> Summary:
    if trades.empty:
        return Summary(
            n_trades=0,
            n_skipped_no_data=skip_counts.get("no_data", 0),
            n_skipped_stale=skip_counts.get("stale", 0),
            n_skipped_bad_price=skip_counts.get("bad_price", 0),
            n_skipped_price_too_low=skip_counts.get("price_too_low", 0),
            n_skipped_low_agreement=skip_counts.get("low_agreement", 0),
            total_pnl=0.0, win_rate=0.0, avg_entry_price=0.0,
            avg_winning_entry=0.0, avg_losing_entry=0.0,
            sharpe=0.0, max_drawdown=0.0,
        )

    pnl = trades["pnl_usd"].to_numpy(dtype=float)
    cum = np.cumsum(pnl[::-1])
    peak = np.maximum.accumulate(cum)
    dd = float((cum - peak).min()) if len(cum) else 0.0
    sharpe = float(pnl.mean() / pnl.std() * np.sqrt(len(pnl))) if pnl.std() > 0 else 0.0

    wins = trades[trades["win"]]
    losses = trades[~trades["win"]]
    return Summary(
        n_trades=len(trades),
        n_skipped_no_data=skip_counts.get("no_data", 0),
        n_skipped_stale=skip_counts.get("stale", 0),
        n_skipped_bad_price=skip_counts.get("bad_price", 0),
        n_skipped_price_too_low=skip_counts.get("price_too_low", 0),
        n_skipped_low_agreement=skip_counts.get("low_agreement", 0),
        total_pnl=float(pnl.sum()),
        win_rate=float(trades["win"].mean()),
        avg_entry_price=float(trades["entry_price_cents"].mean()),
        avg_winning_entry=float(wins["entry_price_cents"].mean()) if not wins.empty else 0.0,
        avg_losing_entry=float(losses["entry_price_cents"].mean()) if not losses.empty else 0.0,
        sharpe=sharpe,
        max_drawdown=dd,
    )


def apply_strategy(
    raw: pd.DataFrame,
    x_sec: int,
    y_usd: float,
    signal: SignalSource = SignalSource.SPOT_VS_STRIKE,
    max_spot_staleness_sec: int = 15,
    max_book_staleness_sec: int = 10,
    min_ask_cents: int = 0,
    retry_enabled: bool = False,
    retry_interval_sec: int = 2,
    min_signal_agreement_pct: float = 0,
) -> tuple[pd.DataFrame, Summary]:
    """Pure: given a pre-fetched raw market dataset, apply the strategy and
    return (trades_df, summary). No I/O; safe to call from cache layers."""
    if raw.empty:
        return pd.DataFrame(), _summarize(pd.DataFrame(), {})

    rows: list[dict] = []
    skip = {"no_data": 0, "stale": 0, "bad_price": 0,
            "price_too_low": 0, "low_agreement": 0}
    for _, row in raw.iterrows():
        trade, reason = _build_trade_row(
            row, signal, y_usd,
            max_spot_staleness_sec, max_book_staleness_sec,
            min_ask_cents, retry_enabled, retry_interval_sec, x_sec,
            min_signal_agreement_pct,
        )
        if trade is None:
            skip[reason] = skip.get(reason, 0) + 1
        else:
            rows.append(trade)

    trades = pd.DataFrame(rows)
    return trades, _summarize(trades, skip)


async def simulate(
    x_sec: int,
    y_usd: float,
    n_markets: int,
    signal: SignalSource = SignalSource.SPOT_VS_STRIKE,
    max_spot_staleness_sec: int = 15,
    max_book_staleness_sec: int = 10,
    min_ask_cents: int = 0,
    retry_enabled: bool = False,
    retry_interval_sec: int = 2,
    min_signal_agreement_pct: float = 0,
) -> tuple[pd.DataFrame, Summary]:
    """Convenience: fetch dataset from DB and apply strategy in one call."""
    raw = await fetch_resolved_dataset(x_sec, n_markets)
    return apply_strategy(
        raw, x_sec, y_usd, signal,
        max_spot_staleness_sec, max_book_staleness_sec,
        min_ask_cents, retry_enabled, retry_interval_sec,
        min_signal_agreement_pct,
    )
