"""Sync hand-curated food-blog picks into the DB.

Blogs vary too much in structure for a one-shot scraper, so this module
takes pre-curated JSON dumps under `data/blog_*.json`. Each entry has:

    {
      "name": "Su Gologone",
      "address": "Loc. Su Gologone, Oliena (NU)",
      "city": "Oliena",
      "region": "Nuoro",
      "country": "Italy",
      "url": "https://www.megliounpostobello.com/ristoranti-sardegna/",
      "description": "...",
      "tags": ["editorial", "italy"]
    }

The ingester geocodes the address via Nominatim (cached) and upserts as
source `blog`. The `source_id` is `{slug}/{name-slugified}` so multiple
blog entries for the same restaurant don't collide across blogs.

Workflow to add a new blog:
1. Ask Tavily/Brave to extract its restaurant list.
2. Manually transcribe (or have an LLM transcribe) into a JSON file
   under `data/blog_<short-name>.json`.
3. Run `python3 scripts/sync_blog.py`.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import geocode  # noqa: E402
from dedup import add_tag, build_dedup_map, dup_key  # noqa: E402

DATA_DIR = Path(__file__).parent.parent / "data"


def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[àáâã]", "a", s)
    s = re.sub(r"[èéêë]", "e", s)
    s = re.sub(r"[ìíîï]", "i", s)
    s = re.sub(r"[òóôõ]", "o", s)
    s = re.sub(r"[ùúûü]", "u", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _blog_slug_from_url(url: str) -> str:
    try:
        host = url.split("//", 1)[1].split("/", 1)[0]
        return host.replace("www.", "").split(".")[0]
    except Exception:
        return "unknown"


def load_entries() -> list[dict]:
    """Read every data/blog_*.json file. Flattens both bare arrays and
    {"entries": [...]} wrappers."""
    entries: list[dict] = []
    for p in sorted(DATA_DIR.glob("blog_*.json")):
        try:
            with p.open() as f:
                blob = json.load(f)
        except json.JSONDecodeError as e:
            print(f"[skip] {p.name}: {e}", file=sys.stderr)
            continue
        rows: list[dict] = []
        if isinstance(blob, list):
            rows = blob
        elif isinstance(blob, dict):
            rows = blob.get("entries", [])
        if isinstance(rows, list):
            entries.extend(r for r in rows if isinstance(r, dict))
    return entries


def _geocode_entry(entry: dict) -> Optional[tuple[float, float, str]]:
    """Return (lat, lng, display_name) for an entry. Tries the most specific
    query first, then falls back to less-specific.
    """
    candidates = []
    addr = entry.get("address") or ""
    city = entry.get("city") or ""
    name = entry.get("name") or ""
    country = entry.get("country") or "Italy"
    if addr:
        candidates.append(f"{name}, {addr}, {country}")
        candidates.append(f"{addr}, {country}")
    if city:
        candidates.append(f"{name}, {city}, {country}")
        candidates.append(f"{city}, {country}")
    seen = set()
    for q in candidates:
        if q in seen:
            continue
        seen.add(q)
        loc = geocode.geocode(q)
        if loc:
            return loc["lat"], loc["lng"], loc.get("display_name") or q
        # Be polite to Nominatim
        time.sleep(1.1)
    return None


def _already_exists(conn, entry: dict) -> bool:
    """Check whether this blog/source_id pair is already in the DB."""
    name = entry.get("name")
    url = entry.get("url") or ""
    if not name or not url:
        return True  # will be skipped by _normalize anyway
    blog = _blog_slug_from_url(url)
    slug = _slugify(name)
    cur = conn.execute(
        "SELECT 1 FROM places WHERE source = 'blog' AND source_id = ?",
        (f"{blog}/{slug}",),
    )
    return cur.fetchone() is not None


def _normalize(entry: dict, conn) -> Optional[dict]:
    name = entry.get("name")
    url = entry.get("url") or ""
    if not name or not url:
        return None
    blog = _blog_slug_from_url(url)
    slug = _slugify(name)
    geo = _geocode_entry(entry)
    if not geo:
        print(f"[no geocode] {name} ({entry.get('address')!r})", file=sys.stderr)
        return None
    lat, lng, display = geo
    return {
        "source": "blog",
        "source_id": f"{blog}/{slug}",
        "source_url": url,
        "name": name,
        "category": entry.get("category") or "restaurant",
        "address": entry.get("address"),
        "city": entry.get("city"),
        "region": entry.get("region"),
        "country": entry.get("country") or "Italy",
        "lat": lat,
        "lng": lng,
        "description": entry.get("description"),
        "tags": ["editorial", f"blog:{blog}"] + (entry.get("tags") or []),
        "raw_json": {**entry, "geocoded": display},
    }



def _source_of(conn, place_id: int) -> str:
    row = conn.execute("SELECT source FROM places WHERE id = ?", (place_id,)).fetchone()
    return row["source"] if row else ""

def sync() -> tuple[int, int]:
    added = updated = 0
    raw = load_entries()
    print(f"loaded {len(raw)} blog entries", file=sys.stderr)
    with db.connect() as conn:
        try:
            # (norm_name, city, country) -> [place_id, ...], built once so a blog
            # entry that matches an existing non-blog place tags it instead of
            # inserting a cross-source duplicate.
            dup_map = build_dedup_map(conn)
            for entry in raw:
                if _already_exists(conn, entry):
                    continue
                place = _normalize(entry, conn)
                if not place:
                    continue
                key = dup_key(place)
                real = [pid for pid in dup_map.get(key, []) if _source_of(conn, pid) != "blog"]
                if real:
                    # Fold this blog's tags onto the existing curated place, keep
                    # its source_url reachable via canonical_links, and skip the
                    # duplicate row.
                    blog_tags = ["editorial", f"blog:{_blog_slug_from_url(place['source_url'])}"]
                    blog_tags += entry.get("tags") or []
                    for t in dict.fromkeys(blog_tags):
                        if add_tag(conn, real[0], t):
                            updated += 1
                    conn.execute(
                        "INSERT OR IGNORE INTO canonical_links (place_id, canonical_id) VALUES (?, ?)",
                        (real[0], f"blog:{_blog_slug_from_url(place['source_url'])}"),
                    )
                    # Backfill description/address if the curated row lacks them.
                    conn.execute(
                        "UPDATE places SET description = COALESCE(description, ?), address = COALESCE(address, ?) WHERE id = ?",
                        (place.get("description"), place.get("address"), real[0]),
                    )
                    conn.commit()
                    continue
                was_new, pid = db.upsert_place(conn, place)
                if was_new:
                    added += 1
                    dup_map.setdefault(key, []).append(pid)
                else:
                    updated += 1
                conn.commit()  # geocoding is slow, persist as we go
            db.record_sync(
                conn, "blog", "ok",
                f"loaded {len(raw)}", added, updated,
            )
        except Exception as e:
            db.record_sync(conn, "blog", "error", str(e)[:500], added, updated)
            conn.commit()
            raise
    return added, updated


if __name__ == "__main__":
    a, u = sync()
    print(f"blog: {a} new, {u} updated")
