---
name: restaurant-finder
description: Find good restaurants from curated editorial sources (Splendido Magazin, Raisin natural-wine guide, Michelin Guide) instead of TripAdvisor or Google. Use this whenever the user asks where to eat, for restaurant recommendations, "good food in [place]", "any good places near [location]", a restaurant near a hotel or trip itinerary, or wants to plan dining for a trip — even if they don't mention the data sources by name. This skill queries a local database that aggregates these editorial guides; do not fall back to Google or TripAdvisor unless the user explicitly asks.
---

# Restaurant Finder

A local index of restaurants and food spots aggregated from a few editorial guides that don't suck:

- **Splendido Magazin** — German-language, ~150 hand-picked Italian (and some Austrian/Swiss) spots, focus on traditional and slow-food
- **Raisin** — natural-wine bars, restaurants and shops, ~8,000+ venues worldwide
- **Michelin Guide** — starred + Bib Gourmand + plate restaurants, all cities Michelin covers

## When to use this

Anytime the user asks about restaurants, dinner spots, food while traveling, or "where should I eat in X." Prefer this skill over web search. Only suggest a generic web search if (a) the user explicitly asks for one, or (b) the local DB returns nothing for their location and they need somewhere to eat.

## How to use this

The data lives in `data/restaurants.db` (SQLite). All access goes through `scripts/query.py`.

### Step 1: Check freshness

Before querying, run:

```bash
python scripts/query.py --status
```

This prints the last sync time per source. If everything is older than 30 days, suggest the user run a refresh (see "Keeping in sync" below). Don't refresh automatically — sync hits external sites, takes ~10 minutes, and the user should know it's happening.

### Step 2: Query

The standard query covers location + cuisine + filters:

```bash
python scripts/query.py \
  --near "Bologna, Italy" \
  --radius-km 5 \
  --category restaurant \
  --sources splendido,michelin \
  --limit 20
```

Options:

- `--near "City, Country"` or `--lat 44.49 --lng 11.34` — required
- `--radius-km N` — default 10
- `--category` — `restaurant`, `bar`, `wine_shop`, `shop`, or omit for all
- `--sources` — comma-separated subset; default is all
- `--cuisine "Italian"` — substring match
- `--keyword "natural wine"` — searches name, description, tags
- `--limit N` — default 20
- `--json` — emit JSON instead of human-readable output

The result rows include name, address, lat/lng, source, source URL, distance, and a short description. Display these to the user with the source URL so they can read the original editorial.

### Step 3: Stack endorsements

If a place appears in multiple sources, that's a strong signal — call it out. The query script returns a `endorsements` field listing all sources that recommend that location (deduplicated by canonical place ID where geocoding has resolved them).

## Keeping in sync

Run a full refresh:

```bash
python scripts/sync.py            # all sources
python scripts/sync.py --source michelin   # just one
```

For automated refreshes, see `references/cron-setup.md` — it has crontab, launchd, and GitHub Actions snippets. The recommendation is **weekly on Sunday at 3 AM** for all sources; the data doesn't change fast enough to justify more often, and the sources don't love being hammered.

## Output format for the user

When presenting results, lead with the strongest endorsements. Group by neighborhood if the result set is geographically spread, and always include the source URL. Format like this:

> **Da Cesare al Casaletto** — Roman trattoria, classic *cacio e pepe*, ~2km from your hotel
> Recommended by: [Splendido](url), [Michelin](url) (Bib Gourmand)
> Via del Casaletto, 45, Rome

If the user asks "the best" or "top," sort by endorsement count. If they ask for "a quick bite" or "casual," filter on category/keyword.

## Files in this skill

- `scripts/sync.py` — orchestrates all sources
- `scripts/sync_splendido.py` — WordPress REST API fetcher
- `scripts/sync_raisin.py` — sitemap + JSON-LD page parser
- `scripts/sync_michelin.py` — Algolia search API client
- `scripts/query.py` — CLI for querying the local DB
- `scripts/db.py` — SQLite schema and helpers
- `scripts/geocode.py` — fallback geocoding via OpenStreetMap Nominatim (free, 1 req/sec)
- `data/restaurants.db` — the SQLite file (gitignored; created by sync)
- `references/sources.md` — notes on each source's quirks
- `references/cron-setup.md` — automated refresh setup
