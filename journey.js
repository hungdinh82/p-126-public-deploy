// Journey view: VietMap map, destination search, route planning and a
// client-side drive simulation along the planned route. Search and routing go
// through the backend so the VietMap service key stays server side.
const $ = (selector) => document.querySelector(selector);
const SDK_VERSION = '6.0.1';
const SDK_BASE = `https://unpkg.com/@vietmap/vietmap-gl-js@${SDK_VERSION}/dist`;
const EARTH_RADIUS_M = 6371000;
// GraphHopper-style maneuver signs returned by the VietMap route API.
const SIGN_ICONS = { '-98': '↶', '-8': '↶', '-7': '↖', '-3': '↰', '-2': '↰', '-1': '↖', 0: '↑', 1: '↗', 2: '↱', 3: '↱', 4: '◎', 5: '•', 6: '↻', 7: '↗', 8: '↷' };

let hooks = { announce: () => {}, setDriving: () => {}, progress: () => {}, route: () => {}, travel: () => {}, network: () => {} };
let backendNavigation = null;
let backendTripPlan = null;
let mapTripPlan = null;
let choiceOwner = 'navigation';
const hiddenInformation = new Set();
try { for (const key of JSON.parse(sessionStorage.getItem('vivi.hiddenInformation') || '[]')) hiddenInformation.add(key); } catch {}
let navigationGrant = null;
let progressAt = 0;
let progressPending = false;
let config = null;
let map = null;
let sdkPromise = null;
let origin = null;          // { lat, lng, label }
let destination = null;     // { lat, lng, name, address }
let route = null;           // backend Route + cumulative distances
let markers = {};
let sim = { running: false, speedup: 5, traveled: 0, last: 0, frame: 0, step: -1, follow: true, lastPaint: 0, bearing: 0 };
let lastNetworkScan = 0;
// 'full' covers the screen for planning, 'mini' is the corner map while driving.
let view = 'hidden';
let searchTimer = 0;
let searchToken = 0;

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
const formatDistance = (meters) => meters < 1000 ? `${Math.max(0, Math.round(meters / 10) * 10)} m` : `${(meters / 1000).toLocaleString('vi-VN', { maximumFractionDigits: 1 })} km`;
function formatDuration(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 60) return `${minutes} phút`;
  return `${Math.floor(minutes / 60)} giờ${minutes % 60 ? ` ${minutes % 60} phút` : ''}`;
}
const formatClock = (date) => new Intl.DateTimeFormat('vi-VN', { hour: '2-digit', minute: '2-digit' }).format(date);

function haversine([lng1, lat1], [lng2, lat2]) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad, dLng = (lng2 - lng1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(a));
}
function bearing([lng1, lat1], [lng2, lat2]) {
  const rad = Math.PI / 180;
  const y = Math.sin((lng2 - lng1) * rad) * Math.cos(lat2 * rad);
  const x = Math.cos(lat1 * rad) * Math.sin(lat2 * rad) - Math.sin(lat1 * rad) * Math.cos(lat2 * rad) * Math.cos((lng2 - lng1) * rad);
  return (Math.atan2(y, x) / rad + 360) % 360;
}

async function api(path) {
  const response = await fetch(path);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || 'Dịch vụ bản đồ không phản hồi');
  return payload;
}

function setMessage(text, tone = '') {
  const element = $('#journey-message');
  element.textContent = text;
  element.className = `journey-message${tone ? ` ${tone}` : ''}`;
  element.hidden = !text;
}

function loadSdk() {
  if (window.vietmapgl) return Promise.resolve();
  sdkPromise ||= new Promise((resolve, reject) => {
    const style = document.createElement('link');
    style.rel = 'stylesheet'; style.href = `${SDK_BASE}/vietmap-gl.css`;
    document.head.append(style);
    const script = document.createElement('script');
    script.src = `${SDK_BASE}/vietmap-gl.js`;
    script.onload = resolve;
    script.onerror = () => { sdkPromise = null; reject(new Error('Không tải được thư viện bản đồ VietMap')); };
    document.head.append(script);
  });
  return sdkPromise;
}

function markerElement(className) {
  const element = document.createElement('div');
  element.className = `journey-marker ${className}`;
  element.innerHTML = '<i></i>';
  return element;
}
function placeMarker(name, lngLat, className) {
  if (!markers[name]) markers[name] = new vietmapgl.Marker({ element: markerElement(className), rotationAlignment: 'map' }).setLngLat(lngLat).addTo(map);
  else markers[name].setLngLat(lngLat);
  return markers[name];
}
function removeMarker(name) { markers[name]?.remove(); delete markers[name]; }

function emptyLine() { return { type: 'Feature', geometry: { type: 'LineString', coordinates: [] } }; }
function addRouteLayers() {
  map.addSource('vivi-route', { type: 'geojson', data: emptyLine() });
  map.addSource('vivi-traveled', { type: 'geojson', data: emptyLine() });
  map.addSource('vivi-trip-preview', { type: 'geojson', data: emptyLine() });
  const line = { 'line-join': 'round', 'line-cap': 'round' };
  map.addLayer({ id: 'vivi-route-glow', type: 'line', source: 'vivi-route', layout: line, paint: { 'line-color': '#9aa8c8', 'line-width': 14, 'line-opacity': .14, 'line-blur': 6 } });
  map.addLayer({ id: 'vivi-route-line', type: 'line', source: 'vivi-route', layout: line, paint: { 'line-color': '#d0d8ea', 'line-width': 4 } });
  map.addLayer({ id: 'vivi-trip-preview-line', type: 'line', source: 'vivi-trip-preview', layout: line, paint: { 'line-color': '#f0cd92', 'line-width': 5, 'line-dasharray': [2, 1] } });
  map.addLayer({ id: 'vivi-traveled-line', type: 'line', source: 'vivi-traveled', layout: line, paint: { 'line-color': '#4b6378', 'line-width': 5 } });
}

// Re-opening the panel while the map is still loading must not create a second map.
let mapPromise = null;
function ensureMap() {
  mapPromise ||= createMap().catch(error => { mapPromise = null; throw error; });
  return mapPromise;
}
async function createMap() {
  config = await api('/api/v1/navigation/config');
  if (!config.available) {
    $('#journey').classList.add('no-map');
    setMessage('Chưa cấu hình VIETMAP_API_KEY trong .env. Thêm key rồi khởi động lại ViVi để mở bản đồ.', 'warn');
    $('#journey-search').disabled = true;
    return;
  }
  if (config.tile_status !== 200) {
    $('#journey-notice').textContent = config.tile_status
      ? `VietMap từ chối tải nền bản đồ (HTTP ${config.tile_status}). Kiểm tra VIETMAP_TILE_KEY (key của consumer Tilemap) — tìm kiếm và chỉ đường vẫn hoạt động.`
      : 'Không kiểm tra được nền bản đồ VietMap; bản đồ có thể hiển thị trống.';
    $('#journey-notice').hidden = false;
  }
  await loadSdk();
  const center = [config.default_center.lng, config.default_center.lat];
  let created;
  try {
    created = new vietmapgl.Map({ container: 'journey-map', style: config.style_url, center, zoom: 13, attributionControl: { compact: true } });
  } catch {
    throw new Error('Trình duyệt không hỗ trợ WebGL nên không hiển thị được bản đồ.');
  }
  // Tile failures are reported but not fatal: route layers only need the style,
  // so search, routing and simulation keep working on a blank background.
  let tileWarned = false;
  created.on('error', event => {
    const status = event.error?.status;
    if (tileWarned || ![401, 403, 423, 429].includes(status)) return;
    tileWarned = true;
    $('#journey-notice').textContent = `VietMap từ chối tải nền bản đồ (HTTP ${status}). Kiểm tra VIETMAP_TILE_KEY (key của consumer Tilemap) — tìm kiếm và chỉ đường vẫn hoạt động.`;
    $('#journey-notice').hidden = false;
  });
  // "load" waits for tiles, so a rejected tile key would hang; the style is enough.
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Không tải được style bản đồ VietMap. Kiểm tra VIETMAP_API_KEY và kết nối mạng.')), 15000);
    const ready = () => { clearTimeout(timer); resolve(); };
    if (created.isStyleLoaded()) ready(); else created.once('style.load', ready);
  }).catch(error => { created.remove(); throw error; });
  map = created;
  map.addControl(new vietmapgl.NavigationControl({ showCompass: true }), 'bottom-right');
  addRouteLayers();
  map.on('click', event => {
    if (sim.running || mapTripPlan || view !== 'full') return;
    if (hooks.event) return hooks.event({kind:'point',id:crypto.randomUUID(),revision:backendNavigation?.revision || 0,position:{lat:event.lngLat.lat,lng:event.lngLat.lng,label:'Điểm đã chọn trên bản đồ',simulated:true}});
    setDestination({ lat: event.lngLat.lat, lng: event.lngLat.lng, name: 'Điểm đã chọn trên bản đồ', address: `${event.lngLat.lat.toFixed(5)}, ${event.lngLat.lng.toFixed(5)}` });
  });
  // Manual panning pauses camera follow until the recenter button is used.
  map.on('dragstart', () => { if (sim.running) sim.follow = false; });
}

