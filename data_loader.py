"""
Data loading & normalization for the TC / Chat Performance Dashboard.

Reads the three raw exports (Lazada, Shopee, TikTok "Performance Tracker" workbooks),
each of which has one sheet per month (e.g. "Jan 2026", "July 2026") plus assorted
pivot-table / scratch sheets that are ignored, and normalizes them into one common
schema so they can be filtered, combined, and summarized together.

Common (normalized) columns produced for every platform:

    platform                 "Lazada" | "Shopee" | "TikTok"
    merchant_id               Merchant / Seller ID (short code, e.g. "GED")
    store_name                Store name (used as "Store ID" in filters)
    country                   Country code
    bx_name                   Graas BX (CS executive) handling the account
    date                      Calendar date (datetime64)
    total_conversations       Conversations received / assigned that day
    responded_conversations   Conversations responded to
    non_responded             Conversations not responded to
    response_rate_pct         Responded / Total, as a 0-100 percentage
    avg_response_time_min     Average response time, in minutes
    csat_pct                  CSAT, as a 0-100 percentage (NaN where platform has none)
    guided_revenue            Revenue attributed to guided/chat-assisted orders
    guided_orders             Orders attributed to guided/chat-assisted orders
    guided_buyers             Buyers attributed to guided/chat-assisted orders

NOTE ON TC vs MP REPLIES: none of the three exports currently distinguish which
replies came from the outsourced "TC" (Graas team) vs the merchant's own "MP" staff
at the row level in a consistent way across all three platforms. Per the agreed
scope, Total TC Replies / Total MP Replies / TC Reply % / MP Reply % are left as
placeholders (None) until the separate TC usage dataset is supplied — see
metrics.py.
"""

import re
import io
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

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


# --------------------------------------------------------------------------
# Platform-specific loaders
# --------------------------------------------------------------------------

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
