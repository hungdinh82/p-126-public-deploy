import { closeJourney, initJourney, openJourney, applyNavigation, getJourneyPosition, setJourneyMotionBlocked, resetJourneyRuntime } from './journey.js?v=23';
import { GearController } from './navigation-state.js?v=2';
import { NavigationStore, MissingRouteError } from './navigation-store.js?v=1';
import { createCabinMusic } from './music.js?v=2';
import { neonBorder } from './neon.js?v=2';
import { createWave } from './wave.js?v=2';
const cabinMusic = createCabinMusic();

const $ = (selector) => document.querySelector(selector);
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
// The FastAPI backend serves this UI, so API calls follow the configured
// VIVI_PORT automatically instead of being tied to a hard-coded port.
const API_BASE = '';
const makeId = () => globalThis.crypto?.randomUUID?.() || `vivi-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const sessionId = localStorage.getItem('vivi-session-id') || makeId();
localStorage.setItem('vivi-session-id', sessionId);
const state = { phase: 'idle', temp: 23, window: false, music: false, driving: false, powerState: 'off', battery: 82, range: 328, powertrainTemp: 45, tirePressures: {}, windows: {}, doors: {}, seatHeatLevels: {}, doorLocked: false, doorOpen: false, hoodOpen: false, trunkOpen: false, seatHeat: 0, alerts: [], alertSequence: 0, reduced: motionPreference.matches, sound: true, busy: false, backendAvailable: false, vehicleProvider: '', sttAvailable: false, sttProvider: '', sttStreaming: false, sttDevice: '', ttsAvailable: false, ttsProvider: '', ttsVoice: '', ttsDevice: '', ttsOptions: [], storeAudio: false, storeTranscripts: false, llmProvider: '', llmOptions: [], lastCommand: '', manualAnswer: '', manualEvidence: [], progress: 0 };
const navigationStore = new NavigationStore();
const snapshotOrder = navigationStore.order;
let syncRequest = null;
const gearController = new GearController({
  send: event=>sendNavigationEvent({...event,revision:currentNavigationRevision}),
  synchronize: ()=>synchronizeNavigation(),
  changed: gear=>{
    setJourneyMotionBlocked(gear.frozen);
    updateDriveUI();
    if(gear.status==='unknown')setPhase('blocked','Chưa đồng bộ trạng thái xe. Bấm số để đồng bộ lại.');
  },
  id:makeId
});
const edgeCommandHistoryKey = 'vivi-command-history';
function readEdgeCommandHistory() {
  try {
    const history = JSON.parse(localStorage.getItem(edgeCommandHistoryKey) || '[]');
    return Array.isArray(history) ? history.slice(0, 12) : [];
  } catch {
    return [];
  }
}
let edgeCommandHistory = readEdgeCommandHistory();
function rememberEdgeCommand(command) {
  if (!state.storeTranscripts) return;
  edgeCommandHistory = readEdgeCommandHistory();
  edgeCommandHistory.unshift({ text: command.trim(), at: Date.now() });
  edgeCommandHistory = edgeCommandHistory.slice(0, 12);
  try { localStorage.setItem(edgeCommandHistoryKey, JSON.stringify(edgeCommandHistory)); } catch {}
}
let activePanelView = '';
const seenAlertIds = new Set();
const alertLastSpoken = new Map();
const ALERT_TTS_COOLDOWN_MS = 300000;
const MAX_SEEN_ALERT_IDS = 1024;
const phaseLabels = { idle: 'ViVi đang ở đây', listening: 'Mình đang nghe bạn', transcribing: 'Mình đang nhận diện lời nói', thinking: 'Để mình xem nhé', validating: 'Đang kiểm tra an toàn', acting: 'Đang chăm sóc không gian của bạn', synthesizing: 'Đang chuẩn bị giọng Mai Chi', speaking: 'Một chút dễ chịu, dành cho bạn', confirm: 'Mình chờ bạn xác nhận', clarify: 'Mình chờ bạn nói thêm', blocked: 'Mình giữ nguyên trạng thái xe' };
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
let plannedRoute = null;    // route shape for the 3D road, kept until the scene loads
let nav = null;             // live journey progress while a route simulation runs
let cruise = { speed: 0, at: 0 };
let scene = null;           // 3D view; stays null when WebGL or three.js is unavailable

// Voice levels feed the wave: microphone RMS while listening, ViVi's own
// audio while speaking.
const voiceLevel = { mic: 0, analyser: null, samples: null };
function ttsLevel() {
  if (!voiceLevel.analyser) return .35 + Math.sin(performance.now() / 140) * .25; // browser speech has no audio tap
  voiceLevel.analyser.getFloatTimeDomainData(voiceLevel.samples);
  let sum = 0;
  for (const sample of voiceLevel.samples) sum += sample * sample;
  return Math.min(1, Math.sqrt(sum / voiceLevel.samples.length) * 5);
}
const wave = createWave($('#wave'), {
  level: phase => phase === 'listening' ? Math.min(1, voiceLevel.mic * 7) : ttsLevel(),
  onLevel: level => scene?.setVoiceLevel(level)
});
const capsuleNeon = neonBorder($('#demo-mic'), { speed: 2, borderSize: 30, glow: 35, thickness: 1.2 });
const maneuverNeon = neonBorder($('#maneuver'), { speed: 4, borderSize: 40, glow: 30, thickness: 1.2 });
const NEON_BY_PHASE = {
  idle: { speed: 2, borderSize: 30, color: '#6E7894' },
  listening: { speed: 12, borderSize: 64, color: '#D6DEEE' },
  thinking: { speed: 14, borderSize: 44, color: '#9F94C6', movement: 'step' },
  speaking: { speed: 8, borderSize: 56, color: '#C3CDE2' },
  blocked: { speed: 4, borderSize: 50, color: '#D6AE78' }
};

import('./scene3d.js?v=43')
  .then(module => {
    scene = module.createScene($('#space'), { reduced: state.reduced });
    scene.setRoute(plannedRoute);
    scene.onBlink(onBlink);
    document.body.classList.add('space-ready');
    updateDriveUI();
    updateVehicle();
  })
  .catch(error => console.warn('3D scene unavailable:', error));

// Live caption: one span per word so only new or revised words animate in.
// Words still being recognised (interim) stay dim until the STT confirms them.
const LIVE_CAPTION_WORDS = 22;
let liveWords = [];
function setPhase(phase, text) {
  state.phase = phase;
  cabinMusic.duck(phase);
  $('#orb-state').textContent = phaseLabels[phase] || phaseLabels.idle;
  wave.setPhase(phase);
  const thinking = ['thinking', 'transcribing', 'validating', 'acting', 'synthesizing'].includes(phase);
  const neon = NEON_BY_PHASE[thinking ? 'thinking' : phase] || (['clarify', 'unverified'].includes(phase) ? NEON_BY_PHASE.blocked : NEON_BY_PHASE.idle);
  capsuleNeon.set({ movement: 'continuous', ...neon });
  document.body.dataset.phase = phase;
  if (text) setResponseText(text);
  document.body.classList.toggle('busy', !['idle', 'confirm', 'clarify', 'blocked', 'unverified'].includes(phase));
}
function setResponseText(text) {
  liveWords = [];
  $('#response-text').classList.remove('live');
  $('#response-text').textContent = text;
}
function renderLiveCaption(final, interim) {
  const caption = $('#response-text');
  if (!caption.classList.contains('live')) { caption.textContent = ''; caption.classList.add('live'); liveWords = []; }
  // STT tokens are sub-word pieces, so a word can straddle the final/interim boundary.
  const words = [...(final + interim).matchAll(/\S+/g)].map(match => ({ text: match[0], final: match.index + match[0].length <= final.length }));
  words.forEach((word, index) => {
    let span = liveWords[index];
    if (span?.textContent !== word.text) {
      const fresh = document.createElement('span');
      fresh.textContent = word.text;
      if (span) span.replaceWith(fresh); else caption.append(fresh);
      liveWords[index] = span = fresh;
    }
    span.classList.toggle('interim', !word.final);
    span.hidden = index < words.length - LIVE_CAPTION_WORDS;
  });
  liveWords.splice(words.length).forEach(span => span.remove());
}
function updateVehicle() {
  $('#temperature').textContent = `${state.temp}°`;
  $('#dock-temp').textContent = `${state.temp}°`;
  updateWindowPanel();
  // Backend reports the opening in percent; the offline demo only knows open or closed.
  scene?.setWindow(state.window ? (state.windows.driver > 0 ? state.windows.driver : 100) : 0);
  updateBodyPanel();
  scene?.setDoors(Object.fromEntries(CABIN_ZONES.map(zone => [zone, Boolean(state.doors[zone]?.open)])));
  scene?.setHood(state.hoodOpen);
  scene?.setTrunk(state.trunkOpen);
  $('#music-toggle').setAttribute('aria-pressed', String(state.music));
  if (state.music) cabinMusic.play().catch(() => {}); else cabinMusic.stop();
  $('#music-toggle').setAttribute('aria-label', state.music ? 'Dừng nhạc' : 'Phát nhạc');
  updateHud();
}
// The four side windows share one popover, sending the spoken command per window.
const WINDOWS = {
  driver: { label: 'Bên tài', noun: 'cửa sổ bên tài' },
  front_passenger: { label: 'Bên phụ', noun: 'cửa sổ bên phụ' },
  rear_left: { label: 'Sau trái', noun: 'cửa sổ sau trái' },
  rear_right: { label: 'Sau phải', noun: 'cửa sổ sau phải' }
};
const windowPercent = zone => zone === 'driver' && !state.windows.driver ? (state.window ? 100 : 0) : Number(state.windows[zone]) || 0;
function updateWindowPanel() {
  const opened = [];
  for (const [zone, pane] of Object.entries(WINDOWS)) {
    const percent = windowPercent(zone), open = percent > 0;
    const chip = $(`#window-pop [data-window="${zone}"]`);
    chip.setAttribute('aria-pressed', String(open));
    chip.setAttribute('aria-label', `${open ? 'Đóng' : 'Mở'} ${pane.noun}`);
    chip.querySelector('small').textContent = !open ? 'Đã đóng' : percent < 100 ? `Mở ${percent}%` : 'Đang mở';
    $(`#window-car [data-pane="${zone}"]`).classList.toggle('open', open);
    if (open) opened.push(pane.label);
  }
  $('#window-summary').textContent = opened.length ? `Đang mở · ${opened.join(', ')}` : 'Tất cả đã đóng';
  $('#window-button').classList.toggle('attention', opened.length > 0);
}
// Doors, hood and tailgate share one popover; each chip sends the same spoken
// command a driver would say, so policy and confirmation stay on the backend.
const CABIN_ZONES = ['driver', 'front_passenger', 'rear_left', 'rear_right'];
const BODY_PANELS = {
  hood: { label: 'Capo', noun: 'nắp capo' },
  driver: { label: 'Cửa tài', noun: 'cửa bên tài' },
  front_passenger: { label: 'Cửa phụ', noun: 'cửa bên phụ' },
  rear_left: { label: 'Cửa sau trái', noun: 'cửa sau trái' },
  rear_right: { label: 'Cửa sau phải', noun: 'cửa sau phải' },
  trunk: { label: 'Cốp', noun: 'cốp sau' }
};
function bodyPanelState(id) {
  if (id === 'hood') return { open: state.hoodOpen, locked: false };
  if (id === 'trunk') return { open: state.trunkOpen, locked: false };
  return { open: Boolean(state.doors[id]?.open), locked: Boolean(state.doors[id]?.locked) };
}
function updateBodyPanel() {
  const opened = [];
  for (const [id, panel] of Object.entries(BODY_PANELS)) {
    const { open, locked } = bodyPanelState(id);
    const chip = $(`#body-pop [data-panel="${id}"]`);
    chip.setAttribute('aria-pressed', String(open));
    chip.setAttribute('aria-label', `${open ? 'Đóng' : 'Mở'} ${panel.noun}`);
    chip.querySelector('small').textContent = open ? 'Đang mở' : locked ? 'Đã khoá' : 'Đã đóng';
    $(`#body-car [data-part="${id}"]`).classList.toggle('open', open);
    if (open) opened.push(panel.label);
  }
  const locked = CABIN_ZONES.every(zone => state.doors[zone]?.locked);
  $('#body-car').classList.toggle('locked', locked);
  $('#lock-all').setAttribute('aria-pressed', String(locked));
  $('#lock-all span').textContent = locked ? 'Mở khoá tất cả cửa' : 'Khoá tất cả cửa';
  $('#open-all span').textContent = opened.length ? 'Đóng tất cả' : 'Mở tất cả';
  $('#open-all').setAttribute('aria-label', opened.length ? 'Đóng tất cả cửa, capo và cốp' : 'Mở tất cả cửa, capo và cốp');
  $('#body-summary').textContent = opened.length ? `Đang mở · ${opened.join(', ')}` : locked ? 'Đã đóng và khoá' : 'Tất cả đã đóng';
  $('#body-button').classList.toggle('attention', opened.length > 0);
}
const formatKm = (meters) => meters < 1000 ? `${Math.round(meters / 10) * 10} m` : `${(meters / 1000).toLocaleString('vi-VN', { maximumFractionDigits: 1 })} km`;
// Left HUD: battery while parked, speed while driving; nothing else competes for attention.
function updateHud() {
  const driving = state.driving && !gearController.frozen;
  const speed = driving ? Math.round(nav?.speedKmh ?? cruise.speed) : 0;
  $('#gear-letter').textContent = state.driving ? 'D' : 'P';
  $('#mode-label').textContent = gearController.status!=='idle' ? (gearController.status==='unknown'?'CHƯA ĐỒNG BỘ':gearController.status==='syncing'?'ĐANG ĐỒNG BỘ':`ĐANG CHUYỂN ${gearController.desired}`) : driving ? (nav ? `ĐẾN ${nav.destination.toUpperCase()}` : 'ĐANG DI CHUYỂN') : 'ĐỖ XE';
  $('#hud-primary').innerHTML = driving ? `${speed}<small>km/h</small>` : `${state.battery}<small>%</small>`;
  $('#hud-primary-label').textContent = driving ? 'TỐC ĐỘ' : 'PIN';
  $('#hud-range').textContent = driving ? `Pin ${state.battery}% · ${state.range} km` : `${state.range} km`;
  $('#hud-cabin').textContent = `Cabin ${state.temp}°`;
  const card = $('#maneuver');
  const next = driving && nav?.next;
  card.hidden = !next;
  if (next) {
    $('#maneuver-icon').textContent = next.icon;
    $('#maneuver-distance').textContent = `Sau ${formatKm(next.distance)}`;
    $('#maneuver-text').textContent = next.text;
    $('#maneuver-eta').textContent = `Còn ${formatKm(nav.remaining)} · đến ${new Intl.DateTimeFormat('vi-VN', { hour: '2-digit', minute: '2-digit' }).format(nav.arrival)}`;
  }
  scene?.setSpeed(speed);
  // VietMap signs: negative turns left, positive right; only near turns get a cue.
  const turning = next && next.distance < 220 && next.sign !== 4 && next.sign !== 5;
  scene?.setTurn(turning ? Math.sign(next.sign) : null);
  scene?.setIndicator(driving ? signalFor(nav) : 0);
}
const SIGNAL_DISTANCE = 70;   // metres before a turn
const EXIT_SIGNAL = 25;       // metres before leaving a roundabout
// Signal shortly before a real turn; not for straight on, arrival or via
// points. Roundabouts (sign 6) run anticlockwise: an exit to the right is
// signalled right all the way; going straight on only signals right at the
// exit; further round signals left until the exit, then right.
const exitSide = turn => turn > 30 ? 1 : turn < -30 ? -1 : 0;
function signalFor(progress) {
  const next = progress?.next;
  if (progress?.roundabout != null) return next && next.distance < EXIT_SIGNAL ? 1 : exitSide(progress.roundabout);
  if (!next || next.distance >= SIGNAL_DISTANCE) return 0;
  if (next.sign === 6) return exitSide(next.exitTurn ?? 0);
  return [0, 4, 5].includes(Math.abs(next.sign)) ? 0 : Math.sign(next.sign);
}

