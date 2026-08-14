"""
TC Chat Performance Dashboard — Lazada / Shopee / TikTok

Monitors TC chat usage, CRR, CRT, and CSAT across all three marketplace chat
channels in one Streamlit application: seller-wise, merchant-ID-wise, and
overall views, MoM/WoW summaries, filtered + full report downloads, and a
manual Chrome-extension sync check with email alert.

This is a SINGLE self-contained file on purpose (data loading, metrics, email
alert, and UI all in one) so there's no folder structure that can go missing
when uploading to GitHub / Streamlit Cloud — just this one file + requirements.txt.

Run locally:
    streamlit run app.py

Deploy on Streamlit Cloud:
    Upload just this app.py and requirements.txt to your repo (root level is
    fine — no subfolders needed). Set "Main file path" to app.py.

Data in:
    Upload the three "Performance Tracker" workbooks in the sidebar (Lazada,
    Shopee, TikTok). Each is read month-sheet by month-sheet and combined.

See README.md for setup notes, metric definitions, and known open items
(TC vs MP reply split awaiting separate data; email recipients/SMTP need
confirming further down in this file and in .streamlit/secrets.toml).
"""

import io
import re
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

# ============================================================================
# SECTION 1: DATA LOADING & NORMALIZATION
# ============================================================================
#
# Reads the three raw exports (Lazada, Shopee, TikTok "Performance Tracker"
# workbooks), each of which has one sheet per month (e.g. "Jan 2026", "July
# 2026") plus assorted pivot-table / scratch sheets that are ignored, and
# normalizes them into one common schema so they can be filtered, combined,
# and summarized together.
#
# Common (normalized) columns produced for every platform:
#   platform, merchant_id, store_name, country, bx_name, date,
#   total_conversations, responded_conversations, non_responded,
#   response_rate_pct, avg_response_time_min, csat_pct,
#   guided_revenue, guided_orders, guided_buyers
#
# NOTE ON TC vs MP REPLIES: none of the three exports currently distinguish
# which replies came from the outsourced "TC" (Graas team) vs the merchant's
# own "MP" staff at the row level in a consistent way across all three
# platforms. Per the agreed scope, Total TC Replies / Total MP Replies /
# TC Reply % / MP Reply % are left as placeholders (None) until the separate
# TC usage dataset is supplied — see compute_scorecard() in Section 2.

MONTH_SHEET_RE = re.compile(
    r"^(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|"
    r"sep(t|tember)?|oct(ober)?|nov(ember)?|dec(ember)?)\.?\s*20\d{2}$",
    re.IGNORECASE,
)


def _is_month_sheet(name: str) -> bool:
    return bool(MONTH_SHEET_RE.match(name.strip()))


_MONTH_NAME_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _sheet_month_year(sheet_name: str):
    """'July 2026' -> (7, 2026). Returns (None, None) if it doesn't parse."""
    m = re.match(r"^([a-z]+)\.?\s*(20\d{2})$", sheet_name.strip().lower())
    if not m:
        return None, None
    month = _MONTH_NAME_TO_NUM.get(m.group(1)[:3])
    return month, int(m.group(2))


def _parse_date_cell(v):
    """Parse one raw date cell (datetime/date/string/number) into a Timestamp,
    without yet correcting for the day/month transposition some rows have."""
    if v is None:
        return pd.NaT
    if isinstance(v, str):
        v = v.strip()
        if v in ("", "-", "--"):
            return pd.NaT
        # Source text dates are written DD-MM-YYYY (e.g. "13-07-2026") — force
        # dayfirst so they aren't misread as MM-DD-YYYY.
        return pd.to_datetime(v, dayfirst=True, errors="coerce")
    return pd.to_datetime(v, errors="coerce")


def _fix_transposed_day_month(ts, expected_year, expected_month):
    """Some source rows have day/month swapped for day-of-month <= 12 — e.g. in
    the 'July 2026' sheet, July 1st sometimes comes through as the Excel date
    2026-01-07 (month and day transposed) instead of 2026-07-01. This is a
    known quirk of how the source workbook was authored (locale mismatch when
    the dates were entered), not something the dashboard can fix upstream —
    but it's detectable, because the sheet name tells us the correct month.
    Detect it and swap back; anything that still doesn't land in the expected
    month/year afterwards is treated as genuinely corrupt and dropped by the
    caller (e.g. a handful of misaligned rows where Date contains stray
    numbers from another column entirely)."""
    if pd.isna(ts) or expected_month is None:
        return ts
    if ts.year == expected_year and ts.month == expected_month:
        return ts
    if ts.year == expected_year and ts.day == expected_month and 1 <= ts.month <= 12:
        try:
            return ts.replace(month=ts.day, day=ts.month)
        except ValueError:
            return ts
    return ts


def _fix_month_sheet_dates(date_series: pd.Series, sheet_name: str) -> pd.Series:
    """Parse + correct a raw 'Date' column from a Lazada/Shopee month sheet.
    Rows that still don't fall in the sheet's declared month/year afterwards
    are set to NaT (dropped downstream) rather than kept as bogus dates."""
    month, year = _sheet_month_year(sheet_name)
    parsed = date_series.apply(_parse_date_cell)
    if month is None:
        return parsed
    fixed = parsed.apply(lambda ts: _fix_transposed_day_month(ts, year, month))
    valid = fixed.apply(lambda ts: pd.notna(ts) and ts.year == year and ts.month == month)
    return fixed.where(valid, pd.NaT)


def _clean_id(x):
    if pd.isna(x):
        return None
    return str(x).strip()


def _canonicalize_casing(series: pd.Series) -> pd.Series:
    """Different month sheets sometimes spell the same store/seller with
    different capitalization (e.g. "Puma" one month, "PUMA" the next) — that
    would otherwise split one entity into two rows in every group-by table.
    Map every case-insensitive duplicate to its most-frequently-used casing."""
    non_null = series.dropna()
    if non_null.empty:
        return series
    counts = non_null.value_counts()  # already sorted descending by frequency
    canonical = {}
    for val in counts.index:
        key = val.lower()
        canonical.setdefault(key, val)  # first = most frequent casing wins
    return series.map(lambda v: canonical.get(v.lower(), v) if pd.notna(v) else v)


def _to_num(series):
    """Coerce a column that may contain '-', '--', blanks, or numbers to float."""
    return pd.to_numeric(
        series.replace({"-": np.nan, "--": np.nan, "": np.nan}), errors="coerce"
    )


