"""Sync Schlemmer Atlas (schlemmer-atlas.de) — German restaurant guide.

Coverage: DE, AT, CH, FR (Elsass), IT
Data per listing page (JSON-LD ItemList): name, address, city, postal code,
region, country, phone, email, cuisine, opening hours, url, image.
Lat/lng from HTML data attributes.

Optional: visit detail pages for rating (Kochlöffel 1-5), price range, features.

Approach:
1. For each country, fetch first page → extract max page count from pagination
2. Loop through all pages, extract JSON-LD + lat/lng from each
3. Deduplicate by name+city+country and save
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

BASE = "https://www.schlemmer-atlas.de"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "restaurants.db"
REQUEST_DELAY = 0.5

SOURCE_NAME = "schlemmeratlas"
DEDUP_TAG = "schlemmeratlas"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NomnomBot/1.0)"}

COUNTRIES = {
    "deutschland": "Germany",
    "oesterreich": "Austria",
    "schweiz": "Switzerland",
    "frankreich": "France",
    "italien": "Italy",
}

# Countries we actually want (user wants DACH + maybe FR/IT)
ACTIVE_COUNTRIES = ["deutschland", "oesterreich", "schweiz", "frankreich", "italien"]


def fetch_page(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_max_page(html: str) -> int:
    """Extract the maximum page number from pagination HTML."""
    pages = re.findall(r'href="/[^"]*\?p=([0-9]+)"', html)
    if pages:
        return max(int(p) for p in pages)
    # Fallback: look for href="/restaurants/xxx/?p=N""
    pages = re.findall(r'\?p=([0-9]+)', html)
    if pages:
        return max(int(p) for p in pages)
    return 1


def listing_urls_for_country(country_slug: str, max_pages: int | None = None) -> list[str]:
    """Generate listing page URLs for a country."""
    base = f"{BASE}/restaurants/{country_slug}/"
    urls = [base]
    # Fetch first page to get max pages
    try:
        html = fetch_page(base)
        total_pages = extract_max_page(html)
        if max_pages:
            total_pages = min(total_pages, max_pages)
        for p in range(2, total_pages + 1):
            urls.append(f"{base}?p={p}")
    except Exception as e:
        print(f"Warning: could not get page count for {country_slug}: {e}")
    return urls


def parse_listing_page(html: str) -> list[dict]:
    """Extract all restaurants from a listing page.

    Strategy:
    1. Build a map of URL → {lat, lng} from HTML li data attributes.
    2. Extract restaurant data from JSON-LD ItemList.
    3. Merge coordinates from HTML into JSON-LD items by matching URL.
    """
    # Step 1: Extract HTML coordinates keyed by URL
    url_coords = {}
    for m in re.finditer(
        r'<li\s+[^\u003e]*data-item-id=\"(\d+)\"[^\u003e]*data-longitude=\"([\d.]+)\"[^\u003e]*data-latitude=\"([\d.]+)\"[^\u003e]*>',
        html, re.IGNORECASE
    ):
        item_id = m.group(1)
        lng = float(m.group(2))
        lat = float(m.group(3))
        # Find the URL for this item by looking at the <a> tag inside the li
        # The li element extends until the next </li> (simple approach)
        li_start = m.start()
        li_end = html.find('</li>', li_start) + len('</li>')
        li_html = html[li_start:li_end]
        url_match = re.search(r'href=\"(/restaurants/[^\"]+)\"', li_html)
        if url_match:
            url = url_match.group(1)
            url_coords[url] = {"lat": lat, "lng": lng}

    # Also try with different attribute order
    for m in re.finditer(
        r'<li\s+[^\u003e]*[^\u003e]*\>data-item-id=\"(\d+)\"',
        html, re.IGNORECASE
    ):
        pass  # Already handled above, simplified regex is sufficient below

    # Step 2: Extract JSON-LD ItemList data
    restaurants = []
    for script_match in re.finditer(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)\u003c/script\u003e',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(script_match.group(1).strip())
        except json.JSONDecodeError:
            continue

        if isinstance(data, dict) and data.get("@type") == "ItemList":
            for list_item in data.get("itemListElement", []):
                item = list_item.get("item", {}) if isinstance(list_item, dict) else {}
                if not isinstance(item, dict):
                    continue
                if item.get("@type") != "Restaurant":
                    continue

                addr = item.get("address", {})
                if not isinstance(addr, dict):
                    continue

                country = COUNTRIES.get(
                    addr.get("addressCountry", "").lower().strip(),
                    addr.get("addressCountry", "").strip()
                )

                url = item.get("url", "")
                path = urllib.parse.urlparse(url).path if url else ""
                coords = url_coords.get(path)

                place = {
                    "source": SOURCE_NAME,
                    "source_id": path.rstrip("/").split("/")[-1] if path else str(list_item.get("position", 0)),
                    "source_url": url,
                    "name": item.get("name", "").strip(),
                    "category": "Restaurant",
                    "address": addr.get("streetAddress", "").strip(),
                    "city": addr.get("addressLocality", "").strip(),
                    "postal_code": addr.get("postalCode", "").strip() or None,
                    "region": addr.get("addressRegion", "").strip() or None,
                    "country": country,
                    "lat": coords["lat"] if coords else None,
                    "lng": coords["lng"] if coords else None,
                    "cuisine": item.get("servesCuisine", "").strip() or None,
                    "tags": ["schlemmeratlas"],
                }
                restaurants.append(place)

    # Log how many got coordinates
    with_coords = sum(1 for r in restaurants if r["lat"] is not None)
    if with_coords != len(restaurants):
        diff = len(restaurants) - with_coords
    return restaurants

    return restaurants


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


def sync(max_pages: int | None = None, max_urls: int | None = None, verbose: bool = True):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    dedup_map = build_dedup_map(conn)

    added = 0
    updated = 0
    skipped = 0
    errors = 0
    total_processed = 0

    for country_slug in ACTIVE_COUNTRIES:
        urls = listing_urls_for_country(country_slug, max_pages=max_pages)
        print(f"\n{country_slug}: {len(urls)} pages")

        for page_num, url in enumerate(urls, 1):
            if verbose and page_num % 50 == 0:
                print(f"  page {page_num}/{len(urls)}...")
                
            try:
                html = fetch_page(url)
                restaurants = parse_listing_page(html)
            except Exception as e:
                if verbose:
                    print(f"  HTTP error {url}: {e}")
                errors += 1
                continue

            for place in restaurants:
                if not place["name"]:
                    continue
                if max_urls and total_processed >= max_urls:
                    break

                total_processed += 1

                # Deduplication
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
                    if verbose:
                        print(f"  DB error: {e}")
                    errors += 1

                dedup_map[dup_key] = [-1]

            if max_urls and total_processed >= max_urls:
                break

            time.sleep(REQUEST_DELAY)

        if max_urls and total_processed >= max_urls:
            break

    conn.close()
    print(f"\nDone. Added: {added}, Updated (tag): {updated}, Skipped (dedup): {skipped}, Errors: {errors}, Total: {total_processed}")
    return added, updated


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sync Schlemmer Atlas restaurants")
    p.add_argument("--max-pages", type=int, default=None, help="Max pages per country")
    p.add_argument("--max-urls", type=int, default=None, help="Max restaurants total")
    args = p.parse_args()
    sync(max_pages=args.max_pages, max_urls=args.max_urls)