import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
from dateutil import parser as dateparser
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import psycopg
from psycopg.rows import dict_row
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# GitHub mirror — updated daily, no geo-block
GITHUB_EVENTS_URL = (
    "https://raw.githubusercontent.com/"
    "oref-alerts/oref-alerts.github.io/main/events.js"
)

EVENT_TYPES = [
    "ירי רקטות וטילים",
    "האירוע הסתיים",
    "בדקות הקרובות צפויות להתקבל התרעות באזורך",
    "ניתן לצאת מהמרחב המוגן אך יש להישאר בקרבתו",
    "ירי רקטות וטילים - האירוע הסתיים",
    "חדירת כלי טיס עוין",
    "חדירת כלי טיס עוין - האירוע הסתיים",
    "סיום שהייה בסמיכות למרחב המוגן",
    "יש לשהות בסמיכות למרחב המוגן",
]

CATEGORY_MAP = {
    "r": 1,   # rockets
    "d": 6,   # drone / UAV
    "w": 13,  # warning
}


# ── GitHub data parser ────────────────────────────────────────────────────

def fetch_from_github():
    """
    Fetches events.js from the oref-alerts GitHub mirror and converts
    EVENTS_BY_AREA → list of alert dicts matching OREF API format.
    """
    log.info("Fetching from GitHub mirror...")
    resp = requests.get(GITHUB_EVENTS_URL, timeout=60)
    resp.raise_for_status()
    content = resp.text

    # Parse DATA_UPDATED
    updated_match = re.search(r'const DATA_UPDATED = "([^"]+)"', content)
    data_updated = updated_match.group(1) if updated_match else None
    log.info(f"GitHub data updated: {data_updated}")

    # Extract EVENTS_BY_AREA JSON blob
    m = re.search(r'const EVENTS_BY_AREA = (\{.+\});?\s*$', content, re.DOTALL)
    if not m:
        raise ValueError("Could not find EVENTS_BY_AREA in events.js")

    events_by_area = json.loads(m.group(1))

    alerts = []
    for area_key, area_data in events_by_area.items():
        area_name = area_data.get("name", area_key)
        for evt in area_data.get("events", []):
            date_str = evt.get("d", "")
            time_str = evt.get("s", "00:00")
            si = evt.get("si", 0)
            k = evt.get("k", "r")

            alert_date = f"{date_str} {time_str}:00"
            title = EVENT_TYPES[si] if si < len(EVENT_TYPES) else EVENT_TYPES[0]
            category = CATEGORY_MAP.get(k, 1)

            alerts.append({
                "alertDate": alert_date,
                "title": title,
                "data": area_name,
                "category": category,
                "category_desc": title,
            })

    log.info(f"Parsed {len(alerts):,} alerts from GitHub mirror")
    return alerts, data_updated


# ── Database ──────────────────────────────────────────────────────────────