// Blinker lamp on the HUD and the relay tick, both driven by the scene's blink.
let tickContext = null;
function blinkerTick(on) {
  if (!state.sound || state.reduced) return;
  try {
    tickContext ??= new AudioContext();
    if (tickContext.state === 'suspended') tickContext.resume();
    const now = tickContext.currentTime, click = tickContext.createOscillator(), gain = tickContext.createGain();
    click.type = 'square';
    click.frequency.value = on ? 1900 : 1300;
    gain.gain.setValueAtTime(.05, now);
    gain.gain.exponentialRampToValueAtTime(.0001, now + .025);
    click.connect(gain).connect(tickContext.destination);
    click.start(now);
    click.stop(now + .03);
  } catch { /* audio unavailable */ }
}
function onBlink(side) {
  document.querySelectorAll('.blinkers [data-blink]').forEach(arrow => arrow.classList.toggle('on', Number(arrow.dataset.blink) === side));
  blinkerTick(side !== 0);
}
function updateDriveUI() {
  $('#drive-toggle').setAttribute('aria-pressed', String(state.driving));
  $('#drive-toggle').setAttribute('aria-label', state.driving ? 'Chuyển về đỗ xe' : 'Chuyển sang lái xe');
  const shifting=gearController.status!=='idle';
  const label=gearController.status==='unknown'?'Chưa đồng bộ':gearController.status==='syncing'?'Đang đồng bộ':shifting?`Đang chuyển ${gearController.desired}`:'';
  $('#drive-toggle span').textContent=shifting?`${gearController.desired || (state.driving?'D':'P')}…`:state.driving?'D':'P';
  $('#drive-toggle').title=label || (state.driving?'Chuyển về P':'Chuyển sang D');
  $('#drive-toggle').setAttribute('aria-busy',String(shifting));
  $('#drive-toggle').classList.toggle('shifting',shifting);
  if(state.backendAvailable) {
    $('#drive-toggle').disabled=!state.navigationSynced || state.vehicleProvider!=='memory'
      || state.busy && !state.driving && gearController.desired!=='D'
      || shifting && gearController.desired==='P' && !state.driving && gearController.status!=='unknown';
  }
  document.body.classList.toggle('driving',state.driving && !gearController.frozen);
  scene?.setMode(state.driving && !gearController.frozen?'driving':'parked');
  if(gearController.frozen)scene?.setSpeed(0);
  updateHud();
}
// Without a route the demo cruises at a gently varying city speed.
setInterval(() => {
  if (!state.driving || nav) { cruise.speed = 0; return; }
  cruise.at += .5;
  cruise.speed = 38 + Math.sin(cruise.at / 4) * 6 + Math.sin(cruise.at / 1.7) * 2;
  updateHud();
}, 500);
const alertCodeLabels = {
  LOW_BATTERY: 'Pin yếu',
  BATTERY_CRITICAL: 'Pin rất thấp',
  ROUTE_ENERGY_LOW: 'Pin không đủ cho tuyến',
  TIRE_PRESSURE_LOW: 'Áp suất lốp thấp',
  TIRE_PRESSURE_HIGH: 'Áp suất lốp cao',
  POWERTRAIN_OVERHEAT: 'Hệ truyền động quá nhiệt',
  DOOR_OPEN_WHEN_READY: 'Cửa đang mở',
  HOOD_OPEN_WHEN_READY: 'Capo đang mở',
  TRUNK_OPEN_WHEN_READY: 'Cốp đang mở'
};
const alertSourceLabels = {
  battery: 'pin', powertrain: 'hệ truyền động', hood: 'capo', trunk: 'cốp sau', driver: 'bên tài', front_passenger: 'bên phụ',
  front_left: 'trước trái', front_right: 'trước phải', rear_left: 'sau trái', rear_right: 'sau phải'
};
function renderAlerts(snapshot) {
  state.alerts = snapshot.active_alerts || [];
  state.alertSequence = snapshot.event_sequence || 0;
  const panel = $('#alert-panel');
  panel.hidden = state.alerts.length === 0;
  $('#alert-count').textContent = state.alerts.length ? `${state.alerts.length} đang hoạt động` : '';
  $('#alert-list').innerHTML = state.alerts.map(alert => `<article class="vehicle-alert ${alert.severity}"><i></i><div><strong>${escapeHtml(alert.message)}</strong><span>${escapeHtml(alertCodeLabels[alert.code] || alert.code)} · ${escapeHtml(alertSourceLabels[alert.source] || alert.source)}</span></div></article>`).join('');
}
function announceNewAlerts(alerts) {
  for (const alert of alerts) {
    if (alert.announce === false) continue;
    if (seenAlertIds.has(alert.alert_id)) {
      seenAlertIds.delete(alert.alert_id);
      seenAlertIds.add(alert.alert_id);
      continue;
    }
    seenAlertIds.add(alert.alert_id);
    while (seenAlertIds.size > MAX_SEEN_ALERT_IDS) seenAlertIds.delete(seenAlertIds.values().next().value);
    const key = `${alert.code}:${alert.source}`;
    const now = Date.now();
    const inCooldown = now - (alertLastSpoken.get(key) || 0) < ALERT_TTS_COOLDOWN_MS;
    if (!state.sound || (alert.severity !== 'critical' && inCooldown)) continue;
    alertLastSpoken.set(key, now);
    queueAnnouncement(alert.message, alert.severity === 'critical' ? 2 : 1, `alert-${alert.alert_id}`);
  }
}
const announcementQueue = [];
let announcementRunning = false;
function queueAnnouncement(text, priority = 0, id = makeId()) {
  if (!state.sound) return;
  announcementQueue.push({text,priority,id});
  announcementQueue.sort((a,b)=>b.priority-a.priority);
  if(priority === 2) { state.audioEpoch=(state.audioEpoch || 0)+1; stopPlayback(); if('speechSynthesis' in window) speechSynthesis.cancel(); }
  void drainAnnouncements();
}
async function drainAnnouncements() {
  if(announcementRunning || !announcementQueue.length) return;
  if((state.busy || currentPlayback) && announcementQueue[0].priority < 2) {
    setTimeout(()=>void drainAnnouncements(),200); return;
  }
  announcementRunning=true;
  try {
    const item=announcementQueue.shift();
    if(state.sound) await speak(item.text,item.id,null,true).catch(()=>say(item.text));
  } finally { announcementRunning=false; void drainAnnouncements(); }
}
async function pollVehicleAlerts() {
  if (!state.backendAvailable) return;
  try {
    const response = await fetch(`${API_BASE}/api/v1/vehicle/alerts?session_id=${encodeURIComponent(sessionId)}`);
    if (!response.ok) return;
    const snapshot = await response.json();
    await synchronizeNavigation();
    renderAlerts(snapshot);
    announceNewAlerts(snapshot.active_alerts || []);
  } catch { /* Polling resumes automatically after a temporary disconnect. */ }
}
function highlight(id) { $(id).classList.add('highlight'); setTimeout(() => $(id).classList.remove('highlight'), 2600); }
const DOCK_BY_INTENT = { climate: '#climate-button', window: '#window-button', door: '#body-button', hood: '#body-button', trunk: '#body-button', body: '#body-button', media: '#music-toggle' };
function say(text) {
  if (!state.sound || !('speechSynthesis' in window)) return;
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text); utterance.lang = 'vi-VN'; utterance.rate = .93;
  const voice = speechSynthesis.getVoices().find(voice => voice.lang.startsWith('vi'));
  if (voice) utterance.voice = voice;
  speechSynthesis.speak(utterance);
}
let ttsAudioContext = null;
let currentPlayback = null;
const TTS_START_BUFFER_SECONDS = 1;
let ttsOutput = null;
async function ensureTtsAudioContext() {
  if (!ttsAudioContext) {
    ttsAudioContext = new AudioContext();
    // All TTS chunks pass through one analyser so the wave can follow ViVi's voice.
    ttsOutput = ttsAudioContext.createAnalyser();
    ttsOutput.fftSize = 512;
    ttsOutput.connect(ttsAudioContext.destination);
    voiceLevel.analyser = ttsOutput;
    voiceLevel.samples = new Float32Array(ttsOutput.fftSize);
  }
  if (ttsAudioContext.state === 'suspended') await ttsAudioContext.resume();
  return ttsAudioContext;
}
function stopPlayback() {
  if (!currentPlayback) return;
  const playback = currentPlayback;
  currentPlayback = null;
  playback.cancelled = true;
  playback.controller.abort();
  for (const source of playback.sources) { try { source.stop(); } catch { /* already stopped */ } }
  playback.sources.clear();
  playback.finish();
}
function createTtsPlayback() {
  const playback = { controller: new AbortController(), sources: new Set(), cancelled: false, streamDone: false, played: false, nextStart: 0 };
  playback.done = new Promise(resolve => { playback.finish = resolve; });
  return playback;
}
async function finishTtsPlayback(playback) {
  if (!playback) return;
  playback.streamDone = true;
  if (!playback.sources.size) playback.finish();
  await playback.done;
  if (currentPlayback === playback) currentPlayback = null;
}
async function speak(text, turnId = makeId(), sequencePlayback = null, announcement = false) {
  if (!state.sound) return;
  if (!announcement) while (announcementRunning) await wait(50);
  if (sequencePlayback?.cancelled) return;
  if (!sequencePlayback) {
    stopPlayback();
    if ('speechSynthesis' in window) speechSynthesis.cancel();
  }
  if (state.ttsAvailable) {
    const playback = sequencePlayback || createTtsPlayback();
    currentPlayback = playback;
    try {
      setPhase('synthesizing', text);
      const context = await ensureTtsAudioContext();
      const response = await fetch(`${API_BASE}/api/v1/tts/stream`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: playback.controller.signal,
        body: JSON.stringify({ text, session_id: sessionId, turn_id: turnId, tts_provider: state.ttsProvider || undefined })
      });
      if (!response.ok) throw new Error((await response.json()).detail || 'TTS lỗi');
      if (!response.body || response.headers.get('X-ViVi-Audio-Format') !== 'pcm_s16le') throw new Error('Định dạng TTS streaming không hợp lệ');
      const sampleRate = Number(response.headers.get('X-ViVi-Sample-Rate'));
      if (!Number.isFinite(sampleRate) || sampleRate <= 0) throw new Error('Tần số mẫu TTS không hợp lệ');
      const reader = response.body.getReader();
      let leftover = new Uint8Array(0);
      let startupChunks = [];
      let startupSeconds = 0;
      const schedule = (samples) => {
        const buffer = context.createBuffer(1, samples.length, sampleRate);
        buffer.copyToChannel(samples, 0);
        const source = context.createBufferSource();
        source.buffer = buffer; source.connect(ttsOutput);
        source.onended = () => {
          playback.sources.delete(source);
          source.disconnect();
          if (playback.streamDone && !playback.sources.size) playback.finish();
        };
        playback.sources.add(source);
        if (!playback.played) {
          playback.nextStart = context.currentTime + .06;
          playback.played = true;
          setPhase('speaking', text);
        }
        // Keep one continuous audio timeline; only recover to "now" after an actual underrun.
        if (playback.nextStart < context.currentTime) playback.nextStart = context.currentTime + .01;
        source.start(playback.nextStart);
        playback.nextStart += buffer.duration;
      };
      const startBufferedPlayback = () => {
        if (playback.cancelled || !startupChunks.length) return;
        for (const samples of startupChunks) schedule(samples);
        startupChunks = [];
      };
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done || playback.cancelled) break;
          const bytes = new Uint8Array(leftover.length + value.length);
          bytes.set(leftover); bytes.set(value, leftover.length);
          const usable = bytes.length - bytes.length % 2;
          leftover = bytes.slice(usable);
          if (!usable) continue;
          const samples = new Float32Array(usable / 2);
          const pcm = new DataView(bytes.buffer, bytes.byteOffset, usable);
          for (let i = 0; i < samples.length; i++) samples[i] = pcm.getInt16(i * 2, true) / 32768;
          if (playback.played) schedule(samples);
          else {
            startupChunks.push(samples);
            startupSeconds += samples.length / sampleRate;
            if (startupSeconds >= TTS_START_BUFFER_SECONDS) startBufferedPlayback();
          }
        }
      } finally { reader.releaseLock(); }
      if (leftover.length && !playback.cancelled) throw new Error('Luồng PCM TTS bị thiếu byte cuối');
      // Short replies may finish before reaching the startup buffer target.
      startBufferedPlayback();
      if (!sequencePlayback) await finishTtsPlayback(playback);
      return;
    } catch (error) {
      if (playback.cancelled) return;
      console.warn('ZeroTTS streaming:', error);
      if (currentPlayback === playback) stopPlayback();
      if (playback.played) return;
    }
  }
  say(text);
}
function normalize(text) { return text.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/đ/g, 'd'); }

