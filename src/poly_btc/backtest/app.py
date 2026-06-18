"""Streamlit UI for the Polymarket BTC 5-minute backtester."""
from __future__ import annotations

import asyncio
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from poly_btc.backtest.core import SignalSource, Summary, apply_strategy
from poly_btc.backtest.sql import fetch_resolved_dataset, fetch_resolved_slugs

st.set_page_config(page_title="Polymarket BTC 5m backtester", layout="wide")


# ---------- URL-backed widget state ----------
# All sidebar controls read their default from st.query_params and write back at
# the end of the script. Filters survive browser reloads and are shareable via URL.

_qp = st.query_params


def _qp_int(key: str, default: int) -> int:
    try:
        return int(_qp.get(key, default))
    except (TypeError, ValueError):
        return default


def _qp_float(key: str, default: float) -> float:
    try:
        return float(_qp.get(key, default))
    except (TypeError, ValueError):
        return default


def _qp_bool(key: str, default: bool) -> bool:
    v = _qp.get(key)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


def _qp_str(key: str, default: str, choices: list[str] | None = None) -> str:
    v = _qp.get(key, default)
    if choices and v not in choices:
        return default
    return v


# ---------- Incremental dataset cache ----------
# Keyed by (slug, x_sec) → dict (one DB row). Lives in st.session_state and
# survives reruns within the browser session. On each call we:
#   1. Ask DB for the slug list of N most-recent resolved markets (cheap).
#   2. Top up only the (slug, x_sec) pairs missing from the cache.
#   3. Build the raw dataset from the cache in original order.
#   4. Apply the strategy purely in-memory.
# So sliders that don't touch X/N replay against cache only — no DB I/O at all.

_CACHE_KEY = "_market_cache"


def _cache() -> dict[tuple[str, int], dict[str, Any]]:
    if _CACHE_KEY not in st.session_state:
        st.session_state[_CACHE_KEY] = {}
    return st.session_state[_CACHE_KEY]


def cached_dataset(x_sec: int, n_markets: int) -> tuple[pd.DataFrame, dict[str, int]]:
    """Returns (raw_df, stats). stats has keys: cached, fetched, total."""
    cache = _cache()
    slugs = asyncio.run(fetch_resolved_slugs(n_markets))
    missing = [s for s in slugs if (s, x_sec) not in cache]
    if missing:
        new_df = asyncio.run(fetch_resolved_dataset(x_sec, n_markets, slugs=missing))
        if not new_df.empty:
            for _, r in new_df.iterrows():
                cache[(r["slug"], x_sec)] = r.to_dict()

    rows = [cache[(s, x_sec)] for s in slugs if (s, x_sec) in cache]
    raw_df = pd.DataFrame(rows) if rows else pd.DataFrame()
    return raw_df, {
        "cached": len(slugs) - len(missing),
        "fetched": len(missing),
        "total": len(slugs),
    }


def run_simulation(
    x_sec: int, y_usd: float, n_markets: int, signal: str,
    max_spot_staleness_sec: int, max_book_staleness_sec: int,
    min_ask_cents: int, retry_enabled: bool, retry_interval_sec: int,
    min_signal_agreement_pct: int,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, int]]:
    raw, stats = cached_dataset(x_sec, n_markets)
    trades, summary = apply_strategy(
        raw, x_sec, y_usd, SignalSource(signal),
        max_spot_staleness_sec=max_spot_staleness_sec,
        max_book_staleness_sec=max_book_staleness_sec,
        min_ask_cents=min_ask_cents,
        retry_enabled=retry_enabled,
        retry_interval_sec=retry_interval_sec,
        min_signal_agreement_pct=min_signal_agreement_pct,
    )
    return trades, summary.__dict__, stats


