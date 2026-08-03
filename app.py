"""
app.py

FastAPI wrapper around permit_watch.py, meant to run as a Railway service.
A Railway Cron Job (or GitHub Actions, see README) hits GET /permits/check on a
schedule. New leads get written to Supabase and trigger an email alert via Gmail SMTP —
free, no third-party service or extra account needed beyond the Gmail you already have.

Dedupe here is done against Supabase directly (is this permit_number already in the
table?), NOT the local seen_permits.json from permit_watch.py — Railway's filesystem
is ephemeral between deploys, so a local JSON file won't reliably persist. The database
is the source of truth in production.

Env vars required:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  SOCRATA_APP_TOKEN                        (optional but recommended)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_EMAIL_TO   (optional — email alerts are
                                                        skipped if these aren't set)

GMAIL_APP_PASSWORD is NOT your normal Gmail password. Generate one at
myaccount.google.com/apppasswords (requires 2-Step Verification to be turned on,
which you should have anyway). Takes about a minute, costs nothing.
"""

import os
import smtplib
from email.mime.text import MIMEText

from fastapi import FastAPI
from supabase import create_client, Client

from permit_watch import fetch_new_permits

app = FastAPI()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO")


def send_email_alert(leads: list):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ALERT_EMAIL_TO) or not leads:
        return

    lines = [
        f"- {p.get('permit_type_description')} at {p.get('address')}, {p.get('city')} "
        f"(${p.get('const_cost', '?')}) — contractor: {p.get('contact', 'Unknown')} "
        f"— issued {p.get('date_issued', '')[:10]}"
        for p in leads
    ]
    body = f"{len(leads)} new permit lead(s):\n\n" + "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"Permit Lead Watch: {len(leads)} new lead(s)"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_EMAIL_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [ALERT_EMAIL_TO], msg.as_string())


@app.get("/permits/check")
def check_permits():
    permits = fetch_new_permits()
    new_leads = []

    for p in permits:
        permit_no = p.get("permit")
        if not permit_no:
            continue

        existing = (
            supabase.table("permit_leads")
            .select("permit_number")
            .eq("permit_number", permit_no)
            .execute()
        )
        if existing.data:
            continue  # already logged on a previous run — skip

        supabase.table("permit_leads").insert(
            {
                "permit_number": permit_no,
                "permit_type": p.get("permit_type_description"),
                "address": p.get("address"),
                "city": p.get("city"),
                "const_cost": p.get("const_cost"),
                "contractor": p.get("contact"),
                "date_issued": p.get("date_issued"),
                "purpose": p.get("purpose"),
            }
        ).execute()

        new_leads.append(p)

    send_email_alert(new_leads)  # one email per run listing everything new, not one per lead

    return {"new_leads": len(new_leads), "leads": new_leads}


@app.get("/health")
def health():
    return {"status": "ok"}
