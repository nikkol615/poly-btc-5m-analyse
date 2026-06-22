"""DB occupancy and growth forecast.

Shows current disk usage per table, daily growth of pm_book, compactor queue
depth, and projects how disk usage will evolve given the 7-day compaction
behavior. All inputs (max disk GB, compression ratio, growth rate) can be
tweaked via the sidebar — useful for "what-if" analysis.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import altair as alt
import pandas as pd
import psycopg
import streamlit as st
from psycopg.rows import dict_row

from poly_btc.config import settings

st.set_page_config(page_title="DB status — Polymarket BTC 5m", layout="wide")


# ---------- Data loaders ----------

@st.cache_data(ttl=30, show_spinner=False)
def load_db_stats() -> dict[str, Any]:
    return asyncio.run(_load_db_stats_async())


async def _load_db_stats_async() -> dict[str, Any]:
    async with await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row
    ) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT relname,
                       pg_total_relation_size(C.oid) AS total_bytes,
                       pg_relation_size(C.oid)       AS table_bytes,
                       pg_indexes_size(C.oid)        AS index_bytes,
                       reltuples::bigint             AS approx_rows
                FROM pg_class C
                JOIN pg_namespace N ON N.oid = C.relnamespace
                WHERE C.relkind = 'r' AND N.nspname = 'public'
                ORDER BY pg_total_relation_size(C.oid) DESC;
            """)
            tables = await cur.fetchall()

            await cur.execute("SELECT pg_database_size(current_database()) AS bytes")
            db_size = (await cur.fetchone())["bytes"]

            await cur.execute("""
                SELECT event_type, count(*)::bigint AS n
                FROM pm_book
                GROUP BY event_type
                ORDER BY n DESC;
            """)
            event_types = await cur.fetchall()

            await cur.execute("""
                SELECT date_trunc('day', ts) AS day, count(*)::bigint AS rows
                FROM pm_book
                WHERE ts >= NOW() - interval '14 days'
                GROUP BY day ORDER BY day;
            """)
            pm_book_daily = await cur.fetchall()

            await cur.execute("""
                SELECT
                  count(*) FILTER (WHERE compacted_at IS NOT NULL) AS compacted,
                  count(*) FILTER (WHERE compacted_at IS NULL
                                   AND window_end < NOW() - interval '7 days') AS pending,
                  count(*) FILTER (WHERE window_end >= NOW() - interval '7 days') AS fresh,
                  count(*)                                                       AS total
                FROM markets;
            """)
            compaction = await cur.fetchone()

            # Average rows per market — separately for uncompacted and compacted —
            # used to derive the compression ratio empirically.
            await cur.execute("""
                WITH uncompacted_sample AS (
                  SELECT m.slug
                  FROM markets m
                  WHERE m.compacted_at IS NULL
                    AND m.window_end < NOW()
                  ORDER BY m.window_end DESC
                  LIMIT 50
                ),
                compacted_sample AS (
                  SELECT m.slug
                  FROM markets m
                  WHERE m.compacted_at IS NOT NULL
                  ORDER BY m.compacted_at DESC
                  LIMIT 50
                )
                SELECT
                  (SELECT AVG(c.n)::bigint FROM (
                     SELECT count(*) AS n FROM pm_book b
                     JOIN uncompacted_sample u ON b.token_id IN (
                         (SELECT token_up FROM markets WHERE slug = u.slug),
                         (SELECT token_down FROM markets WHERE slug = u.slug))
                     JOIN markets mk ON mk.slug = u.slug
                     WHERE b.ts >= mk.window_start AND b.ts <= mk.window_end
                     GROUP BY u.slug
                   ) c)                                                   AS avg_uncompacted_rows,
                  (SELECT AVG(c.n)::bigint FROM (
                     SELECT count(*) AS n FROM pm_book b
                     JOIN compacted_sample co ON b.token_id IN (
                         (SELECT token_up FROM markets WHERE slug = co.slug),
                         (SELECT token_down FROM markets WHERE slug = co.slug))
                     JOIN markets mk ON mk.slug = co.slug
                     WHERE b.ts >= mk.window_start AND b.ts <= mk.window_end
                     GROUP BY co.slug
                   ) c)                                                   AS avg_compacted_rows;
            """)
            ratio_row = await cur.fetchone()

    return {
        "tables": tables,
        "db_size": db_size,
        "event_types": event_types,
        "pm_book_daily": pm_book_daily,
        "compaction": compaction,
        "avg_uncompacted_rows": ratio_row["avg_uncompacted_rows"],
        "avg_compacted_rows": ratio_row["avg_compacted_rows"],
    }


# ---------- Helpers ----------

def fmt_bytes(b: int | float | None) -> str:
    if b is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(b)
    for u in units:
        if abs(f) < 1024 or u == units[-1]:
            return f"{f:,.1f} {u}"
        f /= 1024
    return f"{f:,.1f} TB"


# ---------- Page ----------

st.title("DB occupancy & growth forecast")