# --- Sidebar controls ---
st.sidebar.title("Strategy params")
x_sec = st.sidebar.slider(
    "X — sec before close (entry offset)", 5, 60,
    _qp_int("x_sec", 20), step=5,
)
y_usd = st.sidebar.number_input(
    "Y — bet size, $", min_value=1.0, max_value=10_000.0,
    value=_qp_float("y_usd", 10.0), step=1.0,
)
n_markets = st.sidebar.number_input(
    "N — last resolved markets", min_value=1, max_value=10_000,
    value=_qp_int("n_markets", 200), step=10,
)
_signal_opts = [s.value for s in SignalSource]
signal = st.sidebar.radio(
    "Signal",
    options=_signal_opts,
    format_func=lambda s: {"spot_vs_strike": "Spot vs Strike (BTC)",
                           "pm_price": "PM price tilt"}[s],
    index=_signal_opts.index(_qp_str("signal", "spot_vs_strike", _signal_opts)),
)
st.sidebar.markdown("---")
st.sidebar.subheader("Min price + retry")
min_ask_cents = st.sidebar.number_input(
    "A — min entry price, ¢ (skip if cheaper)",
    min_value=0, max_value=99, value=_qp_int("min_ask", 10), step=1,
)
retry_enabled = st.sidebar.checkbox(
    "Retry until price ≥ A", value=_qp_bool("retry", False),
)
retry_interval_sec = st.sidebar.number_input(
    "R — retry interval, sec", min_value=1, max_value=60,
    value=_qp_int("retry_r", 2), step=1, disabled=not retry_enabled,
)
st.sidebar.caption(
    "If chosen side's best_ask is below A at t_entry, skip the trade. "
    "With Retry on, re-check every R seconds until price reaches A or window closes."
)

st.sidebar.markdown("---")
st.sidebar.subheader("Signal-agreement filter")
min_signal_agreement_pct = st.sidebar.slider(
    "Min pre-entry agreement, %",
    min_value=0, max_value=100,
    value=_qp_int("min_agree", 0), step=5,
)
st.sidebar.caption(
    "Skip the trade if fewer than this fraction of pre-entry BTC ticks "
    "(window_start → t_entry) confirmed the side we picked. "
    "0% = no filter, 75% = take only trades where the prior trend mostly agreed with our entry signal."
)

st.sidebar.markdown("---")
st.sidebar.subheader("Cache")
_total_cache = len(_cache()) if _CACHE_KEY in st.session_state else 0
st.sidebar.caption(f"In session: **{_total_cache}** (slug, X) entries.")
if st.sidebar.button("Clear market cache", help="Forces a full DB re-fetch next run."):
    st.session_state.pop(_CACHE_KEY, None)
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("Data-quality gates")
max_spot_age = st.sidebar.slider(
    "Max BTC spot age, sec", 1, 120, _qp_int("spot_age", 15),
)
max_book_age = st.sidebar.slider(
    "Max PM book age, sec", 1, 60, _qp_int("book_age", 10),
)
st.sidebar.caption(
    "Trades whose strike, spot-at-entry or chosen-side book snapshot are older "
    "than these thresholds are dropped — guards against look-ahead bias when "
    "the BTC tick at entry happens to equal the close tick (sparse data)."
)

st.title("Polymarket BTC 5m — strategy backtester")

with st.spinner("Running simulation..."):
    trades, summary, cache_stats = run_simulation(
        int(x_sec), float(y_usd), int(n_markets), signal,
        int(max_spot_age), int(max_book_age),
        int(min_ask_cents), bool(retry_enabled), int(retry_interval_sec),
        int(min_signal_agreement_pct),
    )

# --- Summary cards ---
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Trades",       f"{summary['n_trades']:,}",
          delta=f"-{summary['n_skipped_no_data'] + summary['n_skipped_stale'] + summary['n_skipped_bad_price']} skipped" if (summary['n_skipped_no_data'] or summary['n_skipped_stale'] or summary['n_skipped_bad_price']) else None)
c2.metric("Total PnL, $", f"{summary['total_pnl']:+,.2f}")
c3.metric("Win rate",     f"{summary['win_rate']*100:.1f}%")
c4.metric("Avg entry, ¢", f"{summary['avg_entry_price']:.1f}")
c5.metric("Max DD, $",    f"{summary['max_drawdown']:,.2f}")

