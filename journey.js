// Journey view: VietMap map, destination search, route planning and a
// client-side drive simulation along the planned route. Search and routing go
// through the backend so the VietMap service key stays server side.
const $ = (selector) => document.querySelector(selector);
const SDK_VERSION = '6.0.1';
const SDK_BASE = `https://unpkg.com/@vietmap/vietmap-gl-js@${SDK_VERSION}/dist`;
const EARTH_RADIUS_M = 6371000;
// GraphHopper-style maneuver signs returned by the VietMap route API.
const SIGN_ICONS = { '-98': '↶', '-8': '↶', '-7': '↖', '-3': '↰', '-2': '↰', '-1': '↖', 0: '↑', 1: '↗', 2: '↱', 3: '↱', 4: '◎', 5: '•', 6: '↻', 7: '↗', 8: '↷' };

let hooks = { announce: () => {}, setDriving: () => {}, progress: () => {}, route: () => {}, travel: () => {} };
let config = null;
let map = null;
let sdkPromise = null;
let origin = null;          // { lat, lng, label }
let destination = null;     // { lat, lng, name, address }
let route = null;           // backend Route + cumulative distances
let markers = {};
let sim = { running: false, speedup: 5, traveled: 0, last: 0, frame: 0, step: -1, follow: true, lastPaint: 0, bearing: 0 };
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
  const line = { 'line-join': 'round', 'line-cap': 'round' };
  map.addLayer({ id: 'vivi-route-glow', type: 'line', source: 'vivi-route', layout: line, paint: { 'line-color': '#9aa8c8', 'line-width': 14, 'line-opacity': .14, 'line-blur': 6 } });
  map.addLayer({ id: 'vivi-route-line', type: 'line', source: 'vivi-route', layout: line, paint: { 'line-color': '#d0d8ea', 'line-width': 4 } });
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
    if (sim.running || view !== 'full') return;
    setDestination({ lat: event.lngLat.lat, lng: event.lngLat.lng, name: 'Điểm đã chọn trên bản đồ', address: `${event.lngLat.lat.toFixed(5)}, ${event.lngLat.lng.toFixed(5)}` });
  });
  // Manual panning pauses camera follow until the recenter button is used.
  map.on('dragstart', () => { if (sim.running) sim.follow = false; });
}

