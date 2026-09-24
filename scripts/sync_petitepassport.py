"""Sync Petite Passport (petitepassport.com) — curated design-travel hotspots.

Petite Passport is an editorial city-guide with a curated directory of
food & drink spots (restaurants, cafés, bars, bakeries, coffee bars,
delis, ice-cream, wineries). The actual spot content is behind a
MemberPress paywall, so this ingester:

  1. Lists the food/drink post URLs via the open WP REST API (filtered by
     the fooddrink category ids discovered from the API taxonomy).
  2. Fetches each gated post HTML using an authenticated session (Cookie
     jar path from the PP_COOKIE_JAR env var, default /tmp/pp.jar).
  3. Parses the ACF "marker" block (name, address, lat/lng), the
     "3 reasons to go there" description, the website link and the
     categories on the page.
  4. Dedups against existing places (tags the existing row instead of
     inserting a cross-source duplicate) and upserts as source
     `petitepassport`.

Auth: a valid MemberPress session cookie jar must be placed at the path
in PP_COOKIE_JAR (generate once via curl against /login/ saving cookies).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
from httputil import get  # noqa: E402
from dedup import add_tag, build_dedup_map, dup_key, normalize_name  # noqa: E402

SOURCE_NAME = "petitepassport"
BASE = "https://www.petitepassport.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Food & drink categories on Petite Passport (id -> canonical nomnom category).
FOODDRINK = {
    16218: "restaurant",
    16216: "cafe",
    16258: "bar",
    16220: "coffee_bar",
    16219: "bakery",
    16275: "deli",
    16224: "ice_cream",
    17225: "winery",
}

CATEGORY_ALIAS = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "bar": "bar",
    "coffee bar": "coffee_bar",
    "bakery": "bakery",
    "deli": "deli",
    "ice cream": "ice_cream",
    "winery": "winery",
}


def _cookie_jar() -> Path:
    jar = os.environ.get("PP_COOKIE_JAR", "/tmp/pp.jar")
    p = Path(jar)
    if not p.exists() or p.stat().st_size == 0:
        raise SystemExit(
            f"No Petite Passport session cookie jar at {jar}. "
            f"Login once via curl -c {jar} and export PP_COOKIE_JAR={jar}"
        )
    return p


def fetch_json(url: str, timeout: int = 20) -> Optional[list]:
    try:
        body = get(url, accept="application/json", timeout=timeout,
                   user_agent=USER_AGENT)
        return json.loads(body)
    except Exception as e:
        print(f"  [ERROR] {url}: {e}")
        return None


def get_categories() -> dict[int, str]:
    """Return {category_id: slug} from the WP taxonomy, for label lookup."""
    data = fetch_json(f"{BASE}/wp-json/wp/v2/categories?per_page=100")
    out = {}
    if data:
        for c in data:
            out[c["id"]] = c.get("slug", "")
    return out


def get_posts(cat: int, per_page: int = 100, page: int = 1) -> list[dict]:
    url = (f"{BASE}/wp-json/wp/v2/posts?categories={cat}"
           f"&per_page={per_page}&page={page}"
           f"&_fields=link,categories,title,date")
    data = fetch_json(url)
    if isinstance(data, list):
        return data
    return []


def collect_urls() -> list[str]:
    """All unique food/drink post URLs via the open REST API."""
    urls: set[str] = set()
    for cat in FOODDRINK:
        page = 1
        while True:
            posts = get_posts(cat, page=page)
            if not posts:
                break
            for p in posts:
                if p.get("link"):
                    urls.add(p["link"])
            if len(posts) < 100:
                break
            page += 1
            time.sleep(0.3)
    return sorted(urls)


def fetch_locked_page(url: str) -> str:
    # httputil.get can't carry a cookie jar, so add the Cookie header directly.
    jar = _cookie_jar()
    cookies = []
    for line in jar.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies.append(f"{parts[5]}={parts[6]}")
    cookie_header = "; ".join(cookies)
    try:
        body = get(url, timeout=30, user_agent=USER_AGENT,
                   extra_headers={"Cookie": cookie_header})
        return body.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [ERROR] fetch {url}: {e}")
        return ""


def parse_page(html: str, url: str) -> dict:
    rec = {"source_url": url}

    # Name from og:title ("HOTEL X - Petite Passport"); the first <h1> is
    # the category label (RESTAURANT / CAFE / ...) on many post layouts.
    og = re.search(r'og:title" content="([^"]+)"', html, re.I)
    if og:
        rec["name"] = og.group(1).split(" - Petite Passport")[0].strip() or None
    if not rec.get("name"):
        t = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if t:
            rec["name"] = t.group(1).split(" - Petite Passport")[0].strip() or None
    if not rec.get("name"):
        m = re.search(r"<h1[^>]*>\s*([^<]{2,120}?)\s*</h1>", html, re.I)
        rec["name"] = (m.group(1).strip() if m else None)

    m = re.search(
        r'<div class="marker"\s+data-lat="([\d.-]+)"\s+data-lng="([\d.-]+)">'
        r'\s*<h3></h3>\s*<p>\s*<em>([^<]+)</em>',
        html,
    )
    if m:
        rec["lat"] = float(m.group(1))
        rec["lng"] = float(m.group(2))
        rec["marker_addr"] = m.group(3).strip()

    w = re.search(r'Check out:?\s*</p>\s*<p>\s*<a href="([^"]+)"', html, re.I)
    rec["website"] = w.group(1) if w else None

    d = re.search(r"3 reasons to go there:\s*(.*?)(?:Check out:|</section>|$)",
                  html, re.I | re.S)
    if d and d.group(1):
        txt = re.sub(r"<[^>]+>", " ", d.group(1))
        rec["description"] = re.sub(r"\s+", " ", txt).strip()

    cats = re.findall(r'class="cat[^"]*"[^>]*>([^<]+)<', html, re.I)
    rec["cats_on_page"] = sorted(set(c.strip() for c in cats))

    # The first <h1> on every post layout is the category label
    # (RESTAURANT / CAFE / COFFEE BAR / ...), so parse it as the canonical
    # category when it is a known one.
    h1_first = re.search(r"<h1[^>]*>\s*([^<]{2,40}?)\s*</h1>", html, re.I)
    rec["first_h1"] = h1_first.group(1).strip() if h1_first else None

    # A page is usable for nomnom only if it yields either geo or a
    # description. The `mepr-login-form-wrap` marker appears even on fully
    # unlocked posts (a non-covered category label), so it must NOT gate
    # ingestion by itself — only a total absence of content does.
    rec["_locked"] = not (rec.get("lat") or rec.get("description"))
    return rec


_CITY_SUFFIXES = {"germany", "netherlands", "belgium", "france", "italy",
                  "spain", "denmark", "sweden", "norway", "austria",
                  "switzerland", "portugal", "greece", "england", "united kingdom",
                  "australia", "usa", "united states", "canada", "japan",
                  "singapore", "czech republic", "croatia", "slovenia",
                  "hungary", "poland", "turkey", "morocco", "mexico",
                  "south africa", "new zealand", "ireland", "iceland"}


def _strip_postal(city: str) -> str:
    """Drop a leading postal code from a city string ('08015 Barcelona')."""
    city = city.strip()
    # European/NL/BE/Nordic postal codes: digits, optionally letters.
    m = re.match(r"^\s*\d{2,6}\s*[A-Za-z]*\s+(.+)$", city)
    if m:
        return m.group(1).strip()
    return city


def split_address(marker_addr: str) -> tuple[str, str, str]:
    """Split 'Street, PLZ City, Country' -> (street, city, country).

    Handles multi-word country/city and strips post codes. Geo is kept from
    the site's ACF marker, so a coarse parse is acceptable.
    """
    if not marker_addr:
        return "", "", ""
    parts = [p.strip() for p in marker_addr.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return "", "", ""
    street = parts[0]
    # Country = last part if it's a known country word, else unknown.
    country = ""
    if len(parts) > 1 and parts[-1].lower() in _CITY_SUFFIXES:
        country = parts[-1]
        parts = parts[:-1]
    # City = last remaining part (after street), post code stripped.
    city = _strip_postal(parts[-1]) if len(parts) > 1 else ""
    return street, city, country


def to_place(rec: dict) -> Optional[dict]:
    name = rec.get("name")
    if not name:
        return None
    marker = rec.get("marker_addr", "")
    street, city, country = split_address(marker)
    # Category: prefer the post's first <h1> (canonical label), else the
    # category tags rendered on the page.
    category = "restaurant"
    h1_label = (rec.get("first_h1") or "").strip().lower()
    if h1_label in CATEGORY_ALIAS:
        category = CATEGORY_ALIAS[h1_label]
    else:
        for c in rec.get("cats_on_page", []):
            key = c.lower().strip()
            if key in CATEGORY_ALIAS:
                category = CATEGORY_ALIAS[key]
                break
    slug = re.sub(r"^https?://www\.petitepassport\.com/", "", rec["source_url"])
    slug = slug.rstrip("/").replace("/", "-")
    tags = ["editorial", "source:petitepassport"] + rec.get("cats_on_page", [])
    return {
        "source": SOURCE_NAME,
        "source_id": slug,
        "source_url": rec["source_url"],
        "name": name,
        "category": category,
        "address": street or marker,
        "city": city,
        "country": country,
        "lat": rec.get("lat"),
        "lng": rec.get("lng"),
        "description": rec.get("description"),
        "tags": tags,
        "raw_json": rec,
    }


def _source_of(conn, place_id: int) -> str:
    row = conn.execute("SELECT source FROM places WHERE id = ?", (place_id,)).fetchone()
    return row["source"] if row else ""


def sync(max_urls: int | None = None, **_kwargs) -> tuple[int, int]:
    added = updated = 0
    urls = collect_urls()
    if max_urls:
        urls = urls[:max_urls]
    print(f"petitepassport: {len(urls)} food/drink URLs", file=sys.stderr)

    with db.connect() as conn:
        dup_map = build_dedup_map(conn)
        for i, u in enumerate(urls, 1):
            html = fetch_locked_page(u)
            if not html:
                print(f"  [skip] {u}", file=sys.stderr)
                continue
            rec = parse_page(html, u)
            if rec.get("_locked"):
                print(f"  [LOCKED] session didn't unlock {u}", file=sys.stderr)
                continue
            place = to_place(rec)
            if not place:
                print(f"  [skip no-name] {u}", file=sys.stderr)
                continue
            key = dup_key(place)
            real = [pid for pid in dup_map.get(key, []) if _source_of(conn, pid) != SOURCE_NAME]
            if real:
                # Tag existing curated place instead of inserting a dup.
                tag_added = False
                for t in dict.fromkeys(place["tags"]):
                    if add_tag(conn, real[0], t):
                        tag_added = True
                conn.execute(
                    "INSERT OR IGNORE INTO canonical_links (place_id, canonical_id) VALUES (?, ?)",
                    (real[0], f"petitepassport:{place['source_id']}"),
                )
                conn.execute(
                    "UPDATE places SET description = COALESCE(description, ?), "
                    "address = COALESCE(address, ?) WHERE id = ?",
                    (place.get("description"), place.get("address"), real[0]),
                )
                if tag_added:
                    updated += 1
                conn.commit()
                continue
            was_new, pid = db.upsert_place(conn, place)
            if was_new:
                added += 1
                dup_map.setdefault(key, []).append(pid)
            else:
                updated += 1
            conn.commit()
            if i % 25 == 0:
                print(f"  {i}/{len(urls)} (added={added} updated={updated})",
                      file=sys.stderr, flush=True)
            time.sleep(0.12)
        db.record_sync(conn, SOURCE_NAME, "ok",
                       f"loaded {len(urls)}", added, updated)
    return added, updated


if __name__ == "__main__":
    _jar = _cookie_jar()
    _a, _u = sync()
    print(f"petitepassport: {_a} new, {_u} updated")