def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        log.warning("No DATABASE_URL — running without persistence")
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id            SERIAL PRIMARY KEY,
                    alert_date    TIMESTAMP,
                    title         TEXT,
                    area          TEXT,
                    category      INTEGER,
                    category_desc TEXT,
                    hour          SMALLINT,
                    date_only     DATE,
                    raw           JSONB,
                    UNIQUE (alert_date, area)
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_date     ON alerts(alert_date);
                CREATE INDEX IF NOT EXISTS idx_alerts_area     ON alerts(area);
                CREATE INDEX IF NOT EXISTS idx_alerts_hour     ON alerts(hour);
                CREATE INDEX IF NOT EXISTS idx_alerts_dateonly ON alerts(date_only);

                CREATE TABLE IF NOT EXISTS sync_log (
                    id             SERIAL PRIMARY KEY,
                    synced_at      TIMESTAMP DEFAULT NOW(),
                    source         TEXT,
                    records_added  INTEGER,
                    total_records  INTEGER,
                    status         TEXT
                );
            """)
        conn.commit()
    log.info("Database ready")


def save_alerts(raw_list, source="github"):
    if not DATABASE_URL or not raw_list:
        return 0, 0

    rows = []
    for alert in raw_list:
        raw_date = alert.get("alertDate", "")
        try:
            dt = dateparser.parse(raw_date)
            hour = dt.hour
            date_only = dt.date()
            ts = dt
        except Exception:
            ts = hour = date_only = None

        rows.append((
            ts,
            alert.get("title", ""),
            (alert.get("data", "") or "").strip(),
            alert.get("category"),
            alert.get("category_desc", ""),
            hour,
            date_only,
            json.dumps(alert, ensure_ascii=False),
        ))

    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # Count before
            cur.execute("SELECT COUNT(*) as cnt FROM alerts")
            before = cur.fetchone()["cnt"]

            # Batch insert in chunks of 5000
            CHUNK = 5000
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i:i + CHUNK]
                cur.executemany("""
                    INSERT INTO alerts
                        (alert_date, title, area, category, category_desc, hour, date_only, raw)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (alert_date, area) DO NOTHING
                """, chunk)
                log.info(f"Inserted chunk {i//CHUNK + 1}/{(len(rows)-1)//CHUNK + 1}")

            cur.execute("SELECT COUNT(*) as cnt FROM alerts")
            total = cur.fetchone()["cnt"]
            added = total - before

            cur.execute("""
                INSERT INTO sync_log (source, records_added, total_records, status)
                VALUES (%s, %s, %s, 'success')
            """, (source, added, total))
        conn.commit()

    log.info(f"Saved: +{added:,} new, {total:,} total")
    return added, total


def log_sync_error(source, error_msg):
    if not DATABASE_URL:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sync_log (source, records_added, total_records, status) "
                    "VALUES (%s, 0, 0, %s)",
                    (source, f"failed: {error_msg}")
                )
            conn.commit()
    except Exception:
        pass


# ── Auto sync job ─────────────────────────────────────────────────────────

def auto_sync():
    log.info("=== Auto-sync starting ===")
    try:
        alerts, updated = fetch_from_github()
        added, total = save_alerts(alerts, source=f"github:{updated or 'unknown'}")
        log.info(f"=== Auto-sync done: +{added:,} new, {total:,} total ===")
    except Exception as e:
        log.error(f"Auto-sync failed: {e}")
        log_sync_error("github-scheduler", str(e))


# ── DB query helpers ──────────────────────────────────────────────────────

def query_alerts(from_date=None, to_date=None, areas=None):
    if not DATABASE_URL:
        return []

    conditions, params = [], []
    if from_date:
        conditions.append("date_only >= %s"); params.append(from_date)
    if to_date:
        conditions.append("date_only <= %s"); params.append(to_date)
    if areas:
        conditions.append("area = ANY(%s)"); params.append(areas)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT alert_date, title, area, category, category_desc,
               hour, date_only::text
        FROM alerts {where}
        ORDER BY alert_date DESC
        LIMIT 1000
    """
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [{
        "alertDate":     r["alert_date"].isoformat() if r["alert_date"] else None,
        "title":         r["title"],
        "data":          r["area"],
        "category":      r["category"],
        "category_desc": r["category_desc"],
        "hour":          r["hour"],
        "date":          r["date_only"],
        "timestamp":     r["alert_date"].isoformat() if r["alert_date"] else None,
    } for r in rows]


