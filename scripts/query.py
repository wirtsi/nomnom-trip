"""CLI for querying the local restaurant DB.

Examples:

    # Status
    python scripts/query.py --status

    # Near a city
    python scripts/query.py --near "Bologna, Italy" --radius-km 5

    # Near explicit coordinates with category filter
    python scripts/query.py --lat 44.49 --lng 11.34 --category restaurant

    # Only specific sources, with cuisine and keyword filters
    python scripts/query.py --near "Rome" --sources splendido,michelin \\
        --cuisine "Italian" --keyword "natural wine" --limit 30

    # JSON output for scripts
    python scripts/query.py --near "Berlin" --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import geocode  # noqa: E402


def cmd_status() -> int:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM sync_log ORDER BY source").fetchall()
        if not rows:
            print("No sync has run yet. Run: python scripts/sync.py")
            return 0
        now = datetime.now(timezone.utc)
        print(f"{'source':<10}  {'last run':<25}  {'age':<10}  {'status':<6}  rows")
        print("-" * 78)
        for r in rows:
            t = datetime.fromisoformat(r["last_run_at"])
            age_days = (now - t).total_seconds() / 86400
            print(
                f"{r['source']:<10}  {r['last_run_at'][:19]:<25}  "
                f"{age_days:>5.1f}d    {r['last_status']:<6}  "
                f"+{r['rows_added']}/{r['rows_updated']}"
                + (f"  | {r['last_message'][:40]}" if r["last_message"] else "")
            )
        counts = conn.execute(
            "SELECT source, COUNT(*) c FROM places GROUP BY source ORDER BY source"
        ).fetchall()
        if counts:
            print("\nTotal rows in DB:")
            for c in counts:
                print(f"  {c['source']:<10}  {c['c']}")
    return 0


def _format_row(r: dict, show_endorsements: bool = True) -> str:
    bits = [f"{r['name']}"]
    if r.get("cuisine"):
        bits.append(f"({r['cuisine']})")
    line = " ".join(bits)
    parts = [line]
    addr_parts = [r.get("address"), r.get("city"), r.get("country")]
    addr = ", ".join(p for p in addr_parts if p)
    if addr:
        parts.append(f"  {addr}")
    parts.append(f"  {r['distance_km']:.2f} km — source: {r['source']}")
    if show_endorsements and r.get("endorsements"):
        others = [e["source"] for e in r["endorsements"] if e["source"] != r["source"]]
        if others:
            parts.append(f"  ALSO recommended by: {', '.join(sorted(set(others)))}")
    parts.append(f"  {r['source_url']}")
    if r.get("description"):
        d = r["description"][:200]
        parts.append(f"  > {d}")
    return "\n".join(parts)


def cmd_search(args: argparse.Namespace) -> int:
    if args.lat is not None and args.lng is not None:
        lat, lng = args.lat, args.lng
        label = f"{lat:.4f}, {lng:.4f}"
    elif args.near:
        loc = geocode.geocode(args.near)
        if not loc:
            print(f"Could not geocode: {args.near!r}", file=sys.stderr)
            return 2
        lat, lng = loc["lat"], loc["lng"]
        label = loc.get("display_name", args.near)
    else:
        print("Provide --near or --lat/--lng", file=sys.stderr)
        return 2

    sources = None
    if args.sources:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    with db.connect() as conn:
        results = db.search_near(
            conn,
            lat=lat,
            lng=lng,
            radius_km=args.radius_km,
            category=args.category,
            sources=sources,
            cuisine=args.cuisine,
            keyword=args.keyword,
            limit=args.limit,
        )

    if args.json:
        print(json.dumps(results, default=str, indent=2))
        return 0

    if not results:
        print(f"No matches within {args.radius_km}km of {label}.")
        return 0

    print(f"{len(results)} result(s) within {args.radius_km}km of {label}:\n")
    for r in results:
        print(_format_row(r))
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="Show last-sync status per source")

    ap.add_argument("--near", help='Free-form place: "Bologna, Italy"')
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lng", type=float)
    ap.add_argument("--radius-km", type=float, default=10.0)
    ap.add_argument("--category", choices=["restaurant", "bar", "wine_shop", "shop"])
    ap.add_argument("--sources", help="Comma-separated: splendido,raisin,michelin,gambero,blog,rawwine")
    ap.add_argument("--cuisine", help="Substring match on cuisine field")
    ap.add_argument("--keyword", help="Free-text search across name/description/tags")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of human text")

    args = ap.parse_args()

    if args.status:
        return cmd_status()
    return cmd_search(args)


if __name__ == "__main__":
    sys.exit(main())