with st.spinner("Querying database..."):
    stats = load_db_stats()

# Sidebar — assumptions
st.sidebar.subheader("Forecast assumptions")
disk_gb = st.sidebar.number_input(
    "Disk capacity, GB", min_value=10, max_value=10_000, value=150, step=10,
)
already_used_gb = st.sidebar.number_input(
    "Non-DB used disk, GB (OS, Docker, other apps)",
    min_value=0, max_value=int(disk_gb), value=15, step=1,
)
days_to_forecast = st.sidebar.slider("Forecast horizon, days", 7, 365, 90, step=7)
manual_compression = st.sidebar.slider(
    "Compression ratio override (0 = use measured)",
    min_value=0.0, max_value=1.0, value=0.0, step=0.05,
)

# --- Current sizes ---
st.subheader("Current occupancy")

tables = stats["tables"]
db_size = stats["db_size"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Database size", fmt_bytes(db_size))
c2.metric("Disk capacity",  f"{disk_gb} GB")
free_for_db_gb = disk_gb - already_used_gb - db_size / 1024**3
c3.metric("Free for DB",    f"{max(0, free_for_db_gb):.1f} GB")
c4.metric("Usage % of disk", f"{100 * (db_size/1024**3 + already_used_gb) / disk_gb:.1f}%")

# Bar chart per table
table_df = pd.DataFrame([
    {"table": r["relname"],
     "table data, MB": r["table_bytes"] / 1024**2,
     "indexes, MB":   r["index_bytes"] / 1024**2}
    for r in tables
])
if not table_df.empty:
    bar = alt.Chart(table_df.melt(id_vars="table", var_name="kind", value_name="MB")).mark_bar().encode(
        x=alt.X("MB:Q", title="MB"),
        y=alt.Y("table:N", sort="-x", title=None),
        color=alt.Color("kind:N",
                        scale=alt.Scale(domain=["table data, MB", "indexes, MB"],
                                        range=["#1f77b4", "#aec7e8"])),
        tooltip=["table:N", "kind:N", alt.Tooltip("MB:Q", format=",.1f")],
    ).properties(height=200)
    st.altair_chart(bar, use_container_width=True)
else:
    st.info("No public tables found.")

# --- pm_book breakdown by event_type ---
st.subheader("pm_book rows by event_type")
et_df = pd.DataFrame(stats["event_types"])
if not et_df.empty:
    et_df["pct"] = et_df["n"] / et_df["n"].sum() * 100
    et_df["approx_MB"] = et_df["n"] * 200 / 1024**2  # rough byte estimate
    st.dataframe(et_df, use_container_width=True, hide_index=True,
                 column_config={
                     "n": st.column_config.NumberColumn("rows", format="%d"),
                     "pct": st.column_config.NumberColumn("%", format="%.1f"),
                     "approx_MB": st.column_config.NumberColumn("≈ MB", format="%.1f"),
                 })

# --- Compaction status ---
st.subheader("Compactor queue")
comp = stats["compaction"]
mc1, mc2, mc3, mc4 = st.columns(4)
mc1.metric("Markets total", f"{comp['total']:,}")
mc2.metric("Fresh (<7d)", f"{comp['fresh']:,}")
mc3.metric("Pending compaction", f"{comp['pending']:,}")
mc4.metric("Already compacted", f"{comp['compacted']:,}")

# --- Measured compression ratio ---
au = stats["avg_uncompacted_rows"]
ac = stats["avg_compacted_rows"]
measured_ratio = (float(ac) / float(au)) if au and ac else None

st.subheader("Compression ratio")
rc1, rc2, rc3 = st.columns(3)
rc1.metric("Avg uncompacted rows / market", f"{au:,}" if au else "—")
rc2.metric("Avg compacted rows / market",   f"{ac:,}" if ac else "—")
rc3.metric("Measured ratio (compact/raw)",
           f"{measured_ratio:.2%}" if measured_ratio is not None else "—",
           help="Smaller = more compression. The compactor stores one row per second per token.")

# Choose effective compression ratio for forecast
if manual_compression > 0:
    effective_ratio = manual_compression
    st.caption(f"Using manual override compression ratio = {effective_ratio:.0%}")
elif measured_ratio is not None and measured_ratio > 0:
    effective_ratio = measured_ratio
    st.caption(f"Using measured compression ratio = {effective_ratio:.1%}")
else:
    effective_ratio = 0.15  # plausible default until compactor processes anything
    st.caption(f"No measured ratio yet — assuming {effective_ratio:.0%} for forecast.")

# --- Daily growth (pm_book) ---
st.subheader("pm_book growth (last 14 days)")
daily_df = pd.DataFrame(stats["pm_book_daily"])
if daily_df.empty:
    st.info("Not enough data yet to compute daily growth.")
    avg_daily_rows = 0.0
else:
    daily_df["day"] = pd.to_datetime(daily_df["day"])
    daily_df["MB"] = daily_df["rows"] * 200 / 1024**2
    # Average daily growth over last 3 full days (excluding today)
    today = pd.Timestamp(datetime.now(timezone.utc).date(), tz="UTC")
    full = daily_df[daily_df["day"] < today].tail(7)
    avg_daily_rows = float(full["rows"].mean()) if not full.empty else 0.0

    chart = alt.Chart(daily_df).mark_bar().encode(
        x=alt.X("day:T"),
        y=alt.Y("MB:Q", title="≈ MB written/day"),
        tooltip=["day:T", alt.Tooltip("rows:Q", format=","),
                 alt.Tooltip("MB:Q", format=",.1f")],
    ).properties(height=200)
    st.altair_chart(chart, use_container_width=True)

# --- Forecast ---
st.subheader(f"Forecast for next {days_to_forecast} days")

# Per-day pm_book bytes (uncompacted)
R_uncompact_bytes = avg_daily_rows * 200  # bytes/day at full event rate
# After 7 days, each old day is compacted to ratio × bytes
# Steady-state daily addition = R_uncompact * effective_ratio
# Standing buffer = 7 days × R_uncompact (always uncompacted, before compactor catches up)

# Other tables — assume they grow at their current sum / (#days of data)
other_tables_bytes = sum(r["total_bytes"] for r in tables if r["relname"] != "pm_book")
# Rough: assume other tables grow linearly with markets, use markets_per_day proxy
# Compactor effect is on pm_book only; pm_trades grows but stays small.

# Build day-by-day projection
now_used = db_size
projection: list[dict[str, Any]] = []
for d in range(days_to_forecast + 1):
    if d == 0:
        pm_book_added = 0
    elif d <= 7:
        pm_book_added = R_uncompact_bytes  # full rate
    else:
        # full rate added, but day (d-7) goes from uncompacted to compacted
        pm_book_added = R_uncompact_bytes - R_uncompact_bytes * (1 - effective_ratio)
        # = R_uncompact * effective_ratio
    if d == 0:
        cumulative = now_used
    else:
        cumulative = projection[-1]["bytes"] + pm_book_added
    projection.append({
        "day_offset": d,
        "date": (datetime.now(timezone.utc).date() + timedelta(days=d)),
        "bytes": cumulative,
        "GB": cumulative / 1024**3,
        "MB_added": pm_book_added / 1024**2,
    })

proj_df = pd.DataFrame(projection)
disk_limit_gb = disk_gb - already_used_gb

# When does it fill?
above_limit = proj_df[proj_df["GB"] >= disk_limit_gb]
fill_day: int | None = int(above_limit.iloc[0]["day_offset"]) if not above_limit.empty else None

fc1, fc2, fc3 = st.columns(3)
fc1.metric("Current DB", fmt_bytes(db_size))
fc2.metric(f"After {days_to_forecast}d", fmt_bytes(proj_df.iloc[-1]["bytes"]))
fc3.metric("Disk full in",
           f"{fill_day} days" if fill_day is not None else "> horizon",
           help="Based on the assumptions above. With compactor running and "
                "current rate, the DB will reach the disk limit at this point.")

# Chart: projected GB over time + disk limit line
proj_chart_df = pd.DataFrame({
    "date":         proj_df["date"].astype(str),
    "Projected DB": proj_df["GB"],
})
limit_df = pd.DataFrame({"y": [disk_limit_gb]})

base = alt.Chart(proj_chart_df).encode(x="date:T")
line = base.mark_line(color="#1f77b4").encode(
    y=alt.Y("Projected DB:Q", title="GB"),
    tooltip=["date:T", alt.Tooltip("Projected DB:Q", format=",.2f", title="GB")],
)
limit_line = alt.Chart(limit_df).mark_rule(color="#d62728", strokeDash=[4, 4]).encode(
    y="y:Q", tooltip=[alt.Tooltip("y:Q", title="disk limit, GB", format=",.1f")],
)
st.altair_chart((line + limit_line).properties(height=300), use_container_width=True)

# Summary text
ss_per_day_mb = R_uncompact_bytes * effective_ratio / 1024**2
buffer_mb = 7 * R_uncompact_bytes / 1024**2
st.markdown(f"""
**Assumptions:**
- Current pm_book write rate ≈ **{R_uncompact_bytes / 1024**2:,.1f} MB/day** (uncompacted)
- After compaction kicks in (day 7+), steady-state addition ≈ **{ss_per_day_mb:,.1f} MB/day**
- Standing "uncompacted buffer" of last 7 days ≈ **{buffer_mb:,.1f} MB**
- Compression ratio: **{effective_ratio:.1%}**

**Takeaway:** if measured rates and compression ratio hold,
{"the disk **will fill in about " + str(fill_day) + " days** — consider lowering retention or lowering write volume."
 if fill_day is not None else
 "we have **plenty of runway** — DB stays well below disk capacity for the entire forecast horizon."}
""")

st.caption(
    "Cached for 30s. Refresh page to recompute. "
    "All sizes use Postgres `pg_total_relation_size` which includes TOAST + indexes."
)
