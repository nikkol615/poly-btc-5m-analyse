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


def _build_trade_row(
    row: pd.Series,
    signal: SignalSource,
    y: float,
    max_spot_staleness_sec: int,
    max_book_staleness_sec: int,
) -> tuple[dict | None, str]:
    """Returns (trade_row_or_none, skip_reason).
    skip_reason values: "" (kept), "no_data", "stale", "bad_price".
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

    if side == "Up":
        entry_ask = row.get("up_ask_entry")
        entry_bid = row.get("up_bid_entry")
        book_age = row.get("up_book_staleness_s")
    else:
        entry_ask = row.get("dn_ask_entry")
        entry_bid = row.get("dn_bid_entry")
        book_age = row.get("dn_book_staleness_s")

    if entry_ask is None or pd.isna(entry_ask) or entry_ask <= 0:
        return None, "no_data"
    if pd.isna(book_age) or int(book_age) > max_book_staleness_sec:
        return None, "stale"
    entry_price = float(entry_ask)
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
        "entry_bid_cents": (round(float(entry_bid) * 100, 2)
                            if entry_bid is not None and not pd.isna(entry_bid) else None),
        "spread_cents": (round((float(entry_ask) - float(entry_bid)) * 100, 2)
                         if entry_bid is not None and not pd.isna(entry_bid) else None),
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
        total_pnl=float(pnl.sum()),
        win_rate=float(trades["win"].mean()),
        avg_entry_price=float(trades["entry_price_cents"].mean()),
        avg_winning_entry=float(wins["entry_price_cents"].mean()) if not wins.empty else 0.0,
        avg_losing_entry=float(losses["entry_price_cents"].mean()) if not losses.empty else 0.0,
        sharpe=sharpe,
        max_drawdown=dd,
    )


async def simulate(
    x_sec: int,
    y_usd: float,
    n_markets: int,
    signal: SignalSource = SignalSource.SPOT_VS_STRIKE,
    max_spot_staleness_sec: int = 15,
    max_book_staleness_sec: int = 10,
) -> tuple[pd.DataFrame, Summary]:
    raw = await fetch_resolved_dataset(x_sec, n_markets)
    if raw.empty:
        return pd.DataFrame(), _summarize(pd.DataFrame(), {})

    rows: list[dict] = []
    skip = {"no_data": 0, "stale": 0, "bad_price": 0}
    for _, row in raw.iterrows():
        trade, reason = _build_trade_row(
            row, signal, y_usd, max_spot_staleness_sec, max_book_staleness_sec
        )
        if trade is None:
            skip[reason] = skip.get(reason, 0) + 1
        else:
            rows.append(trade)

    trades = pd.DataFrame(rows)
    return trades, _summarize(trades, skip)