def _time_to_minutes(value):
    """Convert a datetime.time / timedelta / string / number into minutes (float)."""
    if value is None:
        return np.nan
    if isinstance(value, str):
        v = value.strip()
        if v in ("-", "--", ""):
            return np.nan
        m = re.match(r"([\d.]+)\s*min", v, re.IGNORECASE)
        if m:
            return float(m.group(1))
        try:
            return float(v)
        except ValueError:
            return np.nan
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return np.nan
        return float(value)
    if isinstance(value, timedelta):
        return value.total_seconds() / 60.0
    # datetime.time
    if hasattr(value, "hour"):
        return value.hour * 60 + value.minute + value.second / 60.0
    return np.nan


def _read_month_sheets(file):
    """Return {sheet_name: DataFrame} for every sheet that looks like a month tab."""
    xls = pd.ExcelFile(file)
    out = {}
    for sh in xls.sheet_names:
        if _is_month_sheet(sh):
            df = xls.parse(sh)
            df = df.dropna(how="all")
            if df.empty:
                continue
            out[sh] = df
    return out


def load_lazada(file) -> pd.DataFrame:
    sheets = _read_month_sheets(file)
    frames = []
    for sh, df in sheets.items():
        df = df.rename(columns=lambda c: str(c).strip())
        rename_map = {
            "Merchand id": "merchant_id",
            "Merchant id": "merchant_id",
            "Merchant ID": "merchant_id",
            "Store Name": "store_name",
            "Country": "country",
            "BX Name": "bx_name",
            "Date": "date",
            "Received Conversations": "total_conversations",
            "Responded Conversations": "responded_conversations",
            "Non-Responded Customers": "non_responded",
            "Response Rate": "response_rate_raw",
            "Average Response Time": "avg_response_time_min",
            "Guided Revenue": "guided_revenue",
            "Guided Orders": "guided_orders",
            "Guided Buyers": "guided_buyers",
        }
        df = df.rename(columns=rename_map)
        keep = list(dict.fromkeys(v for v in rename_map.values() if v in df.columns))
        df = df[keep].copy()
        df["merchant_id"] = df["merchant_id"].map(_clean_id)
        for col in ["total_conversations", "responded_conversations", "non_responded",
                    "response_rate_raw", "guided_revenue", "guided_orders", "guided_buyers"]:
            if col in df.columns:
                df[col] = _to_num(df[col])
        if "avg_response_time_min" in df.columns:
            df["avg_response_time_min"] = df["avg_response_time_min"].apply(_time_to_minutes)
        df["date"] = _fix_month_sheet_dates(df["date"], sh)
        df["platform"] = "Lazada"
        df["csat_pct"] = np.nan  # Lazada export has no CSAT field
        frames.append(df)
    if not frames:
        return _empty_frame()
    out = pd.concat(frames, ignore_index=True)
    return _finalize(out)


def load_shopee(file) -> pd.DataFrame:
    sheets = _read_month_sheets(file)
    frames = []
    for sh, df in sheets.items():
        df = df.rename(columns=lambda c: str(c).strip())
        rename_map = {
            "Merchant ID": "merchant_id",
            "Merchand id": "merchant_id",
            "Store Name": "store_name",
            "Country": "country",
            "BX Name": "bx_name",
            "Date": "date",
            "Chat Enquired": "total_conversations",
            "Responded Chats": "responded_conversations",
            "Non-responded Chats": "non_responded",
            "Chat Response Rate": "response_rate_raw",
            "Avg. Response Time": "avg_response_time_min",
            "CSAT %": "csat_raw",
            "Guided Revenue": "guided_revenue",
            "Sales (USD)": "guided_revenue_fallback",
            "Guided Orders": "guided_orders",
            "Orders": "guided_orders_fallback",
            "Guided Buyers": "guided_buyers",
            "Buyers": "guided_buyers_fallback",
        }
        df = df.rename(columns=rename_map)
        keep = list(dict.fromkeys(v for v in rename_map.values() if v in df.columns))
        df = df[keep].copy()
        df["merchant_id"] = df["merchant_id"].map(_clean_id)
        for col in ["total_conversations", "responded_conversations", "non_responded",
                    "response_rate_raw", "csat_raw", "guided_revenue", "guided_revenue_fallback",
                    "guided_orders", "guided_orders_fallback", "guided_buyers", "guided_buyers_fallback"]:
            if col in df.columns:
                df[col] = _to_num(df[col])
        # fold fallback columns (older month sheets used slightly different headers)
        if "guided_revenue_fallback" in df.columns:
            df["guided_revenue"] = df.get("guided_revenue", pd.Series(np.nan, index=df.index)).fillna(df["guided_revenue_fallback"])
        if "guided_orders_fallback" in df.columns:
            df["guided_orders"] = df.get("guided_orders", pd.Series(np.nan, index=df.index)).fillna(df["guided_orders_fallback"])
        if "guided_buyers_fallback" in df.columns:
            df["guided_buyers"] = df.get("guided_buyers", pd.Series(np.nan, index=df.index)).fillna(df["guided_buyers_fallback"])
        if "avg_response_time_min" in df.columns:
            df["avg_response_time_min"] = df["avg_response_time_min"].apply(_time_to_minutes)
        df["date"] = _fix_month_sheet_dates(df["date"], sh)
        df["platform"] = "Shopee"
        if "csat_raw" in df.columns:
            df["csat_pct"] = df["csat_raw"] * 100
        else:
            df["csat_pct"] = np.nan
        frames.append(df)
    if not frames:
        return _empty_frame()
    out = pd.concat(frames, ignore_index=True)
    return _finalize(out)


