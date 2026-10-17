// nomnom PWA — vanilla JS app driving sql.js + Leaflet.
// Mirrors scripts/db.py:search_near() so results match the CLI byte-for-byte.

const DB_URL = `./data/restaurants.pwa.db?t=1790270956`; // set by export_pwa_db.py
const SOURCES = ["michelin", "splendido", "raisin", "gambero", "blog", "rawwine", "identitagolose", "gaultmillau", "wirtshauskultur", "mitvergnuegen", "petitepassport"];

const els = {
  q: document.getElementById("q"),
  radius: document.getElementById("radius"),
  radiusLabel: document.getElementById("radius-label"),
  category: document.getElementById("category"),
  searchText: document.getElementById("searchText"),
  limit: document.getElementById("limit"),
  sources: document.getElementById("sources"),
  search: document.getElementById("search"),
  geo: document.getElementById("geo"),
  results: document.getElementById("results"),
  status: document.getElementById("status"),
  favorites: document.getElementById("favorites"),
  favoritesStatus: document.getElementById("favorites-status"),
};
// Fail loudly if the HTML template is missing an element we depend on,
// instead of cryptic TypeError: Cannot read properties of null later.
for (const [k, v] of Object.entries(els)) {
  if (!v) throw new Error(`nomnom: missing DOM element for "${k}"`);
}

let db = null;
let map = null;
let markerLayer = null;
let lastCenter = null;
let lastSearchRows = [];
let lastSearchStatus = { msg: "Ready — search for a place to begin.", isError: false };
let showingFavorites = false;
let searchMapView = null;
let renderedHearts = [];
let locationRequest = 0;

// ---- favorites — small snapshots, independent of the current DB/search ---
const FAVORITES_KEY = "nomnom:favorites:v1";
let favorites = new Map();

function favoriteKey(place) {
  if (!place || typeof place.source !== "string" || !place.source.trim()) return null;
  const id = place.source_id;
  const identity = (typeof id === "string" && id.trim() ? id : "") ||
    (typeof id === "number" && Number.isFinite(id) ? String(id) : "") ||
    (typeof place.source_url === "string" && place.source_url.trim() ? place.source_url : "");
  return identity ? JSON.stringify([place.source, identity]) : null;
}

function safeSourceUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url) ? url : "";
}

function favoriteSnapshot(place) {
  if (!favoriteKey(place) || typeof place.name !== "string" || !place.name.trim()) return null;
  const snapshot = {};
  for (const field of ["source", "name", "address", "city", "country", "category", "cuisine", "description"]) {
    snapshot[field] = typeof place[field] === "string" ? place[field] : "";
  }
  snapshot.source_id = typeof place.source_id === "string" ||
    (typeof place.source_id === "number" && Number.isFinite(place.source_id))
    ? String(place.source_id) : null;
  snapshot.source_url = safeSourceUrl(place.source_url);
  // Coordinates are optional. Never coerce null/strings into a location at 0,0.
  snapshot.lat = Number.isFinite(place.lat) && Math.abs(place.lat) <= 90 ? place.lat : null;
  snapshot.lng = Number.isFinite(place.lng) && Math.abs(place.lng) <= 180 ? place.lng : null;
  let tags = place.tags;
  if (typeof tags === "string") {
    try { tags = JSON.parse(tags); } catch (_) { tags = []; }
  }
  snapshot.tags = Array.isArray(tags) ? tags.filter((tag) => typeof tag === "string") : [];
  snapshot.endorsements = Array.isArray(place.endorsements) ? place.endorsements
    .filter((e) => e && typeof e.source === "string")
    .map((e) => ({ source: e.source, url: safeSourceUrl(e.url) })) : [];
  return favoriteKey(snapshot) ? snapshot : null;
}

function readFavorites() {
  let raw;
  try {
    raw = localStorage.getItem(FAVORITES_KEY);
  } catch (_) {
    els.favoritesStatus.textContent = "Favorites storage is unavailable. Changes could not be saved.";
    return null;
  }
  const saved = new Map();
  if (raw === null) return saved;
  try {
    const data = JSON.parse(raw);
    if (!data || data.version !== 1 || !Array.isArray(data.places)) throw new Error("Invalid favorites format");
    for (const place of data.places) {
      const snapshot = favoriteSnapshot(place);
      if (snapshot) saved.set(favoriteKey(snapshot), snapshot);
    }
  } catch (_) {
    els.favoritesStatus.textContent = "Saved favorites could not be read. Save a restaurant to start a new list.";
  }
  return saved;
}