c6, c7, c8 = st.columns(3)
c6.metric("Avg winning entry, ¢", f"{summary['avg_winning_entry']:.1f}")
c7.metric("Avg losing entry, ¢",  f"{summary['avg_losing_entry']:.1f}")
c8.metric("Sharpe (naive)",       f"{summary['sharpe']:.2f}")

st.caption(
    f"Skipped: stale={summary['n_skipped_stale']}, "
    f"no_data={summary['n_skipped_no_data']}, "
    f"bad_price={summary['n_skipped_bad_price']}, "
    f"price_too_low={summary['n_skipped_price_too_low']}, "
    f"low_agreement={summary['n_skipped_low_agreement']}"
)
st.caption(
    f"📦 Cache: {cache_stats['cached']} hit, {cache_stats['fetched']} fetched, "
    f"{cache_stats['total']} markets queried."
)

if trades.empty:
    st.warning("Нет сделок прошедших фильтры. Ослабь staleness-гейты или подожди пока коллектор соберёт больше данных.")
    st.stop()

# --- Cumulative PnL ---
chart_df = trades.iloc[::-1].copy()
chart_df["cum_pnl"] = chart_df["pnl_usd"].cumsum()
chart_df = chart_df.set_index("window_end")
st.subheader("Cumulative PnL")
st.line_chart(chart_df["cum_pnl"], height=240)

# --- Trades table ---
st.subheader(f"Trades — {len(trades)} rows (newest first)")
show_btc = st.checkbox("Show BTC strike / spot / close columns",
                       value=_qp_bool("show_btc", True))
show_quality = st.checkbox("Show data-quality columns (ages, sources)",
                           value=_qp_bool("show_quality", True))
show_pm_open = st.checkbox("Show PM open prices",
                           value=_qp_bool("show_pm_open", False))
show_pm_entry = st.checkbox("Show PM entry prices",
                            value=_qp_bool("show_pm_entry", True))
show_pm_close = st.checkbox("Show PM close prices",
                            value=_qp_bool("show_pm_close", True))

cols = ["window_end", "side", "win", "pnl_usd",
        "entry_price_cents", "entry_bid_cents", "spread_cents", "retry_sec",
        "btc_range_before_entry", "crossed_strike_before_entry",
        "signal_agreement_pct",
        "resolved_outcome", "slug"]
if show_btc:
    cols = cols[:7] + ["strike_btc", "spot_at_entry", "spot_minus_strike", "close_btc"] + cols[7:]
if show_quality:
    cols += ["strike_age_s", "spot_age_s", "book_age_s",
             "strike_src", "spot_at_entry_src", "close_src"]
if show_pm_open:
    cols += ["pm_up_open_c", "pm_down_open_c"]
if show_pm_entry:
    cols += ["pm_up_entry_c", "pm_down_entry_c"]
if show_pm_close:
    cols += ["pm_up_close_c", "pm_down_close_c"]

seen, ordered = set(), []
for c in cols:
    if c in trades.columns and c not in seen:
        seen.add(c); ordered.append(c)

styled = trades[ordered].style.format({
    "pnl_usd": "{:+.2f}",
    "entry_price_cents": "{:.1f}", "entry_bid_cents": "{:.1f}",
    "spread_cents": "{:.1f}",
    "strike_btc": "{:,.2f}", "spot_at_entry": "{:,.2f}",
    "spot_minus_strike": "{:+,.2f}", "close_btc": "{:,.2f}",
    "btc_range_before_entry": "{:,.2f}",
    "signal_agreement_pct": "{:.1f}",
    "pm_up_open_c": "{:.1f}", "pm_down_open_c": "{:.1f}",
    "pm_up_entry_c": "{:.1f}", "pm_down_entry_c": "{:.1f}",
    "pm_up_close_c": "{:.1f}", "pm_down_close_c": "{:.1f}",
}, na_rep="—")