let locating = false;
function locate() {
  if (locating) return;
  locating = true;
  $('#journey-origin').textContent = 'Đang xác định vị trí…';
  const fallback = (reason) => {
    locating = false;
    setOrigin({ lat: config.default_center.lat, lng: config.default_center.lng, label: `Vị trí mặc định · ${config.default_center.label || 'Hồ Gươm'}`, simulated:true });
    hooks.position?.({ ...origin, simulated: true });
    setMessage(`${reason} ViVi dùng vị trí mặc định để mô phỏng.`, 'warn');
  };
  if (!('geolocation' in navigator)) return fallback('Trình duyệt không hỗ trợ định vị.');
  const found = position => {
    locating = false;
    setOrigin({ lat: position.coords.latitude, lng: position.coords.longitude, label: 'Vị trí của bạn', simulated:false });
    hooks.position?.({ ...origin, simulated: false });
    if (!destination) setMessage('Tìm điểm đến hoặc chạm vào bản đồ để chọn.');
  };
  const failed = error => fallback(error.code === error.PERMISSION_DENIED ? 'Bạn chưa cho phép truy cập vị trí.' : 'Không lấy được vị trí hiện tại.');
  navigator.geolocation.getCurrentPosition(
    found,
    // Laptops without GPS often time out on high accuracy; Wi-Fi location is enough.
    error => error.code === error.PERMISSION_DENIED ? failed(error)
      : navigator.geolocation.getCurrentPosition(found, failed, { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 }),
    { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
  );
}

function setOrigin(point) {
  origin = point;
  $('#journey-origin').textContent = point.label;
  if (!map) return;
  placeMarker('origin', [point.lng, point.lat], 'origin');
  if (destination) { if (!hooks.command) planRoute(); }
  else map.flyTo({ center: [point.lng, point.lat], zoom: 14 });
}

function setDestination(place) {
  resetSimulation();
  destination = place;
  $('#journey-search').value = place.name;
  closeResults();
  placeMarker('destination', [place.lng, place.lat], 'destination');
  planRoute();
}

function closeResults() {
  $('#journey-results').hidden = true;
  $('#journey-search').setAttribute('aria-expanded', 'false');
}
function renderResults(places) {
  const list = $('#journey-results');
  list.innerHTML = places.length
    ? places.map((place, index) => `<li role="option" tabindex="-1" data-index="${index}"><strong>${escapeHtml(place.name)}</strong><span>${escapeHtml(place.address)}${place.distance_m != null ? ` · ${formatDistance(place.distance_m)} · ${formatDuration(place.duration_s)}` : ''}</span></li>`).join('')
    : '<li class="empty">Không tìm thấy địa điểm phù hợp</li>';
  list.hidden = false;
  $('#journey-search').setAttribute('aria-expanded', 'true');
  list.querySelectorAll('[data-index]').forEach(item => item.addEventListener('click', () => choosePlace(places[Number(item.dataset.index)])));
}
async function search(text) {
  if (hooks.command) return hooks.command('Tìm địa điểm', { capability: 'navigation.search', arguments: { query: text } });
  const token = ++searchToken;
  const near = origin ? `&near=${origin.lat},${origin.lng}` : '';
  try {
    const places = await api(`/api/v1/navigation/search?text=${encodeURIComponent(text)}${near}`);
    if (token === searchToken) renderResults(places);
  } catch (error) {
    if (token === searchToken) setMessage(error.message, 'error');
  }
}
async function choosePlace(place) {
  if (hooks.command && place.handle) return hooks.command('Chọn địa điểm', { capability: 'navigation.select', arguments: { place_handle: place.handle } });
  closeResults();
  setMessage(`Đang lấy vị trí “${place.name}”…`);
  try {
    const detail = await api(`/api/v1/navigation/place/${encodeURIComponent(place.ref_id)}`);
    setDestination({ lat: detail.lat, lng: detail.lng, name: detail.name || place.name, address: detail.address || place.address });
  } catch (error) {
    setMessage(error.message, 'error');
  }
}

const MIN_AVERAGE = 22 / 3.6, MAX_AVERAGE = 70 / 3.6;   // m/s
async function planRoute() {
  if (!origin || !destination || !map) return;
  setMessage('Đang tìm tuyến đường phù hợp…');
  $('#journey').classList.add('loading');
  try {
    const planned = await api(`/api/v1/navigation/route?origin=${origin.lat},${origin.lng}&destination=${destination.lat},${destination.lng}`);
    const cumulative = [0];
    for (let index = 1; index < planned.coordinates.length; index++) cumulative.push(cumulative[index - 1] + haversine(planned.coordinates[index - 1], planned.coordinates[index]));
    const length = cumulative.at(-1) || planned.distance_m;
    // VietMap sometimes times short urban routes at walking pace (2.3 km in
    // 23 min); keep the average a believable city driving speed.
    const duration = length / Math.min(MAX_AVERAGE, Math.max(MIN_AVERAGE, length / Math.max(1, planned.duration_s)));
    route = { ...planned, cumulative, length, duration_s: duration };
    markRoundaboutExits();
    // Manoeuvre points become intersections in the 3D scene; the start and the
    // arrival/via signs are not junctions.
    const junctions = planned.steps.slice(1).filter(step => step.sign !== 4 && step.sign !== 5)
      .map(step => cumulative[Math.min(step.point_index, cumulative.length - 1)]);
    hooks.route({ coordinates: route.coordinates, cumulative, junctions });
    networkKey = '';
    hooks.network([]);
    map.getSource('vivi-route').setData({ type: 'Feature', geometry: { type: 'LineString', coordinates: planned.coordinates } });
    map.getSource('vivi-traveled').setData(emptyLine());
    const bounds = planned.coordinates.reduce((box, point) => box.extend(point), new vietmapgl.LngLatBounds(planned.coordinates[0], planned.coordinates[0]));
    map.fitBounds(bounds, { padding: fitPadding(), duration: 900 });
    renderRoute();
    setMessage(`Đến ${destination.name} · ${destination.address}`);
  } catch (error) {
    route = null;
    hooks.route(null);
    networkKey = '';
    hooks.network([]);
    renderRoute();
    setMessage(error.message, 'error');
  } finally {
    $('#journey').classList.remove('loading');
  }
}
function fitPadding() {
  const panel = $('.journey-panel').getBoundingClientRect();
  return innerWidth > 760 ? { top: 60, bottom: 60, right: 60, left: panel.width + 60 } : { top: 40, bottom: panel.height + 30, left: 30, right: 30 };
}

function renderRoute() {
  const ready = Boolean(route);
  $('#journey-summary').hidden = !ready;
  $('#journey-controls').hidden = !ready;
  $('#journey-steps').innerHTML = ready
    ? route.steps.map((step, index) => `<li data-step="${index}"><b>${SIGN_ICONS[step.sign] ?? '↑'}</b><span>${escapeHtml(step.text)}</span><small>${step.distance_m ? formatDistance(step.distance_m) : ''}</small></li>`).join('')
    : '';
  if (!ready) { $('#journey-next').hidden = true; return; }
  updateSummary(0);
}
function updateSummary(traveled) {
  const remaining = Math.max(0, route.length - traveled);
  const plan=backendNavigation?.energy?.selected_plan;
  const chargingSeconds=plan ? Math.max(0,plan.stops.slice(backendNavigation.stop_index || 0).reduce((total,stop)=>total+stop.charge_minutes*60,0)-(backendNavigation.charge_elapsed_s || 0)) : 0;
  const seconds = route.duration_s * (remaining / route.length || 0)+chargingSeconds;
  if(mapTripPlan)return {remaining,arrival:new Date(Date.now()+seconds*1000)};
  $('#journey-duration').textContent = remaining ? formatDuration(seconds) : 'Đã đến';
  $('#journey-distance').textContent = formatDistance(remaining);
  $('#journey-arrival').textContent = formatClock(new Date(Date.now() + seconds * 1000));
  return { remaining, arrival: new Date(Date.now() + seconds * 1000) };
}
// Displayed speed is the real-world pace of the route (not the simulation
// speed-up), easing off before each manoeuvre.
function cruiseSpeed(now, upcoming) {
  const average = route.length / Math.max(1, route.duration_s) * 3.6;
  const slowdown = upcoming && upcoming.distance < 80 ? .55 + .45 * Math.max(0, upcoming.distance) / 80 : 1;
  return Math.max(5, average * (1 + .07 * Math.sin(now / 2300)) * slowdown);
}

// --- Street network -------------------------------------------------------
// While driving, the corner map has the street tiles around the car loaded.
// Every street in them near the route ahead goes to the 3D scene with its
// real shape, so junctions, bends and roundabouts look like the map. No extra
// requests: only tiles the map already holds are read.
const NETWORK_AHEAD = 320, NETWORK_BEHIND = 60, NETWORK_SIDE = 70, NETWORK_SCAN_MS = 1500;
const ON_ROUTE = 10;   // metres: closer than this, a street is the road we drive on
// The other carriageway of a divided road runs alongside the route this close.
const CARRIAGEWAY = 30;
const ROAD_WIDTH = { motorway: 11, trunk: 11, primary: 10, secondary: 9, tertiary: 8, minor: 6, service: 4.5 };
let networkKey = '';
function roadSourceId() {
  const sources = map.getStyle()?.sources || {};
  return Object.keys(sources).find(id => sources[id].type === 'vector');
}
function scanNetwork(traveled) {
  const source = roadSourceId();
  if (!source) return;
  // Route window in local metres around the car (x east, y north).
  const [lng0, lat0] = positionAt(traveled).point;
  const kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110540;
  const local = ([lng, lat]) => [(lng - lng0) * kx, (lat - lat0) * ky];
  const { cumulative, coordinates } = route;
  const from = traveled - NETWORK_BEHIND, to = Math.min(route.length, traveled + NETWORK_AHEAD);
  const segments = [];
  for (let index = 0; index < coordinates.length - 1; index++) {
    if (cumulative[index + 1] < from - NETWORK_SIDE || cumulative[index] > to + NETWORK_SIDE) continue;
    const [ax, ay] = local(coordinates[index]), [bx, by] = local(coordinates[index + 1]);
    const length = Math.hypot(bx - ax, by - ay);
    if (length > .1) segments.push({ ax, ay, dx: (bx - ax) / length, dy: (by - ay) / length, length, start: cumulative[index] });
  }
  if (!segments.length) return;
  // Nearest route point: distance and metres along the route.
  const nearest = (x, y) => {
    let best = { distance: Infinity, at: 0 };
    for (const segment of segments) {
      const t = Math.max(0, Math.min(segment.length, (x - segment.ax) * segment.dx + (y - segment.ay) * segment.dy));
      const distance = Math.hypot(x - segment.ax - segment.dx * t, y - segment.ay - segment.dy * t);
      if (distance < best.distance) best = { distance, at: segment.start + t, dx: segment.dx, dy: segment.dy };
    }
    return best;
  };
  const inWindow = hit => hit.distance < NETWORK_SIDE && hit.at > from && hit.at < to;
  const roads = [];
  const seen = new Set();
  for (const feature of map.querySourceFeatures(source, { sourceLayer: 'road' })) {
    const width = ROAD_WIDTH[feature.properties.class];
    if (!width || feature.properties.brunnel === 'tunnel') continue;
    const lines = feature.geometry.type === 'LineString' ? [feature.geometry.coordinates] : feature.geometry.type === 'MultiLineString' ? feature.geometry.coordinates : [];
    for (const line of lines) {
      // Neighbouring tiles repeat the same street; draw it once.
      const id = line.map(([lng, lat]) => `${lng.toFixed(6)},${lat.toFixed(6)}`).join(';');
      if (seen.has(id)) continue;
      seen.add(id);
      const hits = line.map(point => { const [x, y] = local(point); return nearest(x, y); });
      // Split the street into stretches off the route; a stretch keeps the
      // route point it leaves from or joins at, so it meets our road.
      let run = null;
      const close = end => {
        if (run && run.points.length > 1 && !alongside(run.indices)) roads.push({ width: run.width, points: run.points, startAt: run.startAt, endAt: end ?? null });
        run = null;
      };
      // Mostly parallel to the route and close by: the other carriageway of
      // the road we are on, already drawn as our road.
      const alongside = indices => {
        let parallel = 0, total = 0;
        for (let k = 1; k < indices.length; k++) {
          const [ax, ay] = local(line[indices[k - 1]]), [bx, by] = local(line[indices[k]]);
          const length = Math.hypot(bx - ax, by - ay);
          const hit = hits[indices[k]];
          total += length;
          if (hit.distance < CARRIAGEWAY && Math.abs(((bx - ax) * hit.dx + (by - ay) * hit.dy) / (length || 1)) > .94) parallel += length;
        }
        return total > 0 && parallel / total > .7;
      };
      hits.forEach((hit, index) => {
        if (hit.distance <= ON_ROUTE) {
          if (run) { run.points.push(line[index]); run.indices.push(index); close(hit.at); }
          return;
        }
        if (!inWindow(hit)) { close(); return; }
        if (!run) {
          const previous = hits[index - 1];
          const attached = previous && previous.distance <= ON_ROUTE;
          run = { width, points: attached ? [line[index - 1]] : [], indices: attached ? [index - 1] : [], startAt: attached ? previous.at : null };
        }
        run.points.push(line[index]);
        run.indices.push(index);
      });
      close();
    }
  }
  // Only a changed network is rebuilt in the scene.
  const key = roads.map(road => `${road.points.length}:${road.points[0]}`).join('|');
  if (key === networkKey) return;
  networkKey = key;
  hooks.network(roads);
}

// --- Drive simulation -----------------------------------------------------
function positionAt(distance) {
  const { cumulative, coordinates } = route;
  let low = 0, high = cumulative.length - 1;
  while (low < high - 1) { const mid = (low + high) >> 1; if (cumulative[mid] <= distance) low = mid; else high = mid; }
  const span = cumulative[high] - cumulative[low] || 1;
  const t = Math.min(1, Math.max(0, (distance - cumulative[low]) / span));
  const [a, b] = [coordinates[low], coordinates[high]];
  return { index: low, point: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], heading: bearing(a, b) };
}
// Which way each roundabout (sign 6) is left, from the heading on the way in
// to the heading after the exit: degrees, positive to the right.
function markRoundaboutExits() {
  const at = step => route.cumulative[Math.min(step.point_index, route.cumulative.length - 1)];
  route.steps.forEach((step, index) => {
    const exit = route.steps[index + 1];
    if (step.sign !== 6 || !exit) return;
    const turn = positionAt(at(exit) + 10).heading - positionAt(Math.max(0, at(step) - 5)).heading;
    step.exitTurn = ((turn % 360) + 540) % 360 - 180;
  });
}
function stepAt(index) {
  let current = 0;
  route.steps.forEach((step, stepIndex) => { if (step.point_index <= index) current = stepIndex; });
  return current;
}
function showNextManeuver(stepIndex, traveled) {
  const next = route.steps[stepIndex + 1];
  if(mapTripPlan)return next ? {next,distance:route.cumulative[Math.min(next.point_index,route.cumulative.length-1)]-traveled} : null;
  const card = $('#journey-next');
  card.hidden = !next;
  if (!next) return;
  const distance = route.cumulative[Math.min(next.point_index, route.cumulative.length - 1)] - traveled;
  $('#journey-next-icon').textContent = SIGN_ICONS[next.sign] ?? '↑';
  $('#journey-next-text').textContent = next.text;
  $('#journey-next-distance').textContent = `Sau ${formatDistance(distance)}`;
  return { next, distance };
}
function highlightStep(stepIndex) {
  if(mapTripPlan)return;
  document.querySelectorAll('#journey-steps li').forEach(item => {
    const index = Number(item.dataset.step);
    item.classList.toggle('active', index === stepIndex);
    item.classList.toggle('done', index < stepIndex);
  });
  if (performance.now() >= manualScrollUntil && stepIndex !== lastHighlightedStep) document.querySelector(`#journey-steps li[data-step="${stepIndex}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  lastHighlightedStep = stepIndex;
}

let manualScrollUntil = 0, lastHighlightedStep = -1;

function tick(now) {
  if (!sim.running) return;
  const elapsed = Math.min(0.25, (now - sim.last) / 1000);
  sim.last = now;
  if (backendNavigation?.status === 'charging') {
    if (!progressPending && now - progressAt >= 1000) sendProgress({ kind: 'charge', elapsed_s: Math.min(30, (now - progressAt) / 1000 * sim.speedup) }, now);
    sim.frame = requestAnimationFrame(tick);
    return;
  }
  const speed = route.length / Math.max(1, route.duration_s);
  sim.traveled = Math.min(route.length, sim.traveled + elapsed * speed * sim.speedup);
  if (backendNavigation?.status === 'running' && !progressPending && now - progressAt >= 1000) sendProgress({ kind: 'progress', distance_m: sim.traveled / route.length * route.distance_m }, now);
  const { index, point, heading } = positionAt(sim.traveled);
  hooks.travel(sim.traveled);
  const car = placeMarker('car', point, 'car');
  car.setRotation(heading);
  // Repainting map sources every frame is wasteful; ~8 Hz is smooth enough.
  if (now - sim.lastPaint > 120) {
    sim.lastPaint = now;
    if(!mapTripPlan)map.getSource('vivi-traveled').setData({ type: 'Feature', geometry: { type: 'LineString', coordinates: [...route.coordinates.slice(0, index + 1), point] } });
    const summary = updateSummary(sim.traveled);
    const stepIndex = stepAt(index);
    const upcoming = showNextManeuver(stepIndex, sim.traveled);
    hooks.progress({
      speedKmh: cruiseSpeed(now, upcoming),
      next: upcoming ? { sign: upcoming.next.sign, exitTurn: upcoming.next.exitTurn, icon: SIGN_ICONS[upcoming.next.sign] ?? '↑', text: upcoming.next.text, distance: upcoming.distance } : null,
      heading, remaining: summary.remaining, arrival: summary.arrival, destination: destination.name,
      // Inside a roundabout: the next step starts at its exit.
      roundabout: route.steps[stepIndex]?.sign === 6 ? route.steps[stepIndex].exitTurn ?? 0 : null
    });
    if (stepIndex !== sim.step) {
      sim.step = stepIndex;
      highlightStep(stepIndex);
      if (upcoming) hooks.announce(`Sau ${formatDistance(upcoming.distance)}, ${upcoming.next.text.charAt(0).toLowerCase()}${upcoming.next.text.slice(1)}`);
    }
  }
  if (now - lastNetworkScan > NETWORK_SCAN_MS) {
    lastNetworkScan = now;
    try { scanNetwork(sim.traveled); } catch (error) { console.warn('Street network scan failed:', error); }
  }
  if (mapTripPlan) { /* Viewing another plan must not move the active-route camera. */ } else if (view === 'mini') {
    // Course-up in the corner map: smooth the heading so turns do not snap.
    const delta = ((heading - sim.bearing + 540) % 360) - 180;
    sim.bearing = (sim.bearing + delta * Math.min(1, elapsed * 4) + 360) % 360;
    map.jumpTo({ center: point, bearing: sim.bearing, zoom: 16.2, pitch: 50 });
  } else if (sim.follow) map.jumpTo({ center: point });
  if (sim.traveled >= route.length) {
    if (hooks.event && backendNavigation?.status === 'running') {
      if (!progressPending) sendProgress({kind:'progress',distance_m:route.distance_m},now);
      sim.frame = requestAnimationFrame(tick);
      return;
    }
    return finishSimulation();
  }
  sim.frame = requestAnimationFrame(tick);
}
function startSimulation(fromServer = false) {
  if (!route) return;
  if (hooks.command && !fromServer) {
    if (navigationGrant && Date.parse(navigationGrant.expires_at) <= Date.now()) navigationGrant=null;
    if (navigationGrant) return hooks.confirm?.(navigationGrant);
    return hooks.command('Bắt đầu hành trình', { capability: 'navigation.start', arguments: { route_revision: backendNavigation?.revision || 0 } });
  }
  if (sim.traveled >= route.length) sim.traveled = 0;
  sim.running = true; sim.follow = true; sim.step = -1; sim.last = performance.now();
  $('#journey').classList.add('simulating');
  $('#journey-start span').textContent = 'Tạm dừng';
  sim.bearing = positionAt(sim.traveled).heading;
  // Driving hands the screen back to the 3D view; the map shrinks to a corner.
  setView('mini');
  if (!fromServer) hooks.setDriving(true);
  if (!fromServer) hooks.announce(`Bắt đầu hành trình đến ${destination.spoken_label || destination.name}. Quãng đường ${formatDistance(route.length)}, khoảng ${formatDuration(route.duration_s)}.`);
  sim.frame = requestAnimationFrame(tick);
}
function stopSimulation(announce = true, fromServer = false) {
  if (!fromServer && hooks.command && backendNavigation?.status === 'running') return hooks.command('Dừng hành trình',{capability:'navigation.stop',arguments:{}});
  const wasRunning = sim.running;
  sim.running = false;
  cancelAnimationFrame(sim.frame);
  $('#journey').classList.remove('simulating');
  $('#journey-start span').textContent = sim.traveled > 0 && route && sim.traveled < route.length ? 'Tiếp tục mô phỏng' : 'Bắt đầu mô phỏng';
  if (wasRunning) {
    hooks.travel(null);
    if(!fromServer) hooks.setDriving(false);
    hooks.progress(null);
    if (announce) hooks.announce('Đã tạm dừng mô phỏng hành trình.');
  }
}
function finishSimulation() {
  sim.running = false;
  $('#journey').classList.remove('simulating');
  $('#journey-start span').textContent = 'Bắt đầu mô phỏng';
  $('#journey-next').hidden = true;
  hooks.travel(null);
  if (!hooks.event) hooks.setDriving(false);
  hooks.progress(null);
  setTimeout(() => { if (!sim.running && view === 'mini') setView('hidden'); }, 6000);
  hooks.announce(backendNavigation?.trip_kind==='round_trip' ? 'Đã hoàn tất hành trình khứ hồi, quay lại điểm xuất phát.' : `Đã đến ${destination.spoken_label || destination.name}.`);
  // Server-owned journeys retain their route/checkpoint; standalone demos reset locally.
  if (!hooks.event) arrive();
}
// The car stays where it arrived: the destination becomes the next origin and
// the finished route is cleared, ready for a new destination.
function arrive() {
  origin = { lat: destination.lat, lng: destination.lng, label: destination.name };
  destination = null;
  route = null;
  sim.traveled = 0;
  $('#journey-origin').textContent = origin.label;
  $('#journey-search').value = '';
  removeMarker('car');
  removeMarker('destination');
  placeMarker('origin', [origin.lng, origin.lat], 'origin');
  map.getSource('vivi-route').setData(emptyLine());
  map.getSource('vivi-traveled').setData(emptyLine());
  hooks.route(null);
  networkKey = '';
  hooks.network([]);
  renderRoute();
  setMessage('Đã đến nơi. Tìm điểm đến tiếp theo để đi tiếp từ đây.');
}
function resetSimulation() {
  stopSimulation(false);
  sim.traveled = 0;
  removeMarker('car');
}

function setView(next) {
  const changed = next !== view;
  view = next;
  const element = $('#journey');
  element.hidden = next === 'hidden';
  element.classList.toggle('mini', next === 'mini');
  const host = next === 'mini' ? document.querySelector('#mini-map-slot') : document.body;
  if (host && element.parentElement !== host) host.append(element);
  document.body.classList.toggle('journey-open', next === 'full');
  document.body.classList.toggle('journey-mini', next === 'mini');
  if (!map || !changed) return;
  if (next === 'full') map.easeTo({ bearing: 0, pitch: 0, duration: 500 });
  // Wait for the CSS size transition before the map measures its container.
  setTimeout(() => map?.resize(), 380);
}

// --- Public API -----------------------------------------------------------
export async function openJourney() {
  setView('full');
  try {
    await ensureMap();
    if (map) {
      $('#journey').classList.remove('no-map');
      $('#journey-search').disabled = false;
      map.resize();
      if (!origin && !mapTripPlan) locate();
    }
  } catch (error) {
    // Search and markers need a live map; keep them off until a reopen succeeds.
    $('#journey').classList.add('no-map');
    $('#journey-search').disabled = true;
    $('#journey-origin').textContent = 'Chưa xác định';
    setMessage(error.message, 'error');
  }
}
// Shifting to P (button, voice or backend state) stops the car on the map too.
export function pauseJourney() {
  if (sim.running) stopSimulation();
}
export function closeJourney() {
  restoreNavigationView();
  // A trip in progress stays visible as the corner map.
  const unfinished = route && sim.traveled > 0 && sim.traveled < route.length;
  setView(sim.running || unfinished ? 'mini' : 'hidden');
}
export function initJourney(options) {
  hooks = { ...hooks, ...options };
  const scroll = document.querySelector('#journey-scroll');
  for (const type of ['wheel', 'touchstart', 'pointerdown', 'keydown']) scroll?.addEventListener(type, () => { manualScrollUntil = performance.now() + 15000; }, {passive:true});
  $('#journey-close').addEventListener('click', () => options.onClose?.());
  $('#journey-expand').addEventListener('click', () => options.onOpen?.());
  $('#journey-locate').addEventListener('click', () => { if (map && !sim.running) { resetSimulation(); locate(); } });
  $('#journey-recenter').addEventListener('click', () => {
    if (!map) return;
    sim.follow = true;
    const target = markers.car?.getLngLat() || (origin && [origin.lng, origin.lat]);
    if (target) map.easeTo({ center: target, zoom: sim.running ? 16 : 14 });
  });
  $('#journey-start').addEventListener('click', () => {
    if (mapTripPlan) return tripCommand('trip.start', mapTripPlan);
    if (sim.running && hooks.command) hooks.command('Dừng hành trình', { capability: 'navigation.stop', arguments: {} });
    else if (sim.running) stopSimulation();
    else startSimulation();
  });
  document.querySelectorAll('[data-speed]').forEach(button => button.addEventListener('click', () => {
    sim.speedup = Number(button.dataset.speed);
    document.querySelectorAll('[data-speed]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  }));
  const input = $('#journey-search');
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const text = input.value.trim();
    if (text.length < 2) { searchToken++; closeResults(); return; }
    if (mapTripPlan) { searchTimer = setTimeout(() => tripCommand('trip.plan', mapTripPlan, {query:text,trip_kind:mapTripPlan.trip_kind}), 700); return; }
    searchTimer = setTimeout(() => search(text), 300);
  });
  input.addEventListener('keydown', event => {
    const items = [...document.querySelectorAll('#journey-results [data-index]')];
    if (event.key === 'Escape') { closeResults(); return; }
    if (event.key === 'ArrowDown' && items.length) { event.preventDefault(); items[0].focus(); }
  });
  $('#journey-results').addEventListener('keydown', event => {
    const item = event.target.closest('[data-index]');
    if (!item) return;
    if (event.key === 'Enter') item.click();
    else if (event.key === 'ArrowDown') { event.preventDefault(); item.nextElementSibling?.focus(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); (item.previousElementSibling || input).focus(); }
    else if (event.key === 'Escape') { closeResults(); input.focus(); }
  });
  document.addEventListener('click', event => { if (!event.target.closest('.journey-search')) closeResults(); });
}


async function sendProgress(value, now) {
  if (!hooks.event || !backendNavigation) return;
  progressPending = true; progressAt = now;
  try { await hooks.event({ ...value, id: crypto.randomUUID(), revision: backendNavigation.revision }); }
  catch (error) { stopSimulation(false,true); setMessage(`Đã tạm dừng cập nhật hành trình: ${error.message}`,'error'); }
  finally { progressPending = false; }
}

function informationKey(next, label) { return `${next.id || next.request_id || `navigation:${next.revision}`}:${label}`; }
function hideInformation(key) {
  hiddenInformation.add(key);
  try { sessionStorage.setItem('vivi.hiddenInformation', JSON.stringify([...hiddenInformation].slice(-100))); } catch {}
  renderInformation();
}
function currentChoice() { return choiceOwner === 'trip' ? backendTripPlan : backendNavigation; }
function identityArguments(next) { return next.id ? {plan_id:next.id,route_revision:next.revision} : {}; }
function panelHeading(panel, text, key) {
  const head = document.createElement('div'); head.className='information-heading';
  const title=document.createElement('strong'); title.textContent=text;
  const hide=document.createElement('button'); hide.type='button';hide.textContent='Ẩn';hide.setAttribute('aria-label',`Ẩn ${text.toLowerCase()}`);
  hide.addEventListener('click',()=>hideInformation(key)); head.append(title,hide);panel.append(head);
}
function reopenButton(next,label) {
  const key=informationKey(next,label);
  if (!hiddenInformation.has(key)) return;
  const button=document.createElement('button');button.type='button';button.textContent=label;
  button.addEventListener('click',()=>{hiddenInformation.delete(key);try{sessionStorage.setItem('vivi.hiddenInformation',JSON.stringify([...hiddenInformation]));}catch{}renderInformation();});
  document.querySelector('#information-tabs')?.append(button);
}
function renderInformation() {
  const tabs=document.querySelector('#information-tabs');if(tabs)tabs.innerHTML='';
  const next=currentChoice();if(next) {renderDestinationChoices(next);if(next.clarification || next.status==='selecting')reopenButton(next,'Địa điểm');}
  if(backendTripPlan) {renderTripCard(backendTripPlan);if(backendTripPlan.route && !backendTripPlan.clarification && backendTripPlan.calculation_status!=='cancelled')reopenButton(backendTripPlan,'Kế hoạch');}
}
let destinationChoiceBusy = false;
function renderDestinationChoices(next) {
  const selecting = !!next.clarification || next.status === 'selecting' || next.pending?.status === 'selecting';
  const clarification = next.clarification;
  const entries = clarification ? clarification.choices || [] : (next.candidates || []).slice(0,3);
  const key=informationKey(next,'Địa điểm');
  for (const selector of ['#destination-choices', '#journey-destination-choices']) {
    const panel = document.querySelector(selector);
    if (!panel) continue;
    panel.hidden = !selecting || hiddenInformation.has(key);
    if(panel.hidden)continue;
    // Preserve scroll, focus and typed input across progress/event renders.
    const signature=JSON.stringify([key,next.revision,clarification,next.candidates,destinationChoiceBusy]);
    if(panel.renderSignature===signature)continue;panel.renderSignature=signature;
    const oldScroll=panel.scrollTop;panel.innerHTML='';
    panelHeading(panel,'Địa điểm',key);
    const heading=document.createElement('p');heading.className='destination-choice-heading';heading.textContent=clarification?.question || (entries.length===1?'Đúng chỗ này chứ?':'Bạn muốn đến địa điểm nào?');panel.append(heading);
    if (clarification?.phase==='change_destination' && next.selected) {
      const point=document.createElement('p');point.className='default-destination';point.textContent=`Điểm đến mặc định: ${next.selected.spoken_label || next.selected.name}`;panel.append(point);
      if(next.summary){const summary=document.createElement('small');summary.textContent=`${formatDistance(next.summary.distance_m)} · ${formatDuration(next.summary.total_seconds)}`;panel.append(summary);}
    }
    for (const [index,entry] of entries.entries()) {
      const button=document.createElement('button');button.type='button';button.className='destination-choice-card';button.disabled=destinationChoiceBusy;
      const number=document.createElement('b');number.textContent=String(index+1);
      const text=document.createElement('span');const name=document.createElement('strong');name.textContent=entry.label || entry.spoken_label || entry.name;
      const detail=document.createElement('small');detail.textContent=clarification ? entry.kind==='representative'?'Điểm đến mặc định':'Nói hoặc nhập tên địa điểm' : `${entry.short_address || entry.address || ''}${entry.distance_m!=null?` · ${formatDistance(entry.distance_m)} · ${formatDuration(entry.duration_s)}`:''}`;
      text.append(name,detail);button.append(number,text);
      button.addEventListener('click',async()=>{
        const current=currentChoice();
        if(destinationChoiceBusy || !current || informationKey(current,'Địa điểm')!==key || current.revision!==next.revision || current.clarification?.revision!==clarification?.revision)return;
        destinationChoiceBusy=true;renderInformation();
        try{await hooks.command?.('Chọn điểm đến',{capability:`${next.id?'trip':'navigation'}.${clarification?'refine':'select'}`,arguments:{...identityArguments(next),...(clarification?{choice_handle:entry.handle}:{place_handle:entry.handle})}});}
        finally{destinationChoiceBusy=false;renderInformation();}
      });panel.append(button);
    }
    if(!clarification && entries.length) {
      const other=document.createElement('button');other.type='button';other.className='information-cancel';other.textContent='Tìm địa điểm khác';other.disabled=destinationChoiceBusy;
      other.addEventListener('click',async()=>{const current=currentChoice();if(destinationChoiceBusy || !current || current.revision!==next.revision || informationKey(current,'Địa điểm')!==key)return;
        destinationChoiceBusy=true;renderInformation();
        try{await hooks.command?.('Tìm địa điểm khác',{capability:`${next.id?'trip':'navigation'}.refine`,arguments:{...identityArguments(next),reject_candidates:true}});}
        finally{destinationChoiceBusy=false;renderInformation();}});panel.append(other);
    }
    if(clarification?.phase==='specific') {
      const form=document.createElement('form');form.className='destination-choice-input';
      const input=document.createElement('input');input.placeholder='Tên địa điểm…';input.maxLength=200;input.setAttribute('aria-label','Địa điểm cụ thể');
      const button=document.createElement('button');button.type='submit';button.textContent='Tìm';button.disabled=destinationChoiceBusy;form.append(input,button);
      form.addEventListener('submit',async event=>{event.preventDefault();const current=currentChoice();if(destinationChoiceBusy || current?.revision!==next.revision || informationKey(current,'Địa điểm')!==key || input.value.trim().length<2)return;destinationChoiceBusy=true;
        try{await hooks.command?.('Tìm điểm đến',{capability:`${next.id?'trip':'navigation'}.refine`,arguments:{...identityArguments(next),query:input.value.trim()}});}
        finally{destinationChoiceBusy=false;renderInformation();}});panel.append(form);
    }
    const cancel=document.createElement('button');cancel.type='button';cancel.className='information-cancel';cancel.textContent='Huỷ yêu cầu';
    cancel.addEventListener('click',()=>{const current=currentChoice();if(current?.revision!==next.revision || informationKey(current,'Địa điểm')!==key)return;next.id ? tripCommand('trip.cancel',next) : hooks.command?.('Huỷ tìm địa điểm',{capability:'navigation.cancel',arguments:{route_revision:next.revision}});});panel.append(cancel);
    panel.scrollTop=oldScroll;
  }
}
function tripCommand(capability,plan,extra={}) {
  if(!backendTripPlan || backendTripPlan.id!==plan.id || backendTripPlan.revision!==plan.revision)return;
  const args=capability==='trip.plan'?extra:{...identityArguments(plan),...extra};
  return hooks.command?.(capability==='trip.start'?'Bắt đầu kế hoạch':'Chỉnh kế hoạch',{capability,arguments:args});
}
function renderTripCard(plan) {
  const panel=document.querySelector('#trip-plan-card');if(!panel)return;
  const key=informationKey(plan,'Kế hoạch');
  panel.hidden= !plan.route || !!plan.clarification || plan.calculation_status==='cancelled' || hiddenInformation.has(key);
  if(panel.hidden)return;
  const signature=JSON.stringify([plan.id,plan.revision,plan.calculation_status,plan.offer,plan.started_revision]);if(panel.renderSignature===signature)return;panel.renderSignature=signature;
  const oldScroll=panel.scrollTop;panel.innerHTML='';panelHeading(panel,'Kế hoạch',key);
  const name=document.createElement('p');name.className='trip-destination';name.textContent=`${plan.trip_kind==='round_trip'?'Khứ hồi':'Một chiều'} · ${plan.selected?.spoken_label || plan.selected?.name || ''}`;panel.append(name);
  const energy=plan.energy?.selected_plan;
  const summary=document.createElement('p');summary.textContent=`${formatDistance(plan.route.distance_m)} · ${formatDuration(energy?.total_seconds || plan.route.duration_s)}${energy?` · pin đến ${Math.floor(energy.arrival_soc)}%`:''}`;panel.append(summary);
  if(!energy){const warning=document.createElement('p');warning.textContent=`Kế hoạch chưa hoàn chỉnh. ${plan.energy?.message || ''}`;panel.append(warning);}
  else if(!energy.stops.length){const none=document.createElement('p');none.textContent='Không cần dừng sạc.';panel.append(none);}
  for(const [index,stop] of (plan.summary?.stops || energy?.stops || []).entries()) {
    const row=document.createElement('div');row.className='route-stop';
    const name=document.createElement('strong');name.textContent=`${index+1}. ${stop.label || stop.name}`;
    const time=document.createElement('small');time.textContent=`${plan.trip_kind==='round_trip'?(stop.phase===1?'Chiều về · ':'Chiều đi · '):''}${Math.ceil(stop.charge_minutes)} phút · pin ${Math.floor(stop.arrival_soc)}% → ${Math.floor(stop.departure_soc)}%`;row.append(name,time);panel.append(row);
  }
  const actions=document.createElement('div');actions.className='trip-actions';
  for(const [label,handler] of [['Xem bản đồ',()=>showTripMap(plan)],['Chỉnh kế hoạch',()=>editTripPlan(plan)],['Bắt đầu',()=>tripCommand('trip.start',plan)]]) {
    const button=document.createElement('button');button.type='button';button.textContent=label;button.disabled=label==='Bắt đầu' && (plan.calculation_status!=='ready' || plan.started_revision===plan.revision);button.addEventListener('click',handler);actions.append(button);
  }panel.append(actions);
  const cancel=document.createElement('button');cancel.type='button';cancel.className='information-cancel';cancel.textContent='Huỷ yêu cầu';cancel.addEventListener('click',()=>tripCommand('trip.cancel',plan));panel.append(cancel);panel.scrollTop=oldScroll;
}
function editTripPlan(plan) {
  const panel=document.querySelector('#trip-plan-card');if(!panel || panel.querySelector?.('form'))return;
  const form=document.createElement('form');form.className='destination-choice-input';
  const input=document.createElement('input');input.placeholder='Điểm đến mới…';input.setAttribute('aria-label','Điểm đến mới');input.maxLength=200;
  const button=document.createElement('button');button.textContent='Tính lại';button.type='submit';form.append(input,button);panel.append(form);input.focus?.();
  form.addEventListener('submit',event=>{event.preventDefault();if(input.value.trim().length>=2)tripCommand('trip.plan',plan,{query:input.value.trim(),trip_kind:plan.trip_kind});});
}
async function showTripMap(plan) {
  if(backendTripPlan?.id!==plan.id || backendTripPlan?.revision!==plan.revision)return;
  mapTripPlan=plan;await openJourney();
  if(backendTripPlan?.id!==plan.id || backendTripPlan?.revision!==plan.revision || !map)return;
  map.getSource('vivi-trip-preview')?.setData({type:'Feature',geometry:{type:'LineString',coordinates:plan.route.coordinates}});
  $('#journey-origin').textContent=plan.origin.label || 'Điểm xuất phát';$('#journey-search').value=plan.selected.spoken_label || plan.selected.name;
  $('#journey-summary').hidden=false;$('#journey-controls').hidden=false;$('#journey-duration').textContent=formatDuration(plan.energy?.selected_plan?.total_seconds || plan.route.duration_s);$('#journey-distance').textContent=formatDistance(plan.route.distance_m);$('#journey-arrival').textContent='Xem trước';
  $('#journey-start span').textContent='Bắt đầu hành trình';$('#journey-start').disabled=plan.calculation_status!=='ready';$('#journey-next').hidden=true;
  $('#journey-steps').innerHTML=plan.route.steps.map(step=>`<li><b>${SIGN_ICONS[step.sign] || '↑'}</b><span>${escapeHtml(step.text)}</span></li>`).join('');
  renderEnergy({...plan,alternatives:[]});setMessage('Xem trước kế hoạch · chưa bắt đầu hành trình');
  if(window.vietmapgl){const bounds=plan.route.coordinates.reduce((box,point)=>box.extend(point),new vietmapgl.LngLatBounds(plan.route.coordinates[0],plan.route.coordinates[0]));map.fitBounds(bounds,{padding:fitPadding(),duration:500});}
}
function restoreNavigationView() {
  if(!mapTripPlan)return;mapTripPlan=null;
  map?.getSource('vivi-trip-preview')?.setData(emptyLine());$('#journey-start').disabled=false;
  if(backendNavigation){$('#journey-origin').textContent=backendNavigation.origin?.label || 'Chưa xác định';$('#journey-search').value=backendNavigation.selected?.name || '';renderRoute();renderEnergy(backendNavigation);}
}

export async function applyNavigation(payload) {
  if(payload.trip_plan) {
    const plan=payload.trip_plan;
    const different=plan.id!==backendTripPlan?.id;
    if(!backendTripPlan || different && (plan.event_sequence || 0)>=(backendTripPlan.event_sequence || 0) || !different && plan.revision>backendTripPlan.revision || !different && plan.revision===backendTripPlan.revision && (plan.event_sequence || 0)>=(backendTripPlan.event_sequence || 0)) {
      if(mapTripPlan && (mapTripPlan.id!==plan.id || mapTripPlan.revision!==plan.revision || plan.calculation_status==='cancelled')) {restoreNavigationView();setView(sim.running?'mini':'hidden');}
      backendTripPlan=plan;
      if(payload.ui_events?.some(event=>event.type.startsWith('trip.')))choiceOwner='trip';
    }
  }
  if(!origin && payload.ui_events?.some(event=>['navigation.locate','trip.locate'].includes(event.type))) {
    config ||= await api('/api/v1/navigation/config');locate();
  }
  const next = payload.navigation;
  if(payload.ui_events?.some(event=>event.type==='navigation.results'))choiceOwner='navigation';
  renderInformation();
  if (!next || (backendNavigation && next.revision < backendNavigation.revision)) return;
  if (backendNavigation && next.revision === backendNavigation.revision && (next.event_sequence || 0) < (backendNavigation.event_sequence || 0)) return;
  const previous = backendNavigation;
  backendNavigation = next;
  renderInformation();
  navigationGrant = payload.confirmation || (next.revision === previous?.revision ? navigationGrant : null);
  const relevant = payload.ui_events?.some(event => event.type.startsWith('navigation.') && event.type !== 'navigation.progress');
  if(relevant && next.status==='running')restoreNavigationView();
  if (next.origin && !origin) setOrigin(next.origin);
  const hasChosenDestination = !!(next.selected && next.route || next.pending?.navigation?.selected && next.pending?.navigation?.route);
  if (relevant && hasChosenDestination && !next.clarification) await openJourney();
  if (next.origin && (!markers.origin || next.origin.lat !== origin?.lat || next.origin.lng !== origin?.lng)) setOrigin(next.origin);
  if ((next.status === 'selecting' || next.pending?.status === 'selecting') && relevant) closeResults();
  if (next.selected && next.route && map && (next.revision !== previous?.revision || !route || (previous?.pending && !next.pending))) {
    if(sim.running && previous?.pending && !next.pending) stopSimulation(false,true);
    destination = next.selected;
    $('#journey-search').value = destination.name;
    placeMarker('destination', [destination.lng, destination.lat], 'destination');
    const cumulative = [0];
    for (let i = 1; i < next.route.coordinates.length; i++) cumulative.push(cumulative[i-1] + haversine(next.route.coordinates[i-1], next.route.coordinates[i]));
    route = { ...next.route, cumulative, length: cumulative.at(-1) || next.route.distance_m };
    hooks.route({ coordinates: route.coordinates, cumulative });
    map.getSource('vivi-route').setData({ type:'Feature', geometry:{ type:'LineString', coordinates:route.coordinates } });
    renderRoute();
    sim.traveled = next.progress_m / route.distance_m * route.length;
  }
  if(!mapTripPlan){renderEnergy(next);renderStationMarkers(next);}
  if (next.status === 'preview' && sim.running) stopSimulation(false,true);
  if (next.status === 'running' && !sim.running && route) { progressAt = performance.now(); startSimulation(true); }
  if (next.status === 'charging') {
    sim.traveled = next.progress_m / route.distance_m * route.length;
    setMessage('Đang dừng sạc theo kế hoạch.');
  }
  if (next.status === 'completed' && previous?.status !== 'completed' && sim.running) finishSimulation();
  else if (['paused','idle'].includes(next.status) && sim.running) stopSimulation(false,true);
  if(next.pending?.navigation?.route && map && relevant) {
    map.getSource('vivi-route').setData({type:'Feature',geometry:{type:'LineString',coordinates:next.pending.navigation.route.coordinates}});
    setMessage(next.pending.message);
  }
}

export function getJourneyPosition() {
  if (backendNavigation?.status === 'completed' && backendNavigation.route?.coordinates?.length) {
    const point = backendNavigation.route.coordinates.at(-1);
    return {lat:point[1],lng:point[0],label:'Vị trí sau hành trình',simulated:true};
  }
  if (sim.running && route) {
    const {point} = positionAt(sim.traveled);
    return {lat:point[1],lng:point[0],label:'Vị trí mô phỏng hiện tại',simulated:true};
  }
  return origin ? {...origin,simulated:origin.simulated ?? true} : null;
}

function renderEnergy(next) {
  const element = document.querySelector('#journey-energy');
  if (!element) return;
  const energy = next.pending?.navigation?.energy || next.energy;
  element.hidden = !energy;
  if (!energy) return;
  const plan = energy.selected_plan;
  const summary = next.pending?.navigation?.summary || next.summary;
  const stops = summary?.stops || plan?.stops || [];
  element.innerHTML = plan ? `<strong>${stops.length ? `${stops.length} điểm dừng sạc` : 'Không cần dừng sạc'} · khoảng ${formatDuration(plan.total_seconds)}</strong><p>Pin dự kiến khi đến: ${Math.floor(plan.arrival_soc)}%</p>${stops.map(stop => `<div class="route-stop"><strong>${escapeHtml(stop.label || stop.name)}</strong><p>${next.trip_kind==='round_trip'?(stop.phase===1?'Chiều về · ':'Chiều đi · '):''}Khoảng ${stop.minutes ?? Math.ceil(stop.charge_minutes)} phút · pin ${Math.floor(stop.arrival_soc)}% → ${Math.floor(stop.departure_soc)}%</p>${stop.road_context ? `<small>Gần ${escapeHtml(stop.road_context)}</small>` : ''}</div>`).join('')}${(next.pending?.navigation?.alternatives || next.alternatives || []).map(item => `<button type="button" data-plan="${escapeHtml(item.id)}">${{fewest_stops:'Ít dừng',fastest:'Nhanh nhất',higher_reserve:'Dự phòng cao'}[item.strategy]} · ${formatDuration(item.total_seconds)}</button>`).join('')}` : `<p>${escapeHtml(energy.message || 'Chưa có kế hoạch sạc khả thi.')}</p>`;
  element.querySelectorAll('[data-plan]').forEach(button => button.addEventListener('click', () => hooks.event?.({ kind:'select_plan', id:crypto.randomUUID(), revision:next.revision, plan_id:button.dataset.plan })));
  if (!plan) renderChargingChoices(element, energy);
  if (plan) {
    const details = document.createElement('details');
    const title = document.createElement('summary'); title.textContent = 'Giả định và nguồn dữ liệu'; details.append(title);
    const assumptions = document.createElement('p');
    assumptions.textContent = `Dự phòng ${plan.reserve_soc || 15}%; tiêu hao ×${plan.assumptions?.conservative_factor || 1}; ${plan.assumptions?.stop_overhead_minutes || 5} phút thao tác mỗi lần dừng. Chưa tính thời gian chờ trụ.`;
    details.append(assumptions);
    for (const stop of stops) {
      const source = document.createElement('p');
      source.textContent = `${stop.label || stop.name}: ${stop.power_kw} kW${stop.equipment_basis === 'assumed' ? ' (giả định)' : ''} · ${stop.connector}. Vị trí: VietMap. Thông số: ${stop.source}`;
      details.append(source);
    }
    element.append(details);
  }
  if (next.pending) {
    const message = document.createElement('p'); message.textContent = next.pending.message;
    element.append(message);
    if(next.pending.navigation) {
      const button = document.createElement('button');
      if(next.pending.status === 'preview' && navigationGrant) {
        button.textContent='Xác nhận đổi tuyến';
        button.addEventListener('click',()=>hooks.confirm?.(navigationGrant));
      } else {
        button.textContent='Xem phương án mới';
        button.addEventListener('click',()=>hooks.command?.('Xem phương án mới',{capability:'navigation.start',arguments:{route_revision:next.revision,plan_id:'pending'}}));
      }
      element.append(button);
    }
  }
}

function renderChargingChoices(element, energy) {
  for (const [index, station] of (energy.station_candidates || []).entries()) {
    const item = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = `${index + 1}. ${station.name}`;
    const details = document.createElement('p');
    details.textContent = `${station.address}${station.rest_area_name ? ` · Gần ${station.rest_area_name}` : ''}${station.road_context ? ` · Gần ${station.road_context}` : ''} · ${formatDistance(station.distance_m)} · khoảng ${formatDuration(station.duration_s)} · pin đến ${Math.floor(station.arrival_soc)}%${station.detour_m ? ` · vòng thêm ${formatDistance(station.detour_m)}` : ''}`;
    const equipment = document.createElement('small');
    equipment.textContent = ['verified','assumed'].includes(station.equipment_status) ? `${station.power_kw} kW${station.equipment_status === 'assumed' ? ' (giả định)' : ''} · ${station.connector}` : 'Chưa có công suất/đầu sạc để tính phút sạc';
    const source = document.createElement('small');
    source.textContent = 'Địa điểm: VietMap · Chưa có thông tin trụ trống/giờ hoạt động';
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = `Dẫn tới trạm ${index + 1}`;
    button.addEventListener('click', () => hooks.command?.('Dẫn tới trạm sạc', {capability:'navigation.select', arguments:{place_handle:station.handle}}));
    item.append(name, details, equipment, source, button);
    element.append(item);
  }
}

function renderStationMarkers(next) {
  if (!map) return;
  const energy = next.pending?.navigation?.energy || next.energy || {};
  const stations = energy.selected_plan?.stops || energy.station_candidates || [];
  const ids = new Set(stations.map(station => `charging:${station.id || station.station_id}`));
  for (const id of Object.keys(markers)) if (id.startsWith('charging:') && !ids.has(id)) removeMarker(id);
  for (const station of stations) placeMarker(`charging:${station.id || station.station_id}`, [station.lng, station.lat], 'charging');
}
