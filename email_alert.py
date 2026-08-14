"""
Sync-check + email alert.

Trigger model (per requirements): a manual "Check sync status" button inside the
dashboard — not a background schedule. Clicking it:
  1. Looks at the most recent date present in the uploaded data for each platform.
  2. Flags any platform whose latest date is more than SYNC_LAG_DAYS behind
     "today", which is the signal that the Chrome extension has stopped syncing.
  3. Lets the user send an alert email to the configured recipients.

SETUP REQUIRED before this can actually send mail:
  - Fill in the real email addresses in RECIPIENTS below (Preethy's is filled in
    from context; Swaroop Joy's and Yamini A.S's are placeholders — confirm and
    replace).
  - Add SMTP credentials to .streamlit/secrets.toml (see secrets.toml.example).
    Any SMTP provider works (Gmail app password, Outlook, SES, SendGrid SMTP, etc).
"""

from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import pandas as pd
import streamlit as st

# --- Recipients -------------------------------------------------------------
# TODO: confirm/replace these — only Preethy's address is known for certain.
RECIPIENTS = [
    "preethy@graas.ai",       # Preethy AK (confirmed)
    "swaroop@graas.ai",       # Swaroop Joy — PLACEHOLDER, please confirm
    "yamini@graas.ai",        # Yamini A.S — PLACEHOLDER, please confirm
]

# How many days of lag before we consider a platform "not synced"
SYNC_LAG_DAYS = 1


def check_sync_status(df: pd.DataFrame, as_of: datetime = None) -> pd.DataFrame:
    """Return a per-platform sync status table based on the most recent date
    present in the loaded data."""
    if as_of is None:
        as_of = datetime.now()
    rows = []
    platforms = df["platform"].dropna().unique() if not df.empty else []
    for platform in platforms:
        latest = df.loc[df["platform"] == platform, "date"].max()
        lag_days = (pd.Timestamp(as_of).normalize() - latest.normalize()).days if pd.notna(latest) else None
        status = "Not synced" if (lag_days is None or lag_days > SYNC_LAG_DAYS) else "Synced"
        rows.append({
            "Platform": platform,
            "Latest data date": latest.date() if pd.notna(latest) else None,
            "Days behind": lag_days,
            "Status": status,
        })
    return pd.DataFrame(rows)


def send_sync_alert(status_df: pd.DataFrame, recipients=None) -> tuple[bool, str]:
    """Send an email alert listing which platforms appear out of sync.
    Returns (success, message)."""
    recipients = recipients or RECIPIENTS

    try:
        smtp_cfg = st.secrets["smtp"]
    except Exception:
        return False, (
            "No SMTP configuration found in st.secrets['smtp']. Add host, port, "
            "username, password, and sender to .streamlit/secrets.toml (see "
            "secrets.toml.example) before this can send mail."
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
