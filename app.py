import os
import re
import json
import logging
import threading
import time
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

                CREATE TABLE IF NOT EXISTS geocache (
                    area          TEXT PRIMARY KEY,
                    lat           DOUBLE PRECISION,
                    lng           DOUBLE PRECISION,
                    geocoded_at   TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
    log.info("Database ready")
    seed_geocache_from_known()


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


# ── Known OREF area coordinates ───────────────────────────────────────────
# Hardcoded lat/lng for common Israeli cities, towns and OREF zone names.
# These seed the geocache instantly on startup, bypassing Nominatim.

KNOWN_COORDS = {
    # Tel Aviv city zones
    "תל אביב": (32.0853, 34.7818),
    "תל אביב - מזרח": (32.087, 34.812),
    "תל אביב - דרום העיר ויפו": (32.048, 34.762),
    "תל אביב - עבר הירקון": (32.102, 34.800),
    "תל אביב - צפון": (32.111, 34.804),
    "תל אביב - מרכז": (32.080, 34.790),
    "תל אביב - מערב": (32.073, 34.773),
    "יפו": (32.053, 34.753),
    # Gush Dan
    "חולון": (32.011, 34.773),
    "בת ים": (32.023, 34.747),
    "רמת גן": (32.068, 34.824),
    "רמת גן - מערב": (32.071, 34.815),
    "רמת גן - מזרח": (32.072, 34.837),
    "גבעתיים": (32.071, 34.812),
    "בני ברק": (32.084, 34.834),
    "אור יהודה": (32.029, 34.855),
    "קריית אונו": (32.061, 34.858),
    "גבעת שמואל": (32.082, 34.845),
    "אזור": (31.984, 34.823),
    "ראשון לציון": (31.973, 34.793),
    "ראשון לציון - מזרח": (31.973, 34.810),
    "ראשון לציון - מערב": (31.967, 34.775),
    "נס ציונה": (31.930, 34.798),
    "רחובות": (31.894, 34.808),
    "יהוד": (32.031, 34.887),
    "יהוד מונוסון": (32.031, 34.897),
    "אלעד": (32.053, 34.953),
    "פתח תקווה": (32.084, 34.888),
    "בית דגן": (32.002, 34.832),
    "מקווה ישראל": (32.018, 34.825),
    "סביון": (32.041, 34.879),
    "גני תקווה": (32.059, 34.880),
    "שוהם": (31.999, 34.941),
    "אבן יהודה": (32.263, 34.890),
    "לוד": (31.952, 34.890),
    "רמלה": (31.928, 34.870),
    "בית שמש": (31.745, 34.988),
    "מודיעין": (31.899, 35.010),
    "מודיעין - מכבים רעות": (31.893, 35.014),
    # North Tel Aviv metro
    "הרצליה": (32.166, 34.840),
    "הרצליה פיתוח": (32.157, 34.803),
    "כפר סבא": (32.177, 34.907),
    "רעננה": (32.184, 34.871),
    "הוד השרון": (32.151, 34.889),
    "רמת השרון": (32.146, 34.840),
    "כפר שמריהו": (32.189, 34.818),
    "תל מונד": (32.261, 34.928),
    "נתניה": (32.322, 34.853),
    "אור עקיבא": (32.506, 34.921),
    "קיסריה": (32.493, 34.902),
    # East Tel Aviv / Sharon
    "ראש העין": (32.094, 34.956),
    "כפר קאסם": (32.115, 34.976),
    "כפר נטר": (32.303, 34.904),
    "קדימה": (32.276, 34.908),
    "טייבה": (32.263, 35.006),
    "טירה": (32.234, 34.952),
    "קלנסווה": (32.281, 34.978),
    "ג'לג'וליה": (32.154, 34.956),
    "כוכב יאיר": (32.174, 34.961),
    "צור יגאל": (32.198, 34.928),
    "רינתיה": (32.140, 34.980),
    "מזור": (32.069, 34.950),
    "נחשונים": (32.050, 34.919),
    "כפר סירקין": (32.073, 34.903),
    "מגשימים": (32.041, 34.875),
    "גת רימון": (32.074, 34.906),
    "בארות יצחק": (32.069, 34.928),
    "חמד": (32.074, 34.898),
    "נחלים": (32.113, 34.876),
    "בני עטרות": (32.099, 34.967),
    "עשרת": (31.848, 34.631),
    "מעש": (32.113, 34.910),
    "גנות": (31.992, 34.830),
    "ברקת": (32.058, 34.979),
    "נופך": (32.041, 34.965),
    "גבעת כ'ח": (32.108, 34.948),
    "בית עריף": (32.093, 34.973),
    "כפר טרומן": (32.025, 34.930),
    "משמר השבעה": (31.975, 34.881),
    "צפריה": (31.961, 34.876),
    "גבעת ברנר": (31.863, 34.808),
    "פארק אריאל שרון": (32.055, 34.870),
    "איירפורט סיטי": (32.002, 34.900),
    "אזור תעשייה חבל מודיעין שוהם": (31.992, 34.958),
    "חוות עולם חסד": (32.012, 34.891),
    "חוות אביחי": (32.133, 34.975),
    "טירת יהודה": (32.082, 34.888),
    "עופרים": (32.139, 35.071),
    "בית אריה": (32.017, 35.068),
    "כפר יונה": (32.321, 34.938),
    # Haifa and north
    "חיפה": (32.794, 34.990),
    "קריית ים": (32.849, 35.069),
    "קריית אתא": (32.810, 35.104),
    "קריית ביאליק": (32.835, 35.085),
    "קריית מוצקין": (32.838, 35.075),
    "קריית חיים": (32.819, 35.061),
    "קריית שמונה": (33.208, 35.570),
    "נהריה": (33.009, 35.094),
    "עכו": (32.927, 35.080),
    "עפולה": (32.607, 35.290),
    "נצרת": (32.702, 35.298),
    "בית שאן": (32.500, 35.499),
    "מגדל העמק": (32.677, 35.238),
    "יוקנעם": (32.659, 35.106),
    "טבריה": (32.792, 35.531),
    "צפת": (32.964, 35.496),
    "מעלות תרשיחא": (33.016, 35.272),
    "כרמיאל": (32.918, 35.294),
    "טירת כרמל": (32.759, 34.975),
    "פרדס חנה": (32.474, 34.970),
    "חדרה": (32.435, 34.919),
    "זכרון יעקב": (32.570, 34.950),
    "בנימינה": (32.523, 34.945),
    "זכרון יעקב - עמיקם": (32.547, 34.957),
    "שלומי": (33.068, 35.151),
    "ראש פינה": (32.971, 35.543),
    "קצרין": (32.994, 35.692),
    "שפרעם": (32.806, 35.169),
    "טמרה": (32.856, 35.197),
    "סחנין": (32.858, 35.296),
    "מגאר": (32.886, 35.403),
    "עוספיה": (32.733, 35.064),
    "דליית אל כרמל": (32.708, 35.061),
    "ג'סר אז-זרקא": (32.537, 34.912),
    "ג'ת": (32.368, 35.051),
    "ום אל פחם": (32.516, 35.151),
    "כפר כנא": (32.748, 35.335),
    "ירכא": (32.954, 35.204),
    # Jerusalem area
    "ירושלים": (31.768, 35.214),
    "ירושלים - מערב": (31.770, 35.175),
    "ירושלים - מזרח": (31.780, 35.240),
    "ירושלים - דרום": (31.740, 35.200),
    "מעלה אדומים": (31.777, 35.296),
    "גבעת זאב": (31.862, 35.177),
    "בית אל": (31.938, 35.210),
    "אפרת": (31.658, 35.158),
    "גוש עציון": (31.657, 35.113),
    "בית לחם": (31.705, 35.200),
    "ביתר עילית": (31.695, 35.116),
    "מעלה מכמש": (31.876, 35.253),
    "הר גילה": (31.701, 35.178),
    "אבו גוש": (31.806, 35.111),
    "מבשרת ציון": (31.795, 35.151),
    "בית שמש": (31.745, 34.988),
    "אשתאול": (31.785, 35.001),
    # South / Gaza border area
    "אשדוד": (31.804, 34.655),
    "אשקלון": (31.669, 34.572),
    "קריית גת": (31.610, 34.765),
    "קריית מלאכי": (31.730, 34.742),
    "שדרות": (31.525, 34.597),
    "נתיבות": (31.420, 34.588),
    "אופקים": (31.316, 34.621),
    "באר שבע": (31.252, 34.791),
    "דימונה": (31.070, 35.033),
    "ירוחם": (30.989, 34.930),
    "ערד": (31.259, 35.212),
    "מצפה רמון": (30.611, 34.802),
    "אילת": (29.558, 34.952),
    "יבנה": (31.876, 34.742),
    "גן יבנה": (31.792, 34.707),
    "גדרה": (31.814, 34.776),
    "מזכרת בתיה": (31.857, 34.843),
    "נחל עוז": (31.447, 34.511),
    "כפר עזה": (31.454, 34.505),
    "בארי": (31.388, 34.468),
    "נירים": (31.367, 34.500),
    "רעים": (31.410, 34.495),
    "ניר עוז": (31.363, 34.466),
    "בית הגדי": (31.421, 34.521),
    "תקומה": (31.428, 34.537),
    "אשבול": (31.384, 34.553),
    "מגן": (31.317, 34.487),
    "כפר מימון": (31.356, 34.577),
    "גברעם": (31.567, 34.658),
    "מרחבים": (31.377, 34.627),
    "אלומים": (31.476, 34.598),
    "עוז": (31.504, 34.545),
    "נבטים": (31.280, 34.780),
    "משאבי שדה": (31.249, 34.629),
    "רוחמה": (31.483, 34.679),
    "בית קמה": (31.458, 34.698),
    "שובל": (31.369, 34.689),
    "חצרים": (31.261, 34.724),
    "עומר": (31.271, 34.851),
    "להבים": (31.368, 34.832),
    "כסיפה": (31.222, 35.013),
    "רהט": (31.393, 34.754),
    "לקיה": (31.375, 34.772),
    "הוצה": (31.270, 34.668),
    # Judea/Samaria / West Bank settlements
    "אריאל": (32.106, 35.168),
    "מעלה שומרון": (32.227, 35.139),
    "אלקנה": (32.170, 35.041),
    "שבי שומרון": (32.248, 35.153),
    "חרמש": (32.363, 35.063),
    "פדואל": (32.172, 35.072),
    "ברוכין": (32.182, 35.022),
    "עלי זהב": (32.110, 35.198),
    "עלי": (32.062, 35.241),
    "רבבה": (32.191, 35.082),
    "גופנה": (31.981, 35.201),
    "מעלה לבונה": (32.030, 35.238),
    "נווה צוף": (32.017, 35.179),
    "יקיר": (32.132, 35.072),
    "טלמון": (31.947, 35.120),
    "דולב": (31.963, 35.130),
    "שילה": (32.042, 35.293),
    "אלפי מנשה": (32.168, 35.030),
    "קרני שומרון": (32.172, 35.041),
    "עמנואל": (32.159, 35.098),
    "מבוא חורון": (31.877, 35.032),
    "שבות רחל": (32.086, 35.279),
    "רחלים": (32.112, 35.263),
    "נופים": (32.179, 35.094),
    "מעון צופיה": (32.103, 35.230),
    "נצר חזני": (31.593, 34.557),
    "חרשה": (32.051, 35.170),
    "גבעת הרואה": (32.339, 35.043),
    "חוות יאיר": (32.198, 35.012),
    "נוף איילון, שעלבים": (31.862, 34.984),
    "נופי נחמיה": (31.870, 35.013),
    "חוות נווה צוף": (32.020, 35.175),
    "חוות שחרית": (32.151, 35.004),
    "חוות צרידה": (32.221, 35.047),
    "חוות שוביאל": (32.143, 35.024),
    "חוות מגנזי": (32.167, 35.060),
    "חוות נוף אב\"י": (32.135, 35.031),
    "חוות נחל שילה": (32.055, 35.261),
    "מסוף אורנית": (32.149, 35.011),
    "מתחם פי גלילות": (32.153, 34.828),
    "מתחם גלילות": (32.130, 34.840),
    # Central Israel moshavim / kibbutzim
    "כפר נוער בן שמן": (31.983, 34.971),
    "קריית עקרון": (31.868, 34.833),
    "כפר ביל'ו": (31.892, 34.812),
    "כפר רות": (31.962, 35.002),
    "כפר מל'ל": (32.234, 34.882),
    "רמות השבים": (32.219, 34.928),
    "גבעת חן": (32.162, 34.939),
    "נווה ימין": (32.237, 34.969),
    "גני יוחנן": (31.892, 34.836),
    "כפר הנגיד": (31.874, 34.850),
    "כרמי יוסף": (31.874, 34.953),
    "גבעת הראל": (31.942, 34.947),
    "בן זכאי": (31.866, 34.841),
    "חולדה": (31.839, 34.912),
    "צופית": (32.213, 34.959),
    "משמר דוד": (31.876, 34.900),
    "בית גמליאל": (31.853, 34.826),
    "ניר אליהו": (32.121, 34.992),
    "פתחיה": (31.998, 34.930),
    "שדה אפרים": (32.097, 34.990),
    "גבעת הראל": (31.942, 34.947),
    "כפר בן נון": (31.819, 34.940),
    "יבוא דודי": (31.563, 34.525),
    "דורות עילית": (31.510, 34.552),
    "מכינת אלישע": (32.105, 34.935),
    # Binyamin / Samaria settlements
    "רמת מגרון": (31.963, 35.251),
    "קידה": (32.059, 35.248),
    "כוכב יעקב": (31.893, 35.202),
    "אש קודש": (32.052, 35.252),
    "אביתר": (32.037, 35.301),
    "מגרון": (31.951, 35.239),
    "פסגות": (31.908, 35.247),
    "עפרה": (31.976, 35.251),
    "יצהר": (32.181, 35.214),
    "כוכב השחר": (32.000, 35.292),
    "מצפה דני": (31.946, 35.262),
    "הר אדר": (31.825, 35.099),
    "רימונים": (31.953, 35.286),
    "אעירה השחר": (31.980, 35.260),
    "חוות מעלה אהוביה": (32.151, 35.100),
    "חוות חנינא": (32.018, 35.248),
    "חוות נחלת צבי": (32.138, 35.183),
    "חוות בניהו": (32.053, 35.201),
    "חוות מלכיאל": (32.097, 35.150),
    "חוות ינון": (31.980, 35.148),
    "חוות גלעד": (32.220, 35.048),
    "מסוף אורנית": (32.149, 35.011),
    "נווה צוף": (32.017, 35.179),
    "שבות רחל": (32.086, 35.279),
    "החווה של זוהר": (32.060, 35.200),
    # Latrun / Judean foothills
    "לטרון": (31.839, 34.983),
    "בקוע": (31.837, 35.058),
    "נחשון": (31.792, 34.970),
    "נווה שלום": (31.837, 34.989),
    "נטף": (31.802, 35.072),
    "צלפון": (31.800, 34.971),
    "גיזו": (31.818, 34.990),
    "מעלה החמישה": (31.822, 35.094),
    "נווע אילן": (31.808, 35.062),
    "נווה אילן": (31.808, 35.062),
    "נוף ירושלים": (31.800, 35.100),
    "הר גילה": (31.701, 35.178),
    "אלון שבות": (31.658, 35.127),
    "תקוע": (31.621, 35.201),
    "קרית ארבע": (31.531, 35.113),
    "חברון": (31.530, 35.095),
    # Sharon / central Israel moshavim
    "שדה ורבורג": (32.237, 34.889),
    "כפר אביב": (31.858, 34.829),
    "כפר מרדכי": (31.840, 34.798),
    "רשפון": (32.204, 34.853),
    "שדמה": (31.752, 34.772),
    "בית ברל": (32.192, 34.940),
    "בניה": (31.847, 34.812),
    "נווע ארז": (32.080, 34.898),
    "נווה ארז": (32.080, 34.898),
    "בני אדם": (31.904, 34.879),
    "גבעת חן": (32.162, 34.939),
    "חמד": (32.074, 34.898),
    "גן השומרון": (32.392, 34.941),
    "שפיים": (32.230, 34.847),
    "כפר מרדכי": (31.840, 34.798),
    "בית עזרא": (31.882, 34.805),
    "תלמי יחיאל": (31.836, 34.793),
    "שדה דוד": (31.836, 34.806),
    "כפר ביאליק": (32.836, 35.056),
    "קסם": (32.098, 34.985),
    "גן חיים": (32.081, 34.940),
    "סתריה": (31.868, 34.864),
    "בית חנניה": (32.477, 34.897),
    "חופית": (32.407, 34.877),
    "ניצני עוז": (32.274, 34.897),
    # More Sharon / Shfela
    "ניר צבי": (31.971, 34.861),
    "נחם": (31.825, 34.948),
    "עזריה": (31.914, 34.964),
    "כפר שמריהו": (32.189, 34.818),
    "מכמורת": (32.428, 34.887),
    "עין החורש": (32.397, 34.940),
    "גן השומרון": (32.392, 34.941),
    "פרדסיה": (32.290, 34.923),
    "תל יצחק": (32.261, 34.882),
    "עין ורד": (32.230, 34.930),
    "שפיים": (32.230, 34.847),
    "הרדוף": (32.700, 35.066),
    "נשר": (32.773, 35.031),
    "קריית טבעון": (32.726, 35.127),
    # South / Negev additional
    "כפר מנחם": (31.657, 34.819),
    "גן הדרום": (31.782, 34.717),
    "גבעת ברנר": (31.863, 34.808),
    "חמדיה": (32.543, 35.473),
    "נהלל": (32.685, 35.197),
    "אלוני אבא": (32.731, 35.177),
    "כפר יהושע": (32.669, 35.145),
    "גבע": (32.621, 35.222),
    "מרחביה": (32.601, 35.252),
    "עין דור": (32.637, 35.413),
    "גשר": (32.580, 35.553),
}


def seed_geocache_from_known():
    """Insert KNOWN_COORDS into geocache on startup — fast, no API calls."""
    if not DATABASE_URL:
        return
    inserted = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for area, (lat, lng) in KNOWN_COORDS.items():
                cur.execute(
                    "INSERT INTO geocache (area, lat, lng) VALUES (%s,%s,%s) "
                    "ON CONFLICT (area) DO NOTHING",
                    (area, lat, lng),
                )
                inserted += 1
        conn.commit()
    log.info(f"Geocache seeded with {inserted} known areas")


def approximate_missing():
    """
    For areas not yet geocoded, infer coordinates from KNOWN_COORDS using:
    1. "City - Suffix" → use the base city coords
    2. A known name is a substring of the area name (or vice versa)
    Runs fast (pure Python, no API calls).
    """
    if not DATABASE_URL:
        return 0
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT DISTINCT a.area FROM alerts a
                LEFT JOIN geocache g ON a.area = g.area
                WHERE a.area IS NOT NULL AND a.area != '' AND g.area IS NULL
            """)
            missing = [r["area"] for r in cur.fetchall()]

    to_insert = []
    for name in missing:
        lat, lng = None, None

        # Strategy 1: "Base - Direction/Suffix" → look up Base
        if " - " in name:
            base = name.split(" - ")[0].strip()
            if base in KNOWN_COORDS:
                lat, lng = KNOWN_COORDS[base]

        # Strategy 2: a known name is contained in this name
        if lat is None:
            for known, coords in KNOWN_COORDS.items():
                if len(known) >= 3 and known in name:
                    lat, lng = coords
                    break

        # Strategy 3: this name is contained in a known name
        if lat is None:
            for known, coords in KNOWN_COORDS.items():
                if len(name) >= 4 and name in known:
                    lat, lng = coords
                    break

        if lat is not None:
            to_insert.append((name, lat, lng))

    if to_insert:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for area, lat, lng in to_insert:
                    cur.execute(
                        "INSERT INTO geocache (area, lat, lng) VALUES (%s,%s,%s) "
                        "ON CONFLICT (area) DO NOTHING",
                        (area, lat, lng),
                    )
            conn.commit()
        log.info(f"Approximated coords for {len(to_insert)} more areas")
    return len(to_insert)


# ── Geocoding ─────────────────────────────────────────────────────────────

_geocoding_lock = threading.Lock()

def geocode_area(name):
    """Query Nominatim for an Israeli area name. Tries multiple strategies."""
    queries = [name]
    if " - " in name:
        queries.append(name.split(" - ")[0].strip())  # base city fallback

    for q in queries:
        for params in [
            {"q": q, "countrycodes": "il", "format": "json", "limit": 1},
            {"q": f"{q} Israel",            "format": "json", "limit": 1},
        ]:
            try:
                resp = requests.get(
                    "https://nominatim.openstreetmap.org/search", params=params,
                    headers={"User-Agent": "oref-alert-analyzer/1.0"},
                    timeout=8,
                )
                data = resp.json()
                if data:
                    return float(data[0]["lat"]), float(data[0]["lon"])
            except Exception as e:
                log.debug(f"Geocode query failed '{q}': {e}")
            time.sleep(1.1)

    return None, None


def geocode_missing(limit=80):
    """Geocode the top `limit` areas not yet in geocache. Rate-limited: 1 req/sec."""
    if not DATABASE_URL:
        return
    if not _geocoding_lock.acquire(blocking=False):
        log.info("Geocoding already running, skipping")
        return
    try:
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT a.area, COUNT(*) AS cnt
                    FROM alerts a
                    LEFT JOIN geocache g ON a.area = g.area
                    WHERE a.area IS NOT NULL AND a.area != ''
                      AND g.area IS NULL
                    GROUP BY a.area
                    ORDER BY cnt DESC
                    LIMIT %s
                """, (limit,))
                missing = [r["area"] for r in cur.fetchall()]

        log.info(f"Geocoding {len(missing)} missing areas...")
        done = 0
        for name in missing:
            lat, lng = geocode_area(name)
            if lat is not None:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO geocache (area, lat, lng) VALUES (%s,%s,%s) "
                            "ON CONFLICT (area) DO NOTHING",
                            (name, lat, lng),
                        )
                    conn.commit()
                done += 1
            time.sleep(1.1)   # Nominatim rate limit
        log.info(f"Geocoding complete: {done}/{len(missing)} resolved")
    finally:
        _geocoding_lock.release()


