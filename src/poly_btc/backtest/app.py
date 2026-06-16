"""Streamlit UI for the Polymarket BTC 5-minute backtester.

Run locally:    streamlit run -m poly_btc.backtest.app
Run in docker:  see docker-compose 'app' service.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd
import streamlit as st

from ..db import close_pool, init_pool
from .core import SignalSource, Summary, simulate

st.set_page_config(page_title="Polymarket BTC 5m backtester", layout="wide")


@st.cache_data(ttl=10, show_spinner=False)
def run_simulation(x_sec: int, y_usd: float, n_markets: int, signal: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    async def _go() -> tuple[pd.DataFrame, Summary]:
        await init_pool()
        try:
            return await simulate(x_sec, y_usd, n_markets, SignalSource(signal))
        finally:
            # Keep pool alive across reruns; only close on process exit.
            pass

    trades, summary = asyncio.run(_go())
    return trades, summary.__dict__


# --- Sidebar controls ---
st.sidebar.title("Strategy params")
x_sec = st.sidebar.slider("X — sec before close (entry offset)", 5, 60, 20, step=5)
y_usd = st.sidebar.number_input("Y — bet size, $", min_value=1.0, max_value=10_000.0, value=10.0, step=1.0)
n_markets = st.sidebar.number_input("N — last resolved markets",
                                    min_value=1, max_value=10_000, value=200, step=10)
signal = st.sidebar.radio(
    "Signal",
    options=[s.value for s in SignalSource],
    format_func=lambda s: {"spot_vs_strike": "Spot vs Strike (BTC)",
                           "pm_price": "PM price tilt"}[s],
    index=0,
)

st.title("Polymarket BTC 5m — strategy backtester")

with st.spinner("Running simulation against Postgres..."):
    trades, summary = run_simulation(int(x_sec), float(y_usd), int(n_markets), signal)

# --- Summary cards ---
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Trades",        f"{summary['n_trades']:,}",
          delta=f"-{summary['n_skipped']} skipped" if summary["n_skipped"] else None)
c2.metric("Total PnL, $",  f"{summary['total_pnl']:+,.2f}")
c3.metric("Win rate",      f"{summary['win_rate']*100:.1f}%")
c4.metric("Avg entry, ¢",  f"{summary['avg_entry_price']:.1f}")
c5.metric("Max DD, $",     f"{summary['max_drawdown']:,.2f}")

c6, c7, c8 = st.columns(3)
c6.metric("Avg winning entry, ¢", f"{summary['avg_winning_entry']:.1f}")
c7.metric("Avg losing entry, ¢",  f"{summary['avg_losing_entry']:.1f}")
c8.metric("Sharpe (naive)",       f"{summary['sharpe']:.2f}")

if trades.empty:
    st.warning("Нет данных для бэктеста — коллектор ещё не собрал ни одного резолвленного рынка либо нет book-снапшотов на нужный момент.")
    st.stop()

# --- Cumulative PnL chart ---
chart_df = trades.iloc[::-1].copy()
chart_df["cum_pnl"] = chart_df["pnl_usd"].cumsum()
chart_df = chart_df.set_index("window_end")

st.subheader("Cumulative PnL")
st.line_chart(chart_df["cum_pnl"], height=240)

# --- Per-trade table ---
st.subheader(f"Trades — {len(trades)} rows (newest first)")

show_btc_cols = st.checkbox("Show BTC strike / spot / close columns", value=True)
show_pm_open = st.checkbox("Show PM open prices", value=False)
show_pm_entry = st.checkbox("Show PM entry prices", value=True)
show_pm_close = st.checkbox("Show PM close prices", value=True)

cols = ["window_end", "side", "win", "pnl_usd",
        "entry_price_cents", "entry_bid_cents", "spread_cents",
        "resolved_outcome", "slug"]
if show_btc_cols:
    cols = cols[:7] + ["strike", "spot_at_entry", "spot_minus_strike", "close_price_btc"] + cols[7:]
if show_pm_open:
    cols += ["pm_up_open_c", "pm_down_open_c"]
if show_pm_entry:
    cols += ["pm_up_entry_c", "pm_down_entry_c"]
if show_pm_close:
    cols += ["pm_up_close_c", "pm_down_close_c"]

# Deduplicate while preserving order
seen, ordered = set(), []
for c in cols:
    if c in trades.columns and c not in seen:
        seen.add(c); ordered.append(c)

styled = trades[ordered].style.format({
    "pnl_usd": "{:+.2f}",
    "entry_price_cents": "{:.1f}",
    "entry_bid_cents": "{:.1f}",
    "spread_cents": "{:.1f}",
    "strike": "{:,.2f}",
    "spot_at_entry": "{:,.2f}",
    "spot_minus_strike": "{:+,.2f}",
    "close_price_btc": "{:,.2f}",
    "pm_up_open_c": "{:.1f}",  "pm_down_open_c": "{:.1f}",
    "pm_up_entry_c": "{:.1f}", "pm_down_entry_c": "{:.1f}",
    "pm_up_close_c": "{:.1f}", "pm_down_close_c": "{:.1f}",
}, na_rep="—")

st.dataframe(styled, use_container_width=True, height=600)

# --- Diagnostic plots ---
st.subheader("Entry-price distribution by outcome")
hist_df = trades.assign(result=trades["win"].map({True: "win", False: "lose"}))
st.bar_chart(
    hist_df.groupby([pd.cut(hist_df["entry_price_cents"],
                            bins=[0, 30, 40, 50, 60, 70, 100]),
                     "result"]).size().unstack(fill_value=0),
    height=240,
)

st.caption("Note: results are cached for 10s — change a slider to refresh.")