def load_tiktok(file) -> pd.DataFrame:
    """TikTok's export is agent-level (one row per CS agent per store per day) —
    aggregate up to store/day so it lines up with the other two platforms."""
    sheets = _read_month_sheets(file)
    frames = []
    for sh, df in sheets.items():
        df = df.rename(columns=lambda c: str(c).strip())
        rename_map = {
            "Merchand id": "merchant_id",
            "Merchant ID": "merchant_id",
            "Store Name": "store_name",
            "Country": "country",
            "BX Name": "bx_name",
            "Date": "date_raw",
            "Assigned chats": "total_conversations",
            "12h human agent responded chats": "responded_conversations",
            "Non-responded chats": "non_responded",
            "Satisfaction rate": "csat_raw",
            "Avg. first response time": "avg_response_time_raw",
        }
        df = df.rename(columns=rename_map)
        keep = list(dict.fromkeys(v for v in rename_map.values() if v in df.columns))
        df = df[keep].copy()
        if df.empty:
            continue
        df["merchant_id"] = df["merchant_id"].map(_clean_id)

        def parse_tiktok_date(v):
            if pd.isna(v):
                return pd.NaT
            try:
                return pd.to_datetime(str(int(float(v))), format="%Y%m%d", errors="coerce")
            except (ValueError, TypeError):
                return pd.to_datetime(v, errors="coerce")

        df["date"] = df["date_raw"].apply(parse_tiktok_date)
        for col in ["total_conversations", "responded_conversations", "non_responded", "csat_raw"]:
            if col in df.columns:
                df[col] = _to_num(df[col])
        if "avg_response_time_raw" in df.columns:
            df["response_time_min"] = df["avg_response_time_raw"].apply(_time_to_minutes)
        else:
            df["response_time_min"] = np.nan

        group_cols = ["merchant_id", "store_name", "country", "bx_name", "date"]
        df["_weighted_rt"] = df["response_time_min"] * df["total_conversations"]
        df["_weighted_csat"] = df["csat_raw"] * df["responded_conversations"]

        agg = df.groupby(group_cols, dropna=False).agg(
            total_conversations=("total_conversations", "sum"),
            responded_conversations=("responded_conversations", "sum"),
            non_responded=("non_responded", "sum"),
            _weighted_rt_sum=("_weighted_rt", "sum"),
            _weighted_csat_sum=("_weighted_csat", "sum"),
        ).reset_index()

        agg["avg_response_time_min"] = np.where(
            agg["total_conversations"] > 0,
            agg["_weighted_rt_sum"] / agg["total_conversations"],
            np.nan,
        )
        agg["csat_pct"] = np.where(
            agg["responded_conversations"] > 0,
            (agg["_weighted_csat_sum"] / agg["responded_conversations"]) * 100,
            np.nan,
        )
        agg = agg.drop(columns=["_weighted_rt_sum", "_weighted_csat_sum"])
        agg["platform"] = "TikTok"
        agg["guided_revenue"] = np.nan
        agg["guided_orders"] = np.nan
        agg["guided_buyers"] = np.nan
        frames.append(agg)
    if not frames:
        return _empty_frame()
    out = pd.concat(frames, ignore_index=True)
    return _finalize(out)


