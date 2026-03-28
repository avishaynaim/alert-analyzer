import os
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
OREF_BASE = "https://www.oref.org.il"
OREF_HISTORY_URLS = [
    f"{OREF_BASE}/WarningMessages/History/AlertsHistory.json",
    f"{OREF_BASE}/warningMessages/alert/History/AlertsHistory.json",
]
OREF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.oref.org.il/heb/alerts-history",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# ── Database ────────────────────────────────────────────────────────────

def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        log.warning("No DATABASE_URL set — running without persistence")
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id          SERIAL PRIMARY KEY,
                    alert_date  TIMESTAMP,
                    title       TEXT,
                    area        TEXT,
                    category    INTEGER,
                    category_desc TEXT,
                    hour        SMALLINT,
                    date_only   DATE,
                    raw         JSONB,
                    UNIQUE (alert_date, area)
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_date ON alerts(alert_date);
                CREATE INDEX IF NOT EXISTS idx_alerts_area ON alerts(area);
                CREATE INDEX IF NOT EXISTS idx_alerts_hour ON alerts(hour);
                CREATE INDEX IF NOT EXISTS idx_alerts_dateonly ON alerts(date_only);

                CREATE TABLE IF NOT EXISTS sync_log (
                    id          SERIAL PRIMARY KEY,
                    synced_at   TIMESTAMP DEFAULT NOW(),
                    source      TEXT,
                    records_added INTEGER,
                    total_records INTEGER,
                    status      TEXT
                );
            """)
        conn.commit()
    log.info("Database initialized")


def save_alerts(raw_list, source="oref"):
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

    added = 0
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            for row in rows:
                try:
                    cur.execute("""
                        INSERT INTO alerts (alert_date, title, area, category, category_desc, hour, date_only, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (alert_date, area) DO NOTHING
                    """, row)
                    added += cur.rowcount
                except Exception as e:
                    log.warning(f"Row insert error: {e}")
                    conn.rollback()
                    continue

            cur.execute("SELECT COUNT(*) as cnt FROM alerts")
            total = cur.fetchone()["cnt"]

            cur.execute("""
                INSERT INTO sync_log (source, records_added, total_records, status)
                VALUES (%s, %s, %s, 'success')
            """, (source, added, total))
        conn.commit()

    log.info(f"Sync done: +{added} new records (total {total})")
    return added, total


# ── OREF Fetcher ────────────────────────────────────────────────────────

def fetch_from_oref(from_date=None, to_date=None):
    params = {}
    if from_date:
        params["fromDate"] = from_date
    if to_date:
        params["toDate"] = to_date

    session = requests.Session()
    try:
        session.get(f"{OREF_BASE}/heb/alerts-history", headers=OREF_HEADERS, timeout=15)
    except Exception:
        pass

    for url in OREF_HISTORY_URLS:
        try:
            resp = session.get(url, headers=OREF_HEADERS, params=params, timeout=45)
            if resp.status_code == 403:
                continue
            resp.raise_for_status()
            text = resp.content.decode("utf-8-sig").strip()
            if not text:
                return None, "Empty response from OREF"
            data = json.loads(text)
            return (data if isinstance(data, list) else []), None
        except requests.exceptions.RequestException as e:
            continue
        except json.JSONDecodeError as e:
            return None, f"JSON parse error: {e}"

    return None, "OREF API blocked (geo-restriction) — use browser sync"


def auto_sync():
    """Called by scheduler — tries to pull latest data from OREF."""
    log.info("Auto-sync started")
    data, err = fetch_from_oref()
    if err:
        log.warning(f"Auto-sync failed: {err}")
        if DATABASE_URL:
            with get_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("INSERT INTO sync_log (source, records_added, total_records, status) VALUES ('scheduler', 0, 0, %s)", (f"failed: {err}",))
                conn.commit()
        return
    added, total = save_alerts(data, source="scheduler")
    log.info(f"Auto-sync complete: +{added} new, {total} total")


# ── DB Query Helpers ────────────────────────────────────────────────────

def query_alerts(from_date=None, to_date=None, areas=None):
    if not DATABASE_URL:
        return []

    conditions = []
    params = []

    if from_date:
        conditions.append("date_only >= %s")
        params.append(from_date)
    if to_date:
        conditions.append("date_only <= %s")
        params.append(to_date)
    if areas:
        conditions.append("area = ANY(%s)")
        params.append(areas)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT
            id, alert_date, title, area, category, category_desc,
            hour, date_only::text as date_only
        FROM alerts
        {where}
        ORDER BY alert_date DESC
        LIMIT 500000
    """
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    result = []
    for r in rows:
        result.append({
            "alertDate": r["alert_date"].isoformat() if r["alert_date"] else None,
            "title": r["title"],
            "data": r["area"],
            "category": r["category"],
            "category_desc": r["category_desc"],
            "hour": r["hour"],
            "date": r["date_only"],
            "timestamp": r["alert_date"].isoformat() if r["alert_date"] else None,
        })
    return result


def get_db_stats():
    if not DATABASE_URL:
        return {}
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT COUNT(*) as total FROM alerts")
            total = cur.fetchone()["total"]
            cur.execute("SELECT MIN(alert_date) as earliest, MAX(alert_date) as latest FROM alerts")
            row = cur.fetchone()
            cur.execute("SELECT synced_at, source, records_added, total_records, status FROM sync_log ORDER BY synced_at DESC LIMIT 1")
            last_sync = cur.fetchone()
    return {
        "total": total,
        "earliest": row["earliest"].isoformat() if row["earliest"] else None,
        "latest": row["latest"].isoformat() if row["latest"] else None,
        "last_sync": last_sync if last_sync else None,
    }


# ── Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/alerts")
def get_alerts():
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    preset = request.args.get("preset")
    areas_param = request.args.get("areas")  # comma-separated

    now = datetime.now()
    if preset == "24h":
        from_date = (now - timedelta(hours=24)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")
    elif preset == "day":
        from_date = now.strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")
    elif preset == "week":
        from_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")

    areas = [a.strip() for a in areas_param.split(",")] if areas_param else None

    if not DATABASE_URL:
        return jsonify({"error": "No database configured", "status": "no_db"}), 503

    data = query_alerts(from_date, to_date, areas)
    return jsonify(data)


@app.route("/api/areas")
def get_areas():
    if not DATABASE_URL:
        return jsonify([])
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT DISTINCT area FROM alerts WHERE area IS NOT NULL AND area != '' ORDER BY area")
            areas = [r["area"] for r in cur.fetchall()]
    return jsonify(areas)


@app.route("/api/sync", methods=["POST"])
def sync():
    """
    Accepts JSON body: array of alert objects (from browser-side OREF fetch)
    OR triggers a direct OREF fetch if body is empty.
    """
    body = request.get_json(force=True, silent=True)

    if body and isinstance(body, list) and len(body) > 0:
        # Browser pushed data
        added, total = save_alerts(body, source="browser")
        return jsonify({"ok": True, "added": added, "total": total})

    # Try direct fetch from server
    data, err = fetch_from_oref()
    if err:
        return jsonify({"ok": False, "error": err, "needs_browser_sync": True}), 422

    added, total = save_alerts(data, source="server")
    return jsonify({"ok": True, "added": added, "total": total})


@app.route("/api/status")
def status():
    stats = get_db_stats()
    oref_reachable = False
    try:
        r = requests.head(OREF_BASE, timeout=5)
        oref_reachable = r.status_code < 500
    except Exception:
        pass

    return jsonify({
        "db": stats,
        "oref_reachable": oref_reachable,
        "has_db": bool(DATABASE_URL),
    })


# ── Startup ─────────────────────────────────────────────────────────────

init_db()

# Background scheduler: try OREF sync every 6 hours
if DATABASE_URL:
    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_sync, "interval", hours=6, id="oref_sync",
                      next_run_time=datetime.now() + timedelta(seconds=30))
    scheduler.start()
    log.info("Scheduler started — OREF sync every 6 hours")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
