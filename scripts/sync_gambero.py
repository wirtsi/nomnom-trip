"""Sync Gambero Rosso gelaterias.

Gambero Rosso is fully behind Cloudflare's JS challenge — Python urllib
gets HTTP 403 immediately. The data has to come from a real browser
session. The companion `scrape_gambero.js` is run inside a tab on
gamberorosso.it and dumps to a JSON file under `data/gambero_*.json`.
This script reads those JSONs and upserts them into the DB.

To re-scrape, the workflow is:
1. Open a Chrome tab on https://www.gamberorosso.it/collections/gelaterie/
2. From the same-origin tab, run the JS in scrape_gambero.js (it fetches
   /locali-sitemap*.xml, filters to /luoghi/locali/gelateria/, fetches
   each, parses lat/lng + name + description).
3. The JS dumps `window.__GR_CACHE` as JSON to clipboard / file. Save it
   under `data/gambero_dump.json` (one big array of records).
4. Run `python3 scripts/sync_gambero.py`.

Each record in the dump has: u (url), s (slug), n (name), lat, lng,
d (description), and optionally e (error)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

DATA_DIR = Path(__file__).parent.parent / "data"


def load_dumps() -> list[dict]:
    """Read every data/gambero*.json file and return all records."""
    records: list[dict] = []
    for p in sorted(DATA_DIR.glob("gambero*.json")):
        try:
            with p.open() as f:
                blob = json.load(f)
        except json.JSONDecodeError as e:
            print(f"[skip] {p.name}: {e}", file=sys.stderr)
            continue
        if isinstance(blob, list):
            records.extend(blob)
        elif isinstance(blob, dict):
            # window.__GR_CACHE shape: { url: record, ... }
            records.extend(blob.values())
    return records


def _decode_html_entities(s: str | None) -> str | None:
    if not s:
        return s
    import html
    return html.unescape(s)


# Map Gambero's URL slug categories to our `category` enum
# (restaurant | bar | wine_shop | shop)
_CATEGORY_MAP = {
    "gelateria": "shop",
    "pasticceria": "shop",
    "panetteria": "shop",
    "panetteria-laboratorio": "shop",
    "pane-e-prodotti-di-forno": "shop",
    "caffe-bar": "bar",
    "caffebar": "bar",
    "caffe": "bar",
    "wine-bar": "bar",
    "enoteca": "wine_shop",
    "ristorante": "restaurant",
    "trattoria": "restaurant",
    "bistrot": "restaurant",
    "osteria": "restaurant",
    "pizzeria": "restaurant",
    "agriturismo": "restaurant",
    "etnico": "restaurant",
    "vegetariano": "restaurant",
    "street-food": "restaurant",
}

# Tag derived from the source category, used so users can filter on
# "what kind of place is this" without losing the original Gambero label.
_TAG_MAP = {
    "gelateria": "gelato",
    "pasticceria": "pastry",
    "panetteria": "bakery",
    "panetteria-laboratorio": "bakery",
    "pane-e-prodotti-di-forno": "bakery",
    "caffe-bar": "cafe",
    "caffebar": "cafe",
    "caffe": "cafe",
}


def _normalize(rec: dict) -> dict | None:
    if rec.get("e") or not rec.get("lat") or not rec.get("n"):
        return None
    url = rec.get("u") or ""
    slug = rec.get("s") or url.rstrip("/").rsplit("/", 1)[-1]
    name = _decode_html_entities(rec.get("n"))
    desc = _decode_html_entities(rec.get("d"))
    if desc and len(desc) > 800:
        desc = desc[:800]
    # Category: use the `c` field (Gambero URL slug) when present, else
    # infer from URL, else fall back to gelato (legacy default).
    cat_slug = rec.get("c")
    if not cat_slug:
        m = re.search(r"/luoghi/locali/([^/]+)/", url)
        cat_slug = m.group(1) if m else "gelateria"
    category = _CATEGORY_MAP.get(cat_slug, "shop")
    tags = ["gambero-rosso", cat_slug]
    if cat_slug in _TAG_MAP:
        tags.append(_TAG_MAP[cat_slug])
    # Make slug unique across categories so a "ciacco" pasticceria and a
    # "ciacco" gelateria don't collide on source_id
    return {
        "source": "gambero",
        "source_id": f"{cat_slug}/{slug}",
        "source_url": url,
        "name": name,
        "category": category,
        "country": "Italy",
        "lat": float(rec["lat"]),
        "lng": float(rec["lng"]),
        "description": desc,
        "tags": tags,
        "raw_json": rec,
    }


def sync() -> tuple[int, int]:
    added = updated = 0
    seen = 0
    raw = load_dumps()
    print(f"loaded {len(raw)} raw records", file=sys.stderr)
    with db.connect() as conn:
        try:
            for rec in raw:
                place = _normalize(rec)
                if not place:
                    continue
                was_new, _ = db.upsert_place(conn, place)
                if was_new:
                    added += 1
                else:
                    updated += 1
                seen += 1
            db.record_sync(
                conn, "gambero", "ok",
                f"loaded {len(raw)}, normalized {seen}", added, updated,
            )
        except Exception as e:
            db.record_sync(conn, "gambero", "error", str(e)[:500], added, updated)
            raise
    return added, updated


if __name__ == "__main__":
    a, u = sync()
    print(f"gambero: {a} new, {u} updated")