function isFavorite(place) {
  return favorites.has(favoriteKey(place));
}

function updateHeart(button, place) {
  const saved = isFavorite(place);
  button.setAttribute("aria-pressed", String(saved));
  button.setAttribute("aria-label", `${saved ? "Remove" : "Save"} ${place.name} ${saved ? "from" : "to"} favorites`);
  button.innerHTML = `<span aria-hidden="true">${saved ? "♥" : "♡"}</span>`;
  button.classList.toggle("is-favorite", saved);
}

function updateFavoritesUI() {
  els.favorites.textContent = `${showingFavorites ? "Back to results · " : "♡ "}Favorites (${favorites.size})`;
  els.favorites.setAttribute("aria-pressed", String(showingFavorites));
  for (const { button, place } of renderedHearts) updateHeart(button, place);
}

function toggleFavorite(place) {
  const snapshot = favoriteSnapshot(place);
  if (!snapshot) {
    els.favoritesStatus.textContent = "This restaurant could not be saved: missing source identity.";
    return false;
  }
  // Re-read before writing, so a recent change in another tab isn't lost.
  const next = readFavorites();
  if (!next) return false;
  const key = favoriteKey(snapshot);
  if (next.has(key)) next.delete(key);
  else next.set(key, snapshot);
  try {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify({ version: 1, places: [...next.values()] }));
  } catch (_) {
    els.favoritesStatus.textContent = "Favorites could not be saved. Browser storage may be full or blocked; your list is unchanged.";
    return false;
  }
  // Only claim success after storage accepts the write (including removals).
  favorites = next;
  els.favoritesStatus.textContent = "";
  updateFavoritesUI();
  if (showingFavorites) renderFavorites();
  return true;
}

function renderFavorites() {
  renderResults([...favorites.values()], null);
  drawStatus(`${favorites.size} favorite${favorites.size === 1 ? "" : "s"} — all cities, regardless of search filters.`);
}

function setFavoritesView(show) {
  if (show === showingFavorites) return;
  if (_searchTimeout) clearTimeout(_searchTimeout);
  _searchTimeout = null;
  if (show && map?.getCenter) searchMapView = { center: map.getCenter(), zoom: map.getZoom() };
  showingFavorites = show;
  if (show) locationRequest++; // Invalidate pending GPS/reverse-geocoding callbacks.
  updateFavoritesUI();
  if (show) renderFavorites();
  else {
    renderResults(lastSearchRows, lastCenter);
    if (searchMapView && map) map.setView(searchMapView.center, searchMapView.zoom);
    drawStatus(lastSearchStatus.msg, lastSearchStatus.isError);
  }
}

function initFavorites() {
  favorites = readFavorites() || new Map();
  updateFavoritesUI();
  els.favorites.addEventListener("click", () => setFavoritesView(!showingFavorites));
  window.addEventListener("storage", (event) => {
    if (event.key !== FAVORITES_KEY && event.key !== null) return;
    // Ignore similarly named sessionStorage changes; accessing storage may throw.
    try { if (event.storageArea && event.storageArea !== localStorage) return; } catch (_) { return; }
    const saved = readFavorites();
    if (!saved) return;
    favorites = saved;
    updateFavoritesUI();
    if (showingFavorites) renderFavorites();
  });
}

// ---- haversine — mirror of db.py:haversine_km ---------------------------
function haversineKm(lat1, lng1, lat2, lng2) {
  const R = 6371.0;
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dp = ((lat2 - lat1) * Math.PI) / 180;
  const dl = ((lng2 - lng1) * Math.PI) / 180;
  const a =
    Math.sin(dp / 2) ** 2 +
    Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// ---- geocode via Nominatim ---------------------------------------------
async function geocode(q) {
  const url =
    "https://nominatim.openstreetmap.org/search?q=" +
    encodeURIComponent(q) +
    "&format=json&limit=1";
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) return null;
  const data = await resp.json();
  if (!data.length) return null;
  const d = data[0];
  return {
    lat: parseFloat(d.lat),
    lng: parseFloat(d.lon),
    display_name: d.display_name,
  };
}

