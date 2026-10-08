import { closeJourney, initJourney, openJourney } from './journey.js?v=5';
import { neonBorder } from './neon.js?v=2';
import { createWave } from './wave.js?v=2';

const $ = (selector) => document.querySelector(selector);
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
// The FastAPI backend serves this UI, so API calls follow the configured
// VIVI_PORT automatically instead of being tied to a hard-coded port.
const API_BASE = '';
const makeId = () => globalThis.crypto?.randomUUID?.() || `vivi-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const sessionId = localStorage.getItem('vivi-session-id') || makeId();
localStorage.setItem('vivi-session-id', sessionId);
const state = { phase: 'idle', temp: 23, window: false, music: false, driving: false, powerState: 'off', battery: 82, range: 328, powertrainTemp: 45, tirePressures: {}, windows: {}, doors: {}, seatHeatLevels: {}, doorLocked: false, doorOpen: false, hoodOpen: false, trunkOpen: false, seatHeat: 0, alerts: [], alertSequence: 0, reduced: motionPreference.matches, sound: false, busy: false, backendAvailable: false, vehicleProvider: '', sttAvailable: false, sttProvider: '', sttStreaming: false, sttDevice: '', ttsAvailable: false, ttsProvider: '', storeAudio: false, storeTranscripts: false, llmProvider: '', llmOptions: [], lastCommand: '', manualAnswer: '', manualEvidence: [], progress: 0 };
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
const ALERT_TTS_COOLDOWN_MS = 30000;
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

import('./scene3d.js?v=32')
  .then(module => {
    scene = module.createScene($('#space'), { reduced: state.reduced });
    scene.setRoute(plannedRoute);
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
  $('#window-toggle').setAttribute('aria-pressed', String(state.window));
  $('#window-toggle').setAttribute('aria-label', state.window ? 'Đóng cửa sổ bên tài' : 'Mở cửa sổ bên tài');
  // Backend reports the opening in percent; the offline demo only knows open or closed.
  scene?.setWindow(state.window ? (state.windows.driver > 0 ? state.windows.driver : 100) : 0);
  updateBodyPanel();
  scene?.setDoors(Object.fromEntries(CABIN_ZONES.map(zone => [zone, Boolean(state.doors[zone]?.open)])));
  scene?.setHood(state.hoodOpen);
  scene?.setTrunk(state.trunkOpen);
  $('#music-toggle').setAttribute('aria-pressed', String(state.music));
  $('#music-toggle').setAttribute('aria-label', state.music ? 'Dừng nhạc' : 'Phát nhạc');
  updateHud();
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
  const driving = state.driving;
  const speed = Math.round(nav?.speedKmh ?? cruise.speed);
  $('#gear-letter').textContent = driving ? 'D' : 'P';
  $('#mode-label').textContent = driving ? (nav ? `ĐẾN ${nav.destination.toUpperCase()}` : 'ĐANG DI CHUYỂN') : 'ĐỖ XE';
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
}
function updateDriveUI() {
  $('#drive-toggle').setAttribute('aria-pressed', String(state.driving));
  $('#drive-toggle').setAttribute('aria-label', state.driving ? 'Chuyển về đỗ xe' : 'Chuyển sang lái xe');
  $('#drive-toggle span').textContent = state.driving ? 'D' : 'P';
  document.body.classList.toggle('driving', state.driving);
  scene?.setMode(state.driving ? 'driving' : 'parked');
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
    speak(alert.message, `alert-${alert.alert_id}`).catch(() => say(alert.message));
  }
}
async function pollVehicleAlerts() {
  if (!state.backendAvailable) return;
  try {
    const response = await fetch(`${API_BASE}/api/v1/vehicle/alerts?session_id=${encodeURIComponent(sessionId)}`);
    if (!response.ok) return;
    const snapshot = await response.json();
    applyBackendState(snapshot.vehicle_state);
    renderAlerts(snapshot);
    announceNewAlerts(snapshot.active_alerts || []);
  } catch { /* Polling resumes automatically after a temporary disconnect. */ }
}
function highlight(id) { $(id).classList.add('highlight'); setTimeout(() => $(id).classList.remove('highlight'), 2600); }
const DOCK_BY_INTENT = { climate: '#climate-button', window: '#window-toggle', door: '#body-button', hood: '#body-button', trunk: '#body-button', body: '#body-button', media: '#music-toggle' };
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
async function speak(text, turnId = makeId(), sequencePlayback = null) {
  if (!state.sound) return;
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
        body: JSON.stringify({ text, session_id: sessionId, turn_id: turnId })
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

// This intentionally deterministic demo changes only in-memory vehicle state.
// A production adapter must verify actual vehicle acknowledgement before success.
function resolveCommand(command) {
  const text = normalize(command);
  if (/\b(dung|khong)\s+(mo|dong|tang|giam|bat|tat|phat|ha)\b/.test(text)) return { type: 'clarify', message: 'Mình giữ nguyên trạng thái xe. Bạn có thể nói rõ thao tác muốn thực hiện nhé.' };
  if (/huong dan|manual|canh bao|ap suat|cam nang/.test(text)) return { type: 'manual' };
  if (/trang thai|pin|nhien lieu|bao nhieu|may do/.test(text)) return { type: 'status' };
  if (/cua so|cua kinh/.test(text)) {
    const opening = /mo|ha/.test(text);
    if (!opening && !/dong|len/.test(text)) return { type: 'clarify', message: 'Bạn muốn mở hay đóng cửa sổ bên tài?' };
    if (opening && state.driving) return { type: 'blocked', message: 'Demo đang ở chế độ lái xe. Theo policy minh họa, mình chỉ mở cửa sổ khi xe đang đỗ.' };
    return { type: 'window', value: opening };
  }
  if (/\b(mo|dong)\s+cua\b/.test(text)) {
    const opening = /\bmo\b/.test(text);
    if (opening && state.driving) return { type: 'blocked', message: 'Demo đang ở chế độ lái xe. Theo policy minh họa, mình chỉ mở cửa xe khi xe đang đỗ.' };
    return { type: 'door', value: opening };
  }
  if (/nhac|bai hat|am thanh|play|pause/.test(text)) return { type: 'music', value: !/dung|tat|pause/.test(text) };
  if (/lanh|am hon|nong|nhiet do|dieu hoa|mat hon/.test(text)) {
    if (/\b(bat|tat)\b/.test(text)) return { type: 'clarify', message: 'Bản demo hiện hỗ trợ đặt nhiệt độ từ 16 đến 30 độ. Bạn muốn đặt bao nhiêu độ?' };
    const number = text.match(/(\d+(?:[.,]\d+)?)\s*(?:do|°)/);
    const amount = number ? Number(number[1].replace(',', '.')) : 2;
    const lower = /\b(giam|ha)\b|\bnong\b|\bmat hon\b/.test(text);
    const relative = /\b(tang|giam|ha|them|bot)\b/.test(text);
    const value = number && !relative ? amount : state.temp + (lower ? -amount : amount);
    if (value < 16 || value > 30) return { type: 'blocked', message: 'Nhiệt độ của xe mô phỏng chỉ nhận từ 16 đến 30 độ. Bạn muốn đặt bao nhiêu độ?' };
    return { type: 'climate', value };
  }
  return { type: 'clarify', message: 'Mình có thể chỉnh nhiệt độ, mở hoặc đóng cửa sổ bên tài, điều khiển nhạc và mở cẩm nang minh họa. Bạn muốn thử việc nào?' };
}
function lockControls(locked) {
  document.querySelectorAll('#climate-pop button, #body-pop button, #window-toggle, #music-toggle, #drive-toggle, .send-button').forEach(button => { button.disabled = locked; });
  // The mic stays enabled while recording so a second tap can stop it.
  $('#demo-mic').disabled = locked && recorder?.state !== 'recording';
  if (state.backendAvailable && state.vehicleProvider !== 'memory') $('#drive-toggle').disabled = true;
  $('#command-form').setAttribute('aria-busy', String(locked));
}
async function runLocalCommand(command, voiceDemo = false) {
  if (state.busy || !command.trim()) return;
  state.busy = true; state.lastCommand = command; state.progress = 0; lockControls(true);
  $('#command-input').value = '';
  try {
    setPhase('listening', voiceDemo ? 'Demo giọng nói: “ViVi ơi, tôi hơi lạnh.”' : `“${command}”`);
    await wait(voiceDemo ? 1400 : 850);
    setPhase('thinking', 'Mình đang kiểm tra yêu cầu và trạng thái xe…'); state.progress = 1;
    await wait(1100);
    const action = resolveCommand(command);
    if (action.type === 'clarify' || action.type === 'blocked') {
      setPhase(action.type, action.message); say(action.message); await wait(1000); return;
    }
    setPhase('acting', action.type === 'manual' ? 'Mình đang mở nội dung hướng dẫn minh họa…' : 'Đã kiểm tra policy demo. Đang cập nhật trạng thái…'); state.progress = 2;
    await wait(1000);
    let message = '';
    switch (action.type) {
      case 'climate': state.temp = action.value; message = `Mình đã đặt nhiệt độ xe mô phỏng ở ${state.temp} độ. Hy vọng bạn thấy dễ chịu hơn.`; highlight('#climate-button'); break;
      case 'window': state.window = action.value; message = `Mình đã ${state.window ? 'mở' : 'đóng'} cửa sổ bên tài trên xe mô phỏng.`; highlight('#window-toggle'); break;
      case 'door': state.doorOpen = action.value; state.doors = { ...state.doors, driver: { ...state.doors.driver, open: action.value } }; message = `Mình đã ${state.doorOpen ? 'mở' : 'đóng'} cửa bên tài trên xe mô phỏng.`; highlight('#body-button'); break;
      case 'music': state.music = action.value; message = state.music ? 'Đã chuyển nhạc sang trạng thái phát trong demo. Một chút bình yên cho hành trình.' : 'Mình đã dừng nhạc mô phỏng.'; highlight('#music-toggle'); break;
      case 'manual': openPanel('manual'); message = 'Mình đã mở cẩm nang minh họa. Bản demo chưa kết nối tài liệu hướng dẫn chính thức.'; break;
      case 'status': openPanel('vehicle'); message = `Xe mô phỏng còn 82% pin, nhiệt độ cài đặt ${state.temp} độ và cửa sổ bên tài ${state.window ? 'đang mở' : 'đang đóng'}.`; break;
    }
    updateVehicle(); state.progress = 3;
    setPhase('speaking', message); say(message); await wait(2300);
    setPhase('idle');
  } finally { state.busy = false; lockControls(false); document.body.classList.remove('busy'); }
}

function applyBackendState(vehicle) {
  if (!vehicle) return;
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

async function runBackendCommand(command, turnId = makeId(), { autoConfirm = false } = {}) {
  if (state.busy || !command.trim()) return;
  rememberEdgeCommand(command);
  state.busy = true; state.lastCommand = command; lockControls(true); $('#command-input').value = '';
  try {
    setPhase('thinking', `“${command}”`);
    const response = await fetch(`${API_BASE}/api/v1/turn/stream`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript: command, session_id: sessionId, turn_id: turnId, llm_provider: state.llmProvider })
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
    let streamedSpeech = false;
    let voiceStreamed = false;
    let speechQueue = Promise.resolve();
    let speechPlayback = null;
    const handleEvent = (event) => {
      if (event.type === 'error') throw new Error(event.detail || 'Pipeline streaming bị lỗi');
      if (event.type === 'progress' && event.text) {
        // Slow turns (handbook lookup) announce the wait; the answer is queued
        // on the same playback so it follows without cutting this phrase off.
        setPhase('thinking', event.text);
        if (state.sound && state.ttsAvailable) {
          speechPlayback ||= createTtsPlayback();
          voiceStreamed = true;
          speechQueue = speechQueue.then(() => speak(event.text, turnId, speechPlayback));
        } else say(event.text);
      } else if (event.type === 'speech' && event.text) {
        // A deliberate touch on a concrete vehicle control is the user's
        // approval. Suppress the intermediate confirmation prompt; the final
        // verified or blocked result is still spoken below.
        if (autoConfirm) return;
        streamedSpeech = true;
        // Speech events may contain one event per sentence. Keep them for
        // low-latency audio, but render only the authoritative final response
        // so a two-sentence answer does not look like two assistant outputs.
        setPhase('speaking');
        if (state.sound && state.ttsAvailable) {
          speechPlayback ||= createTtsPlayback();
          voiceStreamed = true;
          // Fetch/synthesize the next clause as soon as the previous clause is
          // buffered, while sharing one Web Audio timeline for gapless speech.
          speechQueue = speechQueue.then(() => speak(event.text, turnId, speechPlayback));
        }
      } else if (event.type === 'final') {
        payload = event.response;
        streamedSpeech ||= Boolean(event.streamed_speech);
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
    if (!payload) throw new Error('Backend kết thúc luồng trước khi trả kết quả');
    if (autoConfirm && payload.status === 'confirmation_required' && payload.confirmation) {
      if (voiceStreamed) {
        await speechQueue;
        await finishTtsPlayback(speechPlayback);
        voiceStreamed = false;
      }
      turnId = makeId();
      const confirmationResponse = await fetch(
        `${API_BASE}/api/v1/confirmations/${payload.confirmation.confirmation_id}`,
        {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId, turn_id: turnId,
            decision: 'approve', llm_provider: state.llmProvider
          })
        }
      );
      payload = await confirmationResponse.json();
      if (!confirmationResponse.ok) throw new Error(payload.detail || 'Backend không phản hồi');
      streamedSpeech = false;
    }
    await wait(180);
    applyBackendState(payload.vehicle_state);
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
    } else if (!streamedSpeech || state.sound) await speak(payload.message, turnId);
    await wait(500);
    if (payload.status === 'verified') setPhase('idle');
    else setPhase(phase, payload.message);
  } catch (error) {
    setPhase('blocked', `Không kết nối được pipeline local: ${error.message}`);
  } finally {
    state.busy = false; lockControls(false); document.body.classList.remove('busy');
  }
}

function runCommand(command, options = {}) {
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
      } else if (message.type === 'done') resolve(message.transcript);
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
  if (state.busy) return;
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
    const health = await response.json(); state.backendAvailable = true; state.vehicleProvider = health.vehicle.provider; state.sttAvailable = health.stt.available; state.sttProvider = health.stt.provider; state.sttStreaming = Boolean(health.stt.streaming); state.sttDevice = health.stt.device; state.ttsAvailable = health.tts.available; state.ttsProvider = health.tts.provider; state.storeAudio = health.storage.audio; state.storeTranscripts = health.storage.transcripts;
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
    $('#backend-model').textContent = state.llmProvider;
    $('#backend-status').title = `STT ${health.stt.available ? 'sẵn sàng' : 'chưa cài'} · TTS ${health.tts.available ? 'Mai Chi' : 'chưa cài'} · LLM ${state.llmProvider}`;
    $('#drive-toggle').disabled = state.vehicleProvider !== 'memory';
    try {
      const vehicleResponse = await fetch(`${API_BASE}/api/v1/vehicle/state?session_id=${encodeURIComponent(sessionId)}`);
      if (vehicleResponse.ok) applyBackendState(await vehicleResponse.json());
    } catch { /* An external simulator may be temporarily offline. */ }
  } catch {
    state.backendAvailable = false; $('#backend-status').classList.add('offline'); $('#backend-status').innerHTML = '<i></i> Backend offline';
    $('#backend-status').title = 'Chế độ prototype · hãy chạy python run.py';
  }
}

function updateModelStatus() {
  if ($('#backend-model')) $('#backend-model').textContent = state.llmProvider;
}

$('#command-form').addEventListener('submit', event => { event.preventDefault(); runCommand($('#command-input').value); });
$('#demo-mic').addEventListener('click', startRecording);
document.querySelectorAll('[data-temp]').forEach(button => button.addEventListener('click', () => runCommand(`Đặt nhiệt độ ${state.temp + Number(button.dataset.temp)} độ`)));
$('#window-toggle').addEventListener('click', () => runCommand(
  state.window ? 'Đóng cửa sổ bên tài' : 'Mở cửa sổ bên tài',
  { autoConfirm: true }
));
$('#body-pop').addEventListener('click', event => {
  const chip = event.target.closest('[data-panel]');
  if (chip) runCommand(
    `${bodyPanelState(chip.dataset.panel).open ? 'Đóng' : 'Mở'} ${BODY_PANELS[chip.dataset.panel].noun}`,
    { autoConfirm: true }
  );
});
// Direct manipulation is the user's approval, while the backend still binds a
// single-use confirmation to the exact grouped action and rechecks safety.
$('#open-all').addEventListener('click', () => {
  const anyOpen = Object.keys(BODY_PANELS).some(id => bodyPanelState(id).open);
  runCommand(`${anyOpen ? 'Đóng' : 'Mở'} tất cả cửa, capo và cốp`, { autoConfirm: true });
});
$('#lock-all').addEventListener('click', () => runCommand(
  CABIN_ZONES.every(zone => state.doors[zone]?.locked) ? 'Mở khóa tất cả cửa' : 'Khóa tất cả cửa',
  { autoConfirm: true }
));
$('#music-toggle').addEventListener('click', () => runCommand(state.music ? 'Dừng nhạc' : 'Phát nhạc thư giãn'));
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
  if (state.busy) return;
  lockControls(true);
  try {
    await setDemoDriving(!state.driving);
  } catch (error) {
    setPhase('blocked', error.message);
    return;
  } finally {
    lockControls(false);
  }
  setPhase('idle', state.driving ? 'Chuyển động đã dịu lại. Mình đồng hành cùng bạn.' : 'Đã về chế độ đỗ xe. Không gian ViVi được mở rộng.');
});
$('#sound-toggle').addEventListener('click', () => {
  state.sound = !state.sound;
  $('#sound-toggle').setAttribute('aria-pressed', String(state.sound));
  $('#sound-toggle').setAttribute('aria-label', state.sound ? 'Tắt giọng Mai Chi' : 'Bật giọng Mai Chi');
  // Enabling sound must not inject a synthetic greeting into the conversation.
  if (!state.sound) { stopPlayback(); if ('speechSynthesis' in window) speechSynthesis.cancel(); }
});

const dialog = $('#info-dialog');
const modelLabels = { rules: 'Kịch bản', openai: 'OpenAI', google: 'Google Gemini', local: 'Local API', openrouter: 'OpenRouter' };
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
    settings: `<h2>Cấu hình ViVi</h2><p>Chọn cách ViVi suy nghĩ và chuyển động trong không gian của bạn.</p><section class="config-section"><div class="config-heading"><span>MÔ HÌNH HỘI THOẠI</span><strong id="active-model">${escapeHtml(modelLabels[state.llmProvider] || state.llmProvider || 'Chưa kết nối')} · ${escapeHtml(currentModel?.model || '')}</strong></div><div class="model-options">${modelOptionsMarkup()}</div><p class="dialog-note">Lựa chọn được áp dụng từ lượt hội thoại tiếp theo. Model ID và API key vẫn được quản lý an toàn trong .env.</p></section><section class="config-section"><div class="config-heading"><span>TRẢI NGHIỆM</span><strong>Không gian & giọng nói</strong></div><label class="motion-setting"><span><strong>Giảm chuyển động</strong><small>Dừng lơ lửng, quỹ đạo và hiệu ứng sóng</small></span><input id="reduce-motion" type="checkbox" ${state.reduced ? 'checked' : ''}><i></i></label><div class="config-runtime"><div><span>NHẬN DIỆN GIỌNG NÓI</span><strong>${state.sttAvailable ? `${escapeHtml(state.sttProvider)} · ${state.sttDevice === 'cloud' ? `cloud${state.sttStreaming ? ' · trực tiếp' : ''}` : 'local'}` : 'Chưa sẵn sàng'}</strong></div><div><span>GIỌNG PHẢN HỒI</span><strong>${state.ttsAvailable ? `${escapeHtml(state.ttsProvider)} · local` : 'Trình duyệt/text fallback'}</strong></div></div><p class="dialog-note">Audio: ${state.storeAudio ? 'đang lưu' : 'không lưu'} · transcript: ${state.storeTranscripts ? 'đang lưu' : 'không lưu'}.</p></section>`
  };
  $('#dialog-content').innerHTML = contents[view];
  dialog.classList.toggle('config-dialog', view === 'settings');
  if (!dialog.open) dialog.showModal();
  $('#try-climate')?.addEventListener('click', () => { dialog.close(); runCommand('Tôi hơi lạnh'); });
  $('#reduce-motion')?.addEventListener('change', event => applyReducedMotion(event.target.checked));
  document.querySelectorAll('[data-llm-provider]').forEach(button => button.addEventListener('click', () => selectLlmProvider(button.dataset.llmProvider)));
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
  announce: text => { setResponseText(text); if (state.sound) speak(text).catch(() => say(text)); },
  setDriving: driving => { if (state.driving !== driving) setDemoDriving(driving).catch(() => {}); },
  progress: progress => { nav = progress; updateHud(); },
  route: route => { plannedRoute = route; scene?.setRoute(route); },
  travel: distance => scene?.setRouteDistance(distance)
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
const dockPops = [['#climate-pop', '#climate-button'], ['#body-pop', '#body-button']];
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
