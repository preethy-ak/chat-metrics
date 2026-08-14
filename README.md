# TC Chat Performance Dashboard

A single Streamlit app that monitors TC chat usage, CRR, CRT, and CSAT across
Lazada, Shopee, and TikTok chat performance exports — with seller-wise,
merchant-ID-wise, and overall views, MoM/WoW summaries, filtered/full report
downloads, a manual Chrome-extension sync check with email alert, an optional
TC Usage Summary upload (Total TC Replies / MP Replies / Total Seller
Replies / TC Reply % / MP Reply %), and Guided Revenue converted to USD
across the multiple local currencies in the data.

**Everything lives in one file, `app.py`.** Data loading, metrics, the email
alert, and the UI are all inlined into that single script on purpose — no
`utils/` folder, no package imports, nothing that can go missing when
uploading to GitHub. If you're deploying to Streamlit Cloud, you only need
`app.py` and `requirements.txt`.

## Quick start (local)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then upload the three tracker workbooks (Lazada / Shopee / TikTok) in the
sidebar. The app reads every sheet that looks like a month tab (e.g. "Jan
2026", "July 2026") and ignores pivot-table / scratch sheets automatically.

## Deploying on Streamlit Cloud

1. In your GitHub repo, make sure only **`app.py`** and **`requirements.txt`**
   are at the root (delete any leftover `data_loader.py`, `metrics.py`,
   `email_alert.py`, `__init__.py` — they're no longer used and having them
   sit there unused is harmless, but removing them keeps things tidy).
2. On [share.streamlit.io](https://share.streamlit.io), set **Main file
   path** to `app.py`.
3. If you use the email alert feature, add your SMTP credentials under your
   app's **Settings → Secrets** (not a file — paste this directly in the
   Streamlit Cloud UI):
   ```toml
   [smtp]
   host = "smtp.gmail.com"
   port = 587
   username = "your_sending_account@graas.ai"
   password = "app_password_here"
   sender = "your_sending_account@graas.ai"
   ```
4. After pushing any code change: open your app, click **⋮ (Manage app)**
   in the bottom-right corner → **Reboot app** (use **Clear cache** first if
   you've just changed the uploaded Excel files and old results seem to be
   sticking around — the data loader is cached per-file for speed).

### Why the earlier version broke

The first version split code across `app.py` and a `utils/` folder
(`data_loader.py`, `metrics.py`, `email_alert.py`). When those files got
uploaded to GitHub via drag-and-drop, they landed flat in the repo root
instead of inside an actual `utils/` subfolder — so `from utils.data_loader
import load_all` failed with `ModuleNotFoundError: No module named 'utils'`.
This version removes the possibility of that mistake entirely by not using a
subfolder at all.

## Filters

Sidebar filters (all optional except Date range, which defaults to your full
loaded range): **Date range**, **Platform** (Lazada/Shopee/TikTok),
**Merchant ID / Seller ID**, **Store**, and **Country**. Country filters on
the same `country` field used for currency conversion (e.g. `SG`, `MY`, `PH`,
`TH`, `ID`) and applies to the three tracker workbooks only — the TC Usage
Summary upload has no country field, so (like Store) it isn't affected by
this filter; it's still narrowed by Date range and Merchant ID/Seller ID.

## What was decided vs. what's still open

This was scoped through a clarifying round before building. Decisions made:

- **Data source: Excel re-uploads**, not a live database connection. Someone
  re-uploads the latest tracker export(s) whenever the dashboard needs
  refreshing; there's no background job pulling from a database.
- **TC Reply % / MP Reply %: now wired up via a separate upload.** None of
  the three tracker exports distinguish "TC" (Graas team) vs "MP" (merchant's
  own staff) replies at the row level, so these four scorecard tiles need a
  second file — the **"TC Usage Summary (.csv or .xlsx)"** uploader in the
  sidebar. Without a file there, the tiles show **"Pending\*"**; with one,
  they show real totals. See "TC Usage Summary upload" below.
- **CSAT excludes Lazada.** Lazada's export has no CSAT-equivalent column at
  all (Shopee has `CSAT %`, TikTok has `Satisfaction rate`). Overall CSAT is
  computed only from Shopee + TikTok rows currently in view; if a filter
  selects Lazada-only data, CSAT shows "N/A".
- **Sync alert is manual**, triggered by a button in the app (not a scheduled
  job), per your answer. It compares each platform's most recent data date to
  today and flags anything more than 1 day behind as "Not synced."

Still needs your input before the email feature is real:

1. **Recipient email addresses** — Section 3 of `app.py` (`RECIPIENTS`) has
   Preethy's address filled in (`preethy@graas.ai`, from this conversation)
   but Swaroop Joy's and Yamini A.S's are **placeholders**
   (`swaroop@graas.ai`, `yamini@graas.ai`) — please confirm or correct these.
2. **SMTP credentials** — locally, copy `.streamlit/secrets.toml.example` to
   `.streamlit/secrets.toml`; on Streamlit Cloud, use Settings → Secrets (see
   above). Without this the "Send alert email now" button shows a clear error
   instead of failing silently.
3. **Metric definitions** — CRR is recomputed as Responded ÷ Total
   Conversations (not each platform's own pre-built rate column, since those
   are defined slightly differently per platform). CRT is a conversation-
   count-weighted average of response time in minutes. If your team defines
   CRR/CRT differently (e.g. CRR should include a holiday-mode adjustment
   from Lazada's "Response Rate (Holiday Mode)" column), flag it — it's a
   small change in Section 2 of `app.py`.
4. **"Seller-wise" vs "Merchant ID-wise."** In the source data, "Seller ID"
   and "Merchant ID" are literally the same field (confirmed via the `tiktok
   dksh TH data` sheet, which labels it "Seller ID" using the same codes as
   "Merchant ID" elsewhere). To make the two requested views actually
   different, "Seller-wise Performance" is grouped by the **Graas BX
   executive** (`bx_name`) managing the account, while "Merchant ID-wise
   Performance" is grouped by the **Merchant ID**. If you intended something
   else by "Seller ID," say so — it's a one-line change
   (`seller_performance()` in Section 2 of `app.py`).
5. Neither the SQL you mentioned nor a sample "chat monitor" Streamlit app
   came through as attachments — this was built from the structure of the
   three uploaded trackers instead. If you still want to share either, I can
   fold in anything they'd change.

## TC Usage Summary upload

Upload a CSV (or Excel) export from your database/SQL Lab with these columns
(case-insensitive): `MERCHANT_ID`, `LOG_DATE`, `TC_REPLY_COUNT`,
`MP_REPLY_COUNT`, and ideally `CHANNEL` (e.g. `"lazada-3"`, `"shopee-12"` —
used to derive a platform label; rows with no channel or an "Unknown"
channel just won't show a platform). Both sample formats you shared work
as-is: the plain `sqllab_tc_chat_usage` export, and the richer
`tc_chat_filtered_data` export (its extra columns — `SELLER_ID`,
`STORE_CODE`, `CRR_PERCENT`, `AVG_CSAT`, `AVG_CRT_MINS`, etc. — are read but
not currently used elsewhere; ask if you'd like those folded in too, e.g. as
an alternate CRR/CSAT/CRT source).

Once uploaded, it feeds:
- Five scorecard tiles: Total TC Replies, Total MP Replies, **Total Seller
  Replies** (TC + MP — the two simply add up), TC Reply %, MP Reply % —
  filtered by the current **Date range** and **Merchant ID/Seller ID**
  selections only. The Platform and Store filters don't apply to this file
  (it doesn't have a matching Store field, and its channel-derived platform
  is looser than the tracker data's).
- A **TC Replies / MP Replies / Total Seller Replies / TC Reply %** breakdown
  merged into the **Merchant ID-wise Performance** table (direct match on
  Merchant ID) and the **Seller-wise Performance (BX)** table (via a
  Merchant ID → BX executive lookup built from the currently-loaded tracker
  data).
- A **"TC Usage Data (Raw)"** sheet in the complete Excel report download.

If a merchant appears in the trackers but not in the TC usage file (or vice
versa), its TC/MP columns just show blank rather than a misleading 0.

## Guided Revenue currency conversion

Guided Revenue is recorded in each country's own local currency (SGD, MYR,
THB, PHP, IDR) — summing it directly across countries, which the dashboard
did in the first version, silently mixes currencies together into a
meaningless number. Every row is now converted to USD using the **Currency**
section in the sidebar (an expandable "FX rates" panel, one number input per
currency actually present in your data, defaulting to approximate rates you
should update to your real ones). Guided Revenue columns everywhere in the
app are now labeled **"Guided Revenue (USD)"** and recompute instantly when
you change a rate — no re-upload needed.

If a country code shows up in your data that isn't in `CURRENCY_BY_COUNTRY`
near the top of `app.py`, the sidebar shows a visible warning rather than
silently treating that country's revenue as already-USD (this bit the first
version of this fix: Indonesia/"ID" wasn't in the map, and its guided
revenue numbers are large enough in IDR that they looked like plausible USD
figures until they were actually converted — down to roughly 1/16,000th).
If you add sales in a new country, add its currency + a default rate to that
dict.

## Data quality fixes already applied

While building this, a few issues turned up in the source Excel files
themselves — these are handled automatically, but worth knowing about:

- **Day/month transposed dates.** Some rows (mostly in Shopee) had the day
  and month of the `Date` column swapped — e.g. July 1st stored as the Excel
  date "Jan 7" instead of "Jul 7". The app detects this using each sheet's
  own name (e.g. "July 2026") as ground truth and corrects it. Rows that
  still don't land in the right month after correction are dropped as
  genuinely corrupt (a handful of misaligned rows, mostly Lazada merchant
  "HFC" in the June sheet, where the Date column contained stray numbers
  from a different column entirely).
- **Inconsistent capitalization.** The same store/seller was sometimes typed
  differently across month sheets (e.g. "PUMA" vs "Puma" vs "puma"). The app
  folds these to the most common casing so they aggregate as one entity
  instead of splitting into duplicate rows in every table.
- **Un-mapped currency (Indonesia).** Guided Revenue is in local currency per
  country; Indonesia ("ID") was missing from the country→currency map, so its
  (very large, because IDR) revenue numbers were being treated as if already
  in USD — see "Guided Revenue currency conversion" above.
- **"Seller ID-wise Performance" tab was showing Merchant-ID data.** Under
  Summary Tables, that tab was wired to the same table as "Merchant ID-wise
  Performance" instead of the BX-executive grouping — fixed so it now shows
  Seller (BX Name) rows as labeled.

## Why Total Conversations can look much smaller than Total TC/MP Replies

This came up because a live dashboard view showed Total Conversations far
below Total TC Replies + Total MP Replies. Checked directly against your
uploaded files — this is not a duplicate-counting bug:

- `tc_chat_filtered_data.csv` (one of your two TC Usage Summary samples) has
  **both** a `TOTAL_CONVERSATIONS` column and `TC_REPLY_COUNT` /
  `MP_REPLY_COUNT` columns at the exact same row grain (per merchant/date/
  channel). Summed across the whole file: 66,871 total conversations vs.
  235,969 combined TC+MP replies — replies outnumber conversations by about
  **3.5x**, in the same file, on the same rows.
- That ratio makes sense once you consider what each column counts: Total
  Conversations is chat *threads*, while TC/MP Reply counts are individual
  reply *messages*. A single conversation thread naturally contains several
  back-and-forth reply messages, so reply totals will always run several
  times higher than conversation totals — that's expected, not an error.
- No duplicate rows were found in either TC Usage Summary sample file
  (checked for exact-duplicate rows, and for repeated merchant/date/channel
  combinations — the only repeats were rows where `CHANNEL` was "Unknown"
  but a real per-store channel was recorded in `NICKNAME_ID` instead, i.e.
  different stores, not duplicates).
- The two scorecard groups also draw from different source files (Total
  Conversations from the three tracker workbooks; TC/MP Replies from the TC
  Usage Summary upload) and the TC Usage file isn't filtered by Platform/
  Store the way the tracker data is — so the exact ratio you see will shift
  with your filters, but the direction (replies > conversations) is expected
  throughout.

Even with that explanation on record, **Total Conversations has since been
removed from the scorecard** per a follow-up request — the tile that showed
it next to Total TC/MP Replies is gone, so there's no more units mismatch to
explain at a glance. It's still tracked internally (it feeds CRR, the
Merchant/Seller/Store/Platform performance tables, the MoM/WoW summaries, and
the "Daily conversations by platform" / "Top merchants by conversation
volume" charts — removing it there too would break those), just no longer
shown as its own top-line number. Say the word if you'd like it stripped out
of those tables/charts as well.

## Number formatting

Every number in the app — scorecard tiles, all summary/performance tables,
and both downloads (filtered CSV and the complete Excel report) — is now
rounded to exactly 2 decimal places. Whole-number counts (Total Conversations,
row counts) still display with no decimals since rounding those wouldn't
change anything. Rounding happens only on the final table just before display
or export, so it doesn't compound into the underlying calculations.

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
| guided_revenue | Chat-assisted revenue, in the country's local currency |
| guided_orders / guided_buyers | Chat-assisted commerce impact, where available |
| currency | Local currency derived from country (added by `add_usd_conversion()`) |
| guided_revenue_usd | guided_revenue converted to USD using the sidebar FX rates |

TikTok's raw export is agent-level (one row per CS agent per store per day);
`load_tiktok()` aggregates it up to store/day so it lines up with the other
two platforms.

The separate **TC Usage Summary** upload (see above) has its own, unrelated
schema: `merchant_id, date, channel, platform, tc_reply_count,
mp_reply_count[, buyer_message_count]` — normalized by `load_tc_usage()`.
