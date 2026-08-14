"""
TC Chat Performance Dashboard — Lazada / Shopee / TikTok

Monitors TC chat usage, CRR, CRT, and CSAT across all three marketplace chat
channels in one Streamlit application: seller-wise, merchant-ID-wise, and
overall views, MoM/WoW summaries, filtered + full report downloads, and a
manual Chrome-extension sync check with email alert.

Run:
    streamlit run app.py

Data in:
    Upload the three "Performance Tracker" workbooks in the sidebar (Lazada,
    Shopee, TikTok). Each is read month-sheet by month-sheet and combined.

See README.md for setup notes, metric definitions, and known open items
(TC vs MP reply split awaiting separate data; email recipients/SMTP need
confirming in utils/email_alert.py and .streamlit/secrets.toml).
"""

import io
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from utils.data_loader import load_all
from utils.metrics import (
    compute_scorecard, mom_summary, wow_summary,
    merchant_performance, seller_performance, store_performance, platform_performance,
)
from utils.email_alert import check_sync_status, send_sync_alert, RECIPIENTS

# --- Validated categorical palette (dataviz skill default) ------------------
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
PLATFORM_COLORS = {"Lazada": CATEGORICAL[0], "Shopee": CATEGORICAL[1], "TikTok": CATEGORICAL[2]}
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6", "#184f95"]

st.set_page_config(page_title="TC Chat Performance Dashboard", layout="wide")


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

@st.cache_data(show_spinner="Reading uploaded trackers...")
def _load(lazada_bytes, shopee_bytes, tiktok_bytes):
    lz = io.BytesIO(lazada_bytes) if lazada_bytes else None
    sp = io.BytesIO(shopee_bytes) if shopee_bytes else None
    tk = io.BytesIO(tiktok_bytes) if tiktok_bytes else None
    return load_all(lz, sp, tk)


st.sidebar.title("Data")
st.sidebar.caption("Upload the latest tracker exports. Re-upload any time to refresh the dashboard.")
lazada_upload = st.sidebar.file_uploader("Lazada Performance Tracker (.xlsx)", type=["xlsx"], key="lazada")
shopee_upload = st.sidebar.file_uploader("Shopee Performance Tracker (.xlsx)", type=["xlsx"], key="shopee")
tiktok_upload = st.sidebar.file_uploader("TikTok Performance Tracker (.xlsx)", type=["xlsx"], key="tiktok")

if not (lazada_upload or shopee_upload or tiktok_upload):
    st.title("TC Chat Performance Dashboard")
    st.info("Upload at least one tracker file in the sidebar to get started (Lazada, Shopee, and/or TikTok).")
    st.stop()

df_all = _load(
    lazada_upload.getvalue() if lazada_upload else None,
    shopee_upload.getvalue() if shopee_upload else None,
    tiktok_upload.getvalue() if tiktok_upload else None,
)

if df_all.empty:
    st.error("No usable rows were found in the uploaded file(s). Check that month-tab sheet names look like 'July 2026'.")
    st.stop()


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

st.sidebar.divider()
st.sidebar.title("Filters")

min_date, max_date = df_all["date"].min().date(), df_all["date"].max().date()
date_range = st.sidebar.date_input(
    "Date range", value=(min_date, max_date), min_value=min_date, max_value=max_date
)
if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date, end_date = min_date, max_date

platform_opts = sorted(df_all["platform"].dropna().unique().tolist())
platform_sel = st.sidebar.multiselect("Platform", platform_opts, default=platform_opts)

merchant_opts = sorted(df_all["merchant_id"].dropna().unique().tolist())
merchant_sel = st.sidebar.multiselect("Merchant ID / Seller ID", merchant_opts, default=[])

store_opts = sorted(df_all["store_name"].dropna().unique().tolist())
store_sel = st.sidebar.multiselect("Store", store_opts, default=[])

mask = (
    (df_all["date"].dt.date >= start_date)
    & (df_all["date"].dt.date <= end_date)
    & (df_all["platform"].isin(platform_sel))
)
if merchant_sel:
    mask &= df_all["merchant_id"].isin(merchant_sel)
if store_sel:
    mask &= df_all["store_name"].isin(store_sel)

df = df_all[mask].copy()

st.sidebar.divider()
st.sidebar.caption(f"{len(df):,} rows in view (of {len(df_all):,} total loaded)")


# --------------------------------------------------------------------------
# Header + Scorecard
# --------------------------------------------------------------------------

st.title("TC Chat Performance Dashboard")
st.caption("Lazada · Shopee · TikTok — chat usage, CRR, CRT, and CSAT in one view")

if df.empty:
    st.warning("No rows match the current filters.")
    st.stop()

sc = compute_scorecard(df)


def fmt_pct(v):
    return "N/A" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.1f}%"


def fmt_num(v):
    return "N/A" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:,.0f}"


def fmt_min(v):
    return "N/A" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.1f} min"


row1 = st.columns(4)
row1[0].metric("Total Conversations", fmt_num(sc["total_conversations"]))
row1[1].metric("Total TC Replies", "Pending*")
row1[2].metric("Total MP Replies", "Pending*")
row1[3].metric("TC Reply %", "Pending*")

row2 = st.columns(4)
row2[0].metric("MP Reply %", "Pending*")
row2[1].metric("CRR (Response Rate)", fmt_pct(sc["crr_pct"]))
row2[2].metric("CRT (Response Time)", fmt_min(sc["crt_min"]))
row2[3].metric("CSAT", fmt_pct(sc["csat_pct"]) if not df["csat_pct"].dropna().empty else "N/A")

st.caption(
    "*TC/MP reply metrics are placeholders until the separate TC usage dataset is added. "
    "CSAT excludes Lazada rows (no CSAT field in that export)."
)

