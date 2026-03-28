# 🚨 מנתח התרעות — OREF Alert Analyzer

Real-time dashboard for analyzing Israeli Home Front Command (פיקוד העורף) alert history.

**Live:** https://web-production-9c22d.up.railway.app
**GitHub:** https://github.com/avishaynaim/alert-analyzer

---

## What it does

- Shows alert distribution across the day (00:00–23:59) as a color-coded bar chart
- Filters by: All time / Last 24h / Today / Last week / Custom date range
- Filter by area or city (multi-select)
- Global start date setting (default: all time)
- Top 15 most-alerted areas chart
- Stats: total alerts, peak hour, top area, date range
- **215,000+ historical records** auto-synced into PostgreSQL

---

## Architecture

```
Browser → Flask (Railway) → PostgreSQL (Railway)
                ↑
         GitHub mirror (oref-alerts/oref-alerts.github.io)
         fetches events.js every 6 hours automatically
```

### Why GitHub mirror?
OREF's API (`oref.org.il`) geo-blocks non-Israeli IPs. Railway servers are not in Israel.
The GitHub repo [oref-alerts/oref-alerts.github.io](https://github.com/oref-alerts/oref-alerts.github.io) mirrors the full OREF dataset daily as `events.js` — publicly accessible from anywhere.

---

## Stack

| Component | Technology |
|---|---|
| Backend | Python 3 / Flask 3 |
| Database | PostgreSQL 16 (Railway) |
| Frontend | Vanilla JS + Chart.js |
| Hosting | Railway |
| Data source | GitHub mirror (oref-alerts) |
| Scheduler | APScheduler — syncs every 6h |

---

## Railway deployment

| Resource | ID |
|---|---|
| Project | `f3c1390f-d94f-4cd2-b2c6-35a1e5869e54` |
| Web service | `9c5a7034-c0d3-4903-9f46-b74538521837` |
| Postgres service | `8e8870be-c87b-42e9-b45a-d5d3bbd0105f` |
| Environment (production) | `704fa7c4-2674-48fc-a9ac-1f6bcaec8e7c` |
| Public URL | `web-production-9c22d.up.railway.app` |

**DATABASE_URL** (set as Railway env var on web service):
`postgresql://alertuser:Str0ngPass2024!@postgres.railway.internal:5432/alertdb`

---

## Running locally

```bash
git clone https://github.com/avishaynaim/alert-analyzer
cd alert-analyzer
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."   # optional — runs without DB (no data)
python3 app.py
# open http://localhost:5050
```

### Manual one-time sync from Israel

```bash
python3 sync.py
```

Uses only Python built-ins, no pip install needed. Must be run from an Israeli IP.

---

## File structure

```
alert-analyzer/
├── app.py              # Flask backend + scheduler + DB logic
├── requirements.txt    # Python dependencies
├── Procfile            # gunicorn 1 worker, 300s timeout
├── sync.py             # Standalone sync tool (run from Israel)
├── static/
│   ├── index.html      # Dashboard HTML (RTL Hebrew)
│   ├── style.css       # Dark theme styles
│   └── app.js          # Frontend logic + charts
└── README.md
```

---

## Data format

The GitHub mirror stores data as `EVENTS_BY_AREA`:
```js
const EVENTS_BY_AREA = {
  "area-key": {
    "name": "שם האזור",
    "events": [
      { "d": "2024-10-07", "s": "06:29", "k": "r", "si": 0 }
    ]
  }
}
```

- `d` = date (YYYY-MM-DD)
- `s` = time (HH:MM)
- `k` = category: `r`=rockets, `d`=drone/UAV, `w`=warning
- `si` = event type index into EVENT_TYPES array

Converted to OREF-compatible format before storing in DB.

---

## API endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /api/alerts` | Query alerts (params: `from_date`, `to_date`, `preset`, `areas`) |
| `GET /api/areas` | List all distinct areas |
| `POST /api/sync` | Trigger GitHub sync (empty body) or load pasted data (JSON array body) |
| `GET /api/status` | DB stats + last sync info |

---

## ✅ Done

- [x] Flask backend with PostgreSQL persistence
- [x] GitHub mirror as geo-block-free data source
- [x] Auto-sync every 6 hours via APScheduler
- [x] Dark themed RTL Hebrew dashboard
- [x] Hour distribution chart (00–23) with color intensity
- [x] Top 15 areas horizontal bar chart
- [x] Date range filters (all / 24h / today / week / custom)
- [x] Multi-select area/city filter
- [x] Global start date setting
- [x] Stats cards (total, peak hour, top area, date range)
- [x] 215k+ records loaded in DB
- [x] Deployed and live on Railway

## 🔜 Next steps / ideas

- [ ] Alert type filter (rockets / drones / warnings separately)
- [ ] Timeline chart — alerts over days/weeks/months
- [ ] Heatmap view (hour × day-of-week)
- [ ] Export to CSV
- [ ] Telegram notifications when new alerts come in
- [ ] Add alert count per region on a map (Leaflet.js)
- [ ] "Danger score" — weighted score per area by time of day
- [ ] Compare two date ranges side by side
