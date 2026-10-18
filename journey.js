import {JourneyMap} from './journey-map.js?v=1';
import {DriveSimulation} from './journey-simulation.js?v=1';
import {JourneyRenderer} from './journey-renderer.js?v=1';
// Journey view: VietMap map, destination search, route planning and a
// client-side drive simulation along the planned route. Search and routing go
// through the backend so the VietMap service key stays server side.
const $ = (selector) => document.querySelector(selector);
const EARTH_RADIUS_M = 6371000;
// GraphHopper-style maneuver signs returned by the VietMap route API.
const SIGN_ICONS = { '-98': '↶', '-8': '↶', '-7': '↖', '-3': '↰', '-2': '↰', '-1': '↖', 0: '↑', 1: '↗', 2: '↱', 3: '↱', 4: '◎', 5: '•', 6: '↻', 7: '↗', 8: '↷' };

let hooks = { announce: () => {}, setDriving: () => {}, progress: () => {}, route: () => {}, travel: () => {}, network: () => {} };
let backendNavigation = null;
let backendTripPlan = null;
let mapTripPlan = null; // Read-only map target, never the implicit action target.
let mapAction = null;
let commandPending = false;
let backendWorkspace = null;
let choiceOwner = 'navigation';
let planningSteps = [];
let routeIdentity = null;
let motionBlocked = false;
let chargingTimer = null;
let activeMapLoad = null;
let lastEnergySignature = null;
const spokenChargingEvents = new Set();
let pauseRequest = null;
let journeyInformationHidden = false;
const retiredTripPlans = new Set();
try { journeyInformationHidden = sessionStorage.getItem('vivi.journeyInformationHidden') === 'true'; } catch {}
const hiddenInformation = new Set();
try { for (const key of JSON.parse(sessionStorage.getItem('vivi.hiddenInformation') || '[]')) hiddenInformation.add(key); } catch {}
let navigationGrant = null;
let progressAt = 0;
let progressPending = false;
let config = null;
let map = null;

let origin = null;          // { lat, lng, label }
let destination = null;     // { lat, lng, name, address }
let route = null;           // backend Route + cumulative distances
let markers = {};
let sim = new DriveSimulation();
const presentation = new JourneyRenderer();
let lastNetworkScan = 0;
// 'full' covers the screen for planning, 'mini' is the corner map while driving.
let view = 'hidden';
let searchTimer = 0;
let searchToken = 0;

function draftNavigation() {
  if(backendWorkspace && Object.hasOwn(backendWorkspace,'draft')) {
    const draft=backendWorkspace.draft;
    return draft && draft.purpose!=='itinerary' && draft.calculation_status!=='cancelled'
      && draft.started_revision!==draft.revision ? draft : null;
  }
  const nav = backendNavigation;
  if (!nav) return null;
  return nav.pending?.navigation || (['preview','selecting'].includes(nav.status) ? nav : null);
}
function activeJourney() { return ['running','paused','charging'].includes(backendNavigation?.status); }
function planOwner(plan) { return plan.id && plan.id===backendTripPlan?.id?'trip':'navigation'; }
function planAction(plan, owner) {
  return {owner, id:plan.id || plan.request_id, revision:plan.revision};
}
function actionCurrent(target) {
  if(!target)return false;
  const plan=target.owner==='trip'?backendTripPlan:draftNavigation();
  return !!plan && (plan.id || plan.request_id)===target.id && plan.revision===target.revision;
}
async function acceptPlan(target) {
  if (commandPending || !actionCurrent(target)) return;
  commandPending=true;renderInformation();renderMapAction();
  try {
    if(target.owner==='trip') await tripCommand('trip.start',backendTripPlan);
    else await hooks.command?.('Bắt đầu tuyến đã chọn',{capability:'navigation.start',arguments:identityArguments(draftNavigation())});
  } finally {commandPending=false;renderInformation();renderMapAction();}
}
async function cancelPlan(target) {
  if(commandPending || !actionCurrent(target))return;
  const plan=target.owner==='trip'?backendTripPlan:draftNavigation();
  commandPending=true;renderInformation();renderMapAction();
  try {
    return await hooks.command?.('Huỷ kế hoạch',{capability:`${target.owner}.cancel`,arguments:identityArguments(plan)});
  } finally {commandPending=false;renderInformation();renderMapAction();}
}
function renderMapAction() {
  const button=document.querySelector('#journey-start');if(!button)return;
  const target=mapAction;
  const plan=target?.owner==='trip'?backendTripPlan:draftNavigation();
  button.hidden=!target || !actionCurrent(target);
  button.disabled=commandPending || !plan?.energy?.selected_plan || !!plan?.clarification;
  const label=document.querySelector('#journey-start span');
  if(label)label.textContent=activeJourney()?`Thay tuyến sang ${plan?.selected?.spoken_label || plan?.selected?.name || 'đích mới'}`:'Bắt đầu hành trình';
}

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

