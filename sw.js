// Bumped by export_pwa_db.py alongside the DB cache-buster.
const CACHE = "nomnom-1782794185";
const ASSETS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon.svg",
  "./app.js",
  "./sqlite3.js",
  "./sqlite3.wasm",
];

// The SQLite DB is ~23 MB.  We cache it on first successful fetch so the
// app works offline.  It's NOT in the install-time ASSETS list (that
// would block SW activation on a 23 MB download).  Instead, we let the
// app fetch it once, intercept the response, and stash it in the cache.
// On subsequent loads the SW serves the cached copy instantly.

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then(async (c) => {
      for (const url of ASSETS) {
        const res = await fetch(url, { cache: "reload" });
        if (!res.ok) throw new Error(`SW install failed: ${url} returned ${res.status}`);
        c.put(url, res);
      }
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);

  // DB requests: cache-first, but fall through to network if missing.
  // On a successful network fetch, clone the response into cache so
  // the next load is instant / offline.
  if (url.pathname.endsWith("restaurants.pwa.db")) {
    event.respondWith(
      caches.open(CACHE).then(async (c) => {
        // Try cache first — match ignoring the ?t= cache-buster query.
        // We store under the pathname-only key so a new ?t= doesn't
        // create a new cache entry.
        const cacheKey = url.pathname;
        const hit = await c.match(cacheKey);
        if (hit) return hit;
        // Cache miss — fetch from network, stash for next time.
        try {
          const resp = await fetch(event.request);
          if (resp.ok) c.put(cacheKey, resp.clone());
          return resp;
        } catch (e) {
          // Network failed and no cache — return a 503.
          return new Response("DB unavailable offline", { status: 503 });
        }
      })
    );
    return;
  }

  // All other GET requests: cache-first, fall back to network.
  event.respondWith(
    caches.match(event.request).then((hit) => hit || fetch(event.request))
  );
});