# Source notes

## Michelin Guide

**Backend:** Algolia search (https://www.algolia.com)

**Endpoint:** `POST https://8nvhrd7onv-dsn.algolia.net/1/indexes/*/queries`

**Auth:** Public read-only key in headers — embedded in their JS bundle:
- `X-Algolia-Application-Id: 8NVHRD7ONV`
- `X-Algolia-API-Key: 3222e669cf890dc73fa5f38241117ba5`

**Index:** `prod-restaurants-en` (English). Per-language indexes exist
(`prod-restaurants-fr`, etc.) if you ever want them.

**Schema:** Each hit has `_geoloc {lat, lng}`, `name`, `slug`, `city`,
`country`, `region`, `area_name`, `cuisines[]`, `chef`, `michelin_award`,
`price_category`, `green_star`, `good_menu`, `url`, `main_image`.

**Volume:** ~17,000 restaurants worldwide as of last check.

**If it breaks:** Open guide.michelin.com, filter by anything, watch the XHR
to `*-dsn.algolia.net` in DevTools — the Application ID and key live in the
request headers and the index name in the body. Update the constants in
`sync_michelin.py`.

---

## Splendido Magazin

**Backend:** WordPress.

**Primary path:** `GET https://splendido-magazin.de/wp-json/wp/v2/spots?per_page=100`
(the CPT slug may also be `spot` or something else — `_try_wp_api()` probes
several common names).

**Fallback path:** Walk `/sitemap_index.xml` → individual sitemaps →
URLs containing `/spots/`. Each spot page has Open Graph tags and (sometimes)
JSON-LD.

**Volume:** ~150 spots, growing slowly.

**Catch:** The site's filter UI (anschauen / einkehren / einkaufen, Region,
Index) is JS-driven and not very useful for bulk fetching. The sitemap is the
reliable enumeration. Categories must be inferred from the slug or content
since their taxonomy isn't always exposed.

**If `wp-json` returns the spots but fields are sparse:** ACF (Advanced
Custom Fields) may be storing addresses/coords but not exposing them via
REST. In that case rely on the sitemap+scrape path — JSON-LD typically has
geo data even when the WP API hides it.

---

## Raisin

**Backend:** Custom Django/Python app, no public API documentation.

**Path used:** `GET https://www.raisin.digital/sitemap.xml` → individual
sitemaps → venue URLs matching `/explore/{country}/{region}/{city}/venues/{slug-id}/`.

Each venue page embeds JSON-LD with `@type` of `Restaurant`, `BarOrPub`,
`WineShop`, etc., plus address, geo, and description.

**Volume:** 8,000+ venues. A full scrape with the polite 0.4s delay takes
~50 minutes. The default `--max-pages` cap is 1000 per run — incremental
mode prioritises URLs we haven't seen before, so a weekly run captures new
venues quickly while gradually re-fetching older entries to catch updates.

**Mobile app:** The iOS/Android apps (`digital.raisin`) presumably hit a
JSON API. If you can MITM-proxy the app you'll find the endpoint, but
nothing public is documented. The sitemap+scrape route is stable and
sufficient.

**Country slug:** URLs use lowercase hyphenated country names
(`united-states`, `czechia`). The country field is recovered from the URL
when JSON-LD doesn't include it.

---

## Adding more sources

The pattern is: write a `sync_<source>.py` exposing a `sync()` function that
returns `(added, updated)` and writes via `db.upsert_place`. Add the source
name to the `SOURCES` list in `sync.py`.

Worthwhile candidates (see top-level chat for the full list):

- **Slow Food / Osterie d'Italia** — region-by-region, login-gated for full
  reviews but the index is public. Italian only.
- **Gambero Rosso** — has a subscription API; their `/ristoranti/` listings
  also have JSON-LD.
- **Le Fooding** — French, similar WP-backed structure.
- **The Infatuation** — has city APIs at `theinfatuation.com/api/...`,
  inspect via DevTools.
- **OAD** — Steve Plotnicki's lists, mostly static HTML pages, easy to
  scrape annually.
- **The World's 50 Best** — annual JSON dump linked from the site.
