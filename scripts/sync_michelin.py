"""Sync Michelin Guide via their public Algolia search backend.

Discovered from https://guide.michelin.com source: they use Algolia, app id
`8NVHRD7ONV`, public search key `3222e669cf890dc73fa5f38241117ba5`, index
`prod-restaurants-en`. The key is a public read-only key embedded in their
JavaScript bundle for browser search; this is the same query path the website
itself uses.

If this stops working, open guide.michelin.com, filter restaurants once, and
look at the XHR call to `*-dsn.algolia.net/1/indexes/*/queries` in DevTools —
the headers contain the current app id and key.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Iterable

import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

ALGOLIA_APP_ID = "8NVHRD7ONV"
ALGOLIA_API_KEY = "3222e669cf890dc73fa5f38241117ba5"
ALGOLIA_INDEX = "prod-restaurants-en"
ALGOLIA_HOST = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net"

PAGE_SIZE = 1000  # Algolia caps hitsPerPage at 1000


def _algolia_query(page: int) -> dict:
    """Hit Algolia for one page of restaurants."""
    url = f"{ALGOLIA_HOST}/1/indexes/*/queries"
    body = {
        "requests": [
            {
                "indexName": ALGOLIA_INDEX,
                "params": (
                    f"page={page}&hitsPerPage={PAGE_SIZE}"
                    "&attributesToRetrieve="
                    '["objectID","name","slug","city","country","region","area_name",'
                    '"cuisines","chef","michelin_award","price_category","_geoloc",'
                    '"identifier","url","main_image","green_star","good_menu",'
                    '"new_table","take_away","online_booking"]'
                ),
            }
        ]
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Algolia-API-Key": ALGOLIA_API_KEY,
            "X-Algolia-Application-Id": ALGOLIA_APP_ID,
            # The public Algolia key is referer-restricted to guide.michelin.com.
            "Referer": "https://guide.michelin.com/",
            "Origin": "https://guide.michelin.com",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _normalize(hit: dict) -> dict:
    """Convert one Algolia hit into our places schema."""
    geo = hit.get("_geoloc") or {}
    cuisines = hit.get("cuisines") or []
    if isinstance(cuisines, str):
        cuisines = [cuisines]
    # Algolia sometimes returns cuisines as [{label,slug}] dicts
    cuisines = [
        (c.get("label") or c.get("name") or c.get("slug") or "")
        if isinstance(c, dict) else str(c)
        for c in cuisines
    ]
    cuisines = [c for c in cuisines if c]
    award = hit.get("michelin_award") or ""
    tags = []
    if award:
        tags.append(f"michelin:{award}")
    if hit.get("green_star"):
        tags.append("michelin:green-star")
    if hit.get("good_menu"):
        tags.append("michelin:good-menu")

    site_url = hit.get("url") or ""
    if site_url and not site_url.startswith("http"):
        site_url = f"https://guide.michelin.com{site_url}"

    def _flatten(v):
        """Coerce Algolia values that may arrive as dict/list to a string."""
        if v is None or isinstance(v, (str, int, float)):
            return v if not isinstance(v, (int, float)) else str(v)
        if isinstance(v, dict):
            return v.get("label") or v.get("name") or v.get("slug") or v.get("title")
        if isinstance(v, list):
            parts = [_flatten(x) for x in v]
            return ", ".join(p for p in parts if p) or None
        return str(v)

    return {
        "source": "michelin",
        "source_id": str(hit.get("objectID") or hit.get("identifier") or hit.get("slug")),
        "source_url": site_url,
        "name": _flatten(hit.get("name")) or "",
        "category": "restaurant",
        "cuisine": ", ".join(cuisines) if cuisines else None,
        "address": _flatten(hit.get("area_name")),
        "city": _flatten(hit.get("city")),
        "region": _flatten(hit.get("region")),
        "country": _flatten(hit.get("country")),
        "lat": geo.get("lat"),
        "lng": geo.get("lng") if "lng" in geo else geo.get("lon"),
        "description": (
            f"Michelin {award}".strip() if award else None
        ),
        "tags": tags,
        "raw_json": hit,
    }


def fetch_all() -> Iterable[dict]:
    """Yield every Michelin-indexed restaurant, paging through Algolia."""
    page = 0
    while True:
        try:
            resp = _algolia_query(page)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Algolia HTTP {e.code}: {e.read()[:200]!r}") from e
        results = resp.get("results", [])
        if not results:
            break
        first = results[0]
        hits = first.get("hits", [])
        for h in hits:
            yield _normalize(h)
        nb_pages = first.get("nbPages", 0)
        page += 1
        if page >= nb_pages:
            break
        # Be polite even though Algolia is fine with bursts
        time.sleep(0.2)


def sync() -> tuple[int, int]:
    """Run a full Michelin sync. Returns (added, updated)."""
    added = updated = 0
    with db.connect() as conn:
        try:
            for place in fetch_all():
                if not place.get("name"):
                    continue
                was_new, _ = db.upsert_place(conn, place)
                if was_new:
                    added += 1
                else:
                    updated += 1
            db.record_sync(conn, "michelin", "ok", "", added, updated)
        except Exception as e:
            db.record_sync(conn, "michelin", "error", str(e)[:500], added, updated)
            raise
    return added, updated


if __name__ == "__main__":
    a, u = sync()
    print(f"michelin: {a} new, {u} updated")
