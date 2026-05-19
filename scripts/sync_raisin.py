"""Sync Raisin (raisin.digital) — natural wine bars, restaurants, shops.

Raisin is a custom Django/Python app, not WordPress, so there's no /wp-json.
Their public site has a sitemap.xml that includes every venue page. Each venue
page embeds JSON-LD (Restaurant/LocalBusiness/BarOrPub) plus a few microdata
attributes we can lean on.

Venue URL pattern:
    /en/explore/{country}/{region}/{city}/venues/{slug-id}/

There is also a city-level URL ending in /venues/ — we filter those out: a
venue URL has at least 5 path segments after /en/explore/.

Optional: by inspecting their search dropdown XHR, you can sometimes find a
JSON endpoint at /en/api/... but it changes; the sitemap path is more stable.
"""

from __future__ import annotations

import json
import re
import sys
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

BASE = "https://www.raisin.digital"
SITEMAP = f"{BASE}/sitemap.xml"
USER_AGENT = "restaurant-finder-skill/0.1 (+contact in skill repo)"

# Limit how many venue pages we actually fetch in one run. Raisin has 8000+
# venues across many countries; full-scrape on every cron run is excessive.
# Default behaviour: only scrape pages we haven't fetched recently.
DEFAULT_MAX_PAGES_PER_RUN = 1000


_RETRY_EXCEPTIONS = (
    TimeoutError,
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    ConnectionResetError,
    ConnectionError,
)


def _get(url: str, accept: str = "text/html", retries: int = 3, timeout: int = 60) -> bytes:
    # Quote any non-ASCII characters in the path (e.g. è in Italian slugs)
    safe_url = urllib.parse.quote(url, safe=":/?&=#%")
    req = urllib.request.Request(
        safe_url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except _RETRY_EXCEPTIONS as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"unreachable: last_err={last_err}")


def _parse_xml(data: bytes):
    """Strip leading whitespace before <?xml; ElementTree is strict."""
    return ET.fromstring(data.lstrip())


def _all_venue_urls() -> list[str]:
    """Return every venue page URL listed in Raisin's sitemap.

    Raisin's master sitemap has 1700+ child sitemaps (most are wine/winemaker
    pages, not venues). We only need the ones whose name starts with
    `sitemap-venues` (excluding `-images`, `-posts`, `-wines`, etc.)
    """
    sitemap_urls: list[str] = []
    try:
        idx_xml = _get(SITEMAP, accept="application/xml")
    except urllib.error.HTTPError:
        return []

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    try:
        root = _parse_xml(idx_xml)
        children = list(root)
        if children and children[0].tag.endswith("sitemap"):
            for loc in root.findall("sm:sitemap/sm:loc", ns):
                if loc.text:
                    sitemap_urls.append(loc.text)
        else:
            sitemap_urls = [SITEMAP]
    except ET.ParseError:
        return []

    # Keep only the venue listings — `/en/sitemap-venues.xml` and `?p=N` pages
    venue_sitemaps = [
        u for u in sitemap_urls
        if re.search(r"/en/sitemap-venues\.xml(\?p=\d+)?$", u)
    ]

    venue_urls: list[str] = []
    for sm_url in venue_sitemaps:
        try:
            sm_xml = _get(sm_url, accept="application/xml")
            root = _parse_xml(sm_xml)
            for loc in root.findall("sm:url/sm:loc", ns):
                if loc.text and _looks_like_venue(loc.text):
                    venue_urls.append(loc.text)
        except (urllib.error.HTTPError, ET.ParseError):
            continue
        time.sleep(0.3)

    return sorted(set(venue_urls))


def _looks_like_venue(url: str) -> bool:
    """Venue URLs end with /venues/{slug}/ — not just /venues/ (a city listing)."""
    m = re.search(r"/explore/([^/]+/){2,}venues/[^/]+/?$", url)
    return m is not None


# ---------------------------------------------------------------------------
# Page parser
# ---------------------------------------------------------------------------

class _Scraper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_jsonld = False
        self._buf: list[str] = []
        self.jsonld_blobs: list[dict] = []
        self.og: dict[str, str] = {}

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

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buf.append(data)


_VENUE_TYPES = {
    "Restaurant": "restaurant",
    "BarOrPub": "bar",
    "Bar": "bar",
    "WineShop": "wine_shop",
    "LiquorStore": "wine_shop",
    "Store": "shop",
    "LocalBusiness": "restaurant",
    "FoodEstablishment": "restaurant",
    "CafeOrCoffeeShop": "bar",
    "Hotel": "shop",
}


def _category_from_url(url: str) -> str:
    """Best-effort fallback category from URL slug."""
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    if "wineshop" in slug or "wine-shop" in slug:
        return "wine_shop"
    if "bar-" in slug or slug.startswith("bar-") or "wine-bar" in slug:
        return "bar"
    if "restaurant" in slug:
        return "restaurant"
    return "restaurant"


def _country_from_url(url: str) -> Optional[str]:
    m = re.search(r"/explore/([^/]+)/", url)
    if not m:
        return None
    raw = m.group(1).replace("-", " ")
    return raw.title()


def _decode_html_entities(s: str) -> str:
    import html as _html
    return _html.unescape(s) if s else s


