# TC Chat Performance Dashboard

A single Streamlit app that monitors TC chat usage, CRR, CRT, and CSAT across
Lazada, Shopee, and TikTok chat performance exports — with seller-wise,
merchant-ID-wise, and overall views, MoM/WoW summaries, filtered/full report
downloads, and a manual Chrome-extension sync check with email alert.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then upload the three tracker workbooks (Lazada / Shopee / TikTok) in the
sidebar. The app reads every sheet that looks like a month tab (e.g. "Jan
2026", "July 2026") and ignores pivot-table / scratch sheets automatically.

## What was decided vs. what's still open

This was scoped through a clarifying round before building. Decisions made:

- **Data source: Excel re-uploads**, not a live database connection. Someone
  re-uploads the latest tracker export(s) whenever the dashboard needs
  refreshing; there's no background job pulling from a database.
- **TC Reply % / MP Reply %: on hold.** None of the three current exports
  reliably distinguish "TC" (Graas team) vs "MP" (merchant's own staff)
  replies at the row level — the only hint is TikTok's `Customer service
  agent` column, which is agent-level, not present in Lazada/Shopee at all.
  These four scorecard tiles show **"Awaiting TC data"** as a placeholder.
  Once the separate TC usage dataset arrives, wire it into
  `utils/metrics.py::compute_scorecard()` — that's the one place these are
  computed.
- **CSAT excludes Lazada.** Lazada's export has no CSAT-equivalent column at
  all (Shopee has `CSAT %`, TikTok has `Satisfaction rate`). Overall CSAT is
  computed only from Shopee + TikTok rows currently in view; if a filter
  selects Lazada-only data, CSAT shows "N/A".
- **Sync alert is manual**, triggered by a button in the app (not a scheduled
  job), per your answer. It compares each platform's most recent data date to
  today and flags anything more than 1 day behind as "Not synced."

Still needs your input before the email feature is real:

1. **Recipient email addresses** — `utils/email_alert.py` has Preethy's
   address filled in (`preethy@graas.ai`, from this conversation) but
   Swaroop Joy's and Yamini A.S's are **placeholders** (`swaroop@graas.ai`,
   `yamini@graas.ai`) — please confirm or correct these.
2. **SMTP credentials** — copy `.streamlit/secrets.toml.example` to
   `.streamlit/secrets.toml` and fill in a real SMTP account (Gmail app
   password, your company mail relay, SendGrid SMTP, etc). Without this the
   "Send alert email now" button will show a clear error instead of failing
   silently.
3. **Metric definitions** — CRR is recomputed as Responded ÷ Total
   Conversations (not each platform's own pre-built rate column, since those
   are defined slightly differently per platform). CRT is a conversation-
   count-weighted average of response time in minutes. If your team defines
   CRR/CRT differently (e.g. CRR should include a holiday-mode adjustment
   from Lazada's "Response Rate (Holiday Mode)" column), flag it and it's a
   small change in `utils/metrics.py`.
4. **"Seller-wise" vs "Merchant ID-wise."** In the source data, "Seller ID"
   and "Merchant ID" are literally the same field (confirmed via the `tiktok
   dksh TH data` sheet, which labels it "Seller ID" using the same codes as
   "Merchant ID" elsewhere). To make the two requested views actually
   different, "Seller-wise Performance" is grouped by the **Graas BX
   executive** (`bx_name`) managing the account, while "Merchant ID-wise
   Performance" is grouped by the **Merchant ID**. If you intended something
   else by "Seller ID," say so and it's a one-line change
   (`utils/metrics.py::seller_performance`).
5. Neither the SQL you mentioned nor a sample "chat monitor" Streamlit app
   came through as attachments — this was built from the structure of the
   three uploaded trackers instead. If you still want to share either, I can
   fold in anything they'd change.

## Project layout

```
app.py                          Streamlit UI: filters, scorecard, tabs, downloads, sync check
utils/data_loader.py            Reads + normalizes the 3 platform exports into one schema
utils/metrics.py                Scorecard, MoM/WoW, and group (merchant/seller/store) tables
utils/email_alert.py            Sync check + email send (recipients/SMTP config here)
.streamlit/secrets.toml.example Copy to secrets.toml and fill in SMTP creds
```

## Data schema (normalized, one row per platform/merchant/store/day)

| Column | Meaning |
|---|---|
| platform | Lazada / Shopee / TikTok |
| merchant_id | Merchant / Seller ID short code (e.g. "GED") |
| store_name | Store name (used as "Store" filter) |
| country | Country code |
| bx_name | Graas BX executive on the account |
| date | Calendar date |
| total_conversations | Conversations received/assigned that day |
| responded_conversations | Conversations responded to |
| non_responded | Conversations not responded to |
| response_rate_pct | responded / total × 100 (CRR building block) |
| avg_response_time_min | Average response time, minutes (CRT building block) |
| csat_pct | CSAT %, NaN for Lazada |
| guided_revenue / guided_orders / guided_buyers | Chat-assisted commerce impact, where available |

TikTok's raw export is agent-level (one row per CS agent per store per day);
`data_loader.load_tiktok()` aggregates it up to store/day so it lines up with
the other two platforms.
