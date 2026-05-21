# Restaurant Finder Skill

Local index of curated, editorial restaurant guides — an alternative to
TripAdvisor / Google reviews for finding good food while travelling.

## Sources

| Source | Strength | Volume |
|---|---|---|
| [Michelin Guide](https://guide.michelin.com) | Global, structured, includes Bib Gourmand | ~17,000 |
| [Splendido Magazin](https://splendido-magazin.de) | Hand-picked Italy/AT/CH spots, slow-food bias | ~150 |
| [Raisin](https://www.raisin.digital) | Natural-wine bars/restaurants worldwide | ~8,000 |

All three are aggregated into a single SQLite DB queryable by location,
cuisine, category, and source. When a place appears in multiple sources,
that's stacked as an "endorsement" signal.

## Quick start

```bash
# 1. Run a first sync (~10–60 minutes depending on Raisin pagination)
uv run python scripts/sync.py

# 2. Check it worked
uv run python scripts/query.py --status

# 3. Query
uv run python scripts/query.py --near "Rome, Italy" --radius-km 3
```

## Automating refresh

`python scripts/sync.py` is the only command needed. Wire it into:

- **Linux/WSL:** `crontab -e` — see `references/cron-setup.md`
- **macOS:** launchd plist — see `references/cron-setup.md`
- **No machine of your own:** GitHub Actions workflow — see `references/cron-setup.md`

Recommended cadence: **weekly, Sunday 03:00**.

## PWA

An offline-capable Progressive Web App ships the DB to the browser via sql.js
(WASM) and mirrors `db.py:search_near()`, so results match the CLI. Build the
slim DB and serve from the repo root:

```bash
uv run python scripts/export_pwa_db.py
python3 -m http.server 8080
# open http://localhost:8080/
```

## Layout

```
restaurant-finder/
├── index.html                     # PWA shell (served on GitHub Pages)
├── app.js                         # PWA logic (sql.js + Leaflet)
├── sw.js                          # Service Worker for offline shell
├── manifest.json                  # Installable app manifest
├── icon.svg                       # App icon
├── sqlite3.js / sqlite3.wasm      # sql.js WASM distribution
├── SKILL.md                       # Trigger & usage instructions
├── README.md                      # This file
├── scripts/
│   ├── db.py                      # SQLite schema + helpers
│   ├── sync.py                    # Orchestrator
│   ├── sync_michelin.py           # Michelin via Algolia
│   ├── sync_splendido.py          # Splendido via WP REST + sitemap fallback
│   ├── sync_raisin.py             # Raisin via sitemap + JSON-LD scrape
│   ├── query.py                   # CLI for searching the DB
│   └── geocode.py                 # OSM Nominatim geocoder
├── references/
│   ├── sources.md                 # Per-source quirks and recovery notes
│   └── cron-setup.md              # Three scheduler setups
└── data/
    ├── restaurants.db             # The aggregated DB (created by sync)
    ├── restaurants.pwa.db         # Slim WASM-shippable DB (created by export)
    └── geocode_cache.sqlite       # Geocoding cache
```
## Dependencies

Pure Python 3.10+ standard library — no `pip install` needed. (The choice
to avoid `requests`, `beautifulsoup`, and `geopy` is deliberate: keeps the
skill droppable into any environment, including GitHub Actions, without a
requirements step.)