st.divider()


# --------------------------------------------------------------------------
# Views: Overall / Merchant ID-wise / Seller-wise
# --------------------------------------------------------------------------

tab_overall, tab_merchant, tab_seller = st.tabs([
    "Overall Performance", "Merchant ID-wise Performance", "Seller-wise Performance (BX)"
])

with tab_overall:
    st.subheader("Trend over time")
    trend = df.groupby([pd.Grouper(key="date", freq="D"), "platform"]).agg(
        total_conversations=("total_conversations", "sum"),
        responded=("responded_conversations", "sum"),
    ).reset_index()
    trend["crr_pct"] = np.where(trend["total_conversations"] > 0,
                                 trend["responded"] / trend["total_conversations"] * 100, np.nan)

    fig1 = px.line(trend, x="date", y="total_conversations", color="platform",
                   color_discrete_map=PLATFORM_COLORS, markers=False,
                   labels={"total_conversations": "Conversations", "date": "Date", "platform": "Platform"},
                   title="Daily conversations by platform")
    fig1.update_layout(plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb", legend_title_text="")
    st.plotly_chart(fig1, use_container_width=True)

    fig2 = px.line(trend, x="date", y="crr_pct", color="platform",
                   color_discrete_map=PLATFORM_COLORS,
                   labels={"crr_pct": "CRR %", "date": "Date", "platform": "Platform"},
                   title="Daily CRR (response rate) by platform")
    fig2.update_layout(plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb", legend_title_text="")
    st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Platform breakdown")
    st.dataframe(platform_performance(df), use_container_width=True, hide_index=True)

with tab_merchant:
    st.subheader("Merchant ID-wise performance")
    mp = merchant_performance(df)
    st.dataframe(mp, use_container_width=True, hide_index=True)
    if not mp.empty:
        top = mp.head(15)
        fig3 = px.bar(top, x="Merchant / Seller ID", y="Total Conversations",
                      color_discrete_sequence=[CATEGORICAL[0]],
                      title="Top merchants by conversation volume")
        fig3.update_layout(plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb")
        st.plotly_chart(fig3, use_container_width=True)

with tab_seller:
    st.caption(
        "Grouped by the Graas BX executive managing the account (bx_name), since "
        "'Seller ID' and 'Merchant ID' refer to the same field in this data. "
        "If you'd rather this tab be identical to Merchant ID-wise, let me know."
    )
    st.subheader("Seller-wise performance")
    st.dataframe(seller_performance(df), use_container_width=True, hide_index=True)

st.divider()


# --------------------------------------------------------------------------
# Summary tables
# --------------------------------------------------------------------------

st.header("Summary Tables")

t1, t2, t3 = st.tabs(["Month-on-Month (MoM)", "Week-on-Week (WoW)", "Seller ID-wise Performance"])
with t1:
    st.dataframe(mom_summary(df), use_container_width=True, hide_index=True)
with t2:
    st.dataframe(wow_summary(df), use_container_width=True, hide_index=True)
with t3:
    st.dataframe(merchant_performance(df), use_container_width=True, hide_index=True)

st.divider()


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------

st.header("Report Download")

col_a, col_b = st.columns(2)

with col_a:
    st.write("**Filtered data** (matches current filters above)")
    st.download_button(
        "Download filtered data (CSV)",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"tc_chat_filtered_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
    )

with col_b:
    st.write("**Complete report** (all loaded data + summaries, ignores filters)")

    def build_full_report(all_df: pd.DataFrame) -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            all_df.to_excel(writer, sheet_name="Raw Data (Combined)", index=False)
            mom_summary(all_df).to_excel(writer, sheet_name="MoM Summary", index=False)
            wow_summary(all_df).to_excel(writer, sheet_name="WoW Summary", index=False)
            merchant_performance(all_df).to_excel(writer, sheet_name="Merchant-Seller ID Wise", index=False)
            seller_performance(all_df).to_excel(writer, sheet_name="Seller (BX) Wise", index=False)
            store_performance(all_df).to_excel(writer, sheet_name="Store Wise", index=False)
            platform_performance(all_df).to_excel(writer, sheet_name="Platform Wise", index=False)
        return buf.getvalue()

    st.download_button(
        "Download complete report (Excel)",
        data=build_full_report(df_all),
        file_name=f"tc_chat_full_report_{datetime.now():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.divider()


# --------------------------------------------------------------------------
# Chrome extension sync check + email alert
# --------------------------------------------------------------------------

st.header("Chrome Extension Sync Check")
st.caption(
    "Manual check (per your requirement): click below to see whether each "
    "platform's data looks current, and optionally email the team if not."
)

if st.button("Check sync status"):
    status_df = check_sync_status(df_all)
    st.session_state["sync_status_df"] = status_df

if "sync_status_df" in st.session_state:
    status_df = st.session_state["sync_status_df"]

    def _style_status(val):
        color = STATUS_GOOD if val == "Synced" else STATUS_CRITICAL
        return f"color: {color}; font-weight: 600"

    st.dataframe(
        status_df.style.map(_style_status, subset=["Status"]),
        use_container_width=True, hide_index=True,
    )

    not_synced = status_df[status_df["Status"] == "Not synced"]
    if not not_synced.empty:
        st.warning(f"{len(not_synced)} platform(s) appear out of sync.")
    else:
        st.success("All platforms look up to date.")

    st.caption(f"Alert will be sent to: {', '.join(RECIPIENTS)} (confirm/edit in utils/email_alert.py)")
    if st.button("Send alert email now"):
        ok, msg = send_sync_alert(status_df)
        (st.success if ok else st.error)(msg)
