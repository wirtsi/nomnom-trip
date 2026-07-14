"""Shared deduplication helpers for sync_*.py ingesters.

Consolidates `normalize_name()`, `build_dedup_map()`, and the "tag an
existing place on dedup" pattern that appeared in 5 ingesters
(gaultmillau_at/ch, wirtshauskultur, identitagolose, rawwine).

`sync_rawwine.py:_add_tag()` was the factored version — promoted here
and re-exported so other ingesters can drop their inline copies.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove non-alphanumeric.

    Used as the dedup key component. Matches the NFKD + combining-strip
    + `[^a-z0-9]` removal that gaultmillau_at/ch and wirtshauskultur
    already use.
    """
    name = name.lower().strip()
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", name)


def build_dedup_map(conn: sqlite3.Connection) -> dict:
    """Return {(norm_name, city.lower, country.lower) -> [place_id, ...]}.

    Call once at the start of a sync run; look up new places by their
    (norm, city, country) tuple to decide insert-vs-tag-existing.
    """
    dup_map: dict = {}
    for row in conn.execute("SELECT id, name, city, country FROM places"):
        key = (
            normalize_name(row["name"]),
            (row["city"] or "").lower().strip(),
            _normalize_country(row["country"] or ""),
        )
        dup_map.setdefault(key, []).append(row["id"])
    return dup_map


def add_tag(conn: sqlite3.Connection, place_id: int, tag: str) -> bool:
    """Append `tag` to a place's tags JSON if not already present.

    Returns True if the tag was added (caller should bump `updated`),
    False if it was already there (caller bumps `skipped`).
    """
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


_COUNTRY_ALIASES = {
    "österreich": "austria",
    "deutschland": "germany",
    "schweiz": "switzerland",
    "italia": "italy",
    "españa": "spain",
    "france": "france",
    "nederland": "netherlands",
    "belgique": "belgium",
    "slovenija": "slovenia",
    "hrvatska": "croatia",
    "magyarország": "hungary",
    "polska": "poland",
    "česko": "czech republic",
    "slovensko": "slovakia",
}


def _normalize_country(country: str) -> str:
    c = country.lower().strip()
    return _COUNTRY_ALIASES.get(c, c)


def dup_key(place: dict) -> tuple:
    """Build the dedup lookup key from a parsed place dict."""
    return (
        normalize_name(place["name"]),
        (place.get("city") or "").lower().strip(),
        _normalize_country(place.get("country") or ""),
    )