#!/usr/bin/env python3
"""
OREF Alert Sync Tool
Run this once from any machine in Israel to populate the database.
Usage:  python3 sync.py
"""
import urllib.request
import urllib.error
import json
import sys

OREF_URL    = "https://www.oref.org.il/WarningMessages/History/AlertsHistory.json"
BACKEND_URL = "https://web-production-9c22d.up.railway.app/api/sync"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://www.oref.org.il/heb/alerts-history",
    "Accept":     "application/json",
}

def fetch(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8-sig")

print("=" * 50)
print("  OREF Alert Sync")
print("=" * 50)

# Step 1: fetch from OREF
print("\n[1/2] Fetching alerts from OREF...")
try:
    raw = fetch(OREF_URL, headers=HEADERS)
    alerts = json.loads(raw.strip())
    if not isinstance(alerts, list):
        print("ERROR: Unexpected response format")
        sys.exit(1)
    print(f"      Got {len(alerts):,} alerts")
except urllib.error.HTTPError as e:
    print(f"ERROR: OREF returned HTTP {e.code}")
    print("       Make sure you're running this from Israel (or Israeli VPN)")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

# Step 2: push to backend
print(f"\n[2/2] Sending to database...")
try:
    body = json.dumps(alerts).encode("utf-8")
    result_raw = fetch(
        BACKEND_URL,
        headers={"Content-Type": "application/json"},
        data=body,
        method="POST",
    )
    result = json.loads(result_raw)
    if result.get("ok"):
        print(f"      Added : {result.get('added', 0):,} new records")
        print(f"      Total : {result.get('total', 0):,} records in DB")
        print("\n✅  Sync complete! Open the dashboard to see the data.")
    else:
        print(f"ERROR from backend: {result.get('error')}")
        sys.exit(1)
except Exception as e:
    print(f"ERROR sending to backend: {e}")
    sys.exit(1)
