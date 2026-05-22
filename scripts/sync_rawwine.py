"""Sync RAW WINE (rawwine.com) — natural wine shops, bars, and restaurants.

RAW WINE is the community/event brand behind Isabelle Legeron MW's natural-
wine fairs. Their site hosts profile pages for members, filterable by type:

    https://www.rawwine.com/profile?profile_type=shop

The site is server-rendered HTML with no JSON API. Profile cards link to
`/profile/{slug}` and the per-profile page exposes name, bio, and a
`.vendor-location` / `.v-address` block. No coordinates are published.

Many RAW WINE shops/bars also appear on Raisin. To avoid double-counting we
do an in-process fuzzy dedup against existing rows: if we find a venue with
the same normalized name in the same city/country, we just augment its tags
with `raw-wine` instead of inserting a parallel row. New venues are inserted
as a standalone `rawwine` source row, so they can later be cross-linked via
the `canonical_links` table by other tooling.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

BASE = "https://www.rawwine.com"
LISTING_URL = f"{BASE}/profile?profile_type=shop"
USER_AGENT = "restaurant-finder-skill/0.1 (+contact in skill repo)"

# RAW WINE has ~3.7k total members; the shop/bar/restaurant subset is much
# smaller. Cap fetches per run so a cron pass stays bounded.
DEFAULT_MAX_PAGES_PER_RUN = 500

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


def _decode_html_entities(s: str) -> str:
    import html as _html
    return _html.unescape(s) if s else s


# ---------------------------------------------------------------------------
# Listing page: collect /profile/{slug} URLs
# ---------------------------------------------------------------------------

class _ListingParser(HTMLParser):
    """Scrape `/profile/{slug}` hrefs from a Shop/Bar/Restaurant listing page.

    We don't strictly require the `plp_product_format` class — RAW WINE has
    tweaked its template before, and as long as the href is a profile detail
    URL (not the listing itself) we want it. The page is filtered server-side
    to the requested profile_type, so every `/profile/{slug}` link here is in
    scope.
    """

    def __init__(self) -> None:
        super().__init__()
        self.slugs: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        if tag != "a":
            return
        a = dict(attrs)
        href = a.get("href") or ""
        slug = _slug_from_profile_href(href)
        if slug and slug not in self._seen:
            self._seen.add(slug)
            self.slugs.append(slug)


_PROFILE_HREF_RE = re.compile(r"^/profile/([A-Za-z0-9][A-Za-z0-9._\-]*)/?$")


def _slug_from_profile_href(href: str) -> Optional[str]:
    """Return the slug portion of a `/profile/{slug}` link, or None.

    Filters out the listing URL itself (`/profile?profile_type=...`) and any
    absolute URLs that don't point to rawwine.com.
    """
    if not href:
        return None
    # Drop query string / fragment before matching the path
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc and parsed.netloc not in ("www.rawwine.com", "rawwine.com"):
        return None
    m = _PROFILE_HREF_RE.match(parsed.path)
    return m.group(1) if m else None


def _list_slugs(max_pages: int) -> list[str]:
    """Walk paginated listings until pages stop yielding new slugs."""
    all_slugs: list[str] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in LISTING_URL else "?"
        url = LISTING_URL if page == 1 else f"{LISTING_URL}{sep}page={page}"
        try:
            html = _get(url).decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            break
        parser = _ListingParser()
        parser.feed(html)
        new = [s for s in parser.slugs if s not in seen]
        if not new:
            # No fresh slugs — either we paginated past the end or the site
            # ignores the `page` param and returns the same content.
            break
        seen.update(new)
        all_slugs.extend(new)
        time.sleep(0.4)
    return all_slugs


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

class _ProfileParser(HTMLParser):
    """Pull name, bio, vendor-location, and v-address from a profile page."""

    def __init__(self) -> None:
        super().__init__()
        # Stack of (tag, classes) so we can tell when we're inside a target div
        self._stack: list[tuple[str, set[str]]] = []
        # Capture buffers
        self._capture: Optional[str] = None  # which field we're collecting
        self._buf: list[str] = []
        # Results
        self.h1: Optional[str] = None
        self.title: Optional[str] = None
        self.meta_description: Optional[str] = None
        self.vendor_location: Optional[str] = None
        self.v_address: Optional[str] = None
        self.bio: Optional[str] = None

    @staticmethod
    def _classes(attrs: dict[str, str]) -> set[str]:
        return set((attrs.get("class") or "").split())

    def _matching_field(self, tag: str, classes: set[str]) -> Optional[str]:
        if tag == "h1" and self.h1 is None:
            return "h1"
        if tag in ("div", "p", "span"):
            if "vendor-location" in classes and self.vendor_location is None:
                return "vendor_location"
            if "v-address" in classes and self.v_address is None:
                return "v_address"
            # Bio container — RAW WINE has used `bio`, `profile-bio`, and
            # `vendor-bio` historically; match any of them.
            if self.bio is None and (
                "bio" in classes
                or "profile-bio" in classes
                or "vendor-bio" in classes
                or "vendor-description" in classes
            ):
                return "bio"
        return None

    # Void / self-closing tags never get an end tag — skip them so they
    # don't pollute the depth-tracking stack.
    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        a = dict(attrs)

        if tag == "meta":
            name = (a.get("name") or "").lower()
            if name == "description" and self.meta_description is None:
                self.meta_description = _decode_html_entities(a.get("content") or "") or None
            return

        if tag in self._VOID:
            return

        classes = self._classes(a)
        self._stack.append((tag, classes))

        if tag == "title" and self.title is None and self._capture is None:
            self._capture = "title"
            self._buf = []
            return

        if self._capture is None:
            field = self._matching_field(tag, classes)
            if field:
                self._capture = field
                self._buf = []
                # Remember the depth so we know when to stop
                self._capture_depth = len(self._stack)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        # Self-closing tags (e.g. <meta />) — handle the meta case
        if tag == "meta":
            a = dict(attrs)
            name = (a.get("name") or "").lower()
            if name == "description" and self.meta_description is None:
                self.meta_description = _decode_html_entities(a.get("content") or "") or None

    def handle_endtag(self, tag: str) -> None:
        # Pop matching tag from stack (resilient to malformed HTML)
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                popped_depth = i + 1
                del self._stack[i:]
                break
        else:
            popped_depth = None

        if self._capture == "title" and tag == "title":
            self.title = _decode_html_entities("".join(self._buf).strip()) or None
            self._capture = None
            self._buf = []
            return

        if self._capture and popped_depth is not None and popped_depth <= getattr(self, "_capture_depth", 0):
            text = _decode_html_entities(" ".join(self._buf).strip())
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                if self._capture == "h1":
                    self.h1 = text
                elif self._capture == "vendor_location":
                    self.vendor_location = text
                elif self._capture == "v_address":
                    self.v_address = text
                elif self._capture == "bio":
                    self.bio = text
            self._capture = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)


def _scrape_profile(slug: str) -> Optional[dict]:
    url = f"{BASE}/profile/{slug}"
    try:
        html = _get(url).decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None

    parser = _ProfileParser()
    parser.feed(html)

    # Name: prefer h1, fall back to the page title with site-suffix stripped.
    name = parser.h1
    if not name and parser.title:
        # Titles look like "Some Wine Bar | RAW WINE"
        name = re.sub(r"\s*[\|\-–]\s*RAW\s*WINE.*$", "", parser.title, flags=re.IGNORECASE).strip() or None
    if not name:
        return None

    # Location: vendor-location is "Region, Country" per the site template.
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    if parser.vendor_location:
        parts = [p.strip() for p in parser.vendor_location.split(",") if p.strip()]
        if len(parts) == 1:
            # Some profiles list only a country
            country = parts[0]
        elif len(parts) >= 2:
            region = parts[0]
            country = parts[-1]
            city = region  # Best-effort: the "region" line is often the city

    description = parser.bio or parser.meta_description

    return {
        "source": "rawwine",
        "source_id": slug,
        "source_url": url,
        "name": name,
        # No reliable category split on RAW WINE; everything in this scrape is
        # a shop / bar / restaurant. Leave broad — refinement is the query
        # layer's job, and the `raw-wine` tag carries the natural-wine signal.
        "category": None,
        "address": parser.v_address,
        "city": city,
        "region": region,
        "country": country,
        "lat": None,
        "lng": None,
        "description": description,
        "tags": ["natural-wine", "raw-wine"],
        "raw_json": {
            "url": url,
            "h1": parser.h1,
            "title": parser.title,
            "vendor_location": parser.vendor_location,
            "v_address": parser.v_address,
        },
    }


# ---------------------------------------------------------------------------
# Dedup against existing rows
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for fuzzy matching."""
    s = (name or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Drop common venue noise so "Foo Wine Bar" matches "Foo".
    return s


def _find_duplicate(
    conn: sqlite3.Connection,
    name: str,
    city: Optional[str],
    country: Optional[str],
) -> Optional[int]:
    """Return the id of an existing place that looks like the same venue.

    Matches by normalized name. We restrict candidates by city+country first
    (cheap, precise) and fall back to country-only if the city is missing.
    Returns None if no candidate's normalized name matches.
    """
    norm = _normalize_name(name)
    if not norm:
        return None

    query = "SELECT id, name FROM places WHERE source != 'rawwine'"
    params: list = []
    if city and country:
        query += " AND LOWER(city) = LOWER(?) AND LOWER(country) = LOWER(?)"
        params.extend([city, country])
    elif country:
        query += " AND LOWER(country) = LOWER(?)"
        params.append(country)
    else:
        # Without any location signal, name-only matching is too lossy —
        # better to insert a standalone row than to merge incorrect venues.
        return None

    for row in conn.execute(query, params).fetchall():
        if _normalize_name(row["name"]) == norm:
            return row["id"]
    return None


def _add_tag(conn: sqlite3.Connection, place_id: int, tag: str) -> bool:
    """Append `tag` to a place's tags JSON if not already present."""
    row = conn.execute("SELECT tags FROM places WHERE id = ?", (place_id,)).fetchone()
    if row is None:
        return False
    tags: list[str] = []
    if row["tags"]:
        try:
            loaded = json.loads(row["tags"])
            if isinstance(loaded, list):
                tags = [str(t) for t in loaded]
        except json.JSONDecodeError:
            tags = []
    if tag in tags:
        return False
    tags.append(tag)
    conn.execute(
        "UPDATE places SET tags = ? WHERE id = ?",
        (json.dumps(tags, ensure_ascii=False), place_id),
    )
    return True


def _link_canonical(conn: sqlite3.Connection, place_id: int, canonical_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO canonical_links (place_id, canonical_id) VALUES (?, ?)",
        (place_id, canonical_id),
    )


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------

def fetch_incremental(
    existing_ids: set[str],
    max_pages: int = DEFAULT_MAX_PAGES_PER_RUN,
) -> Iterable[dict]:
    """Yield scraped profiles, prioritising slugs not already stored."""
    slugs = _list_slugs(max_pages=max_pages)
    # Unknown slugs first, then known ones (refresh tail)
    slugs.sort(key=lambda s: s in existing_ids)
    fetched = 0
    for slug in slugs:
        if fetched >= max_pages:
            break
        place = _scrape_profile(slug)
        if place:
            yield place
            fetched += 1
        time.sleep(0.4)


def sync(
    max_pages: int = DEFAULT_MAX_PAGES_PER_RUN,
    commit_every: int = 25,
) -> tuple[int, int]:
    """Run the RAW WINE sync.

    Returns (rows_added, rows_updated_or_tagged). A `tag-only` augmentation of
    a non-rawwine row is counted as an update — it's still a side-effect on
    the DB worth surfacing in the sync log.
    """
    added = updated = 0
    with db.connect() as conn:
        try:
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT source_id FROM places WHERE source = 'rawwine'"
                )
            }
            seen = 0
            for place in fetch_incremental(existing, max_pages=max_pages):
                slug = place["source_id"]
                canonical = f"rawwine:{slug}"

                # Is this venue already on file under another source?
                dup_id = _find_duplicate(
                    conn, place["name"], place.get("city"), place.get("country")
                )
                if dup_id is not None:
                    # Augment the existing row instead of inserting a parallel
                    # rawwine entry. Tag it `raw-wine` and record the link.
                    changed = _add_tag(conn, dup_id, "raw-wine")
                    _link_canonical(conn, dup_id, canonical)
                    if changed:
                        updated += 1
                else:
                    was_new, place_id = db.upsert_place(conn, place)
                    _link_canonical(conn, place_id, canonical)
                    if was_new:
                        added += 1
                    else:
                        updated += 1

                seen += 1
                if seen % commit_every == 0:
                    conn.commit()
            db.record_sync(
                conn, "rawwine", "ok",
                f"max_pages={max_pages}", added, updated,
            )
        except Exception as e:
            db.record_sync(conn, "rawwine", "error", str(e)[:500], added, updated)
            conn.commit()
            raise
    return added, updated


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES_PER_RUN)
    args = ap.parse_args()
    a, u = sync(max_pages=args.max_pages)
    print(f"rawwine: {a} new, {u} updated")
