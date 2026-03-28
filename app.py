from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import json
import os
from datetime import datetime, timedelta
from dateutil import parser as dateparser

app = Flask(__name__, static_folder='static')
CORS(app)

OREF_BASE = "https://www.oref.org.il"

# Try multiple known OREF endpoints in order
OREF_HISTORY_URLS = [
    f"{OREF_BASE}/WarningMessages/History/AlertsHistory.json",
    f"{OREF_BASE}/api/alerts/history",
    f"{OREF_BASE}/warningMessages/alert/History/AlertsHistory.json",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.oref.org.il/heb/alerts-history",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
}

# In-memory store for uploaded/pasted data
_uploaded_data = []


def enrich_alerts(data):
    enriched = []
    for alert in data:
        entry = dict(alert)
        raw_date = alert.get("alertDate", "")
        try:
            dt = dateparser.parse(raw_date)
            entry["hour"] = dt.hour
            entry["date"] = dt.strftime("%Y-%m-%d")
            entry["time"] = dt.strftime("%H:%M:%S")
            entry["timestamp"] = dt.isoformat()
        except Exception:
            entry["hour"] = None
            entry["date"] = None
            entry["time"] = None
            entry["timestamp"] = None
        enriched.append(entry)
    return enriched


def fetch_oref_alerts(from_date=None, to_date=None):
    params = {}
    if from_date:
        params["fromDate"] = from_date
    if to_date:
        params["toDate"] = to_date

    last_error = None
    session = requests.Session()
    # Visit the main page first to pick up cookies
    try:
        session.get(f"{OREF_BASE}/heb/alerts-history", headers=HEADERS, timeout=15)
    except Exception:
        pass

    for url in OREF_HISTORY_URLS:
        try:
            resp = session.get(url, headers=HEADERS, params=params, timeout=30)
            if resp.status_code == 403:
                last_error = f"403 Forbidden from {url}"
                continue
            resp.raise_for_status()
            text = resp.content.decode("utf-8-sig").strip()
            if not text:
                return []
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            continue
        except json.JSONDecodeError as e:
            return {"error": f"JSON parse error: {e}", "status": "parse_failed"}

    return {"error": last_error or "All endpoints failed", "status": "fetch_failed"}


# ── Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/alerts")
def get_alerts():
    global _uploaded_data

    # If user uploaded their own data, use that
    if _uploaded_data:
        return jsonify(enrich_alerts(_uploaded_data))

    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    preset = request.args.get("preset")  # 24h, week, day

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

    data = fetch_oref_alerts(from_date, to_date)

    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502

    return jsonify(enrich_alerts(data))


@app.route("/api/upload", methods=["POST"])
def upload_data():
    """Accept raw JSON alert data (array) from the client to use instead of live fetch."""
    global _uploaded_data
    try:
        body = request.get_json(force=True)
        if not isinstance(body, list):
            return jsonify({"error": "Expected a JSON array"}), 400
        _uploaded_data = body
        return jsonify({"ok": True, "count": len(body)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/upload", methods=["DELETE"])
def clear_upload():
    global _uploaded_data
    _uploaded_data = []
    return jsonify({"ok": True})


@app.route("/api/areas")
def get_areas():
    """Return distinct areas/cities from all alerts."""
    global _uploaded_data
    if _uploaded_data:
        areas = sorted(set(a.get("data", "").strip() for a in _uploaded_data if a.get("data")))
        return jsonify(areas)

    data = fetch_oref_alerts()
    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502

    areas = sorted(set(a.get("data", "").strip() for a in data if a.get("data")))
    return jsonify(areas)


@app.route("/api/status")
def status():
    """Quick health-check — also tells client if live data is available."""
    global _uploaded_data
    return jsonify({
        "uploaded_data": len(_uploaded_data),
        "oref_base": OREF_BASE,
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