function lockControls(locked) {
  document.querySelectorAll('#climate-pop button, #body-pop button, #window-pop button, #music-toggle, #drive-toggle, .send-button').forEach(button => { button.disabled = locked; });
  // The mic stays enabled while recording so a second tap can stop it.
  $('#demo-mic').disabled = !state.backendAvailable || !state.sttAvailable;
  if (state.backendAvailable && (!state.navigationSynced || state.vehicleProvider !== 'memory')) $('#drive-toggle').disabled = true;
  else if(state.driving || gearController.desired==='D')$('#drive-toggle').disabled=false; // P remains available while ViVi is answering.
  $('#command-form').setAttribute('aria-busy', String(locked));
}
function applyBackendState(vehicle) {
  if (!vehicle) return;
  if (vehicle.state_version != null && state.vehicleVersion != null && vehicle.state_version < state.vehicleVersion) return;
  if (vehicle.state_version != null) state.vehicleVersion=vehicle.state_version;
  state.temp = vehicle.temperature_celsius;
  state.window = vehicle.window_driver_percent > 0;
  state.music = vehicle.media_playing;
  state.driving = vehicle.driving;
  state.powerState = vehicle.power_state || (vehicle.driving ? 'driving' : 'off');
  state.battery = vehicle.battery_percent;
  state.range = vehicle.range_km;
  state.powertrainTemp = vehicle.powertrain_temperature_celsius ?? 45;
  state.tirePressures = vehicle.tire_pressures_kpa || {};
  state.windows = vehicle.window_positions || { driver: vehicle.window_driver_percent };
  state.doors = vehicle.door_states || { driver: { open: vehicle.door_driver_open, locked: vehicle.door_driver_locked } };
  state.seatHeatLevels = vehicle.seat_heat_levels || { driver: vehicle.seat_driver_heat_level };
  state.doorLocked = state.doors.driver?.locked ?? vehicle.door_driver_locked;
  state.doorOpen = state.doors.driver?.open ?? vehicle.door_driver_open;
  state.seatHeat = state.seatHeatLevels.driver ?? vehicle.seat_driver_heat_level;
  state.hoodOpen = Boolean(vehicle.hood_open);
  state.trunkOpen = Boolean(vehicle.trunk_open);
  updateDriveUI();
  updateVehicle();
  if (activePanelView === 'vehicle' && $('#info-dialog').open) {
    $('#dialog-content').innerHTML = vehicleDetailsMarkup();
  }
}

