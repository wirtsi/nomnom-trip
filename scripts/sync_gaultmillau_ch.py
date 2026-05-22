"""Sync Gault & Millau Switzerland (gaultmillau.ch) — restaurant guide.

GM CH is a React SPA with no Restaurant JSON-LD. Instead, we parse:
- Restaurant name from <h1>
- Description from <meta name="description"> or the article body
- We use the sitemap to find all /restaurants/* URLs

Available data per restaurant (from React app):
- Name (from <h1>)
- URL path contains location info
- Description from sitemap <image:caption> or meta description

Note: Full structured data (address, rating, chef, etc.) is only available
via the React app's GraphQL API which requires auth. We extract what we can
from server-rendered HTML.
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

BASE = "https://www.gaultmillau.ch"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "restaurants.db"
REQUEST_DELAY = 0.5

SOURCE_NAME = "gaultmillau_ch"
DEDUP_TAG = "gaultmillau-ch"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NomnomBot/1.0)"}


# ── Sitemap ──────────────────────────────────────────────────


def fetch_sitemap_urls() -> list[str]:
    """Collect all /restaurants/* URLs from GM CH monthly sitemaps."""
    req = urllib.request.Request(SITEMAP_INDEX, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        index_root = ET.fromstring(r.read())

    # Get all monthly sitemap URLs
    sitemap_urls = [
        loc.text.strip()
        for loc in index_root.findall(".//sm:loc", NS)
        if loc.text
    ]

    all_restaurants = set()
    for sitemap_url in sitemap_urls:
        try:
            req = urllib.request.Request(sitemap_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                root = ET.fromstring(r.read())
            for loc in root.findall(".//sm:loc", NS):
                if loc.text and "/restaurants/" in loc.text:
                    all_restaurants.add(loc.text.strip())
        except Exception:
            continue

    return sorted(all_restaurants)


# ── Detail page ──────────────────────────────────────────────


def fetch_page(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_restaurant(url: str, html: str) -> dict | None:
    """Parse a GM CH restaurant detail page (React SPA)."""
    # Name from <h1>
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
    name = h1_match.group(1).strip() if h1_match else ""
    # Clean HTML entities
    name = name.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    name = re.sub(r"<[^>]+>", "", name).strip()
    if not name:
        return None

    # Description from meta description
    desc_match = re.search(
        r'<meta\s+name="description"\s+content="([^"]*)"',
        html, re.IGNORECASE
    )
    description = desc_match.group(1).strip() if desc_match else None

    # Try to get structured data from articleId in the page's JSON-LD
    # GM CH uses @type WebPage in JSON-LD with description
    article_id = None
    for m in re.finditer(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(m.group(1).strip())
            if isinstance(data, dict):
                if data.get("articleId", "").startswith("bm9kZTo"):
                    article_id = data["articleId"]
                # Also grab description if not found in meta
                if not description and data.get("description"):
                    description = data["description"][:500]
        except (json.JSONDecodeError, AttributeError):
            continue

    # Extract address info from the articleId endpoint or page content
    # GM CH's React app loads all data dynamically, so we can only get
    # the name and description from server-rendered HTML.
    # For a more complete dataset, we'd need the GraphQL API.

    # Generate source_id from URL slug
    slug = url.rstrip("/").split("/")[-1]
    source_id = slug

    place = {
        "source": SOURCE_NAME,
        "source_id": source_id,
        "source_url": url,
        "name": name,
        "category": "Restaurant",
        "country": "Switzerland",  # GM CH covers Switzerland
        "description": description,
    }

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
        key = (normalize_name(row["name"]), (row["city"] or "").lower().strip(), (row["country"] or "").lower().strip())
        dup_map.setdefault(key, []).append(row["id"])
    return dup_map


# ── Main sync ────────────────────────────────────────────────


def sync(max_urls: int | None = None, verbose: bool = True):
    urls = fetch_sitemap_urls()
    print(f"Found {len(urls)} restaurant URLs from sitemaps")

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
            html = fetch_page(url)
        except Exception as e:
            print(f"  HTTP error {url}: {e}")
            errors += 1
            continue

        place = parse_restaurant(url, html)
        if not place:
            errors += 1
            continue

        # Deduplication
        dup_key = (normalize_name(place["name"]), (place.get("city") or "").lower(), place.get("country", "").lower())
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
                conn.execute("UPDATE places SET tags = ? WHERE id = ?", (json.dumps(tags, ensure_ascii=False), existing_id))
                conn.commit()
                updated += 1
                if verbose:
                    print(f"  → dedup: added tag to existing id={existing_id}")
            else:
                skipped += 1
            continue

        # Insert new place
        try:
            was_new, pid = db.upsert_place(conn, place)
            conn.commit()
            if was_new:
                added += 1
                if verbose:
                    print(f"  + added: {place['name']}")
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
    p = argparse.ArgumentParser(description="Sync Gault & Millau Switzerland restaurants")
    p.add_argument("--max-urls", type=int, default=None, help="Only process N URLs")
    args = p.parse_args()
    sync(max_urls=args.max_urls)