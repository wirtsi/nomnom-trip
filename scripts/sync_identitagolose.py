"""Sync Identità Golose (identitagolose.it) — curated restaurant guide.

Identità Golose is Italy's premier food magazine and restaurant guide,
run by Paolo Marchi. It features ~990 restaurants worldwide with
editorial reviews, chef profiles, and structured data.

Source: https://www.identitagolose.it/_xml_guida.php (sitemap)
       https://www.identitagolose.it/_xml_guida_pec.php (pizzerie/cocktailbars)

Scraping approach:
1. Parse sitemap XML for all restaurant URLs
2. Per-page HTML scraping with BeautifulSoup
3. Extract: name, chef, address, city, region, country, lat/lng, phone,
   description, awards, menu prices
4. Fuzzy dedup against existing DB places
5. Insert into nomnom database
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
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import dedup  # noqa: E402

BASE = "https://www.identitagolose.it"
GUIDE_SITEMAP = f"{BASE}/_xml_guida.php"
PEC_SITEMAP = f"{BASE}/_xml_guida_pec.php"

# Italian country names to English mapping
COUNTRY_MAP = {
    "thailandia": "Thailand",
    "francia": "France",
    "germania": "Germany",
    "svizzera": "Switzerland",
    "austria": "Austria",
    "spagna": "Spain",
    "belgio": "Belgium",
    "paesi bassi": "Netherlands",
    "gran bretagna": "United Kingdom",
    "regno unito": "United Kingdom",
    "stati uniti": "United States",
    "giappone": "Japan",
    "cina": "China",
    "brasile": "Brazil",
    "argentina": "Argentina",
    "australia": "Australia",
    "canada": "Canada",
    "messico": "Mexico",
    "perù": "Peru",
    "singapore": "Singapore",
    "corea del sud": "South Korea",
    "india": "India",
    "indonesia": "Indonesia",
    "maleisia": "Malaysia",
    "vietnam": "Vietnam",
    "portogallo": "Portugal",
    "grecia": "Greece",
    "croazia": "Croatia",
    "danimarca": "Denmark",
    "finlandia": "Finland",
    "norvegia": "Norway",
    "svezia": "Sweden",
    "polonia": "Poland",
    "repubblica ceca": "Czech Republic",
    "romania": "Romania",
    "serbia": "Serbia",
    "slovenia": "Slovenia",
    "turchia": "Turkey",
    "ungheria": "Hungary",
    "bulgaria": "Bulgaria",
    "albania": "Albania",
    "monaco": "Monaco",
    "emirati arabi uniti": "United Arab Emirates",
    "bahrain": "Bahrain",
    "cile": "Chile",
    "colombia": "Colombia",
    "ecuador": "Ecuador",
    "marocco": "Morocco",
    "senegal": "Senegal",
}

# Italian regions (for detecting if h5 first part is a region or country)
ITALIAN_REGIONS = {
    "abruzzo", "basilicata", "calabria", "campania", "emilia romagna",
    "emilia-romagna", "friuli venezia giulia", "friuli-venezia giulia",
    "lazio", "liguria", "lombardia", "marche", "molise", "piemonte",
    "puglia", "sardegna", "sicilia", "toscana", "trentino alto adige",
    "trentino-alto adige", "trentino-alto adige/südtirol",
    "umbria", "valle d'aosta", "valle d'aosta/vallée d'aoste",
    "veneto",
}

class MLStripper(HTMLParser):
    """Strip HTML tags from text."""
    def __init__(self):
        super().__init__()
        self.reset()
        self.fed: list[str] = []

    def handle_data(self, data: str) -> None:
        self.fed.append(data)

    def get_data(self) -> str:
        return "".join(self.fed).strip()


def strip_tags(html: str) -> str:
    s = MLStripper()
    s.feed(html)
    return s.get_data()


def fetch(url: str, retries: int = 3, timeout: int = 30) -> Optional[str]:
    """Fetch URL content with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; nomnom-bot/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (503, 502, 504, 429):
                wait = 2 ** attempt
                print(f"    HTTP {e.code} — retry in {wait}s ({attempt+1}/{retries})")
                time.sleep(wait)
                continue
            print(f"    HTTP error {e.code}: {url}")
            return None
        except Exception as e:
            wait = 2 ** attempt
            print(f"    Error: {e} — retry in {wait}s")
            time.sleep(wait)
    return None