let activeTurn = null;
let currentNavigationRevision = 0;
async function acceptNavigationResponse(payload,authoritative=false) {
  let accepted;
  try{accepted=navigationStore.receive(payload,authoritative);}catch(error){
    if(error instanceof MissingRouteError){void synchronizeNavigation();return false;}throw error;
  }
  if(!accepted) {
    if(payload.runtime_epoch && payload.runtime_epoch!==snapshotOrder.epoch && !snapshotOrder.retired.has(payload.runtime_epoch))void synchronizeNavigation();
    return false;
  }
  payload=accepted;
  const restarted=state.runtimeEpoch && state.runtimeEpoch!==snapshotOrder.epoch;
  state.runtimeEpoch=snapshotOrder.epoch;
  if(restarted){state.vehicleVersion=null;currentNavigationRevision=0;resetJourneyRuntime();}
  state.navigationSynced=true;
  currentNavigationRevision=Math.max(currentNavigationRevision,payload.navigation?.revision || 0);
  if(payload.diagnostics?.observation_available!==false)applyBackendState(payload.vehicle_state);
  gearController.observe(payload,authoritative);
  // Rendering the route/map cannot delay vehicle controls.
  queueNavigationRender(payload);
  announceNewAlerts(payload.navigation?.alerts || []);
  return true;
}
let navigationRenderPayload=null,navigationRenderScheduled=false;
function queueNavigationRender(payload){
  navigationRenderPayload=payload;
  if(navigationRenderScheduled)return;
  navigationRenderScheduled=true;
  queueMicrotask(()=>{
    navigationRenderScheduled=false;
    const next=navigationRenderPayload;navigationRenderPayload=null;
    Promise.resolve(applyNavigation(next)).catch(error=>console.warn('Navigation view failed:',error.message));
  });
}
async function synchronizeNavigation() {
  if(syncRequest)return syncRequest;
  syncRequest=(async()=>{
    const response=await fetch(`${API_BASE}/api/v1/navigation/snapshot?session_id=${encodeURIComponent(sessionId)}${gearController.pending?`&event_id=${encodeURIComponent(gearController.pending.id)}`:''}`);
    if(!response.ok)throw Error('Chưa đồng bộ được trạng thái xe');
    const payload=await response.json();
    await acceptNavigationResponse(payload,true);
    return payload;
  })().finally(()=>{syncRequest=null;});
  return syncRequest;
}
async function sendNavigationEvent(event) {
  const journey=navigationStore.select().journey;
  event={runtime_epoch:state.runtimeEpoch,...event};
  if(journey && ['progress','charge','pause','resume','end'].includes(event.kind))event.target_id ||= journey.route_id;
  const response = await fetch(`${API_BASE}/api/v1/navigation/events`, {
    method:'POST', signal:AbortSignal.timeout(8000), headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:sessionId,event,known_routes:navigationStore.knownRoutes})
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || 'Không cập nhật được hành trình');
  if(!await acceptNavigationResponse(payload)) {
    const snapshot=await synchronizeNavigation();
    return {...snapshot,status:'blocked',message:'Trạng thái xe đã thay đổi.'};
  }
  if (['position','pause','resume','clear','gear','end'].includes(event.kind) && payload.message) {
    setPhase(payload.confirmation ? 'confirm' : payload.status === 'clarify' ? 'clarify' : 'idle',payload.message);
    if(state.sound && event.kind!=='gear') queueAnnouncement(payload.message,0,`navigation-position:${event.id}`);
  }
  return payload;
}
async function confirmNavigation(grant) {
  const revision = currentNavigationRevision;
  const response = await fetch(`${API_BASE}/api/v1/confirmations/${encodeURIComponent(grant.confirmation_id)}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:sessionId,turn_id:makeId(),decision:'approve',llm_provider:state.llmProvider})
  });
  const payload = await response.json();
  if (revision !== currentNavigationRevision) return;
  if (!response.ok) { setPhase('error',payload.detail || 'Không bắt đầu được hành trình'); return; }
  await acceptNavigationResponse(payload);
  setPhase(payload.status === 'verified' ? 'idle' : payload.status,payload.message);
  if(state.sound) await speak(payload.message);
}
function interruptActiveTurn() {
  stopPlayback();
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  const previous = activeTurn;
  activeTurn = null;
  if (previous) {
    previous.controller.abort();
    void fetch(`${API_BASE}/api/v1/turns/${encodeURIComponent(previous.turnId)}/cancel`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    }).catch(() => {});
  }
  state.busy = false;
  lockControls(false);
}

async function runBackendCommand(command, turnId = makeId(), { autoConfirm = false, action = null } = {}) {
  if (!command.trim()) return;
  if (state.busy) interruptActiveTurn();
  if (handleSpeakerCommand(command)) return;
  const ownedTurn = { turnId, controller: new AbortController(), sequence: 0, audioEpoch: state.audioEpoch || 0 };
  activeTurn = ownedTurn;
  rememberEdgeCommand(command);
  renderTurnSteps([]);
  state.busy = true; state.lastCommand = command; lockControls(true); $('#command-input').value = '';
  try {
    setPhase('thinking', `“${command}”`);
    const response = await fetch(`${API_BASE}/api/v1/turn/stream`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      signal: ownedTurn.controller.signal,
      body: JSON.stringify({ transcript: command, session_id: sessionId, turn_id: turnId, llm_provider: state.llmProvider, ...(action ? { action } : {}), position: getJourneyPosition() })
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail || 'Backend không phản hồi');
    }
    if (!response.body) throw new Error('Backend không hỗ trợ streaming');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let payload = null;
    let voiceStreamed = false;
    let answerVoiceQueued = false;
    let speechQueue = Promise.resolve();
    let speechPlayback = null;
    const handleEvent = (event) => {
      if (activeTurn !== ownedTurn) return;
      if (event.turn_id && event.turn_id !== ownedTurn.turnId) return;
      if (event.sequence && event.sequence <= ownedTurn.sequence) return;
      if (event.sequence) ownedTurn.sequence = event.sequence;
      if (event.type === 'cancelled') return;
      if (event.type === 'provider' && event.provider) {
        state.effectiveProvider = event.provider;
        updateModelStatus();
      }
      if (event.type === 'trip_plan' && event.committed!==false) acceptNavigationResponse(event).catch(error => console.warn('Trip plan display:', error.message));
      if (event.type === 'step' && event.step?.message) setPhase('thinking', event.step.message);
      if (event.type === 'error') {
        if (event.error_code === 'turn_timeout') void synchronizeNavigation();
        throw new Error(event.detail || 'Pipeline streaming bị lỗi');
      }
      if (event.type === 'progress' && event.text) {
        // Slow turns (handbook lookup) announce the wait; the answer is queued
        // on the same playback so it follows without cutting this phrase off.
        setPhase('thinking', event.text);
        if (state.sound && state.ttsAvailable) {
          speechPlayback ||= createTtsPlayback();
          voiceStreamed = true;
          speechQueue = speechQueue.then(() => activeTurn === ownedTurn && ownedTurn.audioEpoch === (state.audioEpoch || 0) ? speak(event.text, turnId, speechPlayback) : undefined);
        } else say(event.text);
      } else if (event.type === 'speech' && event.text) {
        // A deliberate touch on a concrete vehicle control is the user's
        // approval. Suppress the intermediate confirmation prompt; the final
        // verified or blocked result is still spoken below.
        if (autoConfirm) return;
        // Speech events may contain one event per sentence. Keep them for
        // low-latency audio, but render only the authoritative final response
        // so a two-sentence answer does not look like two assistant outputs.
        setPhase('speaking');
        if (state.sound && state.ttsAvailable) {
          speechPlayback ||= createTtsPlayback();
          voiceStreamed = true;
          answerVoiceQueued = true;
          // Fetch/synthesize the next clause as soon as the previous clause is
          // buffered, while sharing one Web Audio timeline for gapless speech.
          speechQueue = speechQueue.then(() => activeTurn === ownedTurn && ownedTurn.audioEpoch === (state.audioEpoch || 0) ? speak(event.text, turnId, speechPlayback) : undefined);
        }
      } else if (event.type === 'final') {
        payload = event.response;
      }
    };
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) if (line.trim()) handleEvent(JSON.parse(line));
      if (done) break;
    }
    if (buffer.trim()) handleEvent(JSON.parse(buffer));
    if (activeTurn !== ownedTurn) return;
    if (!payload) throw new Error('Backend kết thúc luồng trước khi trả kết quả');
    if (autoConfirm && payload.status === 'confirmation_required' && payload.confirmation) {
      if (voiceStreamed) {
        await speechQueue;
        await finishTtsPlayback(speechPlayback);
        voiceStreamed = false;
      }
      if (activeTurn !== ownedTurn) return;
      turnId = makeId();
      ownedTurn.turnId = turnId;
      const confirmationResponse = await fetch(
        `${API_BASE}/api/v1/confirmations/${payload.confirmation.confirmation_id}`,
        {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          signal: ownedTurn.controller.signal,
          body: JSON.stringify({
            session_id: sessionId, turn_id: turnId,
            decision: 'approve', llm_provider: state.llmProvider
          })
        }
      );
      payload = await confirmationResponse.json();
      if (!confirmationResponse.ok) throw new Error(payload.detail || 'Backend không phản hồi');
      answerVoiceQueued = false;
    }
    await wait(180);
    if (activeTurn !== ownedTurn) return;
    state.effectiveProvider = payload.diagnostics?.effective_provider || state.llmProvider;
    state.generationProvider = payload.diagnostics?.generation_provider || '';
    updateModelStatus();
    renderTurnSteps(payload.steps || []);
    await acceptNavigationResponse(payload);
    if (payload.route === 'handbook' && payload.status === 'verified') {
      state.manualAnswer = payload.message;
      state.manualEvidence = payload.evidence || [];
      openPanel('manual');
    }
    const phase = payload.status === 'verified'
      ? 'speaking'
      : payload.status === 'confirmation_required' ? 'confirm' : payload.status;
    setPhase(phase, payload.message);
    if (payload.status === 'verified') {
      const intent = payload.action?.intent || '';
      const card = DOCK_BY_INTENT[intent.split('.')[0]] || null;
      if (card) highlight(card);
    }
    if (voiceStreamed) {
      await speechQueue;
      await finishTtsPlayback(speechPlayback);
    }
    if (!answerVoiceQueued && ownedTurn.audioEpoch === (state.audioEpoch || 0)) await speak(payload.message, turnId);
    await wait(500);
    if (activeTurn !== ownedTurn) return;
    if (payload.status === 'verified') setPhase('idle');
    else setPhase(phase, payload.message);
  } catch (error) {
    if (activeTurn === ownedTurn && error.name !== 'AbortError') setPhase('blocked', `Không kết nối được pipeline local: ${error.message}`);
  } finally {
    if (activeTurn === ownedTurn) {
      activeTurn = null;
      state.busy = false; lockControls(false); document.body.classList.remove('busy');
    }
  }
}

function renderTurnSteps(steps) {
  const container = $('#turn-steps');
  if (!container) return;
  container.hidden = steps.length < 2;
  container.replaceChildren();
  const labels = { completed: 'Đã xong', cancelled: 'Đã huỷ', pending: 'Chưa chạy', waiting_slot: 'Cần thông tin', waiting_confirmation: 'Chờ xác nhận', blocked: 'Bị chặn', unknown: 'Chưa xác minh' };
  for (const step of steps) {
    const item = document.createElement('li');
    item.textContent = `${labels[step.status] || 'Đang xử lý'}: ${step.result?.message || ({ 'climate.set_temperature': 'Chỉnh điều hoà', 'window.set_position': 'Chỉnh cửa kính', 'door.set_open': 'Mở/đóng cửa', 'door.set_lock': 'Khoá cửa', 'hood.set_open': 'Mở/đóng capo', 'trunk.set_open': 'Mở/đóng cốp', 'body.set_open': 'Mở/đóng các cửa', 'seat.set_heat_level': 'Chỉnh sưởi ghế', 'media.play': 'Phát nhạc', 'media.pause': 'Dừng nhạc', 'manual.search': 'Tra cẩm nang', 'vehicle.get_status': 'Đọc trạng thái xe', 'memory.remember': 'Ghi nhớ', 'memory.forget': 'Quên thông tin', 'memory.reset': 'Xoá bộ nhớ', 'memory.recall': 'Đọc bộ nhớ', 'navigation.search': 'Tìm địa điểm' }[step.capability] || 'Thao tác tiếp theo')}`;
    container.append(item);
  }
}

function runCommand(command, options = {}) {
  if (handleSpeakerCommand(command)) return Promise.resolve();
  if (state.backendAvailable) return runBackendCommand(command, makeId(), options);
  setPhase('unverified', 'ViVi local chưa kết nối. Xe mô phỏng không nhận lệnh nào.');
  return Promise.resolve();
}

let recorder = null, recorderStream = null, recorderChunks = [], audioContext = null, silenceFrame = null, sttSocket = null, sttResult = null;
function stopRecording() {
  if (recorder?.state === 'recording') recorder.stop();
}
// Live STT: audio chunks go to the backend over a WebSocket while the user
// speaks, and partial text appears word by word as a caption above the wave.
function openSttStream(turnId) {
  return new Promise((resolve, reject) => {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const query = `session_id=${encodeURIComponent(sessionId)}&turn_id=${encodeURIComponent(turnId)}`;
    const socket = new WebSocket(`${scheme}://${location.host}/api/v1/stt/stream?${query}`);
    socket.onopen = () => resolve(socket);
    socket.onerror = () => reject(new Error('Không mở được STT streaming'));
  });
}
function listenSttStream(socket) {
  const result = new Promise((resolve, reject) => {
    socket.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.type === 'partial') {
        $('#command-input').value = message.final + message.interim;
        renderLiveCaption(message.final, message.interim);
      } else if (message.type === 'done') {
        // Soniox may close the utterance at a pause before the local silence timer.
        resolve(message.transcript); stopRecording();
      }
      else if (message.type === 'error') { reject(new Error(message.detail)); stopRecording(); }
    };
    socket.onclose = () => { reject(new Error('STT streaming bị ngắt')); stopRecording(); };
  });
  result.catch(() => {}); // Awaited after recording stops; avoid an unhandled rejection meanwhile.
  return result;
}
async function finishStreaming(socket, result, blob, turnId) {
  // Keep the live caption on screen while the last words are confirmed.
  const captionLive = $('#response-text').classList.contains('live');
  lockControls(true); setPhase('transcribing', captionLive ? '' : `${state.sttProvider} đang chốt câu…`);
  let transcript;
  try {
    if (socket.readyState === WebSocket.OPEN) socket.send('stop');
    transcript = await result;
  } catch {
    // Live STT failed (network, quota…): the upload endpoint uses the local fallback.
    lockControls(false);
    await submitRecording(blob, turnId);
    return;
  }
  $('#command-input').value = transcript;
  renderLiveCaption(transcript, '');
  lockControls(false);
  await runBackendCommand(transcript, turnId);
}
async function submitRecording(blob, turnId) {
  lockControls(true); setPhase('transcribing', `${state.sttProvider || 'STT local'} đang chuyển giọng nói thành văn bản…`);
  try {
    const form = new FormData();
    form.append('audio', blob, `vivi-${turnId}.webm`); form.append('session_id', sessionId); form.append('turn_id', turnId);
    const response = await fetch(`${API_BASE}/api/v1/stt`, { method: 'POST', body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'STT lỗi');
    $('#command-input').value = payload.transcript;
    lockControls(false);
    await runBackendCommand(payload.transcript, turnId);
  } catch (error) {
    setPhase('blocked', error.message); lockControls(false);
  }
}
async function startRecording() {
  if (!state.backendAvailable) { setPhase('blocked', 'Hãy chạy backend local trước khi dùng microphone.'); return; }
  if (!state.sttAvailable) { setPhase('blocked', 'STT local chưa sẵn sàng. Kiểm tra trang health và hướng dẫn setup runtime.'); return; }
  if (recorder?.state === 'recording') { stopRecording(); return; }
  interruptActiveTurn();
  try {
    recorderStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    recorderChunks = [];
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
    recorder = new MediaRecorder(recorderStream, mimeType ? { mimeType } : undefined);
    const turnId = makeId();
    sttSocket = null; sttResult = null;
    if (state.sttStreaming) {
      // Fall back to the upload endpoint when the live socket cannot open.
      try { sttSocket = await openSttStream(turnId); sttResult = listenSttStream(sttSocket); $('#command-input').value = ''; } catch { sttSocket = null; }
    }
    recorder.ondataavailable = event => {
      if (!event.data.size) return;
      recorderChunks.push(event.data);
      if (sttSocket?.readyState === WebSocket.OPEN) sttSocket.send(event.data);
    };
    recorder.onstop = async () => {
      $('#demo-mic').classList.remove('recording'); $('#demo-mic').setAttribute('aria-label', 'Bắt đầu thu âm');
      cancelAnimationFrame(silenceFrame); voiceLevel.mic = 0; recorderStream?.getTracks().forEach(track => track.stop()); await audioContext?.close(); audioContext = null;
      const blob = new Blob(recorderChunks, { type: recorder.mimeType || 'audio/webm' }); recorder = null;
      const socket = sttSocket, result = sttResult; sttSocket = null; sttResult = null;
      if (socket) await finishStreaming(socket, result, blob, turnId);
      else await submitRecording(blob, turnId);
    };
    recorder.start(250); $('#demo-mic').classList.add('recording'); $('#demo-mic').setAttribute('aria-label', 'Dừng thu âm');
    setPhase('listening', sttSocket ? 'Mình đang nghe… lời bạn nói sẽ hiện ở đây.' : 'Mình đang nghe… Nhấn mic lần nữa để dừng.');
    audioContext = new AudioContext();
    const source = audioContext.createMediaStreamSource(recorderStream), analyser = audioContext.createAnalyser(), samples = new Uint8Array(512);
    analyser.fftSize = 1024; source.connect(analyser);
    let heardSpeech = false, silentSince = 0, startedAt = performance.now();
    const monitor = () => {
      analyser.getByteTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / samples.length);
      voiceLevel.mic = rms;
      if (rms > .035) { heardSpeech = true; silentSince = 0; }
      else if (heardSpeech && !silentSince) silentSince = performance.now();
      if ((silentSince && performance.now() - silentSince > 1400) || performance.now() - startedAt > 15000) stopRecording();
      else silenceFrame = requestAnimationFrame(monitor);
    };
    monitor();
  } catch (error) { sttSocket?.close(); sttSocket = null; setPhase('blocked', `Không mở được microphone: ${error.message}`); }
}

