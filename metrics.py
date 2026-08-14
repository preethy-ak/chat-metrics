"""
Metric calculations and summary-table builders for the TC / Chat Performance Dashboard.

Definitions used (confirm these match your internal definitions — see README):

  CRR (Chat/Customer Response Rate) = Responded Conversations / Total Conversations, %
      Recomputed from raw counts so it's consistent across Lazada / Shopee / TikTok
      rather than trusting each platform's own differently-defined rate column.

  CRT (Chat/Customer Response Time) = average response time in minutes, weighted
      by the number of conversations each row represents (so a store with 500
      chats influences the blended average more than one with 5).

  CSAT = satisfaction %, weighted by responded conversations. Lazada's export has
      no CSAT-equivalent field at all, so Lazada rows are excluded from CSAT
      (per the agreed handling) rather than silently treated as 0.

  TC Replies / MP Replies / TC Reply % / MP Reply % = placeholders (None) until
      the separate "TC usage data" is supplied. The scorecard renders these as
      "Awaiting TC data" rather than a misleading 0 or N/A-as-zero.
"""

import numpy as np
import pandas as pd


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
    return out


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
            "Guided Revenue": float(g["guided_revenue"].sum(skipna=True)),
        })
    out = pd.DataFrame(rows).sort_values("Total Conversations", ascending=False).reset_index(drop=True)
    return out


def merchant_performance(df: pd.DataFrame) -> pd.DataFrame:
    return group_performance(df, "merchant_id", "Merchant / Seller ID")


def seller_performance(df: pd.DataFrame) -> pd.DataFrame:
    """'Seller-wise' is interpreted here as the Graas BX executive managing the
    account (bx_name) — a distinct cut from Merchant ID, since in this data
    'Seller ID' and 'Merchant ID' are the same field. Adjust in the sidebar note
    if you actually want this grouped by Merchant ID instead."""
    return group_performance(df, "bx_name", "Seller (BX Name)")


def store_performance(df: pd.DataFrame) -> pd.DataFrame:
    return group_performance(df, "store_name", "Store")


def platform_performance(df: pd.DataFrame) -> pd.DataFrame:
    return group_performance(df, "platform", "Platform")
