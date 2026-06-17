"""Shared sitemap walking helpers for sync_*.py ingesters.

Consolidates the duplicated `_parse_xml()` + sitemap-index traversal
that appeared in 5 ingesters (gaultmillau_at/ch, wirtshauskultur,
splendido, raisin).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable, Optional

from httputil import get

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def parse_xml(data: bytes) -> ET.Element:
    """Parse sitemap XML, tolerating leading whitespace before <?xml.

    ElementTree is strict about `<?xml` being the very first bytes;
    some sites (notably splendido-magazin.de) emit leading whitespace.
    """
    return ET.fromstring(data.lstrip())


def iter_urls(
    index_url: str,
    *,
    path_filter: Optional[Callable[[str], bool]] = None,
    timeout: int = 30,
    user_agent: str = "restaurant-finder-skill/0.1 (+contact in skill repo)",
) -> list[str]:
    """Walk a sitemap (index or flat) and return all matching `<loc>` URLs.

    If `index_url` points to a sitemap index (a list of child sitemaps),
    each child is fetched and its URLs are collected. If it points to a
    flat sitemap, its URLs are returned directly.

    `path_filter(url) -> bool` selects which `<loc>` URLs to keep.
    Defaults to "keep everything".
    """
    if path_filter is None:
        path_filter = lambda _url: True  # noqa: E731

    try:
        idx_body = get(index_url, accept="application/xml", timeout=timeout, user_agent=user_agent)
    except Exception:
        return []

    try:
        root = parse_xml(idx_body)
    except ET.ParseError:
        return []

    children = list(root)
    is_index = children and children[0].tag.endswith("sitemap")

    sitemap_urls: list[str] = []
    if is_index:
        for loc in root.findall("sm:sitemap/sm:loc", NS):
            if loc.text:
                sitemap_urls.append(loc.text.strip())
    else:
        sitemap_urls = [index_url]

    out: list[str] = []
    for sm_url in sitemap_urls:
        try:
            sm_body = get(sm_url, accept="application/xml", timeout=timeout, user_agent=user_agent)
            sm_root = parse_xml(sm_body)
        except Exception:
            continue
        for loc in sm_root.findall("sm:url/sm:loc", NS):
            if loc.text and path_filter(loc.text.strip()):
                out.append(loc.text.strip())
    return out