function resolveLlmProvider(options, backendDefault, savedProvider, savedDefault) {
  const available = provider => options.some(item => item.provider === provider && item.available);
  const defaultProvider = available(backendDefault) ? backendDefault
    : options.find(item => item.available)?.provider || 'rules';
  // A saved override belongs to the configuration in which it was selected.
  // Old selections without that context must not mask a new backend default.
  return savedDefault === backendDefault && available(savedProvider) ? savedProvider : defaultProvider;
}

async function checkBackend() {
  try {
    const response = await fetch(`${API_BASE}/api/v1/health`); if (!response.ok) throw new Error();
    const health = await response.json(); state.backendAvailable = true; state.vehicleProvider = health.vehicle.provider; state.sttAvailable = health.stt.available; state.sttProvider = health.stt.provider; state.sttStreaming = Boolean(health.stt.streaming); state.sttDevice = health.stt.device; state.ttsOptions = health.tts.options || []; applyTtsProvider(resolveTtsProvider(health.tts)); state.storeAudio = health.storage.audio; state.storeTranscripts = health.storage.transcripts;
    edgeCommandHistory = state.storeTranscripts ? readEdgeCommandHistory() : [];
    if (!state.storeTranscripts) localStorage.removeItem(edgeCommandHistoryKey);
    state.llmOptions = health.llm.options || [];
    const saved = localStorage.getItem('vivi-llm-provider');
    state.llmProvider = resolveLlmProvider(state.llmOptions, health.llm.provider, saved,
      localStorage.getItem('vivi-llm-default-provider'));
    state.llmDefaultProvider = health.llm.provider;
    localStorage.setItem('vivi-llm-provider', state.llmProvider);
    localStorage.setItem('vivi-llm-default-provider', state.llmDefaultProvider);
    updateModelStatus();
    $('#backend-status').classList.remove('offline'); $('#backend-status').innerHTML = '<i></i> Local · <span id="backend-model"></span>';
    $('#backend-model').textContent = state.effectiveProvider || state.llmProvider;
    $('#backend-status').title = `STT ${health.stt.available ? 'sẵn sàng' : 'chưa cài'} · TTS ${state.ttsAvailable ? ttsVoiceLabel(state.ttsVoice) : 'chưa cài'} · LLM ${state.llmProvider}`;
    $('#drive-toggle').disabled = true;
    try {
      await synchronizeNavigation();
      lockControls(state.busy);
    } catch { state.navigationSynced=false;setPhase('blocked','Đang chờ đồng bộ trạng thái xe.'); }
  } catch {
    state.backendAvailable = false; $('#backend-status').classList.add('offline'); $('#backend-status').innerHTML = '<i></i> Backend offline';
    $('#backend-status').title = 'Chế độ prototype · hãy chạy python run.py';
  }
}

