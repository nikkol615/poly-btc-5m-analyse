"""Standalone page: detail view of one 5-minute Polymarket BTC window.

Runs independently of the backtester sliders — the only input is `slug`. Slug is
read from `st.query_params` so the page is shareable via direct URL like:
    /Inspect_market?slug=btc-updown-5m-1781645400&x_sec=20
The optional `x_sec` param controls the position of the dashed t_entry marker.
"""
from __future__ import annotations

import asyncio
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from poly_btc.backtest.sql import fetch_market_detail

st.set_page_config(page_title="Inspect market — Polymarket BTC 5m", layout="wide")


@st.cache_data(ttl=30, show_spinner=False)
def load_market_detail(slug: str) -> dict[str, Any]:
    return asyncio.run(fetch_market_detail(slug))


def _qp_int(key: str, default: int) -> int:
    try:
        return int(st.query_params.get(key, default))
    except (TypeError, ValueError):
        return default


def _qp_str(key: str, default: str = "") -> str:
    v = st.query_params.get(key, default)
    return v if isinstance(v, str) else default


st.title("Inspect a market window")
st.caption(
    "Pulls per-second BTC spot (chainlink + binance) and Polymarket Up/Down book "
    "for one 5-minute window. Independent of backtester parameters except for "
    "`x_sec` which controls the dashed `t_entry` marker."
)

current_slug = _qp_str("slug", "")
x_sec = _qp_int("x_sec", 20)

cc1, cc2 = st.columns([3, 1])
with cc1:
    slug = st.text_input(
        "Market slug",
        value=current_slug,
        placeholder="btc-updown-5m-1781645400",
        help="Copy from the Backtester trades table.",
    )
with cc2:
    x_sec = st.number_input(
        "X — entry offset, sec (for t_entry marker)",
        min_value=1, max_value=300, value=x_sec, step=1,
    )

st.query_params["slug"] = slug
st.query_params["x_sec"] = str(int(x_sec))

if not slug:
    st.info("Paste a slug above to render the detail charts.")
    st.stop()

detail = load_market_detail(slug)
m = detail["market"]
if m is None:
    st.warning(f"Market `{slug}` not found in DB.")
    st.stop()

strike_price = float(m["strike"]) if m["strike"] is not None else None
window_start = m["window_start"]
window_end = m["window_end"]
t_entry = window_end - pd.Timedelta(seconds=int(x_sec))

# ---- Market header card ----
mh1, mh2, mh3, mh4, mh5 = st.columns(5)
mh1.metric("Slug", slug.replace("btc-updown-5m-", "…"))
mh2.metric("Window start", window_start.strftime("%H:%M:%S"))
mh3.metric("Strike, $", f"{strike_price:,.2f}" if strike_price is not None else "—")
mh4.metric("Close, $",
           f"{float(m['close_price']):,.2f}" if m["close_price"] is not None else "—")
mh5.metric("Outcome", m["resolved_outcome"] or "—")

# ---- BTC spot chart ----
spot = detail["spot"].copy()
if spot.empty:
    st.info("No BTC spot ticks recorded for this window.")
else:
    spot["price"] = pd.to_numeric(spot["price"], errors="coerce")
    x_domain = [window_start.isoformat(),
                (window_end + pd.Timedelta(seconds=30)).isoformat()]
    spot_chart = alt.Chart(spot).mark_line(point=alt.OverlayMarkDef(size=15)).encode(
        x=alt.X("ts:T", title="time", scale=alt.Scale(domain=x_domain)),
        y=alt.Y("price:Q", title="BTC, $", scale=alt.Scale(zero=False)),
        color=alt.Color("source:N", scale=alt.Scale(
            domain=["chainlink", "binance"], range=["#1f77b4", "#ff7f0e"],
        ), legend=alt.Legend(title="spot source")),
        tooltip=["ts:T", "source:N", alt.Tooltip("price:Q", format=",.2f")],
    )
    layers = [spot_chart]
    if strike_price is not None:
        strike_line = alt.Chart(pd.DataFrame({"y": [strike_price]})).mark_rule(
            color="#d62728", strokeDash=[4, 4],
        ).encode(y="y:Q", tooltip=[alt.Tooltip("y:Q", title="strike", format=",.2f")])
        layers.append(strike_line)
    t_entry_rule = alt.Chart(pd.DataFrame({"x": [t_entry]})).mark_rule(
        color="#888", strokeDash=[2, 4],
    ).encode(x="x:T")
    window_end_rule = alt.Chart(pd.DataFrame({"x": [window_end]})).mark_rule(
        color="#444"
    ).encode(x="x:T")
    layers += [t_entry_rule, window_end_rule]
    st.markdown("**BTC spot** — dashed red = strike, grey dashed = t_entry, solid grey = window_end")
    st.altair_chart(alt.layer(*layers).properties(height=320), use_container_width=True)

# ---- PM Up/Down chart ----
up_b, dn_b = detail["up_book"].copy(), detail["dn_book"].copy()
pm_frames = []
if not up_b.empty:
    up_b["mid"] = (up_b["best_bid"] + up_b["best_ask"]) / 2
    up_b["side"] = "Up"
    pm_frames.append(up_b[["ts", "mid", "best_bid", "best_ask", "side"]])
if not dn_b.empty:
    dn_b["mid"] = (dn_b["best_bid"] + dn_b["best_ask"]) / 2
    dn_b["side"] = "Down"
    pm_frames.append(dn_b[["ts", "mid", "best_bid", "best_ask", "side"]])

if not pm_frames:
    st.info("No PM book snapshots recorded for this window.")
else:
    pm_df = pd.concat(pm_frames, ignore_index=True)
    x_domain = [window_start.isoformat(),
                (window_end + pd.Timedelta(seconds=30)).isoformat()]
    pm_chart = alt.Chart(pm_df).mark_line(point=alt.OverlayMarkDef(size=10)).encode(
        x=alt.X("ts:T", title="time", scale=alt.Scale(domain=x_domain)),
        y=alt.Y("mid:Q", title="PM mid", scale=alt.Scale(domain=[0, 1])),
        color=alt.Color("side:N", scale=alt.Scale(
            domain=["Up", "Down"], range=["#2ca02c", "#d62728"],
        ), legend=alt.Legend(title="PM side")),
        tooltip=["ts:T", "side:N",
                 alt.Tooltip("best_bid:Q", format=".3f"),
                 alt.Tooltip("best_ask:Q", format=".3f"),
                 alt.Tooltip("mid:Q", format=".3f")],
    )
    t_entry_rule2 = alt.Chart(pd.DataFrame({"x": [t_entry]})).mark_rule(
        color="#888", strokeDash=[2, 4],
    ).encode(x="x:T")
    window_end_rule2 = alt.Chart(pd.DataFrame({"x": [window_end]})).mark_rule(
        color="#444"
    ).encode(x="x:T")
    st.markdown("**Polymarket Up/Down mid price** — grey dashed = t_entry, solid grey = window_end")
    st.altair_chart(
        alt.layer(pm_chart, t_entry_rule2, window_end_rule2).properties(height=320),
        use_container_width=True,
    )
