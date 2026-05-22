"""Run all source ingesters end-to-end.

Usage:
    python scripts/sync.py                       # all sources
    python scripts/sync.py --source michelin     # one source
    python scripts/sync.py --source raisin --max-pages 200

Designed to run from cron — exits non-zero on any source failure so the
scheduler picks up problems, but completes the other sources first.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SOURCES = ["michelin", "splendido", "raisin", "gambero", "blog", "rawwine", "identitagolose", "gaultmillau_at", "gaultmillau_ch", "wirtshauskultur", "schlemmeratlas"]


def run_one(name: str, **kwargs) -> tuple[str, int, int, str]:
    """Returns (source, added, updated, error_message). Empty error on success."""
    mod = importlib.import_module(f"sync_{name}")
    started = time.time()
    try:
        if name == "raisin":
            added, updated = mod.sync(
                max_pages=kwargs.get("max_pages", 1000),
                country=kwargs.get("country"),
            )
        elif name == "rawwine":
            added, updated = mod.sync(max_pages=kwargs.get("max_pages", 500))
        elif name in ("gaultmillau_at", "gaultmillau_ch", "wirtshauskultur", "schlemmeratlas"):
            added, updated = mod.sync(max_urls=kwargs.get("max_urls"))
        else:
            added, updated = mod.sync()
        elapsed = time.time() - started
        print(f"[{name}] +{added} new, {updated} updated, {elapsed:.1f}s")
        return name, added, updated, ""
    except Exception as e:
        elapsed = time.time() - started
        msg = f"{type(e).__name__}: {e}"
        print(f"[{name}] FAILED after {elapsed:.1f}s: {msg}", file=sys.stderr)
        traceback.print_exc()
        return name, 0, 0, msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=SOURCES, help="Limit to one source")
    ap.add_argument("--max-pages", type=int, default=1000,
                    help="Cap on pages scraped per run for paginated sources")
    ap.add_argument("--country", help="For raisin: filter URL slug, e.g. 'italy'")
    args = ap.parse_args()

    sources = [args.source] if args.source else SOURCES
    failures: list[str] = []
    for s in sources:
        if s == "raisin":
            kwargs = {"max_pages": args.max_pages, "country": args.country}
        elif s == "rawwine":
            kwargs = {"max_pages": args.max_pages}
        else:
            kwargs = {}
        _, _, _, err = run_one(s, **kwargs)
        if err:
            failures.append(f"{s}: {err}")

    if failures:
        print(f"\n{len(failures)} source(s) failed:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
