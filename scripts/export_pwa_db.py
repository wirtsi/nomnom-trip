"""Export a slim, web-shippable copy of restaurants.db for the PWA.

Drops the `raw_json` column (large), trims description, keeps the canonical_links
table and the indexes the PWA's search_near() will use.

Usage:
    uv run python scripts/export_pwa_db.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "data" / "restaurants.db"
DST = ROOT / "data" / "restaurants.pwa.db"

PWA_SCHEMA = """
CREATE TABLE places (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    name         TEXT NOT NULL,
    category     TEXT,
    cuisine      TEXT,
    address      TEXT,
    city         TEXT,
    region       TEXT,
    country      TEXT,
    lat          REAL,
    lng          REAL,
    description  TEXT,
    tags         TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE INDEX idx_places_geo     ON places(lat, lng);
CREATE INDEX idx_places_city    ON places(city);
CREATE INDEX idx_places_country ON places(country);
CREATE INDEX idx_places_source  ON places(source);

CREATE TABLE canonical_links (
    place_id      INTEGER NOT NULL,
    canonical_id  TEXT NOT NULL,
    PRIMARY KEY (place_id, canonical_id)
);

CREATE INDEX idx_canonical_id ON canonical_links(canonical_id);
"""


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"Source DB not found at {SRC}")

    src_size = SRC.stat().st_size
    print(f"Source: {SRC}  {src_size / 1024 / 1024:.2f} MB")

    if DST.exists():
        DST.unlink()

    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(DST)
    dst.executescript(PWA_SCHEMA)

    cur = src.execute(
        """SELECT id, source, source_id, source_url, name, category, cuisine,
                  address, city, region, country, lat, lng, description, tags,
                  fetched_at
             FROM places"""
    )
    cols = [d[0] for d in cur.description]
    placeholders = ", ".join("?" * len(cols))
    insert = f"INSERT INTO places ({', '.join(cols)}) VALUES ({placeholders})"

    batch: list[tuple] = []
    n = 0
    for row in cur:
        d = dict(row)
        desc = d.get("description") or ""
        if len(desc) > 500:
            desc = desc[:500].rstrip() + "…"
        d["description"] = desc or None
        batch.append(tuple(d[c] for c in cols))
        if len(batch) >= 1000:
            dst.executemany(insert, batch)
            n += len(batch)
            batch.clear()
    if batch:
        dst.executemany(insert, batch)
        n += len(batch)
    print(f"Copied {n} places.")

    canon_rows = src.execute(
        "SELECT place_id, canonical_id FROM canonical_links"
    ).fetchall()
    dst.executemany(
        "INSERT INTO canonical_links (place_id, canonical_id) VALUES (?, ?)",
        [(r["place_id"], r["canonical_id"]) for r in canon_rows],
    )
    print(f"Copied {len(canon_rows)} canonical_links.")

    dst.commit()
    dst.execute("VACUUM")
    dst.commit()
    src.close()
    dst.close()

    dst_size = DST.stat().st_size
    print(f"Output: {DST}  {dst_size / 1024 / 1024:.2f} MB")
    print(
        f"Shrunk: {src_size / 1024 / 1024:.2f} MB → {dst_size / 1024 / 1024:.2f} MB "
        f"({100 * (1 - dst_size / src_size):.1f}% smaller)"
    )

    # Inject a cache-busting timestamp into the PWA so users never get a stale DB
    timestamp = str(int(__import__("time").time()))
    app_js = ROOT / "app.js"
    if app_js.exists():
        content = app_js.read_text()
        content = __import__("re").sub(
            r'restaurants\.pwa\.db\?t=\d+',
            f"restaurants.pwa.db?t={timestamp}",
            content,
        )
        app_js.write_text(content)
        print(f"Bumped DB_URL cache-buster to t={timestamp}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