st.dataframe(styled, use_container_width=True, height=600)

st.subheader("Win rate & PnL by pre-entry feature bins")
analysis_df = trades.copy()
for c in ("retry_sec", "crossed_strike_before_entry"):
    if c in analysis_df.columns:
        analysis_df[c] = pd.to_numeric(analysis_df[c], errors="coerce").fillna(0).astype(int)
for c in ("btc_range_before_entry", "signal_agreement_pct"):
    if c in analysis_df.columns:
        analysis_df[c] = pd.to_numeric(analysis_df[c], errors="coerce")


def _bin_stats(df: pd.DataFrame, bin_col: str, order: list) -> pd.DataFrame:
    g = df.groupby(bin_col, observed=True).agg(
        n=("win", "size"),
        wins=("win", "sum"),
        total_pnl=("pnl_usd", "sum"),
    ).reindex(order, fill_value=0).reset_index()
    g["win_rate"] = (g["wins"] / g["n"] * 100).where(g["n"] > 0, 0)
    g["avg_pnl"] = (g["total_pnl"] / g["n"]).where(g["n"] > 0, 0)
    g[bin_col] = g[bin_col].astype(str)
    return g


def _twin_chart(stats: pd.DataFrame, bin_col: str, bin_title: str) -> alt.LayerChart:
    """Bar = win rate (%). Bar color = sign of total PnL. Text label above = N."""
    base = alt.Chart(stats).encode(
        x=alt.X(f"{bin_col}:N", sort=stats[bin_col].tolist(), title=bin_title),
    )
    bars = base.mark_bar(size=40).encode(
        y=alt.Y("win_rate:Q", title="Win rate, %", scale=alt.Scale(domain=[0, 100])),
        color=alt.condition(
            "datum.total_pnl >= 0",
            alt.value("#2ca02c"),
            alt.value("#d62728"),
        ),
        tooltip=[
            alt.Tooltip(f"{bin_col}:N", title=bin_title),
            alt.Tooltip("n:Q", title="trades"),
            alt.Tooltip("wins:Q", title="wins"),
            alt.Tooltip("win_rate:Q", title="win rate %", format=".1f"),
            alt.Tooltip("total_pnl:Q", title="total PnL $", format="+.2f"),
            alt.Tooltip("avg_pnl:Q", title="avg PnL $", format="+.3f"),
        ],
    )
    text_n = base.mark_text(dy=-8, fontSize=11, color="#888").encode(
        y="win_rate:Q",
        text=alt.Text("n:Q", format="d"),
    )
    text_pnl = base.mark_text(dy=12, fontSize=11, fontWeight="bold").encode(
        y=alt.value(15),  # near the top
        text=alt.Text("total_pnl:Q", format="+.1f"),
        color=alt.condition("datum.total_pnl >= 0",
                            alt.value("#1a661a"), alt.value("#8b1818")),
    )
    return (bars + text_n + text_pnl).properties(height=320)


# --- Retry bins ---
retry_order = ["0", "1–5", "6–15", "16+"]
def _bin_retry(v):
    if v <= 0: return "0"
    if v <= 5: return "1–5"
    if v <= 15: return "6–15"
    return "16+"
analysis_df["_retry_bin"] = analysis_df["retry_sec"].apply(_bin_retry)
retry_stats = _bin_stats(analysis_df, "_retry_bin", retry_order)

# --- Crossings bins ---
cross_order = ["0", "1–2", "3–5", "6+"]
def _bin_cross(v):
    if v <= 0: return "0"
    if v <= 2: return "1–2"
    if v <= 5: return "3–5"
    return "6+"
analysis_df["_cross_bin"] = analysis_df["crossed_strike_before_entry"].apply(_bin_cross)
cross_stats = _bin_stats(analysis_df, "_cross_bin", cross_order)

# --- BTC range bins ---
range_order = ["0–10", "10–25", "25–50", "50–100", "100+"]
def _bin_range(v):
    if pd.isna(v): return None
    if v <= 10:  return "0–10"
    if v <= 25:  return "10–25"
    if v <= 50:  return "25–50"
    if v <= 100: return "50–100"
    return "100+"
