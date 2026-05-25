"""Sync Gault & Millau Austria (gaultmillau.at) — restaurant guide.

Approach:
1. Fetch providers.xml sitemap
2. Filter for /restaurant/* URLs
3. For each: extract JSON-LD (Schema.org Restaurant)
4. Save into nomnom database with source=gaultmillau_at
5. Deduplicate against existing DB — add tag if duplicate

Scraped fields:
- name, address, city, region, postal_code, country
- lat, lng, telephone
- cuisine (if present), description (GM review text)
- rating_value (hats 1-5), rating_scale (5)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

BASE = "https://www.gaultmillau.at"
SITEMAP_URL = f"{BASE}/providers.xml"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "restaurants.db"
REQUEST_DELAY = 0.4  # seconds between requests
NAMESPACES = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

SOURCE_NAME = "gaultmillau"
DEDUP_TAG = "gaultmillau"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NomnomBot/1.0)"
}

# ── Sitemap ──────────────────────────────────────────────────


def fetch_sitemap(timeout: int = 20) -> list[str]:
    """Return all restaurant URLs from the sitemap."""
    req = urllib.request.Request(SITEMAP_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        root = ET.fromstring(response.read())

    restaurant_urls = []
    for loc in root.findall(".//sm:loc", NAMESPACES):
        if loc.text is not None:
            text = loc.text.strip()
            if "/restaurant/" in text and not text.endswith("/restaurant"):
                restaurant_urls.append(text)

    return restaurant_urls


# ── Detail page ──────────────────────────────────────────────


def fetch_page(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


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
        html = fetch_page(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"  HTTP error fetching {url}: {e}")
        return None

    data = extract_json_ld(html)
    if not data:
        # Some pages might not have JSON-LD (rare); try to skip gracefully
        return None

    address = data.get("address", {})
    geo = data.get("geo", {})
    review = data.get("review", {}).get("reviewRating", {}) if isinstance(data.get("review"), dict) else {}
    # Review rating might be top-level or nested
    rating_value = None
    best_rating = None
    if review:
        rating_value = review.get("ratingValue")
        best_rating = review.get("bestRating")

    name = data.get("name", "").strip()
    if not name:
        return None

    # Generate source_id from URL slug
    slug = url.rstrip("/").split("/")[-1]
    source_id = slug

    place = {
        "source": SOURCE_NAME,
        "source_id": source_id,
        "source_url": url,
        "name": name,
        "category": "Restaurant",
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

    # Convert lat/lng
    try:
        if geo.get("latitude"):
            place["lat"] = float(geo["latitude"])
        if geo.get("longitude"):
            place["lng"] = float(geo["longitude"])
    except (ValueError, TypeError):
        pass

    return place


# ── Dedup helpers (same pattern as identitagolose) ────────────


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove non-alphanumeric."""
    import unicodedata

    name = name.lower().strip()
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def build_dedup_map(conn: sqlite3.Connection) -> dict:
    """Return dict {(norm_name, city, country) -> [place_id, ...]}."""
    cursor = conn.execute(
        "SELECT id, name, city, country FROM places"
    )
    dup_map: dict = {}
    for row in cursor.fetchall():
        key = (normalize_name(row["name"]), (row["city"] or "").lower().strip(), (row["country"] or "").lower().strip())
        dup_map.setdefault(key, []).append(row["id"])
    return dup_map


# ── Main sync ────────────────────────────────────────────────


def sync(max_urls: int | None = None, max_workers: int = 1, verbose: bool = True):
    urls = fetch_sitemap()
    print(f"Found {len(urls)} restaurant URLs in sitemap")

    if max_urls:
        urls = urls[:max_urls]
        print(f"Limited to {len(urls)} URLs")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    dedup_map = build_dedup_map(conn)

    added = 0
    updated = 0
    skipped = 0
    errors = 0

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

        # Deduplication
        dup_key = (normalize_name(place["name"]), place["city"].lower(), place["country"].lower())
        if dup_key in dedup_map and dedup_map[dup_key]:
            existing_id = dedup_map[dup_key][0]
            cursor = conn.execute("SELECT tags FROM places WHERE id = ?", (existing_id,))
            row = cursor.fetchone()
            tags = []
            if row and row["tags"]:
                try:
                    tags = json.loads(row["tags"])
                except json.JSONDecodeError:
                    pass
            if DEDUP_TAG not in tags:
                tags.append(DEDUP_TAG)
                conn.execute("UPDATE places SET tags = ? WHERE id = ?",
                             (json.dumps(tags, ensure_ascii=False), existing_id))
                conn.commit()
                updated += 1
                if verbose:
                    print(f"  → dedup: added tag to existing place id={existing_id}")
            else:
                skipped += 1
                if verbose:
                    print(f"  → dedup: already tagged")
            continue

        # Insert new place
        try:
            was_new, pid = db.upsert_place(conn, place)
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

        dedup_map[dup_key] = [-1]  # Mark as processed

        time.sleep(REQUEST_DELAY)

    conn.close()
    print(f"\nDone. Added: {added}, Updated (tag): {updated}, Skipped (dedup): {skipped}, Errors: {errors}")
    return added, updated


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sync Gault & Millau Austria restaurants")
    p.add_argument("--max-urls", type=int, default=None, help="Only process N URLs")
    p.add_argument("--workers", type=int, default=1, help="Parallel workers (NYI)")
    args = p.parse_args()

    sync(max_urls=args.max_urls, max_workers=args.workers)