// ---- search_near — mirror of db.py:search_near --------------------------
function searchNear({
  lat,
  lng,
  radiusKm = 10,
  category = null,
  sources = null,
  searchText = null,
  limit = 20,
}) {
  const dlat = radiusKm / 111.0;
  const dlng = radiusKm / (111.0 * Math.max(Math.cos((lat * Math.PI) / 180), 0.01));

  let sql = `SELECT * FROM places
             WHERE lat BETWEEN ? AND ?
               AND lng BETWEEN ? AND ?
               AND lat IS NOT NULL AND lng IS NOT NULL`;
  const params = [lat - dlat, lat + dlat, lng - dlng, lng + dlng];

  if (category) {
    sql += " AND (category = ?";
    params.push(category);
    // Mitvergnuegen stores activity tags in cuisine field, not category
    if (category === 'restaurant') {
      sql += " OR (source = 'mitvergnuegen' AND cuisine LIKE '%Essen%')";
    } else if (category === 'bar') {
      sql += " OR (source = 'mitvergnuegen' AND cuisine LIKE '%Trinken%' AND cuisine NOT LIKE '%Essen%')";
    }
    sql += ")";
  }
  if (sources && sources.length) {
    sql += ` AND source IN (${sources.map(() => "?").join(",")})`;
    params.push(...sources);
  }
  if (searchText) {
    // Escape LIKE wildcards so user input is treated literally
    const escaped = searchText.replace(/[%_\\]/g, "\\$&");
    sql += " AND (name LIKE ? ESCAPE '\\' OR cuisine LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')";
    const kw = `%${escaped}%`;
    params.push(kw, kw, kw, kw);
  }

  const stmt = db.prepare(sql);
  stmt.bind(params);
  const rows = [];
  while (stmt.step()) rows.push(stmt.getAsObject());
  stmt.free();

  for (const r of rows) {
    r.distance_km = haversineKm(lat, lng, r.lat, r.lng);
  }
  let filtered = rows.filter((r) => r.distance_km <= radiusKm);
  filtered.sort((a, b) => a.distance_km - b.distance_km);

  const ids = filtered.map((r) => r.id);
  if (ids.length) {
    const placeholders = ids.map(() => "?").join(",");
    const cstmt = db.prepare(
      `SELECT cl.place_id, cl.canonical_id, p.source, p.source_url
         FROM canonical_links cl
         JOIN canonical_links cl2 USING (canonical_id)
         JOIN places p ON p.id = cl2.place_id
        WHERE cl.place_id IN (${placeholders})`
    );
    cstmt.bind(ids);
    const endorsements = new Map();
    while (cstmt.step()) {
      const c = cstmt.getAsObject();
      if (!endorsements.has(c.place_id)) endorsements.set(c.place_id, []);
      endorsements.get(c.place_id).push({ source: c.source, url: c.source_url });
    }
    cstmt.free();
    for (const r of filtered) {
      r.endorsements = endorsements.get(r.id) || [];
      if (r.tags) {
        try {
          r.tags = JSON.parse(r.tags);
        } catch (e) {
          console.warn("Bad tags JSON for place", r.id, e.message);
          r.tags = [];
        }
      }
    }
  }

  return filtered.slice(0, limit);
}

// ---- UI -----------------------------------------------------------------
// Module-level debounce for auto-reload on filter changes.
let _searchTimeout = null;
function autoSearch() {
  if (showingFavorites || _searchTimeout) return;
  _searchTimeout = setTimeout(() => {
    _searchTimeout = null;
    if (!showingFavorites) runSearch(lastCenter);
  }, 250);
}

function setStatus(msg, isError = false) {
  lastSearchStatus = { msg, isError };
  if (!showingFavorites) drawStatus(msg, isError);
}

function drawStatus(msg, isError = false) {
  const spinner = db || showingFavorites || isError ? '' : '<span class="spinner"></span>';
  // spinner is trusted HTML; msg may contain user input — escape it.
  els.status.innerHTML = spinner + escapeHtml(msg);
  els.status.classList.toggle("error", isError);
}

function renderSourceCheckboxes() {
  els.sources.innerHTML = "";
  for (const s of SOURCES) {
    const id = `src-${s}`;
    const label = document.createElement("label");
    label.className = "src-toggle";
    label.innerHTML = `<input type="checkbox" id="${id}" value="${s}" checked> ${s}`;
    els.sources.appendChild(label);
  }
  // Auto-reload when any source checkbox toggles (shared 250ms debounce)
  for (const cb of els.sources.querySelectorAll('input[type="checkbox"]')) {
    cb.addEventListener("change", autoSearch);
  }
}