def normalize_name(name: str) -> str:
    """Normalize for fuzzy dedup comparison."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def extract_detail(html: str, url: str) -> dict | None:
    """Extract structured data from an Identità Golose detail page."""
    
    # Extract name from h1
    name_match = re.search(r'<h1[^\u003e]*class="h1[^"]*"[^\u003e]*>(.*?)</h1\u003e', html, re.S | re.I)
    if not name_match:
        # Try more generic h1
        name_match = re.search(r'<h1[^\u003e]*>(.*?)</h1\u003e', html, re.S | re.I)
    if not name_match:
        print(f"    No h1 title found")
        return None
    name = strip_tags(name_match.group(1)).strip()
    if not name or name == "Scheda non trovata":
        return None

    # Extract meta description
    desc_match = re.search(r'<meta name="description" content="([^"]*)"', html)
    description = desc_match.group(1) if desc_match else ""
    # Decode HTML entities
    description = description.replace("&rsquo;", "'").replace("&ndash;", "–").replace("&nbsp;", " ")

    # Extract region/city from h5 text-ristoranti
    region_match = re.search(r'<h5[^>]*class="h5 text-ristoranti"[^>]*>(.*?)</h5>', html, re.S | re.I)
    region_city = strip_tags(region_match.group(1)).strip() if region_match else ""
    
    region = ""
    city = ""
    country = ""
    if "|" in region_city:
        parts = [p.strip() for p in region_city.split("|", 1)]
        left = parts[0].lower()
        city = parts[1] if len(parts) > 1 else ""
        
        # Determine if left part is an Italian region or country
        if any(r in left for r in ITALIAN_REGIONS) or left in ["italia", "italy"]:
            region = parts[0]
            country = "Italy"
        else:
            country = COUNTRY_MAP.get(left, parts[0])
    else:
        # Try to infer from page title or address
        pass

    # Extract address from h2 address block
    addr_match = re.search(r'<h2[^>]*class="h2 address"[^>]*>(.*?)</h2>', html, re.S | re.I)
    if addr_match:
        addr_html = addr_match.group(1)
        # Strip mailto links
        addr_html = re.sub(r'<a[^>]*>.*?</a>', '', addr_html, flags=re.S | re.I)
        address = strip_tags(addr_html).strip()
        address = re.sub(r'\n\s*\n', '\n', address)
        address = address.replace("\r", " ").replace("\n", ", ")
        # Remove multiple spaces
        address = re.sub(r'\s+', ' ', address).strip()
        
        # Extract phone from address (pattern: +XX...)
        phone_match = re.search(r'\+?\d[\d\s*\-+]+', address)
        phone = phone_match.group(0) if phone_match else ""
        # Remove masked phone from address
        address = re.sub(r'\+?\d[\d\s*\-+]+\s*$', '', address).strip(", ")
    else:
        address = ""
        phone = ""

    # Extract coordinates from JavaScript
    lat = None
    lng = None
    coord_match = re.search(r'var igplace\s*=\s*\{lat:\s*([0-9.\-+]+),\s*lng:\s*([0-9.\-+]+)\}', html)
    if coord_match:
        try:
            lat = float(coord_match.group(1))
            lng = float(coord_match.group(2))
        except ValueError:
            pass

    # Extract chef name
    chef = ""
    chef_label_match = re.search(r'<h6[^>]*class="h6[^"]*text-ristoranti[^"]*"[^>]*>\s*chef\s*</h6>\s*<h4[^>]*class="h4[^"]*"[^>]*>(.*?)</h4>', html, re.S | re.I)
    if chef_label_match:
        chef = strip_tags(chef_label_match.group(1)).strip()

    # Extract awards from image data-bs-title
    awards = []
    for award_match in re.finditer(r'<img[^>]*src="[^"]*/premi/[^"]*"[^>]*data-bs-title="([^"]*)"', html):
        award_text = award_match.group(1)
        if award_text and award_text not in awards:
            awards.append(award_text)

    # Extract menu prices
    prices = {}
    # Find all price blocks
    for block_match in re.finditer(r'<h5[^>]*class="h5"[^>]*>(.*?)</h5>\s*<h4[^>]*>(.*?)</h4>', html, re.S | re.I):
        label = strip_tags(block_match.group(1)).strip().rstrip(":").strip()
        value = strip_tags(block_match.group(2)).strip()
        if label and value:
            prices[label] = value

    # Determine source_id from URL
    # URL pattern: .../ristoranti/{slug}.html
    slug_match = re.search(r'/ristoranti/([^/]+)\.html', url)
    source_id = slug_match.group(1) if slug_match else url.split("/")[-1].replace(".html", "")

    tags = ["identita-golose"]
    if awards:
        tags.append("awarded")
    
    raw = {
        "url": url,
        "chef": chef,
        "awards": awards,
        "prices": prices,
        "phone": phone,
        "region_city_line": region_city,
    }

    place = {
        "name": name,
        "address": address,
        "lat": lat,
        "lng": lng,
        "source": "identitagolose",
        "source_id": source_id,
        "source_url": url,
        "city": city,
        "region": region,
        "country": country,
        "description": description,
        "category": "restaurant",
        "tags": tags,
        "raw_json": raw,
    }
    return place


def get_sitemap_urls(sitemap_url: str, max_urls: int = 0) -> list[str]:
    """Parse sitemap XML and return list of URLs."""
    html = fetch(sitemap_url, timeout=20)
    if not html:
        print(f"Failed to fetch sitemap: {sitemap_url}")
        return []
    
    try:
        root = ET.fromstring(html)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = []
        for loc in root.findall(".//sm:loc", ns):
            url = loc.text
            if url:
                urls.append(url)
        if max_urls > 0:
            urls = urls[:max_urls]
        return urls
    except ET.ParseError as e:
        print(f"XML parse error: {e}")
        return []


def main():
    parser = argparse.ArgumentParser(description="Sync Identità Golose guide")
    parser.add_argument("--max-urls", type=int, default=0, help="Limit URLs processed (0 = all)")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    parser.add_argument("--db", type=str, default=str(db.DB_PATH), help="SQLite database path")
    parser.add_argument("--source", choices=["ristoranti", "pec", "both"], default="both",
                       help="Which guide to sync: restaurants, pec (pizza/cocktail), or both")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        sys.exit(1)

    # Collect URLs
    urls = []
    if args.source in ("ristoranti", "both"):
        print("Fetching restaurant sitemap...")
        urls.extend(get_sitemap_urls(GUIDE_SITEMAP, args.max_urls if args.source == "ristoranti" else 0))
    if args.source in ("pec", "both"):
        print("Fetching pizza/cocktail sitemap...")
        pec_urls = get_sitemap_urls(PEC_SITEMAP, args.max_urls if args.source == "pec" else 0)
        urls.extend(pec_urls)

    if not urls:
        print("No URLs found in sitemap")
        sys.exit(0)

    total = len(urls)
    print(f"Total URLs to process: {total}")

    # Load existing places for dedup
    with db.connect() as conn:
        dedup_map = dedup.build_dedup_map(conn)

        # Process
        added = 0
        updated = 0
        skipped = 0
        errors = 0

        for i, url in enumerate(urls, 1):
            print(f"[{i}/{total}] {url}")

            html = fetch(url)
            if not html:
                errors += 1
                continue

            place = extract_detail(html, url)
            if not place:
                errors += 1
                continue

            # Deduplication check
            norm_name = normalize_name(place["name"])
            city_key = (place.get("city") or "").lower()
            country_key = (place.get("country") or "").lower()
            dup_key = (norm_name, city_key, country_key)

            if dup_key in dedup_map:
                # Existing match — add identita-golose tag
                existing_id = dedup_map[dup_key][0]
                if dedup.add_tag(conn, existing_id, "identita-golose"):
                    conn.commit()
                    updated += 1
                    print(f"    → dedup: added identita-golose tag to existing place")
                skipped += 1
                continue

            # Insert new place
            db.upsert_place(conn, place)
            dedup_map[dup_key] = [-1]  # Mark as processed
            added += 1
            print(f"    → added: {place['name']} ({place.get('city', '')}, {place.get('country', '')})")

            if i % 25 == 0:
                conn.commit()
                print(f"  ...committed batch ({i}/{total})")

            if args.delay > 0:
                time.sleep(args.delay)

        conn.commit()
        db.record_sync(
            conn,
            "identitagolose",
            "ok" if errors == 0 else "error",
            f"{errors} errors" if errors else "",
            rows_added=added,
            rows_updated=updated,
        )

    print(f"\nDone. Added: {added}, Updated (tag): {updated}, Skipped (dedup): {skipped}, Errors: {errors}")


if __name__ == "__main__":
    main()
