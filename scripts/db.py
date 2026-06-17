"""Schema and helpers for the local restaurant DB."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "restaurants.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,        -- 'splendido' | 'raisin' | 'michelin' | 'gambero' | 'blog' | 'rawwine'
    source_id    TEXT NOT NULL,        -- ID in source's namespace
    source_url   TEXT NOT NULL,
    name         TEXT NOT NULL,
    category     TEXT,                 -- 'restaurant' | 'bar' | 'wine_shop' | 'shop' | 'other'
    cuisine      TEXT,
    address      TEXT,
    city         TEXT,
    region       TEXT,
    country      TEXT,
    lat          REAL,
    lng          REAL,
    description  TEXT,
    tags         TEXT,                 -- JSON array
    raw_json     TEXT,                 -- full source record
    fetched_at   TEXT NOT NULL,        -- ISO 8601 UTC
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_places_geo     ON places(lat, lng);
CREATE INDEX IF NOT EXISTS idx_places_city    ON places(city);
CREATE INDEX IF NOT EXISTS idx_places_country ON places(country);
CREATE INDEX IF NOT EXISTS idx_places_source  ON places(source);

CREATE TABLE IF NOT EXISTS canonical_links (
    place_id      INTEGER NOT NULL,
    canonical_id  TEXT NOT NULL,       -- e.g. "osm:N123" or "google:ChIJ..."
    PRIMARY KEY (place_id, canonical_id),
    FOREIGN KEY (place_id) REFERENCES places(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_canonical_id ON canonical_links(canonical_id);

CREATE TABLE IF NOT EXISTS sync_log (
    source        TEXT PRIMARY KEY,
    last_run_at   TEXT NOT NULL,
    last_status   TEXT NOT NULL,       -- 'ok' | 'error'
    last_message  TEXT,
    rows_added    INTEGER DEFAULT 0,
    rows_updated  INTEGER DEFAULT 0
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open the DB, ensure schema exists, return a connection."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_place(conn: sqlite3.Connection, place: dict) -> tuple[bool, int]:
    """Insert or update a place. Returns (was_new, place_id).

    Atomic: uses INSERT ... ON CONFLICT(source, source_id) DO UPDATE so
    concurrent syncs on the same source_id can't race the SELECT-then-INSERT.
    The pre-check via SELECT is only to determine the was_new return value;
    the actual write is atomic and survives concurrent calls.
    """
    required = {"source", "source_id", "source_url", "name"}
    missing = required - set(place)
    if missing:
        raise ValueError(f"upsert_place: missing fields {missing}")

    place = dict(place)
    place.setdefault("fetched_at", utc_now())
    if isinstance(place.get("tags"), (list, tuple)):
        place["tags"] = json.dumps(list(place["tags"]), ensure_ascii=False)
    if isinstance(place.get("raw_json"), (dict, list)):
        place["raw_json"] = json.dumps(place["raw_json"], ensure_ascii=False)

    cols = [
        "source", "source_id", "source_url", "name", "category", "cuisine",
        "address", "city", "region", "country", "lat", "lng",
        "description", "tags", "raw_json", "fetched_at",
    ]
    values = [place.get(c) for c in cols]
    placeholders = ", ".join("?" * len(cols))
    update_cols = [c for c in cols if c != "source" and c != "source_id"]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)

    # Pre-check (read-only) for was_new signal — does not race the write
    existed = conn.execute(
        "SELECT id FROM places WHERE source = ? AND source_id = ?",
        (place["source"], place["source_id"]),
    ).fetchone()

    conn.execute(
        f"""INSERT INTO places ({', '.join(cols)}) VALUES ({placeholders})
            ON CONFLICT(source, source_id) DO UPDATE SET {set_clause}""",
        values,
    )
    place_id = conn.execute(
        "SELECT id FROM places WHERE source = ? AND source_id = ?",
        (place["source"], place["source_id"]),
    ).fetchone()[0]
    return existed is None, place_id


def record_sync(
    conn: sqlite3.Connection,
    source: str,
    status: str,
    message: str = "",
    rows_added: int = 0,
    rows_updated: int = 0,
) -> None:
    conn.execute(
        """INSERT INTO sync_log (source, last_run_at, last_status, last_message, rows_added, rows_updated)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
             last_run_at = excluded.last_run_at,
             last_status = excluded.last_status,
             last_message = excluded.last_message,
             rows_added = excluded.rows_added,
             rows_updated = excluded.rows_updated""",
        (source, utc_now(), status, message, rows_added, rows_updated),
    )


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in kilometers."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so user input is treated literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_near(
    conn: sqlite3.Connection,
    lat: float,
    lng: float,
    radius_km: float = 10.0,
    category: Optional[str] = None,
    sources: Optional[Iterable[str]] = None,
    cuisine: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Search places near a point. Cheap bounding-box prefilter then exact distance."""
    # bounding box: 1° lat ≈ 111 km; 1° lng ≈ 111 * cos(lat) km
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))

    query = """
        SELECT * FROM places
        WHERE lat BETWEEN ? AND ?
          AND lng BETWEEN ? AND ?
          AND lat IS NOT NULL AND lng IS NOT NULL
    """
    params: list = [lat - dlat, lat + dlat, lng - dlng, lng + dlng]

    if category:
        query += " AND category = ?"
        params.append(category)
    if sources:
        srcs = list(sources)
        query += f" AND source IN ({','.join('?' * len(srcs))})"
        params.extend(srcs)
    if cuisine:
        query += " AND cuisine LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(cuisine)}%")
    if keyword:
        query += " AND (name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
        kw = f"%{_escape_like(keyword)}%"
        params.extend([kw, kw, kw])

    rows = [dict(r) for r in conn.execute(query, params)]
    for r in rows:
        r["distance_km"] = haversine_km(lat, lng, r["lat"], r["lng"])
    rows = [r for r in rows if r["distance_km"] <= radius_km]
    rows.sort(key=lambda r: r["distance_km"])

    # Stack endorsements via canonical_links
    place_ids = [r["id"] for r in rows]
    if place_ids:
        canon = conn.execute(
            f"""SELECT cl.place_id, cl.canonical_id, p.source, p.source_url
                FROM canonical_links cl
                JOIN canonical_links cl2 USING (canonical_id)
                JOIN places p ON p.id = cl2.place_id
                WHERE cl.place_id IN ({','.join('?' * len(place_ids))})""",
            place_ids,
        ).fetchall()
        endorsements: dict[int, list[dict]] = {}
        for c in canon:
            endorsements.setdefault(c["place_id"], []).append(
                {"source": c["source"], "url": c["source_url"]}
            )
        for r in rows:
            r["endorsements"] = endorsements.get(r["id"], [])
            if r.get("tags"):
                try:
                    r["tags"] = json.loads(r["tags"])
                except (json.JSONDecodeError, TypeError):
                    pass
    return rows[:limit]
