"""Sync Gault & Millau Austria (gaultmillau.at) — restaurant guide.

Approach:
1. Fetch providers.xml sitemap
2. Filter for /restaurant/* URLs
3. For each: extract JSON-LD (Schema.org Restaurant)
4. Save into nomnom database with source=gaultmillau
5. Deduplicate against existing DB — add tag if duplicate
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import dedup  # noqa: E402
import httputil  # noqa: E402
import sitemap  # noqa: E402

BASE = "https://www.gaultmillau.at"
SITEMAP_URL = f"{BASE}/providers.xml"
REQUEST_DELAY = 0.4  # seconds between requests
USER_AGENT = "Mozilla/5.0 (compatible; NomnomBot/1.0)"

SOURCE_NAME = "gaultmillau"
DEDUP_TAG = "gaultmillau"


# ── Sitemap ──────────────────────────────────────────────────


def fetch_sitemap(timeout: int = 20) -> list[str]:
    """Return all restaurant URLs from the sitemap."""
    return sitemap.iter_urls(
        SITEMAP_URL,
        path_filter=lambda u: "/restaurant/" in u and not u.endswith("/restaurant"),
        timeout=timeout,
        user_agent=USER_AGENT,
    )


# ── Detail page ──────────────────────────────────────────────


def extract_json_ld(html: str) -> dict | None:
    """Extract the first Schema.org Restaurant JSON-LD block."""
    pattern = r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>'
    for match in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, dict) and data.get("@type") == "Restaurant":
                return data
        except json.JSONDecodeError:
            continue
    return None


def parse_restaurant_detail(url: str) -> dict | None:
    """Scrape a single restaurant detail page; return place dict or None."""
    try:
        html = httputil.fetch_page(url, user_agent=USER_AGENT)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"  HTTP error fetching {url}: {e}")
        return None

    data = extract_json_ld(html)
    if not data:
        return None

    address = data.get("address", {})
    geo = data.get("geo", {})
    review = data.get("review", {}).get("reviewRating", {}) if isinstance(data.get("review"), dict) else {}
    rating_value = review.get("ratingValue") if review else None
    best_rating = review.get("bestRating") if review else None

    name = data.get("name", "").strip()
    if not name:
        return None

    slug = url.rstrip("/").split("/")[-1]
    place = {
        "source": SOURCE_NAME,
        "source_id": slug,
        "source_url": url,
        "name": name,
        "category": "restaurant",
        "address": address.get("streetAddress", "").strip(),
        "city": address.get("addressLocality", "").strip(),
        "region": address.get("addressRegion", "").strip(),
        "country": address.get("addressCountry", "").strip(),
        "lat": None,
        "lng": None,
        "description": None,
        "telephone": data.get("telephone", "").strip() or None,
        "rating_value": rating_value,
        "rating_scale": best_rating,
        "raw_json": data,
    }

    try:
        if geo.get("latitude"):
            place["lat"] = float(geo["latitude"])
        if geo.get("longitude"):
            place["lng"] = float(geo["longitude"])
    except (ValueError, TypeError):
        pass

    return place


# ── Main sync ────────────────────────────────────────────────


def sync(max_urls: int | None = None, max_workers: int = 1, verbose: bool = True):
    urls = fetch_sitemap()
    print(f"Found {len(urls)} restaurant URLs in sitemap")

    if max_urls:
        urls = urls[:max_urls]
        print(f"Limited to {len(urls)} URLs")

    added = 0
    updated = 0
    skipped = 0
    errors = 0
    err_msg = ""

    with db.connect() as conn:
        dedup_map = dedup.build_dedup_map(conn)

        for i, url in enumerate(urls, 1):
            if verbose and i % 20 == 0:
                print(f"  [{i}/{len(urls)}] added={added} updated={updated} skipped={skipped} errors={errors}")

            try:
                place = parse_restaurant_detail(url)
            except Exception as e:
                print(f"  Error parsing {url}: {e}")
                errors += 1
                continue

            if not place:
                errors += 1
                continue

            key = dedup.dup_key(place)
            if key in dedup_map and dedup_map[key]:
                existing_id = dedup_map[key][0]
                if dedup.add_tag(conn, existing_id, DEDUP_TAG):
                    conn.commit()
                    updated += 1
                    if verbose:
                        print(f"  → dedup: added tag to existing place id={existing_id}")
                else:
                    skipped += 1
                    if verbose:
                        print(f"  → dedup: already tagged")
                continue

            try:
                was_new, _pid = db.upsert_place(conn, place)
                conn.commit()
                if was_new:
                    added += 1
                    if verbose:
                        print(f"  + added: {place['name']} ({place['city']}, {place['country']})")
                else:
                    updated += 1
            except Exception as e:
                print(f"  DB error inserting {place['name']}: {e}")
                errors += 1
                err_msg = str(e)

            dedup_map[key] = [-1]  # Mark as processed
            time.sleep(REQUEST_DELAY)

        db.record_sync(
            conn,
            SOURCE_NAME,
            "ok" if errors == 0 else "error",
            err_msg or f"{errors} errors" if errors else "",
            rows_added=added,
            rows_updated=updated,
        )

    print(f"\nDone. Added: {added}, Updated (tag): {updated}, Skipped (dedup): {skipped}, Errors: {errors}")
    return added, updated


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sync Gault & Millau Austria restaurants")
    p.add_argument("--max-urls", type=int, default=None, help="Only process N URLs")
    p.add_argument("--workers", type=int, default=1, help="Parallel workers (NYI)")
    args = p.parse_args()

    sync(max_urls=args.max_urls, max_workers=args.workers)