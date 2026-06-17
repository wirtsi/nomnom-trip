"""Sync Gault & Millau Switzerland (gaultmillau.ch) — restaurant guide.

GM CH is a React SPA with no Restaurant JSON-LD. We parse:
- Restaurant name from <h1>
- Description from <meta name="description"> or the article body
- We use the sitemap to find all /restaurants/* URLs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import dedup  # noqa: E402
import httputil  # noqa: E402
import sitemap  # noqa: E402

BASE = "https://www.gaultmillau.ch"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
REQUEST_DELAY = 0.5
USER_AGENT = "Mozilla/5.0 (compatible; NomnomBot/1.0)"

SOURCE_NAME = "gaultmillau"
DEDUP_TAG = "gaultmillau"


# ── Sitemap ──────────────────────────────────────────────────


def fetch_sitemap_urls() -> list[str]:
    """Collect all /restaurants/* URLs from GM CH monthly sitemaps."""
    urls = sitemap.iter_urls(
        SITEMAP_INDEX,
        path_filter=lambda u: "/restaurants/" in u,
        timeout=20,
        user_agent=USER_AGENT,
    )
    return sorted(set(urls))


# ── Detail page ──────────────────────────────────────────────


def parse_restaurant(url: str, html: str) -> dict | None:
    """Parse a GM CH restaurant detail page (React SPA)."""
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
    name = h1_match.group(1).strip() if h1_match else ""
    name = name.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    name = re.sub(r"<[^>]+>", "", name).strip()
    if not name:
        return None

    desc_match = re.search(
        r'<meta\s+name="description"\s+content="([^"]*)"',
        html, re.IGNORECASE
    )
    description = desc_match.group(1).strip() if desc_match else None

    # Also try JSON-LD WebPage blocks for description / articleId
    for m in re.finditer(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(m.group(1).strip())
            if isinstance(data, dict):
                if not description and data.get("description"):
                    description = data["description"][:500]
        except (json.JSONDecodeError, AttributeError):
            continue

    slug = url.rstrip("/").split("/")[-1]
    return {
        "source": SOURCE_NAME,
        "source_id": slug,
        "source_url": url,
        "name": name,
        "category": "restaurant",
        "country": "Switzerland",
        "description": description,
    }


# ── Main sync ────────────────────────────────────────────────


def sync(max_urls: int | None = None, verbose: bool = True):
    urls = fetch_sitemap_urls()
    print(f"Found {len(urls)} restaurant URLs from sitemaps")

    if max_urls:
        urls = urls[:max_urls]
        print(f"Limited to {len(urls)} URLs")

    added = 0
    updated = 0
    skipped = 0
    errors = 0
    err_msg = ""

    with db.connect() as conn:
        dedup_map = dedup.build_dedup_map(conn)

        for i, url in enumerate(urls, 1):
            if verbose and i % 20 == 0:
                print(f"  [{i}/{len(urls)}] added={added} updated={updated} skipped={skipped} errors={errors}")

            try:
                html = httputil.fetch_page(url, user_agent=USER_AGENT)
            except Exception as e:
                print(f"  HTTP error {url}: {e}")
                errors += 1
                continue

            place = parse_restaurant(url, html)
            if not place:
                errors += 1
                continue

            key = dedup.dup_key(place)
            if key in dedup_map and dedup_map[key]:
                existing_id = dedup_map[key][0]
                if dedup.add_tag(conn, existing_id, DEDUP_TAG):
                    conn.commit()
                    updated += 1
                    if verbose:
                        print(f"  → dedup: added tag to existing id={existing_id}")
                else:
                    skipped += 1
                continue

            try:
                was_new, _pid = db.upsert_place(conn, place)
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
                err_msg = str(e)

            dedup_map[key] = [-1]
            time.sleep(REQUEST_DELAY)

        db.record_sync(
            conn,
            SOURCE_NAME,
            "ok" if errors == 0 else "error",
            err_msg or (f"{errors} errors" if errors else ""),
            rows_added=added,
            rows_updated=updated,
        )

    print(f"\nDone. Added: {added}, Updated (tag): {updated}, Skipped (dedup): {skipped}, Errors: {errors}")
    return added, updated


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sync Gault & Millau Switzerland restaurants")
    p.add_argument("--max-urls", type=int, default=None, help="Only process N URLs")
    args = p.parse_args()
    sync(max_urls=args.max_urls)