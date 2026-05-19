"""Sync Splendido Magazin via its WordPress REST API.

Splendido publishes editorial restaurant/shop spots at /spots/{slug}/. The
underlying CPT slug should be `spots`; if WP exposes it on /wp-json/wp/v2/spots/
this is one fast call. If they've turned the API endpoint off (some WP themes
do), we fall back to scraping the sitemap and parsing each spot page's JSON-LD
or OpenGraph metadata.

If you see "no spots returned" check:
  curl -s https://splendido-magazin.de/wp-json/wp/v2/types | jq 'keys'
The CPT slug may have been renamed.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

BASE = "https://splendido-magazin.de"
WP_API = f"{BASE}/wp-json/wp/v2"
SITEMAP_INDEX = f"{BASE}/sitemap_index.xml"
USER_AGENT = "restaurant-finder-skill/0.1 (+contact in skill repo)"


def _get(url: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _parse_xml(data: bytes):
    """ElementTree rejects whitespace before <?xml; strip it."""
    return ET.fromstring(data.lstrip())


# ---------------------------------------------------------------------------
# Path A: WordPress REST API
# ---------------------------------------------------------------------------

def _try_wp_api() -> list[dict]:
    """Return raw WP records for the spots CPT, or [] if the endpoint is dead."""
    candidates = ["spots", "spot", "ort", "orte", "places", "place"]
    for cpt in candidates:
        url = f"{WP_API}/{cpt}?per_page=100&page=1"
        try:
            first = json.loads(_get(url))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        except json.JSONDecodeError:
            continue
        if not isinstance(first, list) or not first:
            continue

        # Found a working CPT — page through it.
        all_records: list[dict] = list(first)
        page = 2
        while True:
            try:
                batch = json.loads(_get(f"{WP_API}/{cpt}?per_page=100&page={page}"))
            except urllib.error.HTTPError as e:
                if e.code == 400:  # WP returns 400 when page is past end
                    break
                raise
            if not batch:
                break
            all_records.extend(batch)
            page += 1
            time.sleep(0.3)
        return all_records
    return []


# ---------------------------------------------------------------------------
# Path B: sitemap + scrape
# ---------------------------------------------------------------------------

class _MetaScraper(HTMLParser):
    """Pull JSON-LD blocks, meta tags, and embedded Google Maps links."""

    def __init__(self) -> None:
        super().__init__()
        self._in_jsonld = False
        self._buf: list[str] = []
        self.jsonld_blobs: list[dict] = []
        self.og: dict[str, str] = {}
        self.title: Optional[str] = None
        self._capture_title = False
        self.gmaps_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        a = dict(attrs)
        if tag == "script" and a.get("type") == "application/ld+json":
            self._in_jsonld = True
            self._buf = []
        elif tag == "meta":
            prop = a.get("property") or a.get("name") or ""
            content = a.get("content") or ""
            if prop.startswith("og:") and content:
                self.og[prop[3:]] = content
        elif tag == "title":
            self._capture_title = True
        elif tag == "a":
            href = a.get("href") or ""
            if "google." in href and "/maps" in href:
                self.gmaps_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_jsonld:
            blob = "".join(self._buf).strip()
            self._in_jsonld = False
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, list):
                    self.jsonld_blobs.extend(p for p in parsed if isinstance(p, dict))
                elif isinstance(parsed, dict):
                    self.jsonld_blobs.append(parsed)
            except json.JSONDecodeError:
                pass
        elif tag == "title":
            self._capture_title = False

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buf.append(data)
        elif self._capture_title and self.title is None:
            self.title = data.strip()


def _coords_from_gmaps(links: list[str]) -> tuple[Optional[float], Optional[float]]:
    """Extract (lat, lng) from a Google Maps URL.

    Prefers the !3d{lat}!4d{lng} encoding (the actual place location) over
    the @{lat},{lng},{zoom} prefix (which is just the map viewport).
    """
    for link in links:
        m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", link)
        if m:
            return float(m.group(1)), float(m.group(2))
    for link in links:
        m = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+)", link)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def _address_from_html(html: str) -> Optional[str]:
    """Heuristic: pull the first 'Via/Piazza/Corso ...' street snippet."""
    m = re.search(
        r"\b(Via|Viale|Piazza|Corso|Strada|Largo|Vicolo|Borgo|Lungomare)\s+"
        r"[A-ZÀ-Ü][^\"<>\n]{2,80}",
        html,
    )
    if m:
        return m.group(0).strip()
    return None


def _spot_urls_from_sitemap() -> list[str]:
    """Find /spots/* URLs by walking the WP sitemap index."""
    try:
        idx_xml = _get(SITEMAP_INDEX, accept="application/xml")
    except urllib.error.HTTPError:
        # Try non-index location
        idx_xml = _get(f"{BASE}/sitemap.xml", accept="application/xml")

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_urls: list[str] = []
    try:
        root = _parse_xml(idx_xml)
        for loc in root.findall("sm:sitemap/sm:loc", ns):
            if loc.text:
                sitemap_urls.append(loc.text)
    except ET.ParseError:
        pass

    # If it wasn't an index, treat the original as a single sitemap
    if not sitemap_urls:
        sitemap_urls = [SITEMAP_INDEX]

    spot_urls: list[str] = []
    for sm_url in sitemap_urls:
        try:
            sm_xml = _get(sm_url, accept="application/xml")
            root = _parse_xml(sm_xml)
            for loc in root.findall("sm:url/sm:loc", ns):
                if loc.text and "/spots/" in loc.text:
                    spot_urls.append(loc.text)
        except (urllib.error.HTTPError, ET.ParseError):
            continue
        time.sleep(0.2)
    return sorted(set(spot_urls))


def _scrape_spot(url: str) -> Optional[dict]:
    try:
        html = _get(url, accept="text/html").decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        return None
    parser = _MetaScraper()
    parser.feed(html)

    # Prefer JSON-LD if present (Restaurant / LocalBusiness / Place)
    place_types = {"Restaurant", "LocalBusiness", "Place", "FoodEstablishment",
                   "BarOrPub", "CafeOrCoffeeShop", "Bakery", "IceCreamShop"}
    chosen: Optional[dict] = None
    for blob in parser.jsonld_blobs:
        t = blob.get("@type")
        if isinstance(t, list):
            t = next((x for x in t if x in place_types), t[0] if t else None)
        if t in place_types:
            chosen = blob
            break

    name = (chosen or {}).get("name") or parser.og.get("title") or parser.title or ""
    name = re.sub(r"\s*[-–]\s*Splendido.*$", "", name).strip()
    if not name:
        return None

    addr = (chosen or {}).get("address") or {}
    if isinstance(addr, str):
        address_line = addr
        city = country = region = None
    else:
        parts = [
            addr.get("streetAddress"),
            addr.get("postalCode"),
            addr.get("addressLocality"),
        ]
        address_line = ", ".join(p for p in parts if p) or None
        city = addr.get("addressLocality")
        region = addr.get("addressRegion")
        country = addr.get("addressCountry")

    geo = (chosen or {}).get("geo") or {}
    lat = geo.get("latitude") if isinstance(geo, dict) else None
    lng = geo.get("longitude") if isinstance(geo, dict) else None

    if lat is None or lng is None:
        glat, glng = _coords_from_gmaps(parser.gmaps_links)
        if glat is not None:
            lat, lng = glat, glng

    if not address_line:
        address_line = _address_from_html(html)

    description = (
        (chosen or {}).get("description")
        or parser.og.get("description")
    )

    # Crude category inference from URL slug or content
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    category = "shop"
    for key in ("ristorante", "restaurant", "trattoria", "osteria", "pizzeria"):
        if key in slug or key in (description or "").lower():
            category = "restaurant"
            break
    else:
        for key in ("bar", "caffe", "café", "cafe", "enoteca"):
            if key in slug or key in (description or "").lower():
                category = "bar"
                break

    return {
        "source": "splendido",
        "source_id": slug,
        "source_url": url,
        "name": name,
        "category": category,
        "address": address_line,
        "city": city,
        "region": region,
        "country": country,
        "lat": float(lat) if lat is not None else None,
        "lng": float(lng) if lng is not None else None,
        "description": description,
        "tags": ["splendido-strada"],
        "raw_json": {"jsonld": chosen, "og": parser.og},
    }


# ---------------------------------------------------------------------------
# WP record normalizer
# ---------------------------------------------------------------------------

def _normalize_wp(rec: dict) -> Optional[dict]:
    title = (rec.get("title") or {}).get("rendered") or rec.get("slug") or ""
    if not title:
        return None
    excerpt = (rec.get("excerpt") or {}).get("rendered") or ""
    excerpt = re.sub(r"<[^>]+>", "", excerpt).strip()

    # Attempt to extract address/coords from ACF/meta if exposed
    meta = rec.get("acf") or rec.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    lat = meta.get("lat") or meta.get("latitude")
    lng = meta.get("lng") or meta.get("longitude") or meta.get("lon")
    address = meta.get("address") or meta.get("adresse")

    return {
        "source": "splendido",
        "source_id": str(rec.get("id") or rec.get("slug")),
        "source_url": rec.get("link") or "",
        "name": title,
        "category": "restaurant",  # refined later if we detect otherwise
        "address": address,
        "city": meta.get("city") or meta.get("ort"),
        "region": meta.get("region"),
        "country": meta.get("country") or meta.get("land"),
        "lat": float(lat) if lat else None,
        "lng": float(lng) if lng else None,
        "description": excerpt or None,
        "tags": ["splendido-strada"],
        "raw_json": rec,
    }


def fetch_all() -> Iterable[dict]:
    """Yield every Splendido spot. Try WP API first, then sitemap fallback."""
    wp_records = _try_wp_api()
    if wp_records:
        for rec in wp_records:
            place = _normalize_wp(rec)
            if place:
                yield place
        return

    # Fallback: scrape /spots/ pages from sitemap
    for url in _spot_urls_from_sitemap():
        place = _scrape_spot(url)
        if place:
            yield place
        time.sleep(0.5)  # be nice; ~150 pages, this takes ~75s


def sync() -> tuple[int, int]:
    added = updated = 0
    with db.connect() as conn:
        try:
            for place in fetch_all():
                was_new, _ = db.upsert_place(conn, place)
                if was_new:
                    added += 1
                else:
                    updated += 1
            db.record_sync(conn, "splendido", "ok", "", added, updated)
        except Exception as e:
            db.record_sync(conn, "splendido", "error", str(e)[:500], added, updated)
            raise
    return added, updated


if __name__ == "__main__":
    a, u = sync()
    print(f"splendido: {a} new, {u} updated")
