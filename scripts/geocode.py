"""Lightweight geocoding via OpenStreetMap Nominatim.

Used for two things:
  1. Resolving free-form "City, Country" inputs in queries to lat/lng.
  2. Backfilling missing coords on places that came from sources without geo.

Nominatim's usage policy: max 1 request/second, identify yourself in the User-
Agent, cache aggressively. We do all three. For heavy use, run your own
Nominatim instance or use a paid geocoder.

If you'd rather use Google Places (better quality, billable), set the
GOOGLE_PLACES_API_KEY env var and use `google_geocode()` instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

CACHE_PATH = Path(__file__).parent.parent / "data" / "geocode_cache.sqlite"
USER_AGENT = "restaurant-finder-skill/0.1 (set NOMINATIM_CONTACT to override)"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org"

_last_call_at = 0.0


def _ua() -> str:
    contact = os.environ.get("NOMINATIM_CONTACT")
    return f"{USER_AGENT} contact={contact}" if contact else USER_AGENT


def _cache_conn() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS geocode_cache (
              query TEXT PRIMARY KEY,
              lat   REAL,
              lng   REAL,
              display_name TEXT,
              fetched_at TEXT NOT NULL
           )"""
    )
    return conn


def _rate_limit() -> None:
    """Sleep enough to keep us at <= 1 req/sec."""
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)
    _last_call_at = time.time()


def geocode(query: str) -> Optional[dict]:
    """Return {lat, lng, display_name} for a free-form place query, or None."""
    if not query or not query.strip():
        return None
    q = query.strip()

    with _cache_conn() as cache:
        row = cache.execute(
            "SELECT lat, lng, display_name FROM geocode_cache WHERE query = ?", (q,)
        ).fetchone()
        if row is not None:
            if row[0] is None:  # cached negative
                return None
            return {"lat": row[0], "lng": row[1], "display_name": row[2]}

    _rate_limit()
    url = f"{NOMINATIM_BASE}/search?{urllib.parse.urlencode({'q': q, 'format': 'json', 'limit': 1})}"
    req = urllib.request.Request(url, headers={"User-Agent": _ua(), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        data = []

    result: Optional[dict] = None
    if data:
        d = data[0]
        result = {
            "lat": float(d["lat"]),
            "lng": float(d["lon"]),
            "display_name": d.get("display_name"),
        }

    with _cache_conn() as cache:
        cache.execute(
            "INSERT OR REPLACE INTO geocode_cache (query, lat, lng, display_name, fetched_at) VALUES (?, ?, ?, ?, datetime('now'))",
            (q, result["lat"] if result else None, result["lng"] if result else None,
             result["display_name"] if result else None),
        )
        cache.commit()
    return result


def google_geocode(query: str) -> Optional[dict]:
    """Optional Google Places fallback. Requires GOOGLE_PLACES_API_KEY."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        return None
    url = (
        "https://maps.googleapis.com/maps/api/geocode/json?"
        + urllib.parse.urlencode({"address": query, "key": key})
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    r = data["results"][0]
    loc = r["geometry"]["location"]
    return {
        "lat": loc["lat"],
        "lng": loc["lng"],
        "display_name": r.get("formatted_address"),
        "place_id": r.get("place_id"),
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(geocode(" ".join(sys.argv[1:])), indent=2))
