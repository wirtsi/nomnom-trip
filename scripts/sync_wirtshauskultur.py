"""Sync Wirtshauskultur Niederösterreich (wirtshauskultur.at) — Austrian tavern guide.

Wirtshauskultur is a curated guide to ~179 traditional taverns (Wirtshäuser)
in Lower Austria (Niederösterreich). Each detail page has rich JSON-LD
Schema.org Restaurant data including:
- name, address, city, postal code, country
- lat/lng, telephone, email
- description, opening hours, amenities
- identifier (imxplatform addressbase ID)

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

BASE = "https://www.wirtshauskultur.at"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "restaurants.db"
REQUEST_DELAY = 0.5

SOURCE_NAME = "wirtshauskultur"
DEDUP_TAG = "wirtshauskultur"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NomnomBot/1.0)"}


# ── Sitemap ──────────────────────────────────────────────────


def fetch_sitemap_urls() -> list[str]:
    """Return all gastronomie URLs from the sitemap."""
    req = urllib.request.Request(SITEMAP_INDEX, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        index_root = ET.fromstring(r.read())

    gastro_url = None
    for loc in index_root.findall(".//sm:loc", NS):
        if loc.text and "gastronomy" in (loc.text or "").lower():
            gastro_url = loc.text.strip()
            break

    if not gastro_url:
        raise RuntimeError("Gastronomy sitemap not found in sitemap index")

    req = urllib.request.Request(gastro_url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        gastro_root = ET.fromstring(r.read())

    urls = []
    for loc in gastro_root.findall(".//sm:loc", NS):
        if loc.text and "/gastronomie/" in loc.text:
            urls.append(loc.text.strip())

    return urls


# ── Detail page ──────────────────────────────────────────────


def fetch_page(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


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

        # Check for mainEntity containing a place type
        if isinstance(data, dict) and "mainEntity" in data:
            entities = data["mainEntity"]
            if isinstance(entities, dict):
                entities = [entities]
            for entity in entities:
                if isinstance(entity, dict) and entity.get("@type") in PLACE_TYPES:
                    return entity

        # Direct place type
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


# ── Dedup helpers ────────────────────────────────────────────


def normalize_name(name: str) -> str:
    import unicodedata
    name = name.lower().strip()
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def build_dedup_map(conn: sqlite3.Connection) -> dict:
    cursor = conn.execute("SELECT id, name, city, country FROM places")
    dup_map: dict = {}
    for row in cursor.fetchall():
        key = (
            normalize_name(row["name"]),
            (row["city"] or "").lower().strip(),
            (row["country"] or "").lower().strip(),
        )
        dup_map.setdefault(key, []).append(row["id"])
    return dup_map


# ── Main sync ────────────────────────────────────────────────


def sync(max_urls: int | None = None, verbose: bool = True):
    urls = fetch_sitemap_urls()
    print(f"Found {len(urls)} restaurant URLs from sitemap")

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
        if verbose and i % 10 == 0:
            print(f"  [{i}/{len(urls)}] added={added} updated={updated} skipped={skipped} errors={errors}")

        try:
            html = fetch_page(url)
        except Exception as e:
            print(f"  HTTP error {url}: {e}")
            errors += 1
            continue

        place = parse_restaurant(url, html)
        if not place:
            errors += 1
            continue

        dup_key = (
            normalize_name(place["name"]),
            (place.get("city") or "").lower(),
            place.get("country", "").lower(),
        )
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
                conn.execute(
                    "UPDATE places SET tags = ? WHERE id = ?",
                    (json.dumps(tags, ensure_ascii=False), existing_id),
                )
                conn.commit()
                updated += 1
                if verbose:
                    print(f"  → dedup: added tag to existing id={existing_id}")
            else:
                skipped += 1
            continue

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
            print(f"  DB error: {e}")
            errors += 1

        dedup_map[dup_key] = [-1]
        time.sleep(REQUEST_DELAY)

    conn.close()
    print(f"\nDone. Added: {added}, Updated (tag): {updated}, Skipped (dedup): {skipped}, Errors: {errors}")
    return added, updated


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sync Wirtshauskultur restaurants")
    p.add_argument("--max-urls", type=int, default=None, help="Only process N URLs")
    args = p.parse_args()
    sync(max_urls=args.max_urls)