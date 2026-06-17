"""Sync Mitvergnügen (mitvergnuegen.com) — editorial city guides for DE/AT.

Mitvergnügen is a network of city magazines with real editorial curation:
  - Berlin (mitvergnuegen.com)
  - Hamburg (hamburg.mitvergnuegen.com)
  - München (muenchen.mitvergnuegen.com)
  - Köln (koeln.mitvergnuegen.com)

Data per place:
  - name, address, lat/lng, description, opening_hours
  - category tags (Essen, Trinken, etc.)

Strategy:
  1. Fetch WordPress REST API posts filtered by category (Food, Ausgehen)
  2. For each post, extract tip IDs from article HTML
  3. For each tip, fetch /api/v1/tips/{id}/
  4. Parse address and upsert
"""

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

SOURCE_NAME = "mitvergnuegen"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html"}
RE_DELAY = 0.5

CITIES = {
    "berlin": {
        "domain": "mitvergnuegen.com",
        "fallback_city": "Berlin",
        "country": "Germany",
    },
    "hamburg": {
        "domain": "hamburg.mitvergnuegen.com",
        "fallback_city": "Hamburg",
        "country": "Germany",
    },
    "muenchen": {
        "domain": "muenchen.mitvergnuegen.com",
        "fallback_city": "München",
        "country": "Germany",
    },
    "koeln": {
        "domain": "koeln.mitvergnuegen.com",
        "fallback_city": "Köln",
        "country": "Germany",
    },
}


def fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"  [ERROR] {url}: {e}")
        return None


def fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  [ERROR] {url}: {e}")
        return None


def get_wp_posts(domain: str, categories: list[int], per_page: int = 100, page: int = 1) -> list[dict]:
    cats = ",".join(str(c) for c in categories)
    url = f"https://{domain}/wp-json/wp/v2/posts?categories={cats}&per_page={per_page}&page={page}"
    data = fetch_json(url)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    print(f"  [WARN] Unexpected response: {type(data)}")
    return []


def extract_tip_ids_from_html(html: str) -> list[int]:
    ids = set()
    for m in re.finditer(r'id="tip-(\d+)"', html):
        ids.add(int(m.group(1)))
    return sorted(ids)


def fetch_tip_detail(domain: str, tip_id: int) -> Optional[dict]:
    url = f"https://{domain}/api/v1/tips/{tip_id}/"
    data = fetch_json(url, timeout=10)
    if data:
        return data.get("tip")
    return None

def parse_address(address_str: str, city_config: dict) -> tuple[str, str, str, str]:
    """Parse street, postal, city, region from Mitvergnügen address string."""
    if not address_str:
        return "", "", "", ""
    address_str = address_str.strip()
    postal_match = re.search(r"(\d{5})\s+", address_str)
    postal = postal_match.group(1) if postal_match else ""
    city_match = re.search(r"\d{5}\s+([\w\s\-äöüÄÖÜ]+?)(?:\s*$|\s+\d)", address_str)
    city = city_match.group(1).strip() if city_match else city_config.get("fallback_city", "")
    street = address_str
    if postal_match:
        street = address_str[:postal_match.start()].strip().rstrip(",")
    region = city
    if "Brandenburg" in address_str:
        region = "Brandenburg"
    return street, postal, city, region


def tip_to_place(tip_data: dict, city_config: dict) -> Optional[dict]:
    place = tip_data.get("place", {})
    if not place:
        return None
    name = place.get("title", "").strip()
    if not name:
        return None
    name = name.replace("&amp;", "&")
    address_full = place.get("address", "")
    street, postal, city, region = parse_address(address_full, city_config)
    loc = place.get("location") or {}
    lat = loc.get("lat")
    lng = loc.get("lng")
    try:
        lat = float(lat) if lat else None
        lng = float(lng) if lng else None
    except (ValueError, TypeError):
        lat = lng = None
    description = tip_data.get("contents", "") or tip_data.get("excerpt", "")
    if description:
        description = re.sub(r"<[^>]*>", "", description).strip()
    categories = []
    for cat in place.get("categories", []):
        if cat.get("name"):
            categories.append(cat["name"])
    place_url = tip_data.get("url", "")
    return {
        "source": SOURCE_NAME,
        "source_id": f"mv_{tip_data.get('identifier', '')}",
        "source_url": place_url,
        "name": name,
        "address": street,
        "city": city,
        "region": region,
        "postal_code": postal,
        "country": city_config["country"],
        "lat": lat,
        "lng": lng,
        "telephone": "",
        "cuisine": ", ".join(categories) if categories else "",
        "price_range": "",
        "description": description,
        "opening_hours": tip_data.get("opening_hours", ""),
        "rating": None,
        "max_rating": None,
        "website": tip_data.get("website", "") or tip_data.get("place", {}).get("website", ""),
    }


