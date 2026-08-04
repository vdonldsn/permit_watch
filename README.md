# Metro Nashville Permit Lead Tracker

Pulls newly issued building permits from data.nashville.gov and flags the ones worth
calling about for temp fencing work.

## 1. Before you trust the filters — REQUIRED, not optional

The data source changed platforms (see the top of `permit_watch.py` for the full story
— Nashville's old Socrata API endpoint is dead, this now runs on ArcGIS's REST API
instead). Run this once by hand before doing anything else:

```bash
pip install requests
python -c "from permit_watch import inspect_fields; inspect_fields()"
```

It prints every real field name on the live layer. Compare that output against
`FIELD_MAP` near the top of `permit_watch.py` and fix any names that don't match —
if you skip this, `fetch_new_permits()` won't error, it'll just silently return
nothing, which is a much easier problem to miss.

## 2. Supabase table

Run this in the Supabase SQL editor before deploying `app.py`:

```sql
create table permit_leads (
  id bigserial primary key,
  permit_number text unique not null,
  permit_type text,
  address text,
  city text,
  const_cost numeric,
  contractor text,
  date_issued timestamp,
  purpose text,
  created_at timestamp default now()
);
```

## 3. Environment variables (Railway → Variables tab)

- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `ALERT_EMAIL_TO` — optional, email alerts are
  silently skipped if these aren't set. `GMAIL_APP_PASSWORD` is NOT your normal Gmail
  password — generate one at myaccount.google.com/apppasswords (requires 2-Step
  Verification on the account, which you should have anyway). Free, takes a minute.

(There's no more `SOCRATA_APP_TOKEN` — that was for the old Socrata API, which no
longer exists for this dataset. Not applicable anymore.)

## 4. Deploy the FastAPI service to Railway

Same as your other projects: push this folder to a repo, connect it in Railway,
it picks up `requirements.txt` and runs `uvicorn app:app --host 0.0.0.0 --port $PORT`.
Test it manually first by hitting `https://<your-app>.up.railway.app/permits/check`.

## 5. Put it on a schedule — two options

**Option A: Railway Cron Job (stays inside your existing infra)**
In the same Railway project, add a second service → choose "Cron Job" as the type →
point it at the same repo → set the command to:
```bash
curl -f https://<your-app>.up.railway.app/permits/check
```
and give it a schedule, e.g. `0 12 * * *` (noon UTC / 6am/7am Central depending on DST)
for once a day, or `0 12,18 * * *` for twice a day.

**Option B: GitHub Actions (free, no extra Railway service)**
If you'd rather not spend a Railway service slot on something that runs once a day,
a scheduled GitHub Actions workflow calling the same endpoint works just as well:

```yaml
# .github/workflows/permit-check.yml
name: Permit Check
on:
  schedule:
    - cron: "0 12 * * *"
  workflow_dispatch: {}
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: curl -f https://<your-app>.up.railway.app/permits/check
```

`workflow_dispatch` lets you also trigger it manually from the GitHub Actions tab
whenever you want a check right now instead of waiting for the schedule.

## 6. Next iteration (not built yet)

"Building Permit Applications" is an earlier-stage dataset (pending, not yet
issued) hosted on Metro's ArcGIS Hub rather than Socrata — different API shape,
but it would give you a lead before construction actually starts instead of when
it's already underway. Worth a follow-up build once this one's proven out.
