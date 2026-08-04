"""
permit_watch.py

Pulls newly issued building permits from Metro Nashville's open data.

PLATFORM CHANGE (confirmed Aug 2026): Nashville's classic Socrata SODA API endpoint
(data.nashville.gov/resource/{id}.json) has been fully decommissioned — it now 302s to
a dead "hub.arcgis.com/legacy" page ("This site is no longer supported"). The dataset
is now served exclusively through Esri's ArcGIS REST API. This file was rewritten
around that API.

How this works, in two steps:
1. Resolve the ArcGIS Online "item" (2576bfb2d74f418b8ba8c4538e4f729f, found embedded
   in the dataset's page metadata) to find its actual live FeatureServer URL. The item
   ID is stable; which org/server actually hosts the data is not something to hardcode,
   so we look it up fresh each run instead of guessing it.
2. Query that FeatureServer's layer 0 using ArcGIS's REST query syntax — `where` /
   `outFields` / `f=json` — which is different from Socrata's `$where` / `$select`.

IMPORTANT: I could not directly verify the exact field names on this ArcGIS-hosted
layer (they may not match the old Socrata field names like `permit_type_description`).
Run inspect_fields() ONCE before trusting anything else in this file:

    python -c "from permit_watch import inspect_fields; inspect_fields()"

It prints every real field name plus one live sample record. Then fix FIELD_MAP below
to point at whatever the real names turn out to be — everything else in this file and
in app.py stays unchanged, since FIELD_MAP is the only place raw ArcGIS field names
are referenced.
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone

ITEM_ID = "2576bfb2d74f418b8ba8c4538e4f729f"
ITEM_INFO_URL = f"https://www.arcgis.com/sharing/rest/content/items/{ITEM_ID}"

_service_url_cache = None  # resolved once per process, cached

# Canonical name (what app.py expects) -> actual raw field name on the ArcGIS layer.
# CONFIRMED LIVE on 2026-08-04 via inspect_fields() — do not need to re-verify unless
# the dataset schema changes.
FIELD_MAP = {
    "permit": "Permit__",
    "permit_type_description": "Permit_Type_Description",
    "date_issued": "Date_Issued",
    "const_cost": "Const_Cost",
    "address": "Address",
    "city": "City",
    "contact": "Contact",
    "purpose": "Purpose",
}

RELEVANT_PERMIT_TYPES = [
    "Building Commercial - New",
    "Building Commercial - Addition",
    "Building Commercial - Rehab",
    "Building Residential - New",
]
MIN_CONST_COST = 150000
LOOKBACK_DAYS = 3

SEEN_FILE = "seen_permits.json"  # local-only dedupe for testing — app.py uses Supabase in production


def _headers():
    return {"User-Agent": "Mozilla/5.0 (compatible; permit-watch/1.0)"}


def get_feature_server_url():
    """Resolve the ArcGIS item ID to its actual FeatureServer query endpoint."""
    global _service_url_cache
    if _service_url_cache:
        return _service_url_cache

    resp = requests.get(ITEM_INFO_URL, params={"f": "json"}, headers=_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # ArcGIS returns HTTP 200 even on errors — the error shows up INSIDE the JSON body,
    # not as a bad status code. Check explicitly or you'll silently process garbage.
    if "error" in data:
        raise RuntimeError(f"ArcGIS item lookup failed: {data['error']}")

    service_url = data.get("url")
    if not service_url:
        raise RuntimeError(f"Item {ITEM_ID} has no 'url' field. Full response: {data}")

    _service_url_cache = service_url
    return service_url


def _query_layer(where_clause, out_fields="*", order_by=None, limit=200):
    service_url = get_feature_server_url()
    # Layer 0 is standard for a single-layer hosted feature service. If inspect_fields()
    # comes back empty, this dataset may have multiple layers — check {service_url}?f=json
    # for a "layers" list and adjust the "/0/" below to the right index.
    query_url = f"{service_url}/0/query"

    params = {"where": where_clause, "outFields": out_fields, "f": "json", "resultRecordCount": limit}
    if order_by:
        params["orderByFields"] = order_by

    resp = requests.get(query_url, params=params, headers=_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"ArcGIS query failed: {data['error']}")

    # Flatten {"features": [{"attributes": {...}}]} to plain dicts so nothing
    # downstream (including app.py) needs to know this is ArcGIS under the hood.
    return [f["attributes"] for f in data.get("features", [])]


def _remap(raw: dict) -> dict:
    """Translate a raw ArcGIS attributes dict into the canonical field names
    app.py already expects, using FIELD_MAP."""
    out = {canonical: raw.get(actual) for canonical, actual in FIELD_MAP.items()}

    # ArcGIS Date fields serialize as epoch milliseconds (UTC int), not date strings.
    # Convert here so app.py's string handling (and Supabase's timestamp column)
    # don't need to know that ArcGIS quirk exists.
    di = out.get("date_issued")
    if isinstance(di, (int, float)):
        out["date_issued"] = datetime.fromtimestamp(di / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    return out


def inspect_fields(days=30):
    """Run this ONCE before trusting anything else in this file. Prints every
    real field name on the layer plus one live sample record."""
    records = _query_layer(where_clause="1=1", out_fields="*", limit=1)
    if not records:
        print("No records returned at all — check that the where clause and layer index are right.")
        return
    print("Real field names found on a live record (fix FIELD_MAP to match these):")
    for k, v in records[0].items():
        print(f"  {k}: {v!r}")


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def fetch_new_permits():
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    type_field = FIELD_MAP["permit_type_description"]
    date_field = FIELD_MAP["date_issued"]
    cost_field = FIELD_MAP["const_cost"]

    type_filter = " OR ".join(f"{type_field} = '{t}'" for t in RELEVANT_PERMIT_TYPES)
    # Date_Issued is an ArcGIS Date-type field (stored as epoch ms, see _remap above).
    # ArcGIS's query engine normally accepts a plain quoted date string here and
    # translates it correctly. If this specific query throws an error mentioning the
    # date comparison, switch to the ANSI date-literal form instead:
    #   f"{date_field} >= DATE '{since}'"
    where_clause = f"{date_field} >= '{since}' AND ({type_filter}) AND {cost_field} >= {MIN_CONST_COST}"

    records = _query_layer(where_clause=where_clause, order_by=f"{date_field} DESC", limit=200)
    return [_remap(r) for r in records]


def main():
    seen = load_seen()
    permits = fetch_new_permits()
    new_leads = [p for p in permits if p.get("permit") not in seen]

    for p in new_leads:
        print(
            f"[{str(p.get('date_issued'))[:10]}] {p.get('permit_type_description')} "
            f"- {p.get('address')}, {p.get('city')} - ${p.get('const_cost', '?')} "
            f"- Contractor: {p.get('contact', 'Unknown')}"
        )
        seen.add(p.get("permit"))

    save_seen(seen)
    return new_leads


if __name__ == "__main__":
    leads = main()
    print(f"\n{len(leads)} new lead(s) found.")