def ingest_mitvergnuegen(*, city: str = "all", max_posts: int = 0, max_tips: int = 0) -> tuple[int, int]:
    total_added = total_updated = 0

    with db.connect() as conn:
        cities_to_scrape = list(CITIES.items()) if city == "all" else [(city, CITIES[city])]
        for city_slug, city_cfg in cities_to_scrape:
            domain = city_cfg["domain"]
            print(f"\n=== {city_slug.upper()}: {domain} ===")
            cats_url = f"https://{domain}/wp-json/wp/v2/categories?per_page=100"
            cats_data = fetch_json(cats_url)
            food_cat_ids = []
            if cats_data:
                for cat in cats_data:
                    if cat.get("slug") in ("food", "ausgehen", "erlebnis"):
                        food_cat_ids.append(cat["id"])
            if not food_cat_ids:
                print(f"  No categories found for {city_slug}")
                continue
            print(f"  Category IDs: {food_cat_ids}")
            all_posts = []
            page = 1
            while True:
                posts = get_wp_posts(domain, food_cat_ids, per_page=100, page=page)
                if not posts:
                    break
                all_posts.extend(posts)
                print(f"  Page {page}: +{len(posts)} posts (total {len(all_posts)})")
                if max_posts and len(all_posts) >= max_posts:
                    all_posts = all_posts[:max_posts]
                    break
                page += 1
                time.sleep(RE_DELAY)
            print(f"  Total posts to process: {len(all_posts)}")
            tip_ids = set()
            post_count = 0
            for post in all_posts:
                link = post.get("link", "")
                if not link:
                    continue
                html = fetch_html(link)
                if html:
                    ids = extract_tip_ids_from_html(html)
                    tip_ids.update(ids)
                    post_count += 1
                    if post_count % 5 == 0:
                        print(f"  [{post_count}/{len(all_posts)}] {len(ids)} tips from post (total unique: {len(tip_ids)})")
                    time.sleep(RE_DELAY)
                if max_tips and len(tip_ids) >= max_tips:
                    tip_ids = set(sorted(tip_ids)[:max_tips])
                    break
            print(f"  Total unique tips: {len(tip_ids)}")
            processed = 0
            for tip_id in sorted(tip_ids):
                data = fetch_tip_detail(domain, tip_id)
                if not data:
                    continue
                place = tip_to_place(data, city_cfg)
                if not place:
                    continue
                is_new, _ = db.upsert_place(conn, place)
                if is_new:
                    total_added += 1
                else:
                    total_updated += 1
                processed += 1
                if processed % 10 == 0:
                    print(f"  [{processed}/{len(tip_ids)}] added={total_added} updated={total_updated}")
                if processed % 50 == 0:
                    conn.commit()
                time.sleep(RE_DELAY)
        conn.commit()
        db.record_sync(
            conn,
            "mitvergnuegen",
            "ok",
            "",
            rows_added=total_added,
            rows_updated=total_updated,
        )
    print(f"\n✓ Done: +{total_added} new, {total_updated} updated")
    return total_added, total_updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", choices=["all"] + list(CITIES.keys()), default="all")
    ap.add_argument("--max-posts", type=int, default=0, help="Max WordPress posts to fetch")
    ap.add_argument("--max-tips", type=int, default=0, help="Max tip IDs to extract")
    args = ap.parse_args()
    try:
        ingest_mitvergnuegen(
            city=args.city,
            max_posts=args.max_posts,
            max_tips=args.max_tips,
        )
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