def _empty_frame() -> pd.DataFrame:
    return _finalize(pd.DataFrame(columns=[
        "merchant_id", "store_name", "country", "bx_name", "date", "platform",
        "total_conversations", "responded_conversations", "non_responded",
        "avg_response_time_min", "csat_pct", "guided_revenue", "guided_orders", "guided_buyers",
    ]))


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Common cleanup: dtype coercion, recomputed response rate, drop empty date rows."""
    for col in ["merchant_id", "store_name", "country", "bx_name"]:
        if col not in df.columns:
            df[col] = None
        else:
            # Strip stray whitespace so e.g. "Keerthana " and "Keerthana" don't
            # get treated as two different sellers, then fold case-only
            # variants (e.g. "PUMA" / "Puma") to the most common casing.
            df[col] = df[col].map(_clean_id)
            df[col] = _canonicalize_casing(df[col])
    for col in ["total_conversations", "responded_conversations", "non_responded",
                "avg_response_time_min", "csat_pct", "guided_revenue", "guided_orders", "guided_buyers"]:
        if col not in df.columns:
            df[col] = np.nan
    if "date" not in df.columns:
        df["date"] = pd.NaT

    df = df.dropna(subset=["date"]).copy()
    df["total_conversations"] = df["total_conversations"].fillna(0)
    df["responded_conversations"] = df["responded_conversations"].fillna(0)
    if "non_responded" in df.columns:
        df["non_responded"] = df["non_responded"].fillna(
            (df["total_conversations"] - df["responded_conversations"]).clip(lower=0)
        )
    # Recompute response rate from counts for consistency across platforms
    # (rather than trusting each platform's own pre-computed rate column, which
    # is expressed inconsistently). This is the CRR building block.
    df["response_rate_pct"] = np.where(
        df["total_conversations"] > 0,
        (df["responded_conversations"] / df["total_conversations"]) * 100,
        np.nan,
    )
    cols = [
        "platform", "merchant_id", "store_name", "country", "bx_name", "date",
        "total_conversations", "responded_conversations", "non_responded",
        "response_rate_pct", "avg_response_time_min", "csat_pct",
        "guided_revenue", "guided_orders", "guided_buyers",
    ]
    return df[cols].reset_index(drop=True)


def load_all(lazada_file=None, shopee_file=None, tiktok_file=None) -> pd.DataFrame:
    """Load whichever files were supplied and concatenate into one master dataframe."""
    frames = []
    if lazada_file is not None:
        frames.append(load_lazada(lazada_file))
    if shopee_file is not None:
        frames.append(load_shopee(shopee_file))
    if tiktok_file is not None:
        frames.append(load_tiktok(tiktok_file))
    if not frames:
        return _empty_frame()
    df = pd.concat(frames, ignore_index=True)
    # Re-canonicalize casing across platforms: each loader already folds
    # case-only variants within its own file, but "Chums" (Lazada) vs "CHUMS"
    # (Shopee) only becomes visible once everything is combined.
    for col in ["merchant_id", "store_name", "country", "bx_name"]:
        df[col] = _canonicalize_casing(df[col])
    df = df.sort_values(["date", "platform", "merchant_id"]).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Currency conversion: Guided Revenue is recorded per-row in each country's
# local currency (SGD, MYR, THB, PHP) — summing it directly across countries
# (e.g. an Overall or Merchant-wise total for a merchant selling in multiple
# countries) mixes currencies together, which isn't a meaningful number. This
# converts every row to USD using the country -> currency map below and the
# FX rates set in the sidebar, so revenue totals are apples-to-apples.
# --------------------------------------------------------------------------

CURRENCY_BY_COUNTRY = {"SG": "SGD", "MY": "MYR", "TH": "THB", "PH": "PHP", "ID": "IDR"}

# Approximate defaults — these move with the market, so they're editable in
# the sidebar rather than trusted as-is. Local currency units per 1 USD.
# NOTE: any country code that shows up in your data but isn't in
# CURRENCY_BY_COUNTRY above falls back to being treated as already-USD (rate
# 1.0) — i.e. NOT converted. That's silently wrong for a large-denomination
# currency (this bit us once already with Indonesia/IDR, whose raw guided
# revenue numbers are so large they looked like real USD until "ID" was added
# above). If you add sales in a new country, add its currency here too.
DEFAULT_FX_TO_USD = {"SGD": 1.34, "MYR": 4.70, "THB": 34.50, "PHP": 58.50, "IDR": 15800.0, "USD": 1.0}


def add_usd_conversion(df: pd.DataFrame, fx_rates: dict) -> pd.DataFrame:
    """Adds 'currency' (derived from country) and 'guided_revenue_usd' columns."""
    df = df.copy()
    currency = df["country"].map(CURRENCY_BY_COUNTRY).fillna("USD")
    rate = currency.map(fx_rates).fillna(1.0)
    df["currency"] = currency
    df["guided_revenue_usd"] = df["guided_revenue"] / rate
    return df


# --------------------------------------------------------------------------
# TC Usage Summary: a separate upload (from SQL Lab / your database) that
# breaks out TC (Graas team) vs MP (merchant's own staff) reply counts —
# neither the Lazada/Shopee/TikTok trackers above have this split. Accepts
# either of the two sample schemas provided:
#   - sqllab_tc_chat_usage: MERCHANT_ID, LOG_DATE, CHANNEL, BUYER_MESSAGE_COUNT,
#     MP_REPLY_COUNT, TC_REPLY_COUNT, TOTAL_SELLER_REPLY_COUNT, TC_REPLY_PCT
#   - tc_chat_filtered_data: the same core columns plus extra context
#     (SELLER_ID, STORE_CODE, CRR_PERCENT, AVG_CSAT, AVG_CRT_MINS, ...) — the
#     extra columns are read but not currently used elsewhere in the app.
# --------------------------------------------------------------------------

CHANNEL_PLATFORM_PREFIXES = {"lazada": "Lazada", "shopee": "Shopee", "tiktok": "TikTok"}


def _channel_to_platform(channel):
    """'shopee-12' -> 'Shopee'. Returns None for missing/unknown channels."""
    if pd.isna(channel):
        return None
    ch = str(channel).strip()
    if not ch or ch.lower() == "unknown":
        return None
    prefix = ch.split("-")[0].strip().lower()
    return CHANNEL_PLATFORM_PREFIXES.get(prefix)


def load_tc_usage(file) -> pd.DataFrame:
    """Normalizes a TC usage summary export into: merchant_id, date, channel,
    platform, tc_reply_count, mp_reply_count[, buyer_message_count]."""
    name = getattr(file, "name", "") or ""
    if str(name).lower().endswith((".xlsx", ".xls")):
        raw = pd.read_excel(file)
    else:
        raw = pd.read_csv(file)
    raw = raw.rename(columns=lambda c: str(c).strip().upper())

    required = {"MERCHANT_ID", "LOG_DATE", "TC_REPLY_COUNT", "MP_REPLY_COUNT"}
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(
            f"TC usage file is missing expected column(s): {', '.join(sorted(missing))}. "
            f"Expected at least: {', '.join(sorted(required))} (CHANNEL is used too, if present)."
        )

    out = pd.DataFrame()
    out["merchant_id"] = _canonicalize_casing(raw["MERCHANT_ID"].map(_clean_id))
    out["date"] = pd.to_datetime(raw["LOG_DATE"], errors="coerce")
    if "CHANNEL" in raw.columns:
        out["channel"] = raw["CHANNEL"].astype(str).str.strip()
        out["platform"] = out["channel"].map(_channel_to_platform)
    else:
        out["channel"] = None
        out["platform"] = None
    out["tc_reply_count"] = _to_num(raw["TC_REPLY_COUNT"]).fillna(0)
    out["mp_reply_count"] = _to_num(raw["MP_REPLY_COUNT"]).fillna(0)
    if "BUYER_MESSAGE_COUNT" in raw.columns:
        out["buyer_message_count"] = _to_num(raw["BUYER_MESSAGE_COUNT"]).fillna(0)
    out = out.dropna(subset=["date", "merchant_id"])
    return out.reset_index(drop=True)


# ============================================================================
# SECTION 2: METRICS & SUMMARY TABLES
# ============================================================================
#
# Definitions used (confirm these match your internal definitions):
#
#   CRR (Chat/Customer Response Rate) = Responded Conversations / Total
#       Conversations, %. Recomputed from raw counts so it's consistent
#       across Lazada / Shopee / TikTok rather than trusting each platform's
#       own differently-defined rate column.
#
#   CRT (Chat/Customer Response Time) = average response time in minutes,
#       weighted by the number of conversations each row represents (so a
#       store with 500 chats influences the blended average more than one
#       with 5).
#
#   CSAT = satisfaction %, weighted by responded conversations. Lazada's
#       export has no CSAT-equivalent field at all, so Lazada rows are
#       excluded from CSAT rather than silently treated as 0.
#
#   TC Replies / MP Replies / TC Reply % / MP Reply % = placeholders (None)
#       until the separate "TC usage data" is supplied. The scorecard renders
#       these as "Pending*" rather than a misleading 0 or blank.

def _round2(df: pd.DataFrame) -> pd.DataFrame:
    """Round every float column to 2 decimal places — applied to every table
    just before it's returned for display/export, so numbers read cleanly
    (e.g. '94.5155' -> '94.52', '33829.000' -> '33829.0') without touching the
    full-precision values used internally for further calculations."""
    if df.empty:
        return df
    df = df.copy()
    float_cols = df.select_dtypes(include=["float64", "float32"]).columns
    df[float_cols] = df[float_cols].round(2)
    return df


def _weighted_avg(values, weights):
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = ~np.isnan(values) & ~np.isnan(weights) & (weights > 0)
    if not mask.any():
        return np.nan
    return float(np.average(values[mask], weights=weights[mask]))


def compute_scorecard(df: pd.DataFrame) -> dict:
    total_conversations = float(df["total_conversations"].sum())
    total_responded = float(df["responded_conversations"].sum())
    crr = (total_responded / total_conversations * 100) if total_conversations > 0 else np.nan
    crt = _weighted_avg(df["avg_response_time_min"], df["total_conversations"])

    csat_df = df.dropna(subset=["csat_pct"])
    csat = _weighted_avg(csat_df["csat_pct"], csat_df["responded_conversations"])

    return {
        "total_conversations": total_conversations,
        "total_tc_replies": None,       # awaiting TC usage data
        "total_mp_replies": None,       # awaiting TC usage data
        "tc_reply_pct": None,           # awaiting TC usage data
        "mp_reply_pct": None,           # awaiting TC usage data
        "crr_pct": crr,
        "crt_min": crt,
        "csat_pct": csat,
    }


def _period_summary(df: pd.DataFrame, period_col: str) -> pd.DataFrame:
    rows = []
    for period, g in df.groupby(period_col):
        sc = compute_scorecard(g)
        rows.append({
            "Period": period,
            "Total Conversations": sc["total_conversations"],
            "Responded": float(g["responded_conversations"].sum()),
            "CRR %": sc["crr_pct"],
            "CRT (min)": sc["crt_min"],
            "CSAT %": sc["csat_pct"],
        })
    out = pd.DataFrame(rows).sort_values("Period").reset_index(drop=True)
    for col in ["Total Conversations", "CRR %", "CRT (min)", "CSAT %"]:
        out[f"{col} MoM Δ"] = out[col].diff()
    return _round2(out)


def mom_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["Period"] = d["date"].dt.to_period("M").astype(str)
    return _period_summary(d, "Period")


def wow_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    iso = d["date"].dt.isocalendar()
    d["Period"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    return _period_summary(d, "Period")


def group_performance(df: pd.DataFrame, group_col: str, label: str) -> pd.DataFrame:
    """Generic 'X-wise performance' table (used for Merchant ID, Store, BX/Seller)."""
    if df.empty:
        return pd.DataFrame()
    rows = []
    for key, g in df.groupby(group_col, dropna=False):
        sc = compute_scorecard(g)
        rows.append({
            label: key,
            "Total Conversations": sc["total_conversations"],
            "Responded": float(g["responded_conversations"].sum()),
            "Non-Responded": float(g["non_responded"].sum()),
            "CRR %": sc["crr_pct"],
            "CRT (min)": sc["crt_min"],
            "CSAT %": sc["csat_pct"],
            "Guided Revenue (USD)": float(g["guided_revenue_usd"].sum(skipna=True)) if "guided_revenue_usd" in g.columns else float(g["guided_revenue"].sum(skipna=True)),
        })
    out = pd.DataFrame(rows).sort_values("Total Conversations", ascending=False).reset_index(drop=True)
    return _round2(out)


def merchant_performance(df: pd.DataFrame) -> pd.DataFrame:
    return group_performance(df, "merchant_id", "Merchant / Seller ID")


def bx_performance(df: pd.DataFrame) -> pd.DataFrame:
    """Performance grouped by the Graas BX (team member) managing the account
    (bx_name) — this is BX team member performance, not a "seller" cut. (In
    this data, "Seller ID" and "Merchant ID" are literally the same field, so
    a separate seller-level grouping wouldn't add anything beyond Merchant
    ID-wise performance anyway.)"""
    return group_performance(df, "bx_name", "BX Name")


def store_performance(df: pd.DataFrame) -> pd.DataFrame:
    return group_performance(df, "store_name", "Store")


def platform_performance(df: pd.DataFrame) -> pd.DataFrame:
    return group_performance(df, "platform", "Platform")


# --------------------------------------------------------------------------
# TC usage metrics (from the separate TC Usage Summary upload — see
# load_tc_usage() in Section 1). All of these degrade gracefully to "nothing
# to add" when no TC usage file has been uploaded, so the rest of the app
# doesn't need to branch on whether it's present.
# --------------------------------------------------------------------------

def compute_tc_usage_scorecard(tc_df: pd.DataFrame) -> dict:
    if tc_df is None or tc_df.empty:
        return {
            "total_tc_replies": None,
            "total_mp_replies": None,
            "total_seller_replies": None,
            "tc_reply_pct": None,
            "mp_reply_pct": None,
        }
    total_tc = float(tc_df["tc_reply_count"].sum())
    total_mp = float(tc_df["mp_reply_count"].sum())
    total = total_tc + total_mp
    return {
        "total_tc_replies": total_tc,
        "total_mp_replies": total_mp,
        "total_seller_replies": total,  # TC Replies + MP Replies
        "tc_reply_pct": (total_tc / total * 100) if total > 0 else np.nan,
        "mp_reply_pct": (total_mp / total * 100) if total > 0 else np.nan,
    }


def tc_usage_by_merchant(tc_df: pd.DataFrame) -> pd.DataFrame:
    """TC/MP reply counts summed per merchant_id, with Total Seller Replies (TC + MP) and TC Reply %."""
    if tc_df is None or tc_df.empty:
        return pd.DataFrame(columns=["merchant_id", "TC Replies", "MP Replies", "Total Seller Replies", "TC Reply %"])
    g = tc_df.groupby("merchant_id", dropna=False).agg(
        tc_reply_count=("tc_reply_count", "sum"),
        mp_reply_count=("mp_reply_count", "sum"),
    ).reset_index()
    total = g["tc_reply_count"] + g["mp_reply_count"]
    g["Total Seller Replies"] = total
    g["TC Reply %"] = np.where(total > 0, g["tc_reply_count"] / total * 100, np.nan)
    return _round2(g.rename(columns={"tc_reply_count": "TC Replies", "mp_reply_count": "MP Replies"}))


def merchant_performance_with_tc(df: pd.DataFrame, tc_df: pd.DataFrame) -> pd.DataFrame:
    """Merchant ID-wise performance, with TC/MP reply columns merged in when
    a TC usage file is loaded. No-op (returns the plain table) otherwise."""
    perf = merchant_performance(df)
    if perf.empty or tc_df is None or tc_df.empty:
        return perf
    tc = tc_usage_by_merchant(tc_df).rename(columns={"merchant_id": "Merchant / Seller ID"})
    return _round2(perf.merge(tc, on="Merchant / Seller ID", how="left"))


def bx_performance_with_tc(df: pd.DataFrame, tc_df: pd.DataFrame) -> pd.DataFrame:
    """BX team member performance, with TC/MP reply columns merged in via a
    merchant_id -> bx_name lookup built from the currently-filtered main data
    (each merchant's most common BX in view). No-op if no TC usage file, or
    if none of its merchant IDs match a BX in the current view."""
    perf = bx_performance(df)
    if perf.empty or tc_df is None or tc_df.empty:
        return perf
    mapping = (
        df.dropna(subset=["merchant_id", "bx_name"])
          .groupby("merchant_id")["bx_name"]
          .agg(lambda s: s.value_counts().idxmax())
    )
    tc = tc_df.copy()
    tc["bx_name"] = tc["merchant_id"].map(mapping)
    tc = tc.dropna(subset=["bx_name"])
    if tc.empty:
        return perf
    g = tc.groupby("bx_name", dropna=False).agg(
        tc_reply_count=("tc_reply_count", "sum"),
        mp_reply_count=("mp_reply_count", "sum"),
    ).reset_index()
    total = g["tc_reply_count"] + g["mp_reply_count"]
    g["Total Seller Replies"] = total
    g["TC Reply %"] = np.where(total > 0, g["tc_reply_count"] / total * 100, np.nan)
    g = g.rename(columns={"bx_name": "BX Name", "tc_reply_count": "TC Replies", "mp_reply_count": "MP Replies"})
    return _round2(perf.merge(g, on="BX Name", how="left"))


# ============================================================================
# SECTION 3: CHROME-EXTENSION SYNC CHECK + EMAIL ALERT
# ============================================================================
#
# Trigger model: a manual "Check sync status" button inside the dashboard —
# not a background schedule. Clicking it:
#   1. Looks at the most recent date present in the uploaded data for each
#      platform.
#   2. Flags any platform whose latest date is more than SYNC_LAG_DAYS behind
#      "today", which is the signal that the Chrome extension has stopped
#      syncing.
#   3. Lets the user send an alert email to the configured recipients.
#
# SETUP REQUIRED before this can actually send mail:
#   - Fill in the real email addresses in RECIPIENTS below (Preethy's is
#     filled in from context; Swaroop Joy's and Yamini A.S's are placeholders
#     — confirm and replace).
#   - Add SMTP credentials to .streamlit/secrets.toml, or on Streamlit Cloud:
#     app Settings -> Secrets. Any SMTP provider works (Gmail app password,
#     Outlook, SES, SendGrid SMTP, etc). Format:
#         [smtp]
#         host = "smtp.gmail.com"
#         port = 587
#         username = "your_sending_account@graas.ai"
#         password = "app_password_here"
#         sender = "your_sending_account@graas.ai"

RECIPIENTS = [
    "preethy@graas.ai",       # Preethy AK (confirmed)
    "swaroop@graas.ai",       # Swaroop Joy — PLACEHOLDER, please confirm
    "yamini@graas.ai",        # Yamini A.S — PLACEHOLDER, please confirm
]

SYNC_LAG_DAYS = 1  # days of lag before we consider a platform "not synced"


SYNC_GROUP_LABELS = {
    "platform": "Platform",
    "merchant_id": "Merchant / Seller ID",
    "channel": "Nickname / Channel",
}


def check_sync_status(df: pd.DataFrame, group_cols=("platform",), as_of: datetime = None) -> pd.DataFrame:
    """Return a sync status table grouped by `group_cols`, based on the most
    recent date present in the loaded data for each group. Pass
    group_cols=("platform",) for the platform-level overview (original
    behavior), or a finer grouping like ("platform", "merchant_id") or
    ("platform", "merchant_id", "channel") for Seller ID- or
    Nickname/Channel-level detail — this catches a single store/channel that
    has stopped syncing even while the rest of its platform looks current."""
    if as_of is None:
        as_of = datetime.now()
    group_cols = list(group_cols)
    labeled_cols = [SYNC_GROUP_LABELS.get(c, c) for c in group_cols]
    if df.empty or not all(c in df.columns for c in group_cols):
        return pd.DataFrame(columns=labeled_cols + ["Latest data date", "Days behind", "Status"])
    rows = []
    for key, g in df.dropna(subset=group_cols).groupby(group_cols, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        latest = g["date"].max()
        lag_days = (pd.Timestamp(as_of).normalize() - latest.normalize()).days if pd.notna(latest) else None
        status = "Not synced" if (lag_days is None or lag_days > SYNC_LAG_DAYS) else "Synced"
        row = dict(zip(labeled_cols, key))
        row["Latest data date"] = latest.date() if pd.notna(latest) else None
        row["Days behind"] = lag_days
        row["Status"] = status
        rows.append(row)
    return pd.DataFrame(rows, columns=labeled_cols + ["Latest data date", "Days behind", "Status"]).sort_values(labeled_cols).reset_index(drop=True)


def send_sync_alert(status_df: pd.DataFrame, recipients=None):
    """Send an email alert listing which platforms appear out of sync.
    Returns (success: bool, message: str)."""
    recipients = recipients or RECIPIENTS

    try:
        smtp_cfg = st.secrets["smtp"]
    except Exception:
        return False, (
            "No SMTP configuration found in st.secrets['smtp']. On Streamlit "
            "Cloud, add it under your app's Settings -> Secrets. Locally, add "
            "it to .streamlit/secrets.toml (see secrets.toml.example) before "
            "this can send mail."
        )

    not_synced = status_df[status_df["Status"] == "Not synced"]
    if not_synced.empty:
        subject = "[TC Dashboard] All platforms synced"
        body_lines = ["All platforms are reporting up-to-date data:", ""]
    else:
        subject = "[TC Dashboard] Chrome extension sync alert"
        body_lines = [
            "The following platform(s) have not synced recent data via the Chrome extension:",
            "",
        ]

    for _, row in status_df.iterrows():
        body_lines.append(
            f"  - {row['Platform']}: latest data = {row['Latest data date']}, "
            f"{row['Days behind']} day(s) behind, status = {row['Status']}"
        )
    body_lines += ["", "— Sent from the TC Chat Performance Dashboard"]
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = smtp_cfg["sender"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg.get("port", 587))) as server:
            server.starttls()
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            server.sendmail(smtp_cfg["sender"], recipients, msg.as_string())
        return True, f"Alert email sent to {', '.join(recipients)}."
    except Exception as e:
        return False, f"Failed to send email: {e}"


# ============================================================================
# SECTION 4: STREAMLIT UI
# ============================================================================

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
PLATFORM_COLORS = {"Lazada": CATEGORICAL[0], "Shopee": CATEGORICAL[1], "TikTok": CATEGORICAL[2]}
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"

st.set_page_config(page_title="TC Chat Performance Dashboard", layout="wide")


@st.cache_data(show_spinner="Reading uploaded trackers...")
def _load(lazada_bytes, shopee_bytes, tiktok_bytes):
    lz = io.BytesIO(lazada_bytes) if lazada_bytes else None
    sp = io.BytesIO(shopee_bytes) if shopee_bytes else None
    tk = io.BytesIO(tiktok_bytes) if tiktok_bytes else None
    return load_all(lz, sp, tk)


@st.cache_data(show_spinner="Reading TC usage summary...")
def _load_tc_usage_cached(file_bytes, file_name):
    buf = io.BytesIO(file_bytes)
    buf.name = file_name
    return load_tc_usage(buf)


st.sidebar.title("Data")
st.sidebar.caption("Upload the latest tracker exports. Re-upload any time to refresh the dashboard.")
lazada_upload = st.sidebar.file_uploader("Lazada Performance Tracker (.xlsx)", type=["xlsx"], key="lazada")
shopee_upload = st.sidebar.file_uploader("Shopee Performance Tracker (.xlsx)", type=["xlsx"], key="shopee")
tiktok_upload = st.sidebar.file_uploader("TikTok Performance Tracker (.xlsx)", type=["xlsx"], key="tiktok")

st.sidebar.divider()
st.sidebar.caption(
    "Optional: adds Total TC Replies / MP Replies / TC Reply % / MP Reply % "
    "to the scorecard and the Merchant ID-wise / BX Performance tables."
)
tc_usage_upload = st.sidebar.file_uploader(
    "TC Usage Summary (.csv or .xlsx)", type=["csv", "xlsx"], key="tc_usage"
)

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

if tc_usage_upload is not None:
    try:
        tc_usage_df_all = _load_tc_usage_cached(tc_usage_upload.getvalue(), tc_usage_upload.name)
    except ValueError as e:
        st.sidebar.error(str(e))
        tc_usage_df_all = pd.DataFrame()
else:
    tc_usage_df_all = pd.DataFrame()


# --------------------------------------------------------------------------
# Currency (Guided Revenue -> USD)
# --------------------------------------------------------------------------

st.sidebar.divider()
st.sidebar.title("Currency")
countries_in_data = df_all["country"].dropna().unique().tolist()
unmapped_countries = sorted(c for c in countries_in_data if c not in CURRENCY_BY_COUNTRY)
present_currencies = sorted({CURRENCY_BY_COUNTRY.get(c, "USD") for c in countries_in_data})
with st.sidebar.expander("FX rates (local currency per 1 USD)", expanded=bool(unmapped_countries)):
    fx_rates = {"USD": 1.0}
    for cur in present_currencies:
        if cur == "USD":
            continue
        fx_rates[cur] = st.number_input(
            f"{cur} per USD", min_value=0.0001, value=float(DEFAULT_FX_TO_USD.get(cur, 1.0)),
            step=0.01, format="%.4f", key=f"fx_{cur}",
        )
    st.caption(
        "Approximate defaults — update to your actual rates. Guided Revenue "
        "(USD) recalculates instantly; nothing is re-uploaded."
    )
    if unmapped_countries:
        st.warning(
            f"Country code(s) {', '.join(unmapped_countries)} aren't mapped to a "
            "currency, so their Guided Revenue is being treated as already-USD "
            "(NOT converted) — this is very likely wrong. Add them to "
            "CURRENCY_BY_COUNTRY near the top of app.py."
        )

df_all = add_usd_conversion(df_all, fx_rates)


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

country_opts = sorted(df_all["country"].dropna().unique().tolist())
country_sel = st.sidebar.multiselect("Country", country_opts, default=[])

mask = (
    (df_all["date"].dt.date >= start_date)
    & (df_all["date"].dt.date <= end_date)
    & (df_all["platform"].isin(platform_sel))
)
if merchant_sel:
    mask &= df_all["merchant_id"].isin(merchant_sel)
if store_sel:
    mask &= df_all["store_name"].isin(store_sel)
if country_sel:
    mask &= df_all["country"].isin(country_sel)

df = df_all[mask].copy()

# TC usage is filtered by date range + merchant selection only (it has no
# store_name / country / tracker-platform field of its own to match the
# Store/Country filters against — its "platform" is derived from CHANNEL and
# is looser).
if not tc_usage_df_all.empty:
    tc_mask = (
        (tc_usage_df_all["date"].dt.date >= start_date)
        & (tc_usage_df_all["date"].dt.date <= end_date)
    )
    if merchant_sel:
        tc_mask &= tc_usage_df_all["merchant_id"].isin(merchant_sel)
    tc_usage_df = tc_usage_df_all[tc_mask].copy()
else:
    tc_usage_df = tc_usage_df_all

st.sidebar.divider()
st.sidebar.caption(f"{len(df):,} rows in view (of {len(df_all):,} total loaded)")
if not tc_usage_df_all.empty:
    st.sidebar.caption(f"{len(tc_usage_df):,} TC usage rows in view (of {len(tc_usage_df_all):,} total loaded)")


# --------------------------------------------------------------------------
# Header + Scorecard
# --------------------------------------------------------------------------

st.title("TC Chat Performance Dashboard")
st.caption("Lazada · Shopee · TikTok — chat usage, CRR, CRT, and CSAT in one view")

if df.empty:
    st.warning("No rows match the current filters.")
    st.stop()

sc = compute_scorecard(df)
tc_sc = compute_tc_usage_scorecard(tc_usage_df)
tc_data_loaded = tc_usage_upload is not None and not tc_usage_df_all.empty


def fmt_pct(v):
    return "N/A" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.2f}%"


def fmt_num(v):
    return "N/A" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:,.0f}"


def fmt_min(v):
    return "N/A" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.2f} min"


def fmt_tc(v):
    return fmt_num(v) if tc_data_loaded else "Pending*"


def fmt_tc_pct(v):
    return fmt_pct(v) if tc_data_loaded else "Pending*"


row1 = st.columns(4)
row1[0].metric("Total TC Replies", fmt_tc(tc_sc["total_tc_replies"]))
row1[1].metric("Total MP Replies", fmt_tc(tc_sc["total_mp_replies"]))
row1[2].metric("Total Seller Replies", fmt_tc(tc_sc["total_seller_replies"]))
row1[3].metric("TC Reply %", fmt_tc_pct(tc_sc["tc_reply_pct"]))

row2 = st.columns(4)
row2[0].metric("MP Reply %", fmt_tc_pct(tc_sc["mp_reply_pct"]))
row2[1].metric("CRR (Response Rate)", fmt_pct(sc["crr_pct"]))
row2[2].metric("CRT (Response Time)", fmt_min(sc["crt_min"]))
row2[3].metric("CSAT", fmt_pct(sc["csat_pct"]) if not df["csat_pct"].dropna().empty else "N/A")

if tc_data_loaded:
    st.caption(
        "TC/MP reply metrics are from the uploaded TC Usage Summary, filtered to the "
        "current date range and Merchant ID/Seller ID selection (the Platform, Store, and "
        "Country filters don't apply to this file — see the sidebar note). "
        "CSAT excludes Lazada rows (no CSAT field in that export). "
        "Note: TC/MP Replies (and Total Seller Replies) count individual reply *messages*, "
        "not conversations — a single conversation typically contains several reply "
        "messages, so these totals naturally run several times higher than a conversation "
        "count would."
    )
else:
    st.caption(
        "*TC/MP reply metrics are placeholders until you upload a TC Usage Summary "
        "file in the sidebar. CSAT excludes Lazada rows (no CSAT field in that export)."
    )

st.divider()


# --------------------------------------------------------------------------
# Views: Overall / Merchant ID-wise / BX Performance
# --------------------------------------------------------------------------

tab_overall, tab_merchant, tab_seller = st.tabs([
    "Overall Performance", "Merchant ID-wise Performance", "BX Performance"
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
    mp = merchant_performance_with_tc(df, tc_usage_df)
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
        "BX team member performance — grouped by the Graas BX executive managing "
        "the account (bx_name), not by seller/merchant ('Seller ID' and 'Merchant "
        "ID' refer to the same field in this data, so that cut lives in the "
        "Merchant ID-wise tab instead)."
    )
    st.subheader("BX performance")
    st.dataframe(bx_performance_with_tc(df, tc_usage_df), use_container_width=True, hide_index=True)

st.divider()


# --------------------------------------------------------------------------
# Summary tables
# --------------------------------------------------------------------------

st.header("Summary Tables")

t1, t2, t3 = st.tabs(["Month-on-Month (MoM)", "Week-on-Week (WoW)", "BX Performance"])
with t1:
    st.dataframe(mom_summary(df), use_container_width=True, hide_index=True)
with t2:
    st.dataframe(wow_summary(df), use_container_width=True, hide_index=True)
with t3:
    st.dataframe(bx_performance_with_tc(df, tc_usage_df), use_container_width=True, hide_index=True)

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
        data=_round2(df).to_csv(index=False).encode("utf-8"),
        file_name=f"tc_chat_filtered_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
    )

with col_b:
    st.write("**Complete report** (all loaded data + summaries, ignores filters)")

    def build_full_report(all_df: pd.DataFrame, all_tc_df: pd.DataFrame) -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            _round2(all_df).to_excel(writer, sheet_name="Raw Data (Combined)", index=False)
            mom_summary(all_df).to_excel(writer, sheet_name="MoM Summary", index=False)
            wow_summary(all_df).to_excel(writer, sheet_name="WoW Summary", index=False)
            merchant_performance_with_tc(all_df, all_tc_df).to_excel(writer, sheet_name="Merchant-Seller ID Wise", index=False)
            bx_performance_with_tc(all_df, all_tc_df).to_excel(writer, sheet_name="BX Performance", index=False)
            store_performance(all_df).to_excel(writer, sheet_name="Store Wise", index=False)
            platform_performance(all_df).to_excel(writer, sheet_name="Platform Wise", index=False)
            if all_tc_df is not None and not all_tc_df.empty:
                _round2(all_tc_df).to_excel(writer, sheet_name="TC Usage Data (Raw)", index=False)
        return buf.getvalue()

    st.download_button(
        "Download complete report (Excel)",
        data=build_full_report(df_all, tc_usage_df_all),
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
    "platform's data looks current, and optionally email the team if not. "
    "Expand below for Seller ID- and Nickname/Channel-level detail — a single "
    "store can stop syncing while the rest of its platform still looks current."
)

if st.button("Check sync status"):
    st.session_state["sync_status_df"] = check_sync_status(df_all, group_cols=("platform",))
    st.session_state["sync_status_seller_df"] = check_sync_status(df_all, group_cols=("platform", "merchant_id"))
    if not tc_usage_df_all.empty:
        st.session_state["sync_status_channel_df"] = check_sync_status(
            tc_usage_df_all, group_cols=("platform", "merchant_id", "channel")
        )
    else:
        st.session_state.pop("sync_status_channel_df", None)

if "sync_status_df" in st.session_state:
    status_df = st.session_state["sync_status_df"]

    def _style_status(val):
        color = STATUS_GOOD if val == "Synced" else STATUS_CRITICAL
        return f"color: {color}; font-weight: 600"

    st.subheader("Platform-level overview")
    st.dataframe(
        status_df.style.map(_style_status, subset=["Status"]),
        use_container_width=True, hide_index=True,
    )

    not_synced = status_df[status_df["Status"] == "Not synced"]
    if not not_synced.empty:
        st.warning(f"{len(not_synced)} platform(s) appear out of sync.")
    else:
        st.success("All platforms look up to date.")

    seller_df = st.session_state.get("sync_status_seller_df", pd.DataFrame())
    with st.expander(f"Seller ID-level detail ({len(seller_df)} merchants)", expanded=False):
        if seller_df.empty:
            st.caption("No Merchant ID data available in the current upload.")
        else:
            st.dataframe(
                seller_df.style.map(_style_status, subset=["Status"]),
                use_container_width=True, hide_index=True,
            )
            not_synced_sellers = seller_df[seller_df["Status"] == "Not synced"]
            if not not_synced_sellers.empty:
                st.warning(f"{len(not_synced_sellers)} merchant/platform combination(s) appear out of sync.")
            else:
                st.success("All merchants look up to date.")

    channel_df = st.session_state.get("sync_status_channel_df")
    with st.expander(
        f"Nickname / Channel-level detail ({len(channel_df) if channel_df is not None else 0} channels)",
        expanded=False,
    ):
        if channel_df is None or channel_df.empty:
            st.caption(
                "Upload a TC Usage Summary file in the sidebar to get Nickname/Channel-level "
                "detail (it carries the per-store channel/nickname, e.g. \"lazada-12\", that the "
                "three tracker workbooks don't)."
            )
        else:
            st.dataframe(
                channel_df.style.map(_style_status, subset=["Status"]),
                use_container_width=True, hide_index=True,
            )
            not_synced_channels = channel_df[channel_df["Status"] == "Not synced"]
            if not not_synced_channels.empty:
                st.warning(f"{len(not_synced_channels)} channel(s) appear out of sync.")
            else:
                st.success("All channels look up to date.")

    st.caption(f"Alert will be sent to: {', '.join(RECIPIENTS)} (confirm/edit RECIPIENTS near the top of app.py, Section 3)")
    if st.button("Send alert email now"):
        ok, msg = send_sync_alert(status_df)
        (st.success if ok else st.error)(msg)