function getSelectedSources() {
  return [...els.sources.querySelectorAll("input:checked")].map((i) => i.value);
}

function sourceBadge(s) {
  const badgeClass = SOURCES.includes(s) ? ` badge-${s}` : "";
  return `<span class="badge${badgeClass}">${escapeHtml(s)}</span>`;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderResults(rows, center) {
  els.results.innerHTML = "";
  renderedHearts = [];
  if (markerLayer) markerLayer.clearLayers();
  if (!rows.length) {
    els.results.innerHTML = showingFavorites
      ? `<p class="empty">No favorites yet. Tap the ♡ heart on a restaurant to save it here. Favorites stay on this browser, across all cities.</p>`
      : `<p class="empty">No matches.</p>`;
    return;
  }
  for (const r of rows) {
    const addr = [r.address, r.city, r.country].filter(Boolean).join(", ");
    const others = (r.endorsements || [])
      .map((e) => e.source)
      .filter((s) => s !== r.source);
    const uniqueOthers = [...new Set(others)].sort();
    const endorseLine = uniqueOthers.length
      ? `<div class="endorse">Also recommended by: ${uniqueOthers
          .map(sourceBadge)
          .join(" ")}</div>`
      : "";
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = `
      <header>
        <h3>${escapeHtml(r.name)}</h3>
        ${sourceBadge(r.source)}
        <button type="button" class="favorite-heart" aria-pressed="false" aria-label="Save ${escapeHtml(r.name)} to favorites"><span aria-hidden="true">♡</span></button>
      </header>
      ${r.cuisine ? `<div class="cuisine">${escapeHtml(r.cuisine)}</div>` : ""}
      ${addr ? `<div class="addr">${escapeHtml(addr)}</div>` : ""}
      ${Number.isFinite(r.distance_km) ? `<div class="meta">${r.distance_km.toFixed(2)} km away</div>` : ""}
      ${endorseLine}
      ${
        r.description
          ? `<p class="desc">${escapeHtml(r.description)}</p>`
          : ""
      }
      <a class="source-link" href="${escapeHtml(
        safeSourceUrl(r.source_url)
      )}" target="_blank" rel="noopener">View on ${escapeHtml(r.source)} ↗</a>
    `;
    const heart = card.querySelector(".favorite-heart");
    renderedHearts.push({ button: heart, place: r });
    updateHeart(heart, r);
    heart.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      toggleFavorite(r);
    });
    card.addEventListener("click", () => {
      if (map && r.lat != null && r.lng != null) {
        map.setView([r.lat, r.lng], 17);
      }
    });
    els.results.appendChild(card);

    if (markerLayer && r.lat != null && r.lng != null) {
      const marker = L.marker([r.lat, r.lng]).bindPopup(
        `<b>${escapeHtml(r.name)}</b><br>${escapeHtml(
          r.cuisine || ""
        )}<br><a href="${escapeHtml(safeSourceUrl(r.source_url))}" target="_blank" rel="noopener">${
          escapeHtml(r.source)
        }</a>`
      );
      markerLayer.addLayer(marker);
    }
  }
  if (center && markerLayer && map) {
    L.circleMarker([center.lat, center.lng], {
      radius: 8,
      color: "#ff6b35",
      weight: 2,
      fillColor: "#ff6b35",
      fillOpacity: 0.5,
    }).addTo(markerLayer);
    const bounds = markerLayer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds.pad(0.1));
    else map.setView([center.lat, center.lng], 13);
  } else if (showingFavorites && markerLayer && map) {
    const bounds = markerLayer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds.pad(0.1));
  }
}

