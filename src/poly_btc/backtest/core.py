"""Pure-function backtester.

Strategy:
  At t_entry = window_end - X seconds, pick a side using `signal`:
    - SPOT_VS_STRIKE: Up if spot(t_entry) >= strike, else Down
    - PM_PRICE:       Up if pm_up_mid(t_entry) > 0.5, else Down (ties -> Up)
  Pay best_ask of that side at t_entry. PnL is binary: shares win $1 or $0.
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
    n_skipped: int
    total_pnl: float
    win_rate: float
    avg_entry_price: float        # cents
    avg_winning_entry: float      # cents, conditional on win
    avg_losing_entry: float       # cents, conditional on loss
    sharpe: float                 # naive, per-trade
    max_drawdown: float           # $, peak-to-trough on cumulative PnL


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or pd.isna(bid) or pd.isna(ask):
        return None
    return (float(bid) + float(ask)) / 2.0


def _decide_side(row: pd.Series, signal: SignalSource) -> str | None:
    if signal == SignalSource.SPOT_VS_STRIKE:
        spot = row.get("spot_at_entry")
        strike = row.get("strike")
        if pd.isna(spot) or pd.isna(strike):
            return None
        return "Up" if float(spot) >= float(strike) else "Down"
    # PM_PRICE
    up_mid = _mid(row.get("up_bid_entry"), row.get("up_ask_entry"))
    dn_mid = _mid(row.get("dn_bid_entry"), row.get("dn_ask_entry"))
    if up_mid is None and dn_mid is None:
        return None
    if up_mid is None:
        return "Down" if dn_mid > 0.5 else "Up"
    if dn_mid is None:
        return "Up" if up_mid > 0.5 else "Down"
    # If both present, use which is leading
    return "Up" if up_mid >= dn_mid else "Down"


def _build_trade_row(row: pd.Series, signal: SignalSource, y: float) -> dict | None:
    side = _decide_side(row, signal)
    if side is None:
        return None

    if side == "Up":
        entry_ask = row.get("up_ask_entry")
        entry_bid = row.get("up_bid_entry")
    else:
        entry_ask = row.get("dn_ask_entry")
        entry_bid = row.get("dn_bid_entry")

    if entry_ask is None or pd.isna(entry_ask) or entry_ask <= 0:
        return None
    entry_price = float(entry_ask)
    if entry_price >= 1.0:
        # Side already fully priced — no upside; skip rather than show absurd shares.
        return None

    shares = y / entry_price
    outcome = row.get("resolved_outcome")
    win = side == outcome
    pnl = (shares - y) if win else -y

    return {
        "slug": row["slug"],
        "window_end": row["window_end"],
        "side": side,
        "win": win,
        "pnl_usd": round(pnl, 4),
        "entry_price_cents": round(entry_price * 100, 2),
        "entry_bid_cents": round(float(entry_bid) * 100, 2) if entry_bid is not None and not pd.isna(entry_bid) else None,
        "spread_cents": (round((float(entry_ask) - float(entry_bid)) * 100, 2)
                         if entry_bid is not None and not pd.isna(entry_bid) else None),
        # BTC signal context
        "strike": round(float(row["strike"]), 4) if not pd.isna(row.get("strike")) else None,
        "spot_at_entry": (round(float(row["spot_at_entry"]), 4)
                          if not pd.isna(row.get("spot_at_entry")) else None),
        "close_price_btc": round(float(row["close_price"]), 4),
        "resolved_outcome": outcome,
        "spot_minus_strike": (round(float(row["spot_at_entry"]) - float(row["strike"]), 4)
                              if not pd.isna(row.get("spot_at_entry"))
                              and not pd.isna(row.get("strike")) else None),
        # PM-price snapshots (mid, cents)
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
    }


def _summarize(trades: pd.DataFrame, n_raw: int) -> Summary:
    if trades.empty:
        return Summary(0, n_raw, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    pnl = trades["pnl_usd"].to_numpy(dtype=float)
    cum = np.cumsum(pnl[::-1])  # chronological order (oldest first)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak).min() if len(cum) else 0.0
    sharpe = (pnl.mean() / pnl.std() * np.sqrt(len(pnl))) if pnl.std() > 0 else 0.0

    wins = trades[trades["win"]]
    losses = trades[~trades["win"]]
    return Summary(
        n_trades=len(trades),
        n_skipped=n_raw - len(trades),
        total_pnl=float(pnl.sum()),
        win_rate=float(trades["win"].mean()),
        avg_entry_price=float(trades["entry_price_cents"].mean()),
        avg_winning_entry=float(wins["entry_price_cents"].mean()) if not wins.empty else 0.0,
        avg_losing_entry=float(losses["entry_price_cents"].mean()) if not losses.empty else 0.0,
        sharpe=float(sharpe),
        max_drawdown=float(dd),
    )


async def simulate(
    x_sec: int,
    y_usd: float,
    n_markets: int,
    signal: SignalSource = SignalSource.SPOT_VS_STRIKE,
) -> tuple[pd.DataFrame, Summary]:
    raw = await fetch_resolved_dataset(x_sec, n_markets)
    if raw.empty:
        return pd.DataFrame(), _summarize(pd.DataFrame(), 0)

    trade_rows = []
    for _, row in raw.iterrows():
        t = _build_trade_row(row, signal, y_usd)
        if t is not None:
            trade_rows.append(t)

    trades = pd.DataFrame(trade_rows)
    return trades, _summarize(trades, len(raw))
