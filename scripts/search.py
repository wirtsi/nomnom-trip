"""Web search helpers using Brave and Tavily APIs.

Two complementary search backends:
- **Brave** (`BRAVE_SEARCH_API_KEY`) — fast, structured snippets, best for
  quick "what do crowd reviews say about X" lookups (returns title + URL +
  short description per result).
- **Tavily** (`TAVILY_API_KEY`) — AI-optimized; can run "advanced" search
  with full content extraction. Best for ingesting the body of a food blog
  review or scraping a venue page on a site we don't have a native sync
  for. Also exposes a separate `extract` endpoint for known URLs.

Pure stdlib so the existing codebase constraints still hold.

CLI:

    python3 scripts/search.py brave "Babai Oristano recensioni"
    python3 scripts/search.py tavily "best restaurants Cagliari" --extract
    python3 scripts/search.py extract "https://example.com/review"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# Brave Search
# ---------------------------------------------------------------------------

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def brave_search(
    query: str,
    count: int = 5,
    country: str = "IT",
    safesearch: str = "moderate",
) -> list[dict]:
    """Return a list of {title, url, description} from Brave Search.

    Raises RuntimeError if the API call fails or BRAVE_SEARCH_API_KEY isn't
    set. `count` is capped at 20.
    """
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        raise RuntimeError("BRAVE_SEARCH_API_KEY env var not set")
    params = {
        "q": query,
        "count": min(count, 20),
        "country": country,
        "safesearch": safesearch,
    }
    url = f"{BRAVE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": key,
            "User-Agent": "nomnom/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brave HTTP {e.code}: {e.read()[:200]!r}") from e

    web = (data.get("web") or {}).get("results") or []
    return [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "description": r.get("description"),
            "age": r.get("age"),
        }
        for r in web
    ]


# ---------------------------------------------------------------------------
# Tavily Search + Extract
# ---------------------------------------------------------------------------

TAVILY_SEARCH = "https://api.tavily.com/search"
TAVILY_EXTRACT = "https://api.tavily.com/extract"


def _tavily_post(endpoint: str, body: dict, timeout: int = 60) -> dict:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY env var not set")
    body = {**body, "api_key": key}
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "nomnom/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Tavily HTTP {e.code}: {e.read()[:300]!r}") from e


def tavily_search(
    query: str,
    search_depth: str = "basic",
    max_results: int = 5,
    include_answer: bool = False,
    include_raw_content: bool = False,
) -> dict:
    """Return Tavily search results.

    `search_depth`:
      - "basic" — fast, ~5 results with title/url/snippet/score (<2s)
      - "advanced" — slower, more thorough crawl, deeper extraction
    """
    return _tavily_post(
        TAVILY_SEARCH,
        {
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
        },
    )


def tavily_extract(urls: list[str], extract_depth: str = "basic") -> dict:
    """Pull the full text content from one or more URLs."""
    return _tavily_post(
        TAVILY_EXTRACT,
        {
            "urls": urls if isinstance(urls, list) else [urls],
            "extract_depth": extract_depth,
        },
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_brave(args: argparse.Namespace) -> int:
    rows = brave_search(args.query, count=args.count)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for i, r in enumerate(rows, 1):
            print(f"{i}. {r['title']}")
            print(f"   {r['url']}")
            if r.get("description"):
                print(f"   > {r['description']}")
            print()
    return 0


def _cmd_tavily(args: argparse.Namespace) -> int:
    res = tavily_search(
        args.query,
        search_depth=args.depth,
        max_results=args.count,
        include_answer=args.answer,
    )
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        if res.get("answer"):
            print(f"Answer: {res['answer']}\n")
        for i, r in enumerate(res.get("results") or [], 1):
            print(f"{i}. {r.get('title')}")
            print(f"   {r.get('url')}  (score: {r.get('score'):.2f})")
            if r.get("content"):
                content = r["content"][:300]
                print(f"   > {content}")
            print()
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    res = tavily_extract(args.urls, extract_depth=args.depth)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("brave", help="Brave web search (fast snippets)")
    b.add_argument("query")
    b.add_argument("--count", type=int, default=5)
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=_cmd_brave)

    t = sub.add_parser("tavily", help="Tavily search (AI-optimized)")
    t.add_argument("query")
    t.add_argument("--depth", choices=["basic", "advanced"], default="basic")
    t.add_argument("--count", type=int, default=5)
    t.add_argument("--answer", action="store_true", help="Ask Tavily for a synthesized answer")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=_cmd_tavily)

    e = sub.add_parser("extract", help="Extract full content from URLs via Tavily")
    e.add_argument("urls", nargs="+")
    e.add_argument("--depth", choices=["basic", "advanced"], default="basic")
    e.set_defaults(fn=_cmd_extract)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
