"""Sync Wirtshauskultur Niederösterreich (wirtshauskultur.at) — Austrian tavern guide.

Curated guide to ~179 traditional taverns (Wirtshäuser) in Lower Austria.
Each detail page has rich JSON-LD Schema.org Restaurant data.

Approach:
1. Fetch sitemap.xml?sitemap=gastronomy for all restaurant URLs
2. For each: extract JSON-LD (Schema.org Restaurant)
3. Save into nomnom database with source=wirtshauskultur
4. Deduplicate against existing DB
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import dedup  # noqa: E402
import httputil  # noqa: E402
import sitemap  # noqa: E402

BASE = "https://www.wirtshauskultur.at"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
REQUEST_DELAY = 0.5
USER_AGENT = "Mozilla/5.0 (compatible; NomnomBot/1.0)"

SOURCE_NAME = "wirtshauskultur"
DEDUP_TAG = "wirtshauskultur"


# ── Sitemap ──────────────────────────────────────────────────


def fetch_sitemap_urls() -> list[str]:
    """Return all gastronomie URLs from the sitemap.

    The sitemap index lists a child sitemap whose name contains
    "gastronomy"; we filter that to /gastronomie/ paths.
    """
    # sitemap.iter_urls returns everything from all child sitemaps,
    # but we need to find the "gastronomy" child first. Use iter_urls
    # with a filter that keeps /gastronomie/ paths.
    return sitemap.iter_urls(
        SITEMAP_INDEX,
        path_filter=lambda u: "/gastronomie/" in u,
        timeout=20,
        user_agent=USER_AGENT,
    )


# ── Detail page ──────────────────────────────────────────────


# Schema.org types we consider as places (not just Restaurant)
PLACE_TYPES = {"Restaurant", "BedAndBreakfast", "LodgingBusiness", "LocalBusiness", "FoodEstablishment"}


def extract_json_ld_restaurant(html: str) -> dict | None:
    """Extract the Schema.org place JSON-LD from a Wirtshauskultur page."""
    for m in re.finditer(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue

        if isinstance(data, dict) and "mainEntity" in data:
            entities = data["mainEntity"]
            if isinstance(entities, dict):
                entities = [entities]
            for entity in entities:
                if isinstance(entity, dict) and entity.get("@type") in PLACE_TYPES:
                    return entity

        if isinstance(data, dict) and data.get("@type") in PLACE_TYPES:
            return data

    return None


def parse_restaurant(url: str, html: str) -> dict | None:
    """Parse a Wirtshauskultur restaurant page."""
    data = extract_json_ld_restaurant(html)
    if not data:
        return None

    name = data.get("name", "").strip()
    if not name:
        return None

    address = data.get("address", {})
    geo = data.get("geo", {})

    identifier = data.get("identifier")
    slug = url.rstrip("/").split("/")[-1]
    source_id = str(identifier) if identifier else slug

    place = {
        "source": SOURCE_NAME,
        "source_id": source_id,
        "source_url": url,
        "name": name,
        "category": "restaurant",
        "address": address.get("streetAddress", "").strip() if isinstance(address, dict) else "",
        "city": address.get("addressLocality", "").strip() if isinstance(address, dict) else "",
        "region": "Niederösterreich",
        "country": address.get("addressCountry", "Österreich").strip() if isinstance(address, dict) else "Österreich",
        "lat": None,
        "lng": None,
        "description": data.get("description", "").strip() or None,
        "telephone": address.get("telephone", "").strip() if isinstance(address, dict) else None,
        "tags": ["wirtshauskultur", "niederösterreich"],
        "raw_json": data,
    }

    if place["country"] in ("Österreich", "Austria", "AT"):
        place["country"] = "Austria"

    try:
        if isinstance(geo, dict):
            if geo.get("latitude"):
                place["lat"] = float(geo["latitude"])
            if geo.get("longitude"):
                place["lng"] = float(geo["longitude"])
    except (ValueError, TypeError):
        pass

    amenities = data.get("amenityFeature", [])
    if isinstance(amenities, list):
        amenity_tags = []
        for feat in amenities:
            if isinstance(feat, dict):
                feat_name = feat.get("name", "")
                if feat_name:
                    clean = re.sub(r"^(Allgemein|Service):\s*", "", feat_name).strip()
                    amenity_tags.append(clean)
        if amenity_tags:
            place["tags"] = place.get("tags", []) + amenity_tags

    return place


# ── Main sync ────────────────────────────────────────────────


def sync(max_urls: int | None = None, verbose: bool = True):
    urls = fetch_sitemap_urls()
    print(f"Found {len(urls)} restaurant URLs from sitemap")

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
            if verbose and i % 10 == 0:
                print(f"  [{i}/{len(urls)}] added={added} updated={updated} skipped={skipped} errors={errors}")

            try:
                html = httputil.fetch_page(url, user_agent=USER_AGENT)
            except Exception as e:
                print(f"  HTTP error {url}: {e}")
                errors += 1
                continue

            place = parse_restaurant(url, html)
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
                        print(f"  → dedup: added tag to existing id={existing_id}")
                else:
                    skipped += 1
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
                print(f"  DB error: {e}")
                errors += 1
                err_msg = str(e)

            dedup_map[key] = [-1]
            time.sleep(REQUEST_DELAY)

        db.record_sync(
            conn,
            SOURCE_NAME,
            "ok" if errors == 0 else "error",
            err_msg or (f"{errors} errors" if errors else ""),
            rows_added=added,
            rows_updated=updated,
        )

    print(f"\nDone. Added: {added}, Updated (tag): {updated}, Skipped (dedup): {skipped}, Errors: {errors}")
    return added, updated


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sync Wirtshauskultur restaurants")
    p.add_argument("--max-urls", type=int, default=None, help="Only process N URLs")
    args = p.parse_args()
    sync(max_urls=args.max_urls)