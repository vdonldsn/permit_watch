"""
permit_watch.py

Pulls newly issued building permits from Metro Nashville's Socrata open data
portal and filters down to permit types worth chasing for temp fencing leads.

Data source: data.nashville.gov, dataset "Building Permits Issued" (Socrata ID 3h5w-q8b7)
Confirmed fields: permit, permit_type_description, permit_subtype_description,
date_entered, date_issued, const_cost, address, city, state, zip, contact
(contractor name), purpose, case_status.

IMPORTANT: I confirmed these field names from a live sample, but I have not seen the
full list of permit_type_description or case_status values. Before relying on this,
run once with no filters and print the distinct values you get back (see
inspect_field_values() below) so you can tune RELEVANT_PERMIT_TYPES and MIN_CONST_COST
to what actually shows up.
"""

import os
import json
import requests
from datetime import datetime, timedelta

DATASET_ID = "3h5w-q8b7"
BASE_URL = f"https://data.nashville.gov/resource/{DATASET_ID}.json"
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")  # optional, recommended for regular scheduled use

# Permit types worth chasing for temp fencing — new construction and big rehabs need a
# fenced perimeter; small trade permits (electrical/plumbing only) never do.
# CONFIRM AND ADJUST these against real values from inspect_field_values() below.
RELEVANT_PERMIT_TYPES = [
    "Building Commercial - New",
    "Building Commercial - Addition",
    "Building Commercial - Rehab",
    "Building Residential - New",
]

MIN_CONST_COST = 150000  # filters out small residential remodels not worth chasing — adjust to taste
LOOKBACK_DAYS = 3        # how far back to check each run; overlap is fine, dedupe handles repeats

SEEN_FILE = "seen_permits.json"  # local-only dedupe for testing — see app.py for the production version


def _headers():
    headers = {
        # Some endpoints silently reject the default "python-requests/x.x" UA —
        # a real-looking one avoids that without doing anything shady.
        "User-Agent": "Mozilla/5.0 (compatible; permit-watch/1.0; +https://data.nashville.gov)"
    }
    if APP_TOKEN:
        headers["X-App-Token"] = APP_TOKEN
    return headers


def inspect_field_values(field="permit_type_description", days=30):
    """Run this once by hand to see what values actually show up, before trusting
    RELEVANT_PERMIT_TYPES above. Example: python -c "from permit_watch import inspect_field_values; inspect_field_values()"
    """
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    params = {
        "$select": f"{field}, count(*)",
        "$where": f"date_issued >= '{since}'",
        "$group": field,
        "$order": "count(*) DESC",
        "$limit": 50,
    }
    resp = requests.get(BASE_URL, params=params, headers=_headers(), timeout=30)
    resp.raise_for_status()
    for row in resp.json():
        print(row)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def fetch_new_permits():
    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00")

    type_filter = " OR ".join(f"permit_type_description = '{t}'" for t in RELEVANT_PERMIT_TYPES)
    where_clause = f"date_issued >= '{since}' AND ({type_filter}) AND const_cost >= {MIN_CONST_COST}"

    params = {
        "$where": where_clause,
        "$order": "date_issued DESC",
        "$limit": 200,
    }

    resp = requests.get(BASE_URL, params=params, headers=_headers(), timeout=30)
    resp.raise_for_status()
    try:
        return resp.json()
    except requests.exceptions.JSONDecodeError:
        # Surface what actually came back instead of a bare, useless JSONDecodeError.
        raise RuntimeError(
            f"Socrata returned a non-JSON response. Status: {resp.status_code}. "
            f"First 500 chars of body: {resp.text[:500]!r}"
        )


def main():
    seen = load_seen()
    permits = fetch_new_permits()

    new_leads = [p for p in permits if p.get("permit") not in seen]

    for p in new_leads:
        print(
            f"[{p.get('date_issued', '')[:10]}] {p.get('permit_type_description')} "
            f"- {p.get('address')}, {p.get('city')} - ${p.get('const_cost', '?')} "
            f"- Contractor: {p.get('contact', 'Unknown')}"
        )
        seen.add(p.get("permit"))

    save_seen(seen)
    return new_leads


if __name__ == "__main__":
    leads = main()
    print(f"\n{len(leads)} new lead(s) found.")