analysis_df["_range_bin"] = analysis_df["btc_range_before_entry"].apply(_bin_range)
range_stats = _bin_stats(
    analysis_df[analysis_df["_range_bin"].notna()],
    "_range_bin",
    range_order,
)

# --- Signal-agreement bins ---
agree_order = ["0–25%", "25–50%", "50–75%", "75–95%", "95–100%"]
def _bin_agree(v):
    if pd.isna(v): return None
    if v < 25:  return "0–25%"
    if v < 50:  return "25–50%"
    if v < 75:  return "50–75%"
    if v < 95:  return "75–95%"
    return "95–100%"
analysis_df["_agree_bin"] = analysis_df["signal_agreement_pct"].apply(_bin_agree)
agree_stats = _bin_stats(
    analysis_df[analysis_df["_agree_bin"].notna()],
    "_agree_bin",
    agree_order,
)

st.caption(
    "Bar height = win rate. Color = sign of total PnL (green ≥0, red <0). "
    "Small grey number on top = trade count in that bin. Bold number near "
    "the top axis = total PnL in dollars."
)
col_r, col_c, col_b, col_a = st.columns(4)
with col_r:
    st.markdown("**Retry seconds**")
    st.altair_chart(_twin_chart(retry_stats, "_retry_bin", "retry_sec"),
                    use_container_width=True)
with col_c:
    st.markdown("**Strike crossings before entry**")
    st.altair_chart(_twin_chart(cross_stats, "_cross_bin", "crossed_strike_before_entry"),
                    use_container_width=True)
with col_b:
    st.markdown("**BTC range before entry, $**")
    st.altair_chart(_twin_chart(range_stats, "_range_bin", "btc_range_before_entry"),
                    use_container_width=True)
with col_a:
    st.markdown("**Signal agreement %** (pre-entry ticks confirming our side)")
    st.altair_chart(_twin_chart(agree_stats, "_agree_bin", "signal_agreement_pct"),
                    use_container_width=True)

st.subheader("Entry-price distribution by outcome")
bins = [0, 30, 40, 50, 60, 70, 100]
labels = ["0-30", "30-40", "40-50", "50-60", "60-70", "70-100"]
hist_df = trades.assign(
    result=trades["win"].map({True: "win", False: "lose"}),
    bucket=pd.cut(trades["entry_price_cents"], bins=bins, labels=labels),
)
dist = (
    hist_df.groupby(["bucket", "result"], observed=True)
    .size()
    .unstack(fill_value=0)
    .reindex(labels, fill_value=0)
)
dist.index = dist.index.astype(str)
st.bar_chart(dist, height=240)

st.caption(
    "Markets are cached per (slug, X) in the browser session — sliders that "
    "don't change X/N replay against cache without touching the DB. Use the "
    "**Clear market cache** button in the sidebar if you want a fresh pull."
)
st.caption(
    "Open the **Inspect market** page in the left sidebar to view per-second "
    "BTC + PM charts for a single window — copy any slug from the table above."
)


# Persist all widget values to URL so a browser reload restores the exact view.
st.query_params.update({
    "x_sec": str(int(x_sec)),
    "y_usd": str(float(y_usd)),
    "n_markets": str(int(n_markets)),
    "signal": signal,
    "min_ask": str(int(min_ask_cents)),
    "retry": "1" if retry_enabled else "0",
    "retry_r": str(int(retry_interval_sec)),
    "min_agree": str(int(min_signal_agreement_pct)),
    "spot_age": str(int(max_spot_age)),
    "book_age": str(int(max_book_age)),
    "show_btc": "1" if show_btc else "0",
    "show_quality": "1" if show_quality else "0",
    "show_pm_open": "1" if show_pm_open else "0",
    "show_pm_entry": "1" if show_pm_entry else "0",
    "show_pm_close": "1" if show_pm_close else "0",
})
