const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Exercise the real global script without starting network/SQLite bootstrap.
const script = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8')
  .split('(async function start() {')[0];
class Element {
  constructor() { this.children = []; this.listeners = {}; this.attrs = {}; this.value = ''; this.classList = { toggle() {} }; }
  set innerHTML(html) { this.html = html; this.children = []; this.heart = null; }
  get innerHTML() { return this.html || ''; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  appendChild(child) { this.children.push(child); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelector(selector) {
    if (selector !== '.favorite-heart') return null;
    if (!this.heart) {
      this.heart = new Element();
      for (const [, key, value] of this.innerHTML.matchAll(/(aria-[\w-]+)="([^"]*)"/g)) this.heart.setAttribute(key, value);
    }
    return this.heart;
  }
  querySelectorAll() { return []; }
}
function boot({ raw, blockedRead = false, blockedAccess = false, failWrite = false } = {}) {
  const elements = new Map();
  const events = {};
  let stored = raw ?? null;
  const storage = {
    getItem() { if (blockedRead) throw new Error('blocked'); return stored; },
    setItem(key, value) { if (failWrite) throw new Error('quota'); stored = value; },
  };
  const context = vm.createContext({
    document: { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }, createElement() { return new Element(); } },
    window: { addEventListener(type, fn) { events[type] = fn; } },
    console, setTimeout, clearTimeout, navigator: {}, URL,
    L: { marker() { return { bindPopup() { return this; } }; }, circleMarker() { return { addTo(layer) { layer.addLayer(this); } }; } },
  });
  Object.defineProperty(context, 'localStorage', { get() { if (blockedAccess) throw new Error('blocked access'); return storage; } });
  vm.runInContext(script, context, { filename: 'app.js' });
  const run = (code) => vm.runInContext(code, context);
  run(`map = { moves: 0, setView() { this.moves++; }, fitBounds() {} };
    markerLayer = { layers: [], clearLayers() { this.layers = []; }, addLayer(x) { this.layers.push(x); },
      getBounds() { return { isValid() { return false; }, pad() { return this; } }; } };
    initFavorites();`);
  return { run, elements, events, storage, stored: () => stored, context };
}
const place = { id: 7, source: 'michelin', source_id: 'ristorante-42', source_url: 'https://example.com/42', name: 'A "B" & <C>', city: 'Bologna', country: 'Italy', lat: 44.49, lng: 11.34, cuisine: 'Italian', tags: '["natural wine","pizza"]', endorsements: [{ source: 'blog', url: 'https://example.com/blog' }], distance_km: 1.23 };
function put(app, value = place) { app.context.place = value; }

test('identity survives DB row ID rebuilds, separates sources, and falls back to source URL', () => {
  const app = boot(); put(app);
  assert.equal(app.run('favoriteKey(place)'), '["michelin","ristorante-42"]');
  assert.equal(app.run('favoriteKey({...place, id: 999})'), app.run('favoriteKey(place)'));
  assert.notEqual(app.run('favoriteKey({...place, source: "blog"})'), app.run('favoriteKey(place)'));
  assert.equal(app.run('favoriteKey({...place, source_id: null})'), '["michelin","https://example.com/42"]');
  assert.equal(app.run('favoriteKey({id: 7, source: "blog"})'), null);
});

test('save/un-save roundtrip, compact snapshot and reload with parsed tags', () => {
  const app = boot(); put(app, { ...place, unused: 'do not store' });
  assert.equal(app.run('toggleFavorite(place)'), true);
  assert.equal(app.run('isFavorite({...place, id: 987})'), true);
  const payload = JSON.parse(app.stored());
  assert.equal(payload.version, 1);
  assert.equal(payload.places.length, 1);
  assert.deepEqual(payload.places[0].tags, ['natural wine', 'pizza']);
  assert.equal('id' in payload.places[0], false);
  assert.equal('unused' in payload.places[0], false);
  assert.equal('distance_km' in payload.places[0], false);
  const reload = boot({ raw: app.stored() }); put(reload);
  assert.equal(reload.run('isFavorite(place)'), true);
  assert.equal(reload.run('toggleFavorite({...place, id: 111})'), true);
  assert.equal(JSON.parse(reload.stored()).places.length, 0);
});

test('corrupt JSON, invalid versions/shapes and invalid snapshots are safe', () => {
  for (const raw of ['{bad', 'null', '[]', '{"version":2,"places":[]}', '{"version":1,"places":{}}', '{"version":1,"places":[null,{},7]}']) {
    const app = boot({ raw });
    assert.equal(app.run('favorites.size'), 0, raw);
    app.run('setFavoritesView(true)');
    assert.match(app.elements.get('results').innerHTML, /No favorites yet/);
  }
});

test('blocked storage and quota failure never falsely mark a place saved or removed', () => {
  for (const options of [{ blockedRead: true }, { blockedAccess: true }, { failWrite: true }]) {
    const app = boot(options); put(app);
    const success = app.run('toggleFavorite(place)');
    assert.equal(success, false);
    assert.equal(app.run('isFavorite(place)'), success);
    if (!success) assert.match(app.elements.get('favorites-status').textContent, /not saved|could not|unable/i);
  }
  const seed = boot(); put(seed); seed.run('toggleFavorite(place)');
  const app = boot({ raw: seed.stored(), failWrite: true }); put(app);
  assert.equal(app.run('toggleFavorite(place)'), false);
  assert.equal(app.run('isFavorite(place)'), true);
});

test('global saved view works before a search, survives reload, ignores filters and handles null distances', () => {
  const seed = boot(); put(seed); seed.run('toggleFavorite(place)');
  const app = boot({ raw: seed.stored() });
  app.run('setFavoritesView(true)');
  assert.equal(app.elements.get('results').children.length, 1);
  assert.doesNotMatch(app.elements.get('results').children[0].innerHTML, /km away|NaN/);
  assert.match(app.elements.get('favorites').textContent, /1/);
  assert.equal(app.elements.get('favorites').getAttribute('aria-pressed'), 'true');
  app.run('runSearch = () => { throw new Error("Unexpected search/geocode"); }; autoSearch()');
  assert.equal(app.run('_searchTimeout'), null);
});

test('heart is escaped, labelled, pressed, stops propagation, and removes the last saved marker/card', () => {
  const app = boot(); put(app); app.run('renderResults([place], null)');
  const card = app.elements.get('results').children[0];
  assert.match(card.innerHTML, /A &quot;B&quot; &amp; &lt;C&gt;/);
  const heart = card.querySelector('.favorite-heart');
  assert.equal(heart.getAttribute('aria-pressed'), 'false');
  assert.match(heart.getAttribute('aria-label'), /favorites/);
  let stopped = false; let prevented = false;
  heart.listeners.click({ stopPropagation() { stopped = true; }, preventDefault() { prevented = true; } });
  assert.equal(stopped, true); assert.equal(prevented, true);
  assert.equal(app.run('map.moves'), 0);
  assert.equal(heart.getAttribute('aria-pressed'), 'true');
  app.run('setFavoritesView(true)');
  assert.equal(app.run('markerLayer.layers.length'), 1);
  app.elements.get('results').children[0].querySelector('.favorite-heart').listeners.click({ stopPropagation() {}, preventDefault() {} });
  assert.match(app.elements.get('results').innerHTML, /No favorites yet/);
  assert.equal(app.run('markerLayer.layers.length'), 0);
  assert.match(app.elements.get('favorites').textContent, /0/);
});

test('leaving saved view restores the last search results and center without geocoding', () => {
  const app = boot(); put(app);
  app.run('lastSearchRows = [place]; lastCenter = {lat: 44.49, lng: 11.34}; setStatus("1 result."); setFavoritesView(true); setFavoritesView(false)');
  assert.equal(app.elements.get('results').children.length, 1);
  assert.match(app.elements.get('status').innerHTML, /1 result/);
  assert.equal(app.run('lastCenter.lat'), 44.49);
});

test('cross-tab storage changes refresh saved list and count', () => {
  const app = boot(); put(app); app.run('setFavoritesView(true)');
  const other = boot(); put(other); other.run('toggleFavorite(place)');
  app.storage.setItem('nomnom:favorites:v1', other.stored());
  app.events.storage({ key: 'nomnom:favorites:v1', storageArea: app.storage });
  assert.equal(app.elements.get('results').children.length, 1);
  assert.match(app.elements.get('favorites').textContent, /1/);
  app.storage.setItem('nomnom:favorites:v1', null);
  app.events.storage({ key: null, storageArea: app.storage });
  assert.match(app.elements.get('results').innerHTML, /No favorites yet/);
});

test('identity preserves source tokens literally and supports numeric zero', () => {
  const app = boot(); put(app);
  assert.equal(app.run('favoriteKey({...place, source_id: "  exact ID  "})'), '["michelin","  exact ID  "]');
  assert.equal(app.run('favoriteKey({...place, source_id: 0})'), '["michelin","0"]');
});

test('snapshot normalization accepts arrays, rejects malformed tags/coordinates, and strips unsafe URLs', () => {
  const app = boot(); put(app);
  const snapshot = JSON.parse(app.run('JSON.stringify(favoriteSnapshot({...place, tags: ["wine", null, 2], lat: "44", lng: Infinity, source_url: "javascript:alert(1)"}))'));
  assert.deepEqual(snapshot.tags, ['wine']);
  assert.equal(snapshot.lat, null); assert.equal(snapshot.lng, null);
  assert.equal(snapshot.source_url, '');
  for (const tags of ['broken', 'null', '{}', 7, null]) {
    app.context.tags = tags;
    assert.equal(app.run('favoriteSnapshot({...place, tags}).tags.length'), 0);
  }
});

test('changing any filter while saved never schedules a search; entering saved cancels pending debounce', () => {
  const app = boot(); app.run('wireEvents(); autoSearch(); setFavoritesView(true)');
  assert.equal(app.run('_searchTimeout'), null);
  for (const [id, type] of [['radius', 'change'], ['category', 'change'], ['searchText', 'input'], ['limit', 'change']]) {
    app.elements.get(id).listeners[type]({});
    assert.equal(app.run('_searchTimeout'), null, id);
    assert.equal(app.run('showingFavorites'), true, id);
  }
});

test('explicit search exits saved mode and uses its own center/results', async () => {
  const app = boot(); put(app);
  app.elements.get('radius').value = '10';
  app.elements.get('limit').value = '200';
  app.run('db = {}; searchNear = () => [place]; setFavoritesView(true)');
  await app.run('runSearch({lat: 44.49, lng: 11.34})');
  assert.equal(app.run('showingFavorites'), false);
  assert.equal(app.run('lastSearchRows.length'), 1);
  assert.equal(app.elements.get('results').children.length, 1);
  assert.equal(app.run('lastCenter.lat'), 44.49);
});

test('a pending geocode/DB status cannot displace saved view', async () => {
  const app = boot(); put(app);
  app.elements.get('q').value = 'Bologna';
  app.elements.get('radius').value = '10';
  app.run('db = {}; searchNear = () => [place]; geocode = () => new Promise(resolve => { finishGeocode = resolve; })');
  const pending = app.run('runSearch(null)');
  app.run('setFavoritesView(true); setStatus("Ready — 123 places indexed.")');
  assert.match(app.elements.get('status').innerHTML, /all cities/);
  app.run('finishGeocode({lat: 44.49, lng: 11.34})');
  await pending;
  assert.equal(app.run('showingFavorites'), true);
  assert.match(app.elements.get('results').innerHTML, /No favorites yet/);
  app.run('setFavoritesView(false)');
  assert.equal(app.elements.get('results').children.length, 1);
  assert.match(app.elements.get('status').innerHTML, /1 result/);
});

test('toggle re-reads storage to preserve another tab save and ignores sessionStorage events', () => {
  const app = boot(); put(app);
  const other = boot(); put(other, { ...place, source_id: 'other', name: 'Other' });
  other.run('toggleFavorite(place)');
  app.storage.setItem('nomnom:favorites:v1', other.stored());
  app.events.storage({ key: 'nomnom:favorites:v1', storageArea: {} });
  assert.equal(app.run('favorites.size'), 0);
  app.run('toggleFavorite(place)');
  assert.equal(JSON.parse(app.stored()).places.length, 2);
});

test('late GPS callbacks cannot displace favorites, even after returning to results', async () => {
  const app = boot(); put(app);
  app.run(`db = {}; searchNear = () => [place];
    navigator.geolocation = { getCurrentPosition(success) { finishLocation = success; } };
    fetch = async () => ({ ok: false });
    useMyLocation(); setFavoritesView(true);`);
  await app.run('finishLocation({coords: {latitude: 44.49, longitude: 11.34}})');
  assert.equal(app.run('showingFavorites'), true);
  assert.equal(app.run('lastSearchRows.length'), 0);
  app.run('setFavoritesView(false)');
  await app.run('finishLocation({coords: {latitude: 44.49, longitude: 11.34}})');
  assert.equal(app.run('lastSearchRows.length'), 0);
});

test('late reverse geocoding cannot overwrite favorites or the destination', async () => {
  const app = boot(); put(app);
  app.elements.get('q').value = 'Previous destination';
  app.run(`db = {}; searchNear = () => [place];
    navigator.geolocation = { getCurrentPosition(success) { finishLocation = success; } };
    fetch = () => new Promise(resolve => { finishReverse = resolve; }); useMyLocation();`);
  const pending = app.run('finishLocation({coords: {latitude: 44.49, longitude: 11.34}})');
  app.run('setFavoritesView(true); finishReverse({ok: true, json: async () => ({address: {city: "Bologna"}})})');
  await pending;
  assert.equal(app.run('showingFavorites'), true);
  assert.equal(app.run('lastSearchRows.length'), 0);
  assert.equal(app.elements.get('q').value, 'Previous destination');
});

test('saved snapshots escape source badges and descriptions, not just restaurant names', () => {
  const app = boot(); put(app, { ...place, source: '<img src=x onerror=alert(1)>', description: '<script>alert(1)</script>', endorsements: [{ source: '\" onclick=alert(1) a=\"' }] });
  app.run('toggleFavorite(place); setFavoritesView(true)');
  const html = app.elements.get('results').children[0].innerHTML;
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;script/);
  assert.doesNotMatch(html, /badge-\"/);
});