def geocode_in_background(limit=80):
    threading.Thread(target=geocode_missing, args=(limit,), daemon=True).start()


# ── Auto sync job ─────────────────────────────────────────────────────────

def auto_sync():
    log.info("=== Auto-sync starting ===")
    try:
        alerts, updated = fetch_from_github()
        added, total = save_alerts(alerts, source=f"github:{updated or 'unknown'}")
        log.info(f"=== Auto-sync done: +{added:,} new, {total:,} total ===")
        approximate_missing()
        geocode_in_background(80)
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


@app.route("/api/map")
def get_map_data():
    if not DATABASE_URL:
        return jsonify({"points": [], "geocoded_total": 0})

    from_date   = request.args.get("from_date")
    to_date     = request.args.get("to_date")
    preset      = request.args.get("preset")
    areas_param = request.args.get("areas")

    from_date, to_date = parse_preset(preset, from_date, to_date)
    areas = [a.strip() for a in areas_param.split(",")] if areas_param else None

    conditions, params = [], []
    if from_date:
        conditions.append("a.date_only >= %s"); params.append(from_date)
    if to_date:
        conditions.append("a.date_only <= %s"); params.append(to_date)
    if areas:
        conditions.append("a.area = ANY(%s)"); params.append(areas)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"""
                SELECT a.area, COUNT(*) AS cnt, g.lat, g.lng
                FROM alerts a
                JOIN geocache g ON a.area = g.area
                {where}
                GROUP BY a.area, g.lat, g.lng
                ORDER BY cnt DESC
            """, params)
            points = [{"area": r["area"], "count": r["cnt"],
                       "lat": r["lat"], "lng": r["lng"]} for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) AS n FROM geocache")
            geocoded_total = cur.fetchone()["n"]

    return jsonify({"points": points, "geocoded_total": geocoded_total})


@app.route("/api/geocode", methods=["POST"])
def trigger_geocode():
    geocode_in_background(100)
    return jsonify({"ok": True, "message": "Geocoding started in background"})


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
    # After initial sync completes (~30s), run approximation + Nominatim geocoding
    threading.Timer(30, approximate_missing).start()
    threading.Timer(60, lambda: geocode_in_background(100)).start()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
