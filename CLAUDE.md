# CLAUDE.md — Alert Analyzer Project Context

Read this at the start of every session to resume instantly.

## What this project is

OREF (Israeli Home Front Command) alert history analyzer dashboard.
- **Live:** https://web-production-9c22d.up.railway.app
- **GitHub:** https://github.com/avishaynaim/alert-analyzer
- **Local path:** `/home/claude-user/alert/`

## Current state (as of 2026-03-28)

- ✅ Fully deployed on Railway
- ✅ PostgreSQL database with **215,402 alert records**
- ✅ Auto-syncs every 6h from GitHub mirror (no geo-block)
- ✅ Dashboard working: hour chart, area chart, filters, stats

## Critical facts — do not re-learn

### Data source
OREF's own API (`oref.org.il`) is **geo-blocked from Railway** (non-Israeli IPs get 403).
**Always use the GitHub mirror:** `oref-alerts/oref-alerts.github.io` → `events.js`
This file is updated daily, freely accessible from anywhere.

### Why 1 gunicorn worker
APScheduler runs inside the Flask process. Multiple workers = duplicate schedulers = DB conflicts. Always keep `--workers 1` in Procfile.

### psycopg3, not psycopg2
Railway's environment doesn't have `libpq.so.5`. Use `psycopg[binary]` (v3) which bundles it.

### Batch inserts
217k records must be inserted in chunks (5000 rows via `executemany`), not row-by-row. Row-by-row causes gunicorn worker timeouts.

## Railway IDs

```
Railway token:    [set in env / Railway dashboard]
Project ID:       f3c1390f-d94f-4cd2-b2c6-35a1e5869e54
Web service ID:   9c5a7034-c0d3-4903-9f46-b74538521837
Postgres svc ID:  8e8870be-c87b-42e9-b45a-d5d3bbd0105f
Environment ID:   704fa7c4-2674-48fc-a9ac-1f6bcaec8e7c
```

## GitHub

```
Repo:   https://github.com/avishaynaim/alert-analyzer
User:   avishaynaim
```

## Deploy flow (one session)

```bash
# Edit code
git add -A && git commit -m "..." && git push origin main

# Trigger Railway redeploy
RAILWAY_TOKEN="0cabd0b7-2009-413f-8d99-e3069a9b1552"
SERVICE_ID="9c5a7034-c0d3-4903-9f46-b74538521837"
ENV_ID="704fa7c4-2674-48fc-a9ac-1f6bcaec8e7c"
curl -s -X POST https://backboard.railway.app/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { serviceInstanceDeployV2(serviceId: \\\"$SERVICE_ID\\\", environmentId: \\\"$ENV_ID\\\") }\"}"
```

## Check status

```bash
curl -s https://web-production-9c22d.up.railway.app/api/status | python3 -m json.tool
```

## Trigger manual sync

```bash
curl -s -X POST https://web-production-9c22d.up.railway.app/api/sync \
  -H "Content-Type: application/json" -d "[]"
```

## Next features requested by user

- Alert type filter (rockets / drones / warnings)
- Timeline chart (alerts over days)
- Heatmap (hour × day-of-week)
- Map view with Leaflet.js
- Telegram alerts for new events