function updateModelStatus() {
  if ($('#backend-model')) {
    const served = state.effectiveProvider || state.llmProvider;
    $('#backend-model').textContent = state.generationProvider && state.generationProvider !== served ? `${served} · cẩm nang: ${state.generationProvider}` : served;
  }
}

$('#command-form').addEventListener('submit', event => { event.preventDefault(); runCommand($('#command-input').value); });
$('#demo-mic').addEventListener('click', startRecording);
document.querySelectorAll('[data-temp]').forEach(button => button.addEventListener('click', () => runCommand(`Đặt nhiệt độ ${state.temp + Number(button.dataset.temp)} độ`, { action: { capability: 'climate.set_temperature', arguments: { temperature_delta_celsius: Number(button.dataset.temp) } } })));
const windowAction = (zone, open) => ({ autoConfirm: true, action: { capability: 'window.set_position', arguments: { zone, position_percent: open ? 100 : 0 } } });
$('#window-pop').addEventListener('click', event => {
  const chip = event.target.closest('[data-window]');
  if (!chip) return;
  const zone = chip.dataset.window, open = !(windowPercent(zone) > 0);
  runCommand(`${open ? 'Mở' : 'Đóng'} ${WINDOWS[zone].noun}`, windowAction(zone, open));
});
$('#windows-open-all').addEventListener('click', () => runCommand('Mở tất cả cửa sổ', windowAction('all', true)));
$('#windows-close-all').addEventListener('click', () => runCommand('Đóng tất cả cửa sổ', windowAction('all', false)));
$('#body-pop').addEventListener('click', event => {
  const chip = event.target.closest('[data-panel]');
  if (chip) runCommand(
    `${bodyPanelState(chip.dataset.panel).open ? 'Đóng' : 'Mở'} ${BODY_PANELS[chip.dataset.panel].noun}`,
    { autoConfirm: true, action: { capability: chip.dataset.panel === 'hood' ? 'hood.set_open' : chip.dataset.panel === 'trunk' ? 'trunk.set_open' : 'door.set_open', arguments: { ...(CABIN_ZONES.includes(chip.dataset.panel) ? { zone: chip.dataset.panel } : {}), open: !bodyPanelState(chip.dataset.panel).open } } }
  );
});
// Direct manipulation is the user's approval, while the backend still binds a
// single-use confirmation to the exact grouped action and rechecks safety.
$('#open-all').addEventListener('click', () => {
  const anyOpen = Object.keys(BODY_PANELS).some(id => bodyPanelState(id).open);
  runCommand(`${anyOpen ? 'Đóng' : 'Mở'} tất cả cửa, capo và cốp`, { autoConfirm: true, action: { capability: 'body.set_open', arguments: { open: !anyOpen } } });
});
$('#lock-all').addEventListener('click', () => runCommand(
  CABIN_ZONES.every(zone => state.doors[zone]?.locked) ? 'Mở khóa tất cả cửa' : 'Khóa tất cả cửa',
  { autoConfirm: true, action: { capability: 'door.set_lock', arguments: { zone: 'all', locked: !CABIN_ZONES.every(zone => state.doors[zone]?.locked) } } }
));
$('#music-toggle').addEventListener('click', () => runCommand(state.music ? 'Dừng nhạc' : 'Phát nhạc thư giãn', { action: { capability: state.music ? 'media.pause' : 'media.play', arguments: state.music ? {} : { media_query: 'thư giãn' } } }));
async function setDemoDriving(driving) {
  if (!state.backendAvailable) {
    // Same rule as the memory simulator: no driving off with a door, the hood or the tailgate open.
    if (driving && CABIN_ZONES.some(zone => state.doors[zone]?.open)) throw new Error('Không thể chuyển sang chế độ lái khi cửa xe đang mở.');
    if (driving && (state.hoodOpen || state.trunkOpen)) throw new Error('Không thể chuyển sang chế độ lái khi nắp capo hoặc cốp sau đang mở.');
    state.driving = driving;
    updateDriveUI();
    updateVehicle();
    return;
  }
  if (state.vehicleProvider !== 'memory') throw new Error('Chế độ lái chỉ được đổi bằng fixture của memory simulator.');
  const response = await fetch(`${API_BASE}/api/v1/demo/vehicle/driving`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, driving })
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || 'Không đổi được trạng thái lái');
  applyBackendState(payload);
}
$('#drive-toggle').addEventListener('click', async () => {
  if (state.busy && !state.driving && gearController.desired!=='D') return;
  if (state.busy) interruptActiveTurn();
  lockControls(true);
  try {
    if(state.backendAvailable){
      if(!state.navigationSynced){await synchronizeNavigation();return;}
      await gearController.request(gearController.desired==='D'?'P':state.driving?'P':'D',snapshotOrder.epoch);
      return;
    }
    await setDemoDriving(!state.driving);
  } catch (error) {
    setPhase('blocked', error.message);
    return;
  } finally {
    lockControls(false);
  }
  setPhase('idle', state.driving ? 'Chuyển động đã dịu lại. Mình đồng hành cùng bạn.' : 'Đã về chế độ đỗ xe. Không gian ViVi được mở rộng.');
});
function setSound(on) {
  state.sound = on;
  $('#sound-toggle').setAttribute('aria-pressed', String(on));
  $('#sound-toggle').setAttribute('aria-label', on ? 'Tắt loa' : 'Bật loa');
  $('#sound-toggle use').setAttribute('href', on ? '#i-volume' : '#i-volume-off');
  if (!on) { stopPlayback(); if ('speechSynthesis' in window) speechSynthesis.cancel(); }
}
// "Tắt loa" / "bật loa" is a cabin UI setting, so it never reaches the vehicle backend.
function handleSpeakerCommand(command) {
  const text = normalize(command);
  const off = /\b(tat|dong)\s+(loa|tieng noi|giong noi|giong doc)\b|\b(im lang|tat tieng|dung noi)\b/.test(text);
  const on = /\b(bat|mo)\s+(loa|tieng noi|giong noi|giong doc)\b|\bbat tieng\b/.test(text);
  if (off === on) return false;
  $('#command-input').value = '';
  setSound(on);
  const message = on ? 'Đã bật loa. Mình sẽ trả lời bằng giọng nói.' : 'Đã tắt loa. Mình sẽ chỉ trả lời bằng chữ.';
  setPhase('idle', message);
  if (on) speak(message).catch(() => say(message));
  return true;
}
$('#sound-toggle').addEventListener('click', () => setSound(!state.sound));