def get_db_stats():
    if not DATABASE_URL:
        return {}
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT COUNT(*) as total FROM alerts")
            total = cur.fetchone()["total"]
            cur.execute("SELECT MIN(alert_date) as earliest, MAX(alert_date) as latest FROM alerts")
            row = cur.fetchone()
            cur.execute("""
                SELECT synced_at, source, records_added, total_records, status
                FROM sync_log ORDER BY synced_at DESC LIMIT 1
            """)
            last_sync = cur.fetchone()
    return {
        "total":    total,
        "earliest": row["earliest"].isoformat() if row["earliest"] else None,
        "latest":   row["latest"].isoformat() if row["latest"] else None,
        "last_sync": last_sync if last_sync else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


def build_where(from_date, to_date, areas):
    conditions, params = [], []
    if from_date:
        conditions.append("date_only >= %s"); params.append(from_date)
    if to_date:
        conditions.append("date_only <= %s"); params.append(to_date)
    if areas:
        conditions.append("area = ANY(%s)"); params.append(areas)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def parse_preset(preset, from_date, to_date):
    now = datetime.now()
    if preset == "24h":
        return (now - timedelta(hours=24)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
    if preset == "day":
        d = now.strftime("%Y-%m-%d"); return d, d
    if preset == "week":
        return (now - timedelta(days=7)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
    return from_date, to_date


@app.route("/api/analytics")
def get_analytics():
    """Returns pre-aggregated data — never raw rows. Fast and browser-safe."""
    if not DATABASE_URL:
        return jsonify({"error": "No database configured"}), 503

    from_date   = request.args.get("from_date")
    to_date     = request.args.get("to_date")
    preset      = request.args.get("preset")
    areas_param = request.args.get("areas")

    from_date, to_date = parse_preset(preset, from_date, to_date)
    areas = [a.strip() for a in areas_param.split(",")] if areas_param else None
    where, params = build_where(from_date, to_date, areas)

    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:

            # Total count
            cur.execute(f"SELECT COUNT(*) as cnt FROM alerts {where}", params)
            total = cur.fetchone()["cnt"]

            # Hour distribution (0–23)
            cur.execute(f"""
                SELECT hour, COUNT(*) as cnt
                FROM alerts {where}
                  {"AND" if where else "WHERE"} hour IS NOT NULL
                GROUP BY hour ORDER BY hour
            """, params)
            hour_rows = cur.fetchall()
            hour_buckets = [0] * 24
            for r in hour_rows:
                if 0 <= r["hour"] <= 23:
                    hour_buckets[r["hour"]] = r["cnt"]

            # All areas ranked by count
            cur.execute(f"""
                SELECT area, COUNT(*) as cnt
                FROM alerts {where}
                  {"AND" if where else "WHERE"} area IS NOT NULL AND area != ''
                GROUP BY area ORDER BY cnt DESC
            """, params)
            top_areas = [{"area": r["area"], "count": r["cnt"]} for r in cur.fetchall()]

            # Date range
            cur.execute(f"""
                SELECT MIN(date_only)::text as earliest, MAX(date_only)::text as latest
                FROM alerts {where}
            """, params)
            dates = cur.fetchone()

            # Peak hour
            peak_hour = hour_buckets.index(max(hour_buckets)) if total else None

    return jsonify({
        "total":        total,
        "peak_hour":    peak_hour,
        "hour_buckets": hour_buckets,
        "top_areas":    top_areas,
        "earliest":     dates["earliest"],
        "latest":       dates["latest"],
    })


@app.route("/api/alerts")
def get_alerts():
    # Keep for compatibility but cap at 1000 rows
    if not DATABASE_URL:
        return jsonify({"error": "No database configured"}), 503

    from_date   = request.args.get("from_date")
    to_date     = request.args.get("to_date")
    preset      = request.args.get("preset")
    areas_param = request.args.get("areas")

    from_date, to_date = parse_preset(preset, from_date, to_date)
    areas = [a.strip() for a in areas_param.split(",")] if areas_param else None

    return jsonify(query_alerts(from_date, to_date, areas))


@app.route("/api/areas")
def get_areas():
    if not DATABASE_URL:
        return jsonify([])
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT DISTINCT area FROM alerts "
                "WHERE area IS NOT NULL AND area != '' ORDER BY area"
            )
            areas = [r["area"] for r in cur.fetchall()]
    return jsonify(areas)


@app.route("/api/sync", methods=["POST"])
def sync():
    """
    POST with JSON body array  → save that data directly.
    POST with empty / [] body  → fetch from GitHub mirror and save.
    """
    body = request.get_json(force=True, silent=True)

    # Browser-pasted data
    if body and isinstance(body, list) and len(body) > 0:
        added, total = save_alerts(body, source="browser-paste")
        return jsonify({"ok": True, "added": added, "total": total})

    # Server-side fetch from GitHub
    try:
        alerts, updated = fetch_from_github()
        added, total = save_alerts(alerts, source=f"github:{updated or 'unknown'}")
        return jsonify({"ok": True, "added": added, "total": total,
                        "source": "github", "data_updated": updated})
    except Exception as e:
        log.error(f"Sync error: {e}")
        log_sync_error("api-trigger", str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/status")
def status():
    return jsonify({
        "db":     get_db_stats(),
        "has_db": bool(DATABASE_URL),
        "source": "github-mirror",
    })


# ── Startup ───────────────────────────────────────────────────────────────

init_db()

if DATABASE_URL:
    scheduler = BackgroundScheduler()
    # First sync 10 seconds after startup, then every 6 hours
    scheduler.add_job(auto_sync, "interval", hours=6, id="github_sync",
                      next_run_time=datetime.now() + timedelta(seconds=10))
    scheduler.start()
    log.info("Scheduler started — GitHub sync in 10s, then every 6 hours")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
