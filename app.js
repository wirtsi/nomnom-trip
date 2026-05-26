// nomnom PWA — vanilla JS app driving sql.js + Leaflet.
// Mirrors scripts/db.py:search_near() so results match the CLI byte-for-byte.

const DB_URL = `./data/restaurants.pwa.db?t=1779800226`; // set by export_pwa_db.py
const SOURCES = ["michelin", "splendido", "raisin", "gambero", "rawwine", "identitagolose", "gaultmillau", "wirtshauskultur", "mitvergnuegen"];

const els = {
  q: document.getElementById("q"),
  radius: document.getElementById("radius"),
  radiusLabel: document.getElementById("radius-label"),
  category: document.getElementById("category"),
  cuisine: document.getElementById("cuisine"),
  keyword: document.getElementById("keyword"),
  limit: document.getElementById("limit"),
  sources: document.getElementById("sources"),
  search: document.getElementById("search"),
  geo: document.getElementById("geo"),
  results: document.getElementById("results"),
  status: document.getElementById("status"),
};

let db = null;
let map = null;
let markerLayer = null;
let lastCenter = null;

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
  cuisine = null,
  keyword = null,
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
    sql += " AND category = ?";
    params.push(category);
  }
  if (sources && sources.length) {
    sql += ` AND source IN (${sources.map(() => "?").join(",")})`;
    params.push(...sources);
  }
  if (cuisine) {
    sql += " AND cuisine LIKE ?";
    params.push(`%${cuisine}%`);
  }
  if (keyword) {
    sql += " AND (name LIKE ? OR description LIKE ? OR tags LIKE ?)";
    const kw = `%${keyword}%`;
    params.push(kw, kw, kw);
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
        } catch (_) {}
      }
    }
  }

  return filtered.slice(0, limit);
}

// ---- UI -----------------------------------------------------------------
function setStatus(msg, isError = false) {
  const spinner = db ? '' : '<span class="spinner"></span>';
  els.status.innerHTML = spinner + msg;
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
  // Auto-reload when any source checkbox toggles
  for (const cb of els.sources.querySelectorAll('input[type="checkbox"]')) {
    cb.addEventListener("change", () => {
      setTimeout(() => runSearch(null), 50);
    });
  }
}

function getSelectedSources() {
  return [...els.sources.querySelectorAll("input:checked")].map((i) => i.value);
}

function sourceBadge(s) {
  return `<span class="badge badge-${s}">${s}</span>`;
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
  if (markerLayer) markerLayer.clearLayers();
  if (!rows.length) {
    els.results.innerHTML = `<p class="empty">No matches.</p>`;
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
      </header>
      ${r.cuisine ? `<div class="cuisine">${escapeHtml(r.cuisine)}</div>` : ""}
      ${addr ? `<div class="addr">${escapeHtml(addr)}</div>` : ""}
      <div class="meta">${r.distance_km.toFixed(2)} km away</div>
      ${endorseLine}
      ${
        r.description
          ? `<p class="desc">${escapeHtml(r.description)}</p>`
          : ""
      }
      <a class="source-link" href="${escapeHtml(
        r.source_url
      )}" target="_blank" rel="noopener">View on ${r.source} ↗</a>
    `;
    card.addEventListener("click", () => {
      if (r.lat != null && r.lng != null) {
        map.setView([r.lat, r.lng], 17);
      }
    });
    els.results.appendChild(card);

    if (r.lat != null && r.lng != null) {
      const marker = L.marker([r.lat, r.lng]).bindPopup(
        `<b>${escapeHtml(r.name)}</b><br>${escapeHtml(
          r.cuisine || ""
        )}<br><a href="${escapeHtml(r.source_url)}" target="_blank">${
          r.source
        }</a>`
      );
      markerLayer.addLayer(marker);
    }
  }
  if (center) {
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
  }
}

async function runSearch(center) {
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

  const rows = searchNear({
    lat: center.lat,
    lng: center.lng,
    radiusKm: parseFloat(els.radius.value),
    category: els.category.value || null,
    sources: getSelectedSources(),
    cuisine: els.cuisine.value.trim() || null,
    keyword: els.keyword.value.trim() || null,
    limit: Math.min(Math.max(parseInt(els.limit.value, 10) || 200, 10), 1000),
  });

  setStatus(`${rows.length} result${rows.length === 1 ? "" : "s"}.`);
  renderResults(rows, center);
}

function useMyLocation() {
  if (!navigator.geolocation) {
    setStatus("Geolocation not supported by this browser.", true);
    return;
  }
  setStatus("Getting your location…");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      runSearch({
        lat: pos.coords.latitude,
        lng: pos.coords.longitude,
        display_name: "your location",
      });
    },
    (err) => setStatus(`Location error: ${err.message}`, true),
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 }
  );
}

// ---- bootstrap ----------------------------------------------------------
async function loadDb() {
  setStatus("Loading database…");
  els.search.disabled = true;
  els.geo.disabled = true;
  const SQL = await initSqlJs({ locateFile: () => "./sqlite3.wasm" });
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
  els.radius.addEventListener("change", () => runSearch(null));
  els.radiusLabel.textContent = `${els.radius.value} km`;
  els.search.addEventListener("click", () => runSearch(null));
  els.q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch(null);
  });
  // Auto-reload on any filter change
  let searchTimeout = null;
  function autoSearch() {
    if (searchTimeout) return;
    searchTimeout = setTimeout(() => {
      searchTimeout = null;
      runSearch(null);
    }, 250);
  }
  els.category.addEventListener("change", autoSearch);
  els.cuisine.addEventListener("change", autoSearch);
  els.keyword.addEventListener("change", autoSearch);
  els.limit.addEventListener("change", autoSearch);
  const limitCheckbox = document.getElementById("limit-checkbox");
  if (limitCheckbox) {
    limitCheckbox.addEventListener("change", autoSearch);
  }
  els.geo.addEventListener("click", useMyLocation);
}

(async function start() {
  try {
    renderSourceCheckboxes();
    initMap();
    wireEvents();
    await loadDb();
  } catch (e) {
    setStatus(`Startup failed: ${e.message}`, true);
    console.error(e);
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("./sw.js").catch(console.error);
  }
})();