const dialog = $('#info-dialog');
const modelLabels = { rules: 'Kịch bản', openai: 'OpenAI', google: 'Google Gemini', local: 'Local API', openrouter: 'OpenRouter' };
const ttsLabels = { zerotts: 'ViVi', vieneu: 'VieNeu' };
const ttsModes = { zerotts: 'On-device · ZeroTTS', vieneu: 'Cloud · cần Internet' };
const ttsVoiceLabel = voice => voice === 'VIVI' ? 'ViVi' : voice;
function resolveTtsProvider(tts) {
  // The saved choice wins while it is usable; otherwise the backend default.
  const usable = provider => state.ttsOptions.some(item => item.provider === provider && item.available);
  const saved = localStorage.getItem('vivi-tts-provider');
  if (usable(saved)) return saved;
  if (tts.available && (usable(tts.provider) || !state.ttsOptions.length)) return tts.provider;
  return state.ttsOptions.find(item => item.available)?.provider || '';
}
function applyTtsProvider(provider) {
  const option = state.ttsOptions.find(item => item.provider === provider);
  state.ttsProvider = provider;
  state.ttsAvailable = Boolean(option?.available);
  state.ttsVoice = option?.voice || '';
  state.ttsDevice = option?.device || '';
}
function ttsOptionsMarkup() {
  if (!state.backendAvailable) return '<div class="config-empty"><i></i><span>Backend chưa kết nối</span><small>Chạy python run.py để tải danh sách giọng.</small></div>';
  return state.ttsOptions.map(item => {
    const selected = item.provider === state.ttsProvider;
    const status = item.available ? ttsModes[item.provider] || '' : item.detail || 'Chưa cấu hình trong .env';
    return `<button type="button" class="model-option${selected ? ' selected' : ''}" data-tts-provider="${escapeHtml(item.provider)}" aria-pressed="${selected}" ${item.available ? '' : 'disabled'}><span class="model-option-head"><strong>${escapeHtml(ttsLabels[item.provider] || item.provider)}</strong><i></i></span><span class="model-id">${escapeHtml(ttsVoiceLabel(item.voice))}</span><small>${escapeHtml(status)}</small></button>`;
  }).join('');
}
function selectTtsProvider(provider) {
  if (!state.ttsOptions.some(item => item.provider === provider && item.available)) return;
  applyTtsProvider(provider);
  localStorage.setItem('vivi-tts-provider', provider);
  document.querySelectorAll('[data-tts-provider]').forEach(button => {
    const active = button.dataset.ttsProvider === provider;
    button.classList.toggle('selected', active);
    button.setAttribute('aria-pressed', String(active));
  });
  const activeVoice = $('#active-voice');
  if (activeVoice) activeVoice.textContent = `${ttsLabels[provider] || provider} · ${ttsVoiceLabel(state.ttsVoice)}`;
}
const modelModes = { rules: 'Phản hồi định sẵn', openai: 'Cloud · cần Internet', google: 'Cloud · cần Internet', local: 'On-device · riêng tư', openrouter: 'Cloud · cần Internet' };
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
}
const cabinZoneLabels = { driver: 'Bên tài', front_passenger: 'Bên phụ', rear_left: 'Sau trái', rear_right: 'Sau phải' };
function vehicleDetailsMarkup() {
  const zones = Object.keys(cabinZoneLabels);
  const cabinRows = zones.map(zone => {
    const door = state.doors[zone] || { open: false, locked: false };
    const doorStatus = door.open ? 'Đang mở' : door.locked ? 'Đã đóng · đang khóa' : 'Đã đóng · mở khóa';
    return `<div class="cabin-zone"><h3>${cabinZoneLabels[zone]}</h3><div class="detail-row"><span>Cửa xe</span><strong>${doorStatus}</strong></div><div class="detail-row"><span>Cửa sổ</span><strong>${state.windows[zone] ?? 0}%</strong></div><div class="detail-row"><span>Sưởi ghế</span><strong>Mức ${state.seatHeatLevels[zone] ?? 0}</strong></div></div>`;
  }).join('');
  const tireLabels = { front_left: 'Trước trái', front_right: 'Trước phải', rear_left: 'Sau trái', rear_right: 'Sau phải' };
  const tireRows = Object.entries(tireLabels).map(([position, label]) => `<div class="detail-row"><span>${label}</span><strong>${state.tirePressures[position] ?? 250} kPa</strong></div>`).join('');
  return `<h2>Không gian của bạn</h2><p>Digital Twin · trạng thái toàn bộ cabin được mô phỏng trong phiên trải nghiệm này.</p><div class="detail-row"><span>Pin còn lại</span><strong>${state.battery}% / ${state.range} km</strong></div><div class="detail-row"><span>Nhiệt độ cabin</span><strong>${state.temp}°C</strong></div><div class="detail-row"><span>Nhiệt độ hệ truyền động</span><strong>${state.powertrainTemp}°C</strong></div><div class="detail-row"><span>Chế độ nguồn</span><strong>${escapeHtml(state.powerState)}</strong></div><div class="detail-row"><span>Nắp capo</span><strong>${state.hoodOpen ? 'Đang mở' : 'Đã đóng'}</strong></div><div class="detail-row"><span>Cốp sau</span><strong>${state.trunkOpen ? 'Đang mở' : 'Đã đóng'}</strong></div><div class="cabin-grid">${cabinRows}<section class="cabin-zone tire-zone"><h3>Áp suất bốn lốp</h3>${tireRows}</section></div><div class="detail-row"><span>Multimedia</span><strong>${state.music ? 'Đang phát (mô phỏng)' : 'Đang dừng'}</strong></div><p class="dialog-note">Digital Twin mô phỏng bốn vị trí cabin; chưa kết nối xe thật hoặc dịch vụ VinFast.</p>`;
}
function modelOptionsMarkup() {
  if (!state.backendAvailable) return '<div class="config-empty"><i></i><span>Backend chưa kết nối</span><small>Chạy python run.py để tải danh sách model.</small></div>';
  return state.llmOptions.map(item => {
    const selected = item.provider === state.llmProvider;
    const label = modelLabels[item.provider] || item.provider;
    const model = item.model || 'Chưa cấu hình';
    const status = item.available ? (modelModes[item.provider] || '') : (item.detail || 'Chưa cấu hình trong .env');
    return `<button type="button" class="model-option${selected ? ' selected' : ''}" data-llm-provider="${escapeHtml(item.provider)}" aria-pressed="${selected}" ${item.available ? '' : 'disabled'}><span class="model-option-head"><strong>${escapeHtml(label)}</strong><i></i></span><span class="model-id">${escapeHtml(model)}</span><small>${escapeHtml(status)}</small></button>`;
  }).join('');
}
function selectLlmProvider(provider) {
  const selected = state.llmOptions.find(item => item.provider === provider && item.available);
  if (!selected) return;
  state.llmProvider = provider;
  localStorage.setItem('vivi-llm-provider', provider);
  localStorage.setItem('vivi-llm-default-provider', state.llmDefaultProvider);
  document.querySelectorAll('[data-llm-provider]').forEach(button => {
    const active = button.dataset.llmProvider === provider;
    button.classList.toggle('selected', active);
    button.setAttribute('aria-pressed', String(active));
  });
  const activeModel = $('#active-model');
  if (activeModel) activeModel.textContent = `${modelLabels[provider] || provider} · ${selected.model || 'Chưa cấu hình'}`;
  updateModelStatus();
}
function setActiveRail(view) {
  document.querySelectorAll('.dock [data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  $('#settings-button').classList.toggle('active', view === 'settings');
}
function openPanel(view) {
  if (view === 'journey') { activePanelView = ''; if (dialog.open) dialog.close(); setActiveRail('journey'); openJourney(); return; }
  if (view === 'home') { activePanelView = ''; closeJourney(); if (dialog.open) dialog.close(); setActiveRail('home'); return; }
  activePanelView = view;
  setActiveRail(view);
  const currentModel = state.llmOptions.find(item => item.provider === state.llmProvider);
  const sourceMarkup = state.manualEvidence.length
    ? state.manualEvidence.map((item, index) => `<div class="detail-row"><span>Nguồn ${index + 1}</span><strong>${item.source_url ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.section || item.source_id)}</a>` : escapeHtml(item.section || item.source_id)}</strong></div>`).join('')
    : '<p class="dialog-note">Hãy hỏi một câu về kỹ thuật VF8 để xem nguồn từ cẩm nang.</p>';
  const manualContent = `<h2>Hiểu chiếc xe của bạn</h2><p>${escapeHtml(state.manualAnswer || 'ViVi sẽ hiển thị câu trả lời đã được grounding từ cẩm nang VF8 2026 tại đây.')}</p>${sourceMarkup}`;
  const contents = {
    vehicle: vehicleDetailsMarkup(),
    manual: manualContent,
    settings: `<h2>Cấu hình ViVi</h2><p>Chọn cách ViVi suy nghĩ và chuyển động trong không gian của bạn.</p><section class="config-section"><div class="config-heading"><span>MÔ HÌNH HỘI THOẠI</span><strong id="active-model">${escapeHtml(modelLabels[state.llmProvider] || state.llmProvider || 'Chưa kết nối')} · ${escapeHtml(currentModel?.model || '')}</strong></div><div class="model-options">${modelOptionsMarkup()}</div><p class="dialog-note">Lựa chọn được áp dụng từ lượt hội thoại tiếp theo. Model ID và API key vẫn được quản lý an toàn trong .env.</p></section><section class="config-section"><div class="config-heading"><span>GIỌNG PHẢN HỒI</span><strong id="active-voice">${state.ttsAvailable ? `${escapeHtml(ttsLabels[state.ttsProvider] || state.ttsProvider)} · ${escapeHtml(ttsVoiceLabel(state.ttsVoice))}` : 'Trình duyệt/text fallback'}</strong></div><div class="model-options">${ttsOptionsMarkup()}</div><p class="dialog-note">Áp dụng từ câu trả lời tiếp theo. Lần đầu dùng ViVi local cần vài giây để nạp model.</p></section><section class="config-section"><div class="config-heading"><span>TRẢI NGHIỆM</span><strong>Không gian & giọng nói</strong></div><label class="motion-setting"><span><strong>Giảm chuyển động</strong><small>Dừng lơ lửng, quỹ đạo và hiệu ứng sóng</small></span><input id="reduce-motion" type="checkbox" ${state.reduced ? 'checked' : ''}><i></i></label><div class="config-runtime"><div><span>NHẬN DIỆN GIỌNG NÓI</span><strong>${state.sttAvailable ? `${escapeHtml(state.sttProvider)} · ${state.sttDevice === 'cloud' ? `cloud${state.sttStreaming ? ' · trực tiếp' : ''}` : 'local'}` : 'Chưa sẵn sàng'}</strong></div></div><p class="dialog-note">Audio: ${state.storeAudio ? 'đang lưu' : 'không lưu'} · transcript: ${state.storeTranscripts ? 'đang lưu' : 'không lưu'}.</p></section>`
  };
  $('#dialog-content').innerHTML = contents[view];
  dialog.classList.toggle('config-dialog', view === 'settings');
  if (!dialog.open) dialog.showModal();
  $('#try-climate')?.addEventListener('click', () => { dialog.close(); runCommand('Tôi hơi lạnh'); });
  $('#reduce-motion')?.addEventListener('change', event => applyReducedMotion(event.target.checked));
  document.querySelectorAll('[data-llm-provider]').forEach(button => button.addEventListener('click', () => selectLlmProvider(button.dataset.llmProvider)));
  document.querySelectorAll('[data-tts-provider]').forEach(button => button.addEventListener('click', () => selectTtsProvider(button.dataset.ttsProvider)));
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => openPanel(button.dataset.view)));
$('#settings-button').addEventListener('click', () => openPanel('settings'));
$('#close-dialog').addEventListener('click', () => dialog.close());
dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
dialog.addEventListener('close', () => { activePanelView = ''; setActiveRail(document.body.classList.contains('journey-open') ? 'journey' : 'home'); dialog.classList.remove('config-dialog'); });
initJourney({
  onOpen: () => openPanel('journey'),
  onClose: () => openPanel('home'),
  // Navigation prompts surface in the assistant line and use ViVi's voice.
  announce: text => { setResponseText(text); queueAnnouncement(text); },
  progress: progress => { nav = progress; updateHud(); },
  route: route => { plannedRoute = route; scene?.setRoute(route); },
  travel: distance => scene?.setRouteDistance(distance),
  network: roads => scene?.setNetwork(roads),
  command: (text, action) => runBackendCommand(text, makeId(), { action }),
  confirm: grant => confirmNavigation(grant),
  position: position => sendNavigationEvent({ kind:'position', id:makeId(), revision:currentNavigationRevision, position }),
  event: event => sendNavigationEvent(event),
  gear: async target=>{await gearController.request(target,snapshotOrder.epoch);return true;},
  synchronize:synchronizeNavigation,
  transportLost:()=>{gearController.status="unknown";gearController.changed(gearController);void gearController.reconcile();}
});
function applyReducedMotion(reduced) {
  state.reduced = reduced;
  document.body.classList.toggle('reduced-motion', reduced);
  wave.setReduced(reduced);
  scene?.setReduced(reduced);
  capsuleNeon.set({ speed: reduced ? 0 : (NEON_BY_PHASE[state.phase] || NEON_BY_PHASE.idle).speed });
  maneuverNeon.set({ speed: reduced ? 0 : 4 });
}
motionPreference.addEventListener('change', event => applyReducedMotion(event.matches));
applyReducedMotion(state.reduced);

// The full-screen map hides the 3D scene, so stop rendering it meanwhile.
new MutationObserver(() => scene?.setPaused(document.body.classList.contains('journey-open'))).observe(document.body, { attributes: true, attributeFilter: ['class'] });

$('#keyboard-toggle').addEventListener('click', () => {
  const typing = !document.body.classList.contains('typing');
  document.body.classList.toggle('typing', typing);
  $('#keyboard-toggle').setAttribute('aria-pressed', String(typing));
  if (typing) $('#command-input').focus();
});
$('#command-input').addEventListener('keydown', event => { if (event.key === 'Escape') $('#keyboard-toggle').click(); });
// Dock popovers sit above their button, kept inside the viewport; one at a time.
const dockPops = [['#climate-pop', '#climate-button'], ['#window-pop', '#window-button'], ['#body-pop', '#body-button']];
function togglePop(popId, buttonId, open = $(popId).hidden) {
  const pop = $(popId);
  if (open) dockPops.forEach(([other, button]) => { if (other !== popId) togglePop(other, button, false); });
  pop.hidden = !open;
  $(buttonId).setAttribute('aria-expanded', String(open));
  if (open) {
    const box = $(buttonId).getBoundingClientRect(), half = pop.offsetWidth / 2 + 12;
    pop.style.left = `${Math.min(innerWidth - half, Math.max(half, box.left + box.width / 2))}px`;
  }
}
for (const [popId, buttonId] of dockPops) {
  $(buttonId).addEventListener('click', event => { event.stopPropagation(); togglePop(popId, buttonId); });
}
document.addEventListener('click', event => {
  for (const [popId, buttonId] of dockPops) if (!$(popId).hidden && !event.target.closest(popId)) togglePop(popId, buttonId, false);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') for (const [popId, buttonId] of dockPops) if (!$(popId).hidden) { togglePop(popId, buttonId, false); $(buttonId).focus(); }
});
function updateClock() { $('#clock').textContent = new Intl.DateTimeFormat('vi-VN', { hour: '2-digit', minute: '2-digit' }).format(new Date()); }
if (location.hash === '#journey') openPanel('journey');
updateClock(); setInterval(updateClock, 60000); updateVehicle(); checkBackend().then(pollVehicleAlerts); setInterval(pollVehicleAlerts, 1500);

window.addEventListener('storage', event => {
  if (event.key !== 'vivi-history-reset') return;
  edgeCommandHistory = [];
  state.lastCommand = '';
  state.manualAnswer = '';
  state.manualEvidence = [];
  renderTurnSteps([]);
  renderLiveCaption('', '');
  if (activePanelView === 'manual') openPanel('manual');
  if (!state.busy) setPhase('idle', 'Đã xoá lịch sử hội thoại.');
  synchronizeNavigation().catch(() => {});
});