const mapAdapter = new JourneyMap({
  fetchConfig:async()=>{
    config=await api('/api/v2/navigation/config');
    if(config.available)void api('/api/v2/navigation/map-health').then(health=>{
      if([401,403,423,429].includes(health.tile_status)){
        const notice=$('#journey-notice');notice.textContent=`Nền bản đồ chưa sẵn sàng (HTTP ${health.tile_status}). Bạn vẫn có thể tìm điểm đến.`;notice.hidden=false;
      }
    }).catch(()=>{});
    return config;
  },
  notice:text=>{const notice=$('#journey-notice');notice.textContent=text;notice.hidden=false;},
  ready:created=>{map=created;markers=mapAdapter.markers;hydrateMap();},
  attach:created=>{
    created.on('click',event=>{
      if(sim.running || mapTripPlan || view!=='full')return;
      hooks.event?.({kind:'point',id:crypto.randomUUID(),revision:backendNavigation?.revision || 0,
        position:{lat:event.lngLat.lat,lng:event.lngLat.lng,label:'Điểm đã chọn trên bản đồ',simulated:true}});
    });
    created.on('dragstart',()=>{if(sim.running)sim.follow=false;});
  }
});
function placeMarker(name,lngLat,className){return mapAdapter.marker(name,lngLat,className);}
function removeMarker(name){markers[name]?.remove();delete markers[name];}
function emptyLine(){return {type:'Feature',geometry:{type:'LineString',coordinates:[]}};}
function ensureMap(){return mapAdapter.ensure().then(created=>{map=created;return created;});}
function hydrateMap(){
  if(!map)return;
  const active=backendNavigation;
  map.getSource('vivi-route')?.setData(active?.route?{type:'Feature',geometry:{type:'LineString',coordinates:active.route.coordinates}}:emptyLine());
  const preview=mapTripPlan || active?.pending?.navigation;
  map.getSource(mapTripPlan?'vivi-trip-preview':'vivi-pending-preview')?.setData(preview?.route?{type:'Feature',geometry:{type:'LineString',coordinates:preview.route.coordinates}}:emptyLine());
  if(active?.origin)setOrigin(active.origin);
  if(active?.selected)placeMarker('destination',[active.selected.lng,active.selected.lat],'destination');
  if(active)renderStationMarkers(active);
  if(route && active?.progress_m){const sample=positionAt(sim.traveled);placeMarker('car',sample.point,'car')?.setRotation(sample.heading);}
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
    const places = await api(`/api/v2/navigation/search?text=${encodeURIComponent(text)}${near}`);
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
    const detail = await api(`/api/v2/navigation/place/${encodeURIComponent(place.ref_id)}`);
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
    const planned = await api(`/api/v2/navigation/route?origin=${origin.lat},${origin.lng}&destination=${destination.lat},${destination.lng}`);
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
    map?.getSource('vivi-route')?.setData({ type: 'Feature', geometry: { type: 'LineString', coordinates: planned.coordinates } });
    map?.getSource('vivi-traveled')?.setData(emptyLine());
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
  const container=map?.getContainer?.();
  const width=container?.clientWidth || globalThis.innerWidth || 1024;
  const height=container?.clientHeight || globalThis.innerHeight || 768;
  if(width>760)return {top:60,bottom:60,right:60,left:Math.min(panel.width+60,width*.65)};
  const covered=Number.isFinite(panel.top)?height-panel.top+20:panel.height+30;
  return {top:40,bottom:Math.min(Math.max(30,covered),height*.65),left:30,right:30};
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

function currentLeg(traveled = sim.traveled) {
  const nav = backendNavigation;
  const plan = nav?.energy?.selected_plan;
  const stop = plan?.stops?.[nav.stop_index || 0];
  const progress = route?.length ? traveled / route.length * route.distance_m : nav?.progress_m || 0;
  let end = nav?.route?.distance_m || 0;
  let label = destination?.spoken_label || destination?.name || '';
  if (stop) {
    // leg_index distinguishes repeated visits to one station on a return trip.
    let legIndex = stop.leg_index;
    if (legIndex == null) {
      let from = 0;
      for (const item of plan.stops.slice(0, (nav.stop_index || 0) + 1)) {
        legIndex = plan.legs.findIndex((leg, i) => i >= from && leg.to === item.station_id);
        from = legIndex + 1;
      }
    }
    end = plan.legs.slice(0, legIndex + 1).reduce((total, leg) => total + leg.distance_m, 0);
    label = `Trạm sạc thứ ${(nav.stop_index || 0) + 1}: ${stop.label || stop.name}`;
  } else if (nav?.trip_kind === 'round_trip') {
    let traveled = 0;
    for (const leg of plan?.legs || []) {
      traveled += leg.distance_m;
      if (leg.to === 'destination') {
        if (progress < traveled) end = traveled;
        else label = nav.origin?.label || 'Điểm xuất phát';
        break;
      }
    }
  }
  let seconds = 0, start = 0;
  for (const leg of plan?.legs || []) {
    const overlap = Math.max(0, Math.min(end, start + leg.distance_m) - Math.max(progress, start));
    seconds += leg.duration_s * overlap / Math.max(1, leg.distance_m);
    start += leg.distance_m;
  }
  if (!plan) seconds = (route?.duration_s || 0) * Math.max(0, end - progress) / Math.max(1, end);
  return {destination:label,remaining:Math.max(0, end - progress),arrival:new Date(Date.now() + seconds * 1000),
    chargingMinutes:nav?.status === 'charging' && stop ? Math.ceil(Math.max(0, stop.charge_minutes * 60 - (nav.charge_elapsed_s || 0)) / 60) : null};
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
  if(!map)return;
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
function positionAt(distance){return DriveSimulation.position(route,distance,bearing);}
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
function stepAt(index){return DriveSimulation.step(route,index);}
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
  const elapsed = sim.advance(now,route);
  if (backendNavigation?.status === 'running' && !progressPending && now - progressAt >= 1000) sendProgress({ kind: 'progress', distance_m: (route.length ? sim.traveled / route.length : 0) * route.distance_m }, now);
  const { index, point, heading } = positionAt(sim.traveled);
  hooks.travel(sim.traveled);
  if(view!=='hidden' && now-sim.lastMarker>=1000/30){sim.lastMarker=now;placeMarker('car',point,'car')?.setRotation(heading);}
  // Keep traveled geometry and guidance at 5 Hz.
  if (now - sim.lastPaint > 200) {
    sim.lastPaint = now;
    if(view!=='hidden' && !mapTripPlan)map?.getSource('vivi-traveled')?.setData({ type: 'Feature', geometry: { type: 'LineString', coordinates: [...route.coordinates.slice(0, index + 1), point] } });
    updateSummary(sim.traveled);
    const stepIndex = stepAt(index);
    const upcoming = showNextManeuver(stepIndex, sim.traveled);
    hooks.progress({
      speedKmh: cruiseSpeed(now, upcoming),
      next: upcoming ? { sign: upcoming.next.sign, exitTurn: upcoming.next.exitTurn, icon: SIGN_ICONS[upcoming.next.sign] ?? '↑', text: upcoming.next.text, distance: upcoming.distance } : null,
      heading, ...currentLeg(),
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
  if (view==='hidden' || mapTripPlan) { /* Viewing another plan must not move the active-route camera. */ } else if (view === 'mini') {
    // Course-up in the corner map: smooth the heading so turns do not snap.
    const delta = ((heading - sim.bearing + 540) % 360) - 180;
    sim.bearing = (sim.bearing + delta * Math.min(1, elapsed * 4) + 360) % 360;
    mapAdapter.camera({center:point,bearing:sim.bearing,zoom:16.2,pitch:50},now);
  } else if (sim.follow) mapAdapter.camera({center:point},now);
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
  if (!fromServer) return acceptPlan(mapAction || (draftNavigation() && planAction(draftNavigation(),'navigation')));
  sim.running = true; sim.follow = true; sim.step = -1; sim.last = performance.now();
  $('#journey').classList.add('simulating');
  $('#journey-start span').textContent = 'Tạm dừng';
  sim.bearing = positionAt(sim.traveled).heading;
  // Driving hands the screen back to the 3D view; the map shrinks to a corner.
  setView(journeyInformationHidden ? 'hidden' : 'mini');

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

    hooks.progress(null);
    if (announce) hooks.announce('Đã tạm dừng mô phỏng hành trình.');
  }
  if (fromServer) setView('hidden');
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
  if(!mapAdapter.resizeObserver)setTimeout(() => map?.resize(),380);
}

function setJourneyDetails(expanded) {
  $('#journey').classList.toggle('details-expanded',expanded);
  const button=$('#journey-details-toggle');
  if(button){button.setAttribute('aria-expanded',String(expanded));button.textContent=expanded?'Thu gọn':'Chi tiết';}
}

// --- Public API -----------------------------------------------------------
export async function openJourney() {
  journeyInformationHidden=false;rememberJourneyVisibility();renderInformation();
  setView('full');
  await prepareJourneyMap();
  const draft=draftNavigation();
  if(!mapTripPlan && choiceOwner==='trip' && backendTripPlan?.route && backendTripPlan.calculation_status!=='cancelled' && backendTripPlan.started_revision!==backendTripPlan.revision) await showPlanMap(backendTripPlan,'trip',false);
  else if(!mapTripPlan && draft?.route) await showPlanMap(draft,'navigation',false);
  renderMapAction();
}
async function prepareJourneyMap() {
  try {
    if (!map) await ensureMap();
    if (map) {
      $('#journey').classList.remove('no-map');
      $('#journey-search').disabled = false;
      map.resize();
      hydrateMap();
      if (!origin && !mapTripPlan) locate();
    }
  } catch (error) {
    // Search stays available while map resources recover.
    $('#journey').classList.add('no-map');
    $('#journey-search').disabled = false;
    $('#journey-origin').textContent = origin?.label || 'Chưa xác định';
    setMessage(error.message, 'error');
  }
}
// Shifting to P (button, voice or backend state) stops the car on the map too.
export function pauseJourney() {
  if (pauseRequest) return pauseRequest;
  stopSimulation(false,true);
  setView('hidden');
  if (hooks.event && ['running','charging'].includes(backendNavigation?.status)) {
    pauseRequest=hooks.event({kind:'pause',id:crypto.randomUUID(),revision:backendNavigation.revision}).finally(()=>{pauseRequest=null;});
    return pauseRequest;
  }
}
export function resetJourneyRuntime(){
  stopSimulation(false,true);
  if(chargingTimer)clearInterval(chargingTimer);
  chargingTimer=null;backendNavigation=null;backendWorkspace=null;backendTripPlan=null;
  spokenChargingEvents.clear();
  routeIdentity=null;lastEnergySignature=null;route=null;activeMapLoad=null;
}
export function setJourneyMotionBlocked(blocked) {
  motionBlocked=blocked;
  if(blocked && backendNavigation?.status==='running')stopSimulation(false,true);
  if(!blocked && backendNavigation?.status==='running' && route && !sim.running)startSimulation(true);
}
export async function changeJourneyDriving(driving) {
  if(hooks.gear)return hooks.gear(driving?'D':'P');
  if(!hooks.event)return false;
  await hooks.event({kind:'gear',gear:driving?'D':'P',id:crypto.randomUUID(),revision:backendNavigation?.revision || 0});
  return true;
}
export function closeJourney() {
  restoreNavigationView();
  // A trip in progress stays visible as the corner map.
  setView(sim.running && !journeyInformationHidden ? 'mini' : 'hidden');
}
function rememberJourneyVisibility() {
  try { sessionStorage.setItem('vivi.journeyInformationHidden',String(journeyInformationHidden)); } catch {}
}
function hideJourneyInformation() {
  journeyInformationHidden=true;rememberJourneyVisibility();restoreNavigationView();setView('hidden');renderInformation();
}
export function initJourney(options) {
  hooks = { ...hooks, ...options };
  $('#journey-details-toggle')?.addEventListener('click',()=>setJourneyDetails(!$('#journey').classList.contains('details-expanded')));
  document.querySelector('#journey-visibility')?.addEventListener('click',hideJourneyInformation);
  document.querySelector('#journey-clear')?.addEventListener('click',()=>hooks.event?.({kind:'end',id:crypto.randomUUID(),revision:backendNavigation?.revision || 0}));
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
  $('#journey-start').addEventListener('click', () => acceptPlan(mapAction));
  document.querySelectorAll('[data-speed]').forEach(button => button.addEventListener('click', () => {
    sim.speedup = Number(button.dataset.speed);
    document.querySelectorAll('[data-speed]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  }));
  const input = $('#journey-search');
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const text = input.value.trim();
    if (text.length < 2) { searchToken++; closeResults(); return; }
    if (mapAction?.owner==='trip') { searchTimer = setTimeout(() => tripCommand('trip.plan', backendTripPlan, {query:text,trip_kind:backendTripPlan.trip_kind}), 700); return; }
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
  catch (error) { stopSimulation(false,true); setMessage(`Đang đồng bộ hành trình: ${error.message}`,'error');hooks.transportLost?.(); }
  finally { progressPending = false; }
}

function informationKey(next, label) { return `${next.id || next.request_id || `navigation:${next.revision}`}:${label}`; }
function rememberHiddenInformation(key) {
  hiddenInformation.add(key);
  try { sessionStorage.setItem('vivi.hiddenInformation', JSON.stringify([...hiddenInformation].slice(-100))); } catch {}
}
function hideInformation(key) {
  rememberHiddenInformation(key);
  renderInformation();
}
function currentChoice() { return choiceOwner === 'trip' ? backendTripPlan : draftNavigation() || backendNavigation; }
function identityArguments(next) { return {...(next.id?{plan_id:next.id}:{}),route_revision:next.revision}; }
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
  const next=currentChoice();if(next) {renderDestinationChoices(next);if(next.clarification || next.status==='selecting'){reopenButton(next,'Địa điểm');if(view==='full')setJourneyDetails(true);}}
  else for(const selector of ['#destination-choices','#journey-destination-choices']){const panel=document.querySelector(selector);if(panel)panel.hidden=true;}
  const draft=draftNavigation();
  const plan=choiceOwner==='trip' && backendTripPlan?.calculation_status!=='cancelled' && backendTripPlan?.started_revision!==backendTripPlan?.revision?backendTripPlan:draft;
  const card=document.querySelector('#trip-plan-card');if(card)card.hidden=true;
  if(plan?.route && !next?.clarification && next?.status!=='selecting') {renderTripCard(plan);reopenButton(plan,'Kế hoạch');}
  renderPlanningControls();
  const rail=document.querySelector('#information-rail');rail?.classList.toggle('journey-information-hidden',journeyInformationHidden);
  const visibility=document.querySelector('#journey-visibility');
  if(visibility){visibility.textContent=journeyInformationHidden?'Hiện bản đồ & kế hoạch':'Ẩn bản đồ & kế hoạch';visibility.setAttribute('aria-pressed',String(journeyInformationHidden));}
  const toolbar=document.querySelector('#journey-toolbar');
  if(toolbar)toolbar.hidden=!backendNavigation?.route && !backendNavigation?.clarification && !backendTripPlan && !backendNavigation?.candidates?.length && !planningSteps.some(step=>step.capability?.startsWith('navigation.') && step.status==='waiting_slot');
  const status=document.querySelector('#journey-state');
  if(status){
    const labels={idle:'Chưa có hành trình',selecting:'Đang chọn điểm đến',preview:'Chờ xác nhận',running:'Đang đi',charging:'Đang sạc',paused:'Tạm dừng · P',completed:'Đã hoàn tất',ended:'Đã kết thúc'};
    const selected=backendNavigation?.selected;
    status.textContent=`${labels[backendNavigation?.status] || 'Kế hoạch'}${selected?` · ${selected.spoken_label || selected.name}`:''}`;
    if(backendNavigation?.pending?.navigation?.selected)status.textContent+=` · Chờ xác nhận: ${backendNavigation.pending.navigation.selected.spoken_label || backendNavigation.pending.navigation.selected.name}`;
  }
  const clear=document.querySelector('#journey-clear');if(clear){clear.hidden=!activeJourney();clear.disabled=commandPending;}
}
function renderPlanningControls() {
  const panel=document.querySelector('#planning-controls');if(!panel)return;
  panel.hidden=true;panel.innerHTML='';
  const missing=planningSteps.find(step=>step.status==='waiting_slot' && step.capability?.startsWith('navigation.'));
  const draft=draftNavigation();
  if(missing && (!draft || !draft.route && !draft.clarification && draft.status!=='selecting') && !journeyInformationHidden) {
    panel.hidden=false;const label=document.createElement('span');
    label.textContent='Đang chờ điểm xuất phát';
    const revision=backendNavigation?.revision || 0;
    const cancel=document.createElement('button');cancel.textContent='Huỷ yêu cầu';cancel.className='planning-cancel';cancel.disabled=commandPending;
    const target=draft && planAction(draft,'navigation');
    cancel.addEventListener('click',()=>{if(target)return cancelPlan(target);if(backendNavigation?.revision===revision)return hooks.command?.('Huỷ tuyến',{capability:'navigation.cancel',arguments:{route_revision:revision}});});panel.append(cancel,label);
  }
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
    const signature=JSON.stringify([key,next.revision,clarification,next.candidates,destinationChoiceBusy,commandPending]);
    if(panel.renderSignature===signature)continue;panel.renderSignature=signature;
    const oldScroll=panel.scrollTop;panel.innerHTML='';
    panelHeading(panel,'Địa điểm',key);
    const heading=document.createElement('p');heading.className='destination-choice-heading';heading.textContent=clarification?.question || (entries.length===1?'Đúng chỗ này chứ?':'Bạn muốn đến địa điểm nào?');panel.append(heading);
    if (['change_destination','confirm_destination'].includes(clarification?.phase) && next.selected) {
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
        try{await hooks.command?.('Chọn điểm đến',{capability:`${planOwner(next)}.${clarification?'refine':'select'}`,arguments:{...identityArguments(next),...(clarification?{choice_handle:entry.handle}:{place_handle:entry.handle})}});}
        finally{destinationChoiceBusy=false;renderInformation();}
      });panel.append(button);
    }
    if(!clarification && entries.length) {
      const other=document.createElement('button');other.type='button';other.className='information-cancel';other.textContent='Tìm địa điểm khác';other.disabled=destinationChoiceBusy;
      other.addEventListener('click',async()=>{const current=currentChoice();if(destinationChoiceBusy || !current || current.revision!==next.revision || informationKey(current,'Địa điểm')!==key)return;
        destinationChoiceBusy=true;renderInformation();
        try{await hooks.command?.('Tìm địa điểm khác',{capability:`${planOwner(next)}.refine`,arguments:{...identityArguments(next),reject_candidates:true}});}
        finally{destinationChoiceBusy=false;renderInformation();}});panel.append(other);
    }
    if(clarification?.phase==='specific') {
      const form=document.createElement('form');form.className='destination-choice-input';
      const input=document.createElement('input');input.placeholder='Tên địa điểm…';input.maxLength=200;input.setAttribute('aria-label','Địa điểm cụ thể');
      const button=document.createElement('button');button.type='submit';button.textContent='Tìm';button.disabled=destinationChoiceBusy;form.append(input,button);
      form.addEventListener('submit',async event=>{event.preventDefault();const current=currentChoice();if(destinationChoiceBusy || current?.revision!==next.revision || informationKey(current,'Địa điểm')!==key || input.value.trim().length<2)return;destinationChoiceBusy=true;
        try{await hooks.command?.('Tìm điểm đến',{capability:`${planOwner(next)}.refine`,arguments:{...identityArguments(next),query:input.value.trim()}});}
        finally{destinationChoiceBusy=false;renderInformation();}});panel.append(form);
    }
    const cancel=document.createElement('button');cancel.type='button';cancel.className='information-cancel';cancel.textContent='Huỷ yêu cầu';cancel.disabled=commandPending;
    const target=planAction(next,planOwner(next));
    cancel.addEventListener('click',()=>cancelPlan(target));panel.append(cancel);
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
  const owner=planOwner(plan),target=planAction(plan,owner);
  const key=informationKey(plan,'Kế hoạch');
  panel.hidden= !plan.route || !!plan.clarification || plan.calculation_status==='cancelled' || hiddenInformation.has(key);
  if(panel.hidden)return;
  const signature=JSON.stringify([plan.id,plan.revision,plan.calculation_status,plan.offer,plan.started_revision,commandPending,backendNavigation?.status]);if(!presentation.begin(panel,signature))return;
  const oldScroll=panel.scrollTop;panelHeading(panel,'Kế hoạch',key);
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
  for(const [label,handler] of [['Xem bản đồ',()=>showPlanMap(plan,owner)],['Chỉnh kế hoạch',()=>editTripPlan(plan)],['Bắt đầu',()=>acceptPlan(target)]]) {
    const button=document.createElement('button');button.type='button';button.textContent=label==='Bắt đầu' && activeJourney()?`Thay tuyến sang ${plan.selected?.spoken_label || plan.selected?.name}`:label;button.disabled=commandPending || label==='Bắt đầu' && (!plan.energy?.selected_plan || !!plan.clarification || plan.started_revision===plan.revision);button.addEventListener('click',handler);actions.append(button);
  }panel.append(actions);
  if(plan.started_revision!==plan.revision){const cancel=document.createElement('button');cancel.type='button';cancel.className='information-cancel';cancel.textContent='Huỷ kế hoạch';cancel.disabled=commandPending;cancel.addEventListener('click',()=>cancelPlan(target));panel.append(cancel);}panel.scrollTop=oldScroll;presentation.finish(panel);
}
function editTripPlan(plan) {
  const panel=document.querySelector('#trip-plan-card');if(!panel || panel.querySelector?.('form'))return;
  const form=document.createElement('form');form.className='destination-choice-input';
  const input=document.createElement('input');input.placeholder='Điểm đến mới…';input.setAttribute('aria-label','Điểm đến mới');input.maxLength=200;
  const button=document.createElement('button');button.textContent='Tính lại';button.type='submit';form.append(input,button);panel.append(form);input.focus?.();
  form.addEventListener('submit',event=>{event.preventDefault();if(!actionCurrent(planAction(plan,planOwner(plan))))return;if(input.value.trim().length>=2){const args={query:input.value.trim(),trip_kind:plan.trip_kind};if(planOwner(plan)==='trip')tripCommand('trip.plan',plan,args);else hooks.command?.('Chỉnh tuyến',{capability:'navigation.plan',arguments:args});}});
}
async function showTripMap(plan) {return showPlanMap(plan,'trip');}
async function showPlanMap(plan,owner,open=true) {
  const target=planAction(plan,owner);if(!actionCurrent(target))return;
  mapTripPlan=plan;mapAction=target;
  if(open)await openJourney();
  if(mapTripPlan!==plan || !actionCurrent(target) || !map)return;
  map.getSource('vivi-trip-preview')?.setData({type:'Feature',geometry:{type:'LineString',coordinates:plan.route.coordinates}});
  $('#journey-origin').textContent=plan.origin.label || 'Điểm xuất phát';$('#journey-search').value=plan.selected.spoken_label || plan.selected.name;
  $('#journey-summary').hidden=false;$('#journey-controls').hidden=false;$('#journey-duration').textContent=formatDuration(plan.energy?.selected_plan?.total_seconds || plan.route.duration_s);$('#journey-distance').textContent=formatDistance(plan.route.distance_m);$('#journey-arrival').textContent='Xem trước';
  $('#journey-start span').textContent='Bắt đầu hành trình';$('#journey-start').disabled=plan.calculation_status!=='ready';$('#journey-next').hidden=true;
  $('#journey-steps').innerHTML=plan.route.steps.map(step=>`<li><b>${SIGN_ICONS[step.sign] || '↑'}</b><span>${escapeHtml(step.text)}</span></li>`).join('');
  renderEnergy({...plan,alternatives:owner==='trip'?[]:plan.alternatives});setMessage('Xem trước kế hoạch · chưa bắt đầu hành trình');renderMapAction();
  if(typeof window!=='undefined' && window.vietmapgl){const bounds=plan.route.coordinates.reduce((box,point)=>box.extend(point),new vietmapgl.LngLatBounds(plan.route.coordinates[0],plan.route.coordinates[0]));map.fitBounds(bounds,{padding:fitPadding(),duration:500});}
}
function restoreNavigationView() {
  if(!mapTripPlan)return;mapTripPlan=null;mapAction=null;
  map?.getSource('vivi-trip-preview')?.setData(emptyLine());$('#journey-start').disabled=false;
  if(backendNavigation){$('#journey-origin').textContent=backendNavigation.origin?.label || 'Chưa xác định';$('#journey-search').value=backendNavigation.selected?.name || '';renderRoute();renderEnergy(backendNavigation);}
}

export async function applyNavigation(payload) {
  const incoming=payload.navigation;
  if(incoming && backendNavigation && (incoming.revision<backendNavigation.revision || incoming.revision===backendNavigation.revision && (incoming.event_sequence || 0)<(backendNavigation.event_sequence || 0)))return;
  if(payload.navigation_workspace){
    backendWorkspace=payload.navigation_workspace;
    if(Object.hasOwn(backendWorkspace,'draft'))choiceOwner=backendWorkspace.draft?.purpose==='itinerary'?'trip':'navigation';
  }
  if(payload.steps && !payload.navigation && !payload.trip_plan)planningSteps=payload.steps;
  if(Object.hasOwn(payload,'trip_plan') && payload.trip_plan===null){
    if(backendTripPlan)retiredTripPlans.add(backendTripPlan.id);
    backendTripPlan=null;
    if(mapAction?.owner==='trip'){mapTripPlan=null;mapAction=null;map?.getSource('vivi-trip-preview')?.setData(emptyLine());}
  }
  if(payload.trip_plan) {
    const plan=payload.trip_plan;
    if(retiredTripPlans.has(plan.id))return;
    const different=plan.id!==backendTripPlan?.id;
    if(!backendTripPlan || different && (plan.event_sequence || 0)>=(backendTripPlan.event_sequence || 0) || !different && plan.revision>backendTripPlan.revision || !different && plan.revision===backendTripPlan.revision && (plan.event_sequence || 0)>=(backendTripPlan.event_sequence || 0)) {
      if(plan.started_revision===plan.revision && (different || backendTripPlan?.started_revision!==plan.started_revision))rememberHiddenInformation(informationKey(plan,'Kế hoạch'));
      if(mapAction?.owner==='trip' && mapTripPlan && (mapTripPlan.id!==plan.id || mapTripPlan.revision!==plan.revision || plan.calculation_status==='cancelled')) {restoreNavigationView();setView(sim.running?'mini':'hidden');}
      if(different && payload.ui_events?.some(event=>event.type.startsWith('trip.'))) {journeyInformationHidden=false;rememberJourneyVisibility();}
      backendTripPlan=plan;
      if(payload.steps)planningSteps=payload.steps;
      if(payload.ui_events?.some(event=>event.type.startsWith('trip.')))choiceOwner='trip';
    }
  }
  if(!origin && payload.ui_events?.some(event=>['navigation.locate','trip.locate'].includes(event.type))) {
    config ||= await api('/api/v2/navigation/config');locate();
  }
  const next = payload.navigation;
  renderInformation();
  if (!next || (backendNavigation && next.revision < backendNavigation.revision)) return;
  if (backendNavigation && next.revision === backendNavigation.revision && (next.event_sequence || 0) < (backendNavigation.event_sequence || 0)) return;
  const previous = backendNavigation;
  backendNavigation = next;
  for (const event of payload.ui_events || []) {
    if (!['navigation.charging','navigation.next_leg'].includes(event.type) || !event.message ||
        event.route_id !== next.route_id || spokenChargingEvents.has(event.id)) continue;
    spokenChargingEvents.add(event.id);
    if (spokenChargingEvents.size > 100) spokenChargingEvents.delete(spokenChargingEvents.values().next().value);
    hooks.announce(event.message);
  }
  if(view==='full' && previous?.status==='selecting' && next.status==='preview')setJourneyDetails(false);
  if(next.request_id && next.request_id!==previous?.request_id && payload.ui_events?.some(e=>['navigation.results','navigation.preview'].includes(e.type))) {
    journeyInformationHidden=false;rememberJourneyVisibility();
  }

  if(payload.ui_events?.some(event=>event.type==='navigation.clear')) {
    if(backendTripPlan)retiredTripPlans.add(backendTripPlan.id);
    backendTripPlan=null;mapTripPlan=null;planningSteps=[];hiddenInformation.clear();
    document.querySelector('#trip-plan-card').hidden=true;
    document.querySelector('#destination-choices').hidden=true;
    map?.getSource('vivi-trip-preview')?.setData(emptyLine());
  }
  if(payload.steps)planningSteps=payload.steps;
  if(payload.ui_events?.some(event=>['navigation.results','navigation.preview'].includes(event.type)))choiceOwner='navigation';
  renderInformation();
  navigationGrant = next.pending?.navigation?.offer?.grant || next.offer?.grant || null;
  const relevant = payload.ui_events?.some(event => event.type.startsWith('navigation.') && event.type !== 'navigation.progress');
  if(relevant && next.status==='running')restoreNavigationView();
  if (next.origin && !origin) setOrigin(next.origin);
  if(next.route && ['running','charging'].includes(next.status) && !map && !activeMapLoad && mapAdapter.status==='idle') {
    activeMapLoad=prepareJourneyMap().then(()=>{
      if(map && backendNavigation?.route){
        map.getSource('vivi-route')?.setData({type:'Feature',geometry:{type:'LineString',coordinates:backendNavigation.route.coordinates}});
        setOrigin(backendNavigation.origin || origin);
        if(backendNavigation.selected)placeMarker('destination',[backendNavigation.selected.lng,backendNavigation.selected.lat],'destination');
        renderStationMarkers(backendNavigation);
      }
    }).finally(()=>{activeMapLoad=null;});
  }
  if (backendNavigation !== next) return; // A newer response won while the map loaded.
  if (next.origin && (!markers.origin || next.origin.lat !== origin?.lat || next.origin.lng !== origin?.lng)) setOrigin(next.origin);
  if ((next.status === 'selecting' || next.pending?.status === 'selecting') && relevant) closeResults();
  const nextRouteIdentity=next.route && JSON.stringify([next.route_id,payload.navigation_workspace?.journey?.revision,
    next.selected?.lat,next.selected?.lng,next.route.distance_m,next.route.duration_s,next.route.coordinates.length,
    ['running','paused','charging','completed','ended'].includes(next.status)]);
  if (next.selected && next.route && (nextRouteIdentity!==routeIdentity || !route)) {
    if(sim.running)stopSimulation(false,true);
    destination = next.selected;
    $('#journey-search').value = destination.name;
    placeMarker('destination', [destination.lng, destination.lat], 'destination');
    route=DriveSimulation.prepare(next.route,haversine);
    const cumulative=route.cumulative;
    routeIdentity=nextRouteIdentity;
    markRoundaboutExits();
    const junctions=route.steps.slice(1).filter(step=>![4,5].includes(step.sign)).map(step=>cumulative[Math.min(step.point_index,cumulative.length-1)]);
    hooks.route(['running','paused','charging'].includes(next.status)?{ coordinates: route.coordinates, cumulative, junctions }:null);
    networkKey='';lastNetworkScan=0;hooks.network?.([]);
    map?.getSource('vivi-traveled')?.setData(emptyLine());
    map?.getSource('vivi-route')?.setData({ type:'Feature', geometry:{ type:'LineString', coordinates:route.coordinates } });
    renderRoute();
    sim.traveled = next.progress_m / Math.max(1, route.distance_m) * route.length;
  }
  const energySignature=JSON.stringify([next.route_id,next.revision,next.status,next.energy?.status,next.energy?.selected_plan_id,next.stop_index]);
  if(!mapTripPlan && energySignature!==lastEnergySignature){lastEnergySignature=energySignature;renderEnergy(next);renderStationMarkers(next);}
  if (next.status === 'preview' && sim.running) stopSimulation(false,true);
  if(next.route && route && (next.status==='paused' || !sim.running))sim.traveled=next.progress_m / Math.max(1,route.distance_m) * route.length;
  else if(next.status==='running' && route)sim.traveled=Math.max(sim.traveled,next.progress_m / Math.max(1,route.distance_m) * route.length);
  if (next.status==='running' && !motionBlocked && !sim.running && route) { progressAt = performance.now(); startSimulation(true); }
  if(previous?.pending && !next.pending && sim.running && relevant)setView(journeyInformationHidden?'hidden':'mini');
  if(next.status!=='charging' && chargingTimer){clearInterval(chargingTimer);chargingTimer=null;}
  if(next.status==='charging' && !chargingTimer) {
    progressAt=performance.now();
    chargingTimer=setInterval(()=>{
      const now=performance.now();
      if(backendNavigation?.status==='charging' && !progressPending)void sendProgress({kind:'charge',elapsed_s:Math.min(30,(now-progressAt)/1000*sim.speedup)},now);
    },1000);
  }
  if (next.status === 'charging' && route) {
    if(sim.running)stopSimulation(false,true);
    sim.traveled = next.progress_m / Math.max(1, route.distance_m) * route.length;
    const leg = currentLeg();
    setMessage(`${leg.destination}. Dự kiến còn ${leg.chargingMinutes} phút sạc rồi đi tiếp.`);
    hooks.progress({speedKmh:0,next:null,heading:sim.bearing,...leg});
  }
  if (next.status === 'completed' && previous?.status !== 'completed' && sim.running) finishSimulation();
  else if (['paused','idle','ended'].includes(next.status) && sim.running) stopSimulation(false,true);
  if(next.pending?.navigation?.route && map && relevant) {
    map.getSource('vivi-pending-preview')?.setData({type:'Feature',geometry:{type:'LineString',coordinates:next.pending.navigation.route.coordinates}});
    if(next.pending.message)setMessage(next.pending.message);
  }
  if(!next.pending?.navigation?.route)map?.getSource('vivi-pending-preview')?.setData(emptyLine());
  if(['completed','ended'].includes(next.status)){hooks.route(null);hooks.network?.([]);}
  if(!next.route && (route || routeIdentity)) {
    // A new search, a cancelled draft or an ended trip leaves no route: drop
    // the previous line, markers and energy view instead of keeping them.
    if(sim.running)stopSimulation(false,true);
    route=null;routeIdentity=null;destination=null;sim.traveled=0;
    removeMarker('car');removeMarker('destination');
    map?.getSource('vivi-route')?.setData(emptyLine());
    map?.getSource('vivi-traveled')?.setData(emptyLine());
    hooks.route(null);hooks.network?.([]);
    if(!['selecting','preview'].includes(next.status))$('#journey-search').value='';
  }
  if(next.status==='idle' && !next.route) {
    navigationGrant=null;
    // Hide once when a trip or plan ends; an idle refresh must not close a
    // map the driver opened afterwards.
    if(!mapTripPlan){renderRoute();if(previous && previous.status!=='idle')setView('hidden');}
  }
  if(['paused','ended'].includes(next.status) && previous?.status!==next.status || journeyInformationHidden)setView('hidden');
  if(mapAction && !actionCurrent(mapAction)){restoreNavigationView();}
  renderMapAction();
  renderJourneyHistory();
}

function renderJourneyHistory() {
  const panel=document.querySelector('#journey-history');if(!panel)return;
  panel.innerHTML='';const history=backendWorkspace?.history || [];panel.hidden=!history.length;
  if(!history.length)return;
  const title=document.createElement('summary');title.textContent='Lịch sử hành trình trong phiên';panel.append(title);
  for(const item of [...history].reverse()) {const row=document.createElement('p');row.textContent=`${item.destination?.spoken_label || item.destination?.name || 'Hành trình'} · ${formatDistance(item.distance_m)} · ${item.status==='completed'?'Đã đến':'Đã kết thúc'}`;panel.append(row);}
  const clear=document.createElement('button');clear.textContent='Xoá lịch sử';clear.addEventListener('click',()=>hooks.event?.({kind:'history_clear',id:crypto.randomUUID(),revision:backendNavigation?.revision || 0}));panel.append(clear);
}

export function getJourneyPosition() {
  if (backendNavigation?.status === 'completed' && backendNavigation.route?.coordinates?.length) {
    const point = backendNavigation.route.coordinates.at(-1);
    return {lat:point[1],lng:point[0],label:'Vị trí sau hành trình',simulated:true};
  }
  if ((sim.running || ['paused','charging'].includes(backendNavigation?.status)) && route) {
    const {point} = positionAt(sim.traveled);
    return {lat:point[1],lng:point[0],label:'Vị trí mô phỏng hiện tại',simulated:true};
  }
  return origin ? {...origin,simulated:origin.simulated ?? true} : null;
}

function renderEnergy(next) {
  const element = document.querySelector('#journey-energy');
  if (!element) return;
  const energy = next.energy;
  element.hidden = !energy;
  if (!energy) return;
  const plan = energy.selected_plan;
  const summary = next.summary;
  const stops = summary?.stops || plan?.stops || [];
  element.innerHTML = plan ? `<strong>${stops.length ? `${stops.length} điểm dừng sạc` : 'Không cần dừng sạc'} · khoảng ${formatDuration(plan.total_seconds)}</strong><p>Pin dự kiến khi đến: ${Math.floor(plan.arrival_soc)}%</p>${stops.map(stop => `<div class="route-stop"><strong>${escapeHtml(stop.label || stop.name)}</strong><p>${next.trip_kind==='round_trip'?(stop.phase===1?'Chiều về · ':'Chiều đi · '):''}Khoảng ${stop.minutes ?? Math.ceil(stop.charge_minutes)} phút · pin ${Math.floor(stop.arrival_soc)}% → ${Math.floor(stop.departure_soc)}%</p>${stop.road_context ? `<small>Gần ${escapeHtml(stop.road_context)}</small>` : ''}</div>`).join('')}${(next.status==='preview'?next.alternatives || []:[]).map(item => `<button type="button" data-plan="${escapeHtml(item.id)}">${{fewest_stops:'Ít dừng',fastest:'Nhanh nhất',higher_reserve:'Dự phòng cao'}[item.strategy]} · ${formatDuration(item.total_seconds)}</button>`).join('')}` : `<p>${escapeHtml(energy.message || 'Chưa có kế hoạch sạc khả thi.')}</p>`;
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
  const energy = next.energy || {};
  const stations = energy.selected_plan?.stops || energy.station_candidates || [];
  const ids = new Set(stations.map(station => `charging:${station.id || station.station_id}`));
  for (const id of Object.keys(markers)) if (id.startsWith('charging:') && !ids.has(id)) removeMarker(id);
  for (const station of stations) placeMarker(`charging:${station.id || station.station_id}`, [station.lng, station.lat], 'charging');
}