function locate() {
  $('#journey-origin').textContent = 'Đang xác định vị trí…';
  const fallback = (reason) => {
    setOrigin({ lat: config.default_center.lat, lng: config.default_center.lng, label: 'Vị trí mặc định · Hồ Gươm' });
    setMessage(`${reason} ViVi dùng vị trí mặc định để mô phỏng.`, 'warn');
  };
  if (!('geolocation' in navigator)) return fallback('Trình duyệt không hỗ trợ định vị.');
  navigator.geolocation.getCurrentPosition(
    position => {
      setOrigin({ lat: position.coords.latitude, lng: position.coords.longitude, label: 'Vị trí của bạn' });
      if (!destination) setMessage('Tìm điểm đến hoặc chạm vào bản đồ để chọn.');
    },
    error => fallback(error.code === error.PERMISSION_DENIED ? 'Bạn chưa cho phép truy cập vị trí.' : 'Không lấy được vị trí hiện tại.'),
    { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
  );
}

function setOrigin(point) {
  origin = point;
  $('#journey-origin').textContent = point.label;
  if (!map) return;
  placeMarker('origin', [point.lng, point.lat], 'origin');
  if (destination) planRoute();
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
    ? places.map((place, index) => `<li role="option" tabindex="-1" data-index="${index}"><strong>${escapeHtml(place.name)}</strong><span>${escapeHtml(place.address)}</span></li>`).join('')
    : '<li class="empty">Không tìm thấy địa điểm phù hợp</li>';
  list.hidden = false;
  $('#journey-search').setAttribute('aria-expanded', 'true');
  list.querySelectorAll('[data-index]').forEach(item => item.addEventListener('click', () => choosePlace(places[Number(item.dataset.index)])));
}
async function search(text) {
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
  closeResults();
  setMessage(`Đang lấy vị trí “${place.name}”…`);
  try {
    const detail = await api(`/api/v1/navigation/place/${encodeURIComponent(place.ref_id)}`);
    setDestination({ lat: detail.lat, lng: detail.lng, name: detail.name || place.name, address: detail.address || place.address });
  } catch (error) {
    setMessage(error.message, 'error');
  }
}

async function planRoute() {
  if (!origin || !destination || !map) return;
  setMessage('Đang tìm tuyến đường phù hợp…');
  $('#journey').classList.add('loading');
  try {
    const planned = await api(`/api/v1/navigation/route?origin=${origin.lat},${origin.lng}&destination=${destination.lat},${destination.lng}`);
    const cumulative = [0];
    for (let index = 1; index < planned.coordinates.length; index++) cumulative.push(cumulative[index - 1] + haversine(planned.coordinates[index - 1], planned.coordinates[index]));
    route = { ...planned, cumulative, length: cumulative.at(-1) || planned.distance_m };
    hooks.route({ coordinates: route.coordinates, cumulative });
    map.getSource('vivi-route').setData({ type: 'Feature', geometry: { type: 'LineString', coordinates: planned.coordinates } });
    map.getSource('vivi-traveled').setData(emptyLine());
    const bounds = planned.coordinates.reduce((box, point) => box.extend(point), new vietmapgl.LngLatBounds(planned.coordinates[0], planned.coordinates[0]));
    map.fitBounds(bounds, { padding: fitPadding(), duration: 900 });
    renderRoute();
    setMessage(`Đến ${destination.name} · ${destination.address}`);
  } catch (error) {
    route = null;
    hooks.route(null);
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
  const seconds = route.duration_s * (remaining / route.length || 0);
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
function stepAt(index) {
  let current = 0;
  route.steps.forEach((step, stepIndex) => { if (step.point_index <= index) current = stepIndex; });
  return current;
}
function showNextManeuver(stepIndex, traveled) {
  const next = route.steps[stepIndex + 1];
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
  document.querySelectorAll('#journey-steps li').forEach(item => {
    const index = Number(item.dataset.step);
    item.classList.toggle('active', index === stepIndex);
    item.classList.toggle('done', index < stepIndex);
  });
  document.querySelector(`#journey-steps li[data-step="${stepIndex}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function tick(now) {
  if (!sim.running) return;
  const elapsed = Math.min(0.25, (now - sim.last) / 1000);
  sim.last = now;
  const speed = route.length / Math.max(1, route.duration_s);
  sim.traveled = Math.min(route.length, sim.traveled + elapsed * speed * sim.speedup);
  const { index, point, heading } = positionAt(sim.traveled);
  hooks.travel(sim.traveled);
  const car = placeMarker('car', point, 'car');
  car.setRotation(heading);
  // Repainting map sources every frame is wasteful; ~8 Hz is smooth enough.
  if (now - sim.lastPaint > 120) {
    sim.lastPaint = now;
    map.getSource('vivi-traveled').setData({ type: 'Feature', geometry: { type: 'LineString', coordinates: [...route.coordinates.slice(0, index + 1), point] } });
    const summary = updateSummary(sim.traveled);
    const stepIndex = stepAt(index);
    const upcoming = showNextManeuver(stepIndex, sim.traveled);
    hooks.progress({
      speedKmh: cruiseSpeed(now, upcoming),
      next: upcoming ? { sign: upcoming.next.sign, icon: SIGN_ICONS[upcoming.next.sign] ?? '↑', text: upcoming.next.text, distance: upcoming.distance } : null,
      heading, remaining: summary.remaining, arrival: summary.arrival, destination: destination.name
    });
    if (stepIndex !== sim.step) {
      sim.step = stepIndex;
      highlightStep(stepIndex);
      if (upcoming) hooks.announce(`Sau ${formatDistance(upcoming.distance)}, ${upcoming.next.text.charAt(0).toLowerCase()}${upcoming.next.text.slice(1)}`);
    }
  }
  if (view === 'mini') {
    // Course-up in the corner map: smooth the heading so turns do not snap.
    const delta = ((heading - sim.bearing + 540) % 360) - 180;
    sim.bearing = (sim.bearing + delta * Math.min(1, elapsed * 4) + 360) % 360;
    map.jumpTo({ center: point, bearing: sim.bearing, zoom: 16.2, pitch: 50 });
  } else if (sim.follow) map.jumpTo({ center: point });
  if (sim.traveled >= route.length) return finishSimulation();
  sim.frame = requestAnimationFrame(tick);
}
function startSimulation() {
  if (!route) return;
  if (sim.traveled >= route.length) sim.traveled = 0;
  sim.running = true; sim.follow = true; sim.step = -1; sim.last = performance.now();
  $('#journey').classList.add('simulating');
  $('#journey-start span').textContent = 'Tạm dừng';
  sim.bearing = positionAt(sim.traveled).heading;
  // Driving hands the screen back to the 3D view; the map shrinks to a corner.
  setView('mini');
  hooks.setDriving(true);
  hooks.announce(`Bắt đầu hành trình đến ${destination.name}. Quãng đường ${formatDistance(route.length)}, khoảng ${formatDuration(route.duration_s)}.`);
  sim.frame = requestAnimationFrame(tick);
}
function stopSimulation(announce = true) {
  const wasRunning = sim.running;
  sim.running = false;
  cancelAnimationFrame(sim.frame);
  $('#journey').classList.remove('simulating');
  $('#journey-start span').textContent = sim.traveled > 0 && route && sim.traveled < route.length ? 'Tiếp tục mô phỏng' : 'Bắt đầu mô phỏng';
  if (wasRunning) {
    hooks.travel(null);
    hooks.setDriving(false);
    hooks.progress(null);
    if (announce) hooks.announce('Đã tạm dừng mô phỏng hành trình.');
  }
}
function finishSimulation() {
  sim.running = false;
  $('#journey').classList.remove('simulating');
  $('#journey-start span').textContent = 'Chạy lại mô phỏng';
  $('#journey-next').hidden = true;
  highlightStep(route.steps.length);
  hooks.travel(null);
  hooks.setDriving(false);
  hooks.progress(null);
  setTimeout(() => { if (!sim.running && view === 'mini') setView('hidden'); }, 6000);
  hooks.announce(`Bạn đã đến ${destination.name}. Chúc bạn một ngày tốt lành.`);
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
      if (!origin) locate();
    }
  } catch (error) {
    // Search and markers need a live map; keep them off until a reopen succeeds.
    $('#journey').classList.add('no-map');
    $('#journey-search').disabled = true;
    $('#journey-origin').textContent = 'Chưa xác định';
    setMessage(error.message, 'error');
  }
}
export function closeJourney() {
  // A trip in progress stays visible as the corner map.
  const unfinished = route && sim.traveled > 0 && sim.traveled < route.length;
  setView(sim.running || unfinished ? 'mini' : 'hidden');
}
export function initJourney(options) {
  hooks = { ...hooks, ...options };
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
    if (sim.running) stopSimulation();
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