_H1_RE = re.compile(r'<h1[^>]*>\s*([^<]+?)\s*</h1>', re.DOTALL)
_VENUE_LAT_RE = re.compile(r'venue_lat\s*:\s*["\'](-?\d+\.\d+)["\']')
_VENUE_LNG_RE = re.compile(r'venue_long\s*:\s*["\'](-?\d+\.\d+)["\']')
_ADDR_BLOCK_RE = re.compile(
    r'<p class="text-lg"[^>]*>.*?fa-map-marker[^<]*</i>\s*([^<]+(?:<[^/][^>]*>[^<]*)*)\s*</p>',
    re.DOTALL,
)
_META_DESC_RE = re.compile(r'<meta\s+name="description"\s+content="([^"]+)"')
_TITLE_RE = re.compile(r"<title>([^<]+)</title>")


def _category_from_html(html: str) -> Optional[str]:
    """Raisin pages render category labels in caps under the venue name."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    # Look for the badge sequence "BARRESTAURANTWINE SHOP" or with separators
    if re.search(r"\bRESTAURANT\b", text):
        return "restaurant"
    if re.search(r"\bWINE SHOP\b", text):
        return "wine_shop"
    if re.search(r"\bBAR\b", text):
        return "bar"
    if re.search(r"\bACCOMMODATION\b", text, re.IGNORECASE):
        return "shop"
    return None


def _scrape_venue(url: str) -> Optional[dict]:
    try:
        html = _get(url).decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None

    # Name from h1
    h1 = _H1_RE.search(html)
    name = _decode_html_entities(h1.group(1).strip()) if h1 else None
    if not name:
        return None

    # Address: pull text after the map-marker icon inside the text-lg <p>
    address_line: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    addr_m = _ADDR_BLOCK_RE.search(html)
    if addr_m:
        raw = re.sub(r"<[^>]+>", " ", addr_m.group(1))
        raw = _decode_html_entities(raw)
        raw = re.sub(r"\s+", " ", raw).strip().rstrip(",")
        address_line = raw or None
        # Last comma-separated chunk is country, second-to-last contains city
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            country = parts[-1]
            # City often comes right before country, possibly preceded by postcode
            if len(parts) >= 2:
                city_chunk = parts[-2]
                # Strip leading postcode (4-6 digits) if present
                city = re.sub(r"^\d{4,6}\s+", "", city_chunk).strip() or None

    # Coords from JS object
    lat_m = _VENUE_LAT_RE.search(html)
    lng_m = _VENUE_LNG_RE.search(html)
    lat = float(lat_m.group(1)) if lat_m else None
    lng = float(lng_m.group(1)) if lng_m else None

    # Description: meta description tag is reliable
    desc_m = _META_DESC_RE.search(html)
    description = _decode_html_entities(desc_m.group(1)) if desc_m else None

    cat = _category_from_html(html) or _category_from_url(url)

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    m = re.search(r"-(\d+)$", slug)
    source_id = m.group(1) if m else slug

    return {
        "source": "raisin",
        "source_id": source_id,
        "source_url": url,
        "name": name,
        "category": cat,
        "address": address_line,
        "city": city,
        "region": None,
        "country": country or _country_from_url(url),
        "lat": lat,
        "lng": lng,
        "description": description,
        "tags": ["natural-wine"],
        "raw_json": {"url": url, "h1": name, "addr": address_line, "lat": lat, "lng": lng},
    }


def fetch_incremental(
    existing_ids: set[str],
    max_pages: int = DEFAULT_MAX_PAGES_PER_RUN,
    country: Optional[str] = None,
) -> Iterable[dict]:
    """Yield venues, prioritising pages we don't already have in the DB.

    `country` filters by URL slug (e.g. "italy"); URLs are
    `/explore/{country}/...`.
    """
    urls = _all_venue_urls()
    if country:
        needle = f"/explore/{country.lower()}/"
        urls = [u for u in urls if needle in u]
    # Stable order: unknown URLs first, then known (refresh tail)
    def known(u: str) -> bool:
        m = re.search(r"-(\d+)/?$", u)
        return bool(m and m.group(1) in existing_ids)

    urls.sort(key=known)
    fetched = 0
    for url in urls:
        if fetched >= max_pages:
            break
        place = _scrape_venue(url)
        if place:
            yield place
            fetched += 1
        time.sleep(0.4)  # ~150/min, well under any reasonable rate limit


def sync(
    max_pages: int = DEFAULT_MAX_PAGES_PER_RUN,
    country: Optional[str] = None,
    commit_every: int = 25,
) -> tuple[int, int]:
    """Run the Raisin sync.

    Commits in batches of `commit_every` venues so a mid-run network blip
    doesn't erase 30 minutes of scraping. The HTTP session is short-lived
    enough that periodic disconnects from Cloudflare/Raisin are normal.
    """
    added = updated = 0
    with db.connect() as conn:
        try:
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT source_id FROM places WHERE source = 'raisin'"
                )
            }
            seen = 0
            for place in fetch_incremental(existing, max_pages=max_pages, country=country):
                was_new, _ = db.upsert_place(conn, place)
                if was_new:
                    added += 1
                else:
                    updated += 1
                seen += 1
                if seen % commit_every == 0:
                    conn.commit()
            db.record_sync(
                conn, "raisin", "ok",
                f"max_pages={max_pages} country={country or 'all'}", added, updated,
            )
        except Exception as e:
            # Persist whatever we've upserted so far before the failure
            db.record_sync(conn, "raisin", "error", str(e)[:500], added, updated)
            conn.commit()
            raise
    return added, updated


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES_PER_RUN)
    ap.add_argument("--country", help="Filter to URL slug, e.g. 'italy'")
    args = ap.parse_args()
    a, u = sync(max_pages=args.max_pages, country=args.country)
    print(f"raisin: {a} new, {u} updated")