async function runSearch(center) {
  locationRequest++;
  setFavoritesView(false);
  if (!db) {
    setStatus("Database still loading…");
    return;
  }
  if (!center) {
    const q = els.q.value.trim();
    if (!q) {
      setStatus("Enter a place or tap “Use my location”.", true);
      return;
    }
    setStatus(`Geocoding “${q}”…`);
    try {
      center = await geocode(q);
    } catch (e) {
      setStatus(`Geocoding failed: ${e.message}`, true);
      return;
    }
    if (!center) {
      setStatus(`Could not geocode “${q}”.`, true);
      return;
    }
  }
  lastCenter = center;
  setStatus(
    `Searching within ${els.radius.value} km of ${
      center.display_name || `${center.lat.toFixed(4)}, ${center.lng.toFixed(4)}`
    }…`
  );

  let rows;
  try {
    rows = searchNear({
      lat: center.lat,
      lng: center.lng,
      radiusKm: parseFloat(els.radius.value),
      category: els.category.value || null,
      sources: getSelectedSources(),
      searchText: els.searchText.value.trim() || null,
      limit: Math.min(Math.max(parseInt(els.limit.value, 10) || 200, 10), 1000),
    });
  } catch (e) {
    setStatus(`Search failed: ${e.message}`, true);
    console.error(e);
    return;
  }

  setStatus(`${rows.length} result${rows.length === 1 ? "" : "s"}.`);
  lastSearchRows = rows;
  if (!showingFavorites) renderResults(rows, center);
}

function useMyLocation() {
  setFavoritesView(false);
  const request = ++locationRequest;
  if (!navigator.geolocation) {
    setStatus("Geolocation not supported by this browser.", true);
    return;
  }
  setStatus("Getting your location…");
  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      if (request !== locationRequest) return;
      const lat = pos.coords.latitude;
      const lng = pos.coords.longitude;
      let display_name = "your location";
      // Reverse geocode via Nominatim to get city name
      try {
        const url = `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json`;
        const resp = await fetch(url, { headers: { Accept: "application/json" } });
        if (resp.ok) {
          const data = await resp.json();
          if (request !== locationRequest) return;
          // Prefer city, town, village — fall back to display name
          display_name =
            data.address?.city ||
            data.address?.town ||
            data.address?.village ||
            data.display_name ||
            `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
          els.q.value = display_name;
        }
      } catch (_) {
        // silent fallback — keep "your location"
      }
      if (request === locationRequest) runSearch({ lat, lng, display_name });
    },
    (err) => {
      if (request === locationRequest) setStatus(`Location error: ${err.message}`, true);
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 }
  );
}

// ---- bootstrap ----------------------------------------------------------
async function loadDb() {
  setStatus("Loading database…");
  els.search.disabled = true;
  els.geo.disabled = true;
  const SQL = await initSqlJs({ locateFile: () => "./sqlite3.wasm" });
  // The SW intercepts this fetch and serves the cached DB if available.
  // On first load (or after a DB update), it fetches from network and
  // caches the response.  The ?t= cache-buster ensures we get the new
  // version when the DB changes; the SW stores under pathname-only key.
  const resp = await fetch(DB_URL);
  if (!resp.ok) throw new Error(`DB fetch failed: ${resp.status}`);
  const buf = new Uint8Array(await resp.arrayBuffer());
  db = new SQL.Database(buf);
  const count = db.exec("SELECT COUNT(*) FROM places")[0].values[0][0];
  setStatus(`Ready — ${count.toLocaleString()} places indexed.`);
  els.search.disabled = false;
  els.geo.disabled = false;
}

function initMap() {
  map = L.map("map", { zoomControl: true }).setView([45, 10], 5);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap",
    maxZoom: 19,
  }).addTo(map);
  markerLayer = L.featureGroup().addTo(map);
}

function wireEvents() {
  els.radius.addEventListener("input", () => {
    els.radiusLabel.textContent = `${els.radius.value} km`;
  });
  els.radius.addEventListener("change", autoSearch);
  els.radiusLabel.textContent = `${els.radius.value} km`;
  els.search.addEventListener("click", () => runSearch(null));
  els.q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch(null);
  });
  els.category.addEventListener("change", autoSearch);
  els.searchText.addEventListener("input", autoSearch);
  els.limit.addEventListener("change", autoSearch);
  els.geo.addEventListener("click", useMyLocation);
}

(async function start() {
  try {
    renderSourceCheckboxes();
    initMap();
    wireEvents();
    initFavorites();
    await loadDb();
  } catch (e) {
    setStatus(`Startup failed: ${e.message}`, true);
    console.error(e);
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("./sw.js").catch((e) => {
      console.error("SW registration failed:", e);
      // Non-blocking — app still works online, just no offline mode.
      setStatus("Offline mode unavailable: " + (e.message || "SW failed"), true);
    });
  }
})();
