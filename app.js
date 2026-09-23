const $ = (selector) => document.querySelector(selector);
const canvas = $('#universe-canvas');
const ctx = canvas.getContext('2d');
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
// The FastAPI backend serves this UI, so API calls follow the configured
// VIVI_PORT automatically instead of being tied to a hard-coded port.
const API_BASE = '';
const sessionId = localStorage.getItem('vivi-session-id') || crypto.randomUUID();
localStorage.setItem('vivi-session-id', sessionId);
const state = { phase: 'idle', temp: 23, window: false, music: false, driving: false, reduced: motionPreference.matches, sound: false, busy: false, backendAvailable: false, sttAvailable: false, ttsAvailable: false, lastCommand: '', progress: 0 };
const phaseLabels = { idle: 'ViVi đang ở đây', listening: 'Mình đang nghe bạn', transcribing: 'Mình đang nhận diện lời nói', thinking: 'Để mình xem nhé', validating: 'Đang kiểm tra an toàn', acting: 'Đang chăm sóc không gian của bạn', synthesizing: 'Đang chuẩn bị giọng Mai Chi', speaking: 'Một chút dễ chịu, dành cho bạn', clarify: 'Mình chờ bạn nói thêm', blocked: 'Mình giữ nguyên trạng thái xe' };
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
let width = 1, height = 1, tick = 0, previous = 0;
let pointer = { x: 0, y: 0 };
let stars = [];

function resize() {
  const box = canvas.getBoundingClientRect();
  width = box.width; height = box.height;
  const ratio = Math.min(devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  let seed = 713;
  const rand = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  stars = Array.from({ length: Math.round(width / 7) }, () => ({ x: rand(), y: rand(), r: rand() * .8 + .25, alpha: rand() * .45 + .08, phase: rand() * 6.28 }));
}
new ResizeObserver(resize).observe(canvas);
canvas.addEventListener('pointermove', e => { const box = canvas.getBoundingClientRect(); pointer.x = (e.clientX - box.left) / width - .5; pointer.y = (e.clientY - box.top) / height - .5; });
canvas.addEventListener('pointerleave', () => { pointer.x = 0; pointer.y = 0; });

function glow(x, y, radius, stops) {
  const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius);
  stops.forEach(([position, color]) => gradient.addColorStop(position, color));
  ctx.fillStyle = gradient; ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.fill();
}

function draw(now) {
  if (document.hidden) { previous = now; requestAnimationFrame(draw); return; }
  const delta = Math.min((now - previous) / 1000 || 0, .05); previous = now;
  const quiet = state.reduced || state.driving;
  tick += delta * (state.reduced ? 0 : state.driving ? .2 : 1);
  const t = tick;
  ctx.clearRect(0, 0, width, height);
  const mobile = width < 700;
  const cx = width * (mobile ? .5 : .66) + (quiet ? 0 : pointer.x * 12);
  const cy = height * (mobile ? .65 : .49) + (quiet ? 0 : Math.sin(t * .62) * 7 + pointer.y * 7);
  const size = mobile ? Math.min(width * .235, 94) : Math.min(width * .117, 149);
  const energetic = ['listening', 'speaking'].includes(state.phase);
  const think = state.phase === 'thinking';
  const breath = 1 + Math.sin(t * 1.15) * .02 + (energetic && !quiet ? Math.sin(t * 7.2) * .022 : 0);

  for (const star of stars) {
    const opacity = star.alpha * (.75 + Math.sin(t * .3 + star.phase) * .25);
    ctx.fillStyle = `rgba(176,214,246,${opacity})`;
    ctx.beginPath(); ctx.arc(star.x * width, star.y * height * .89, star.r, 0, Math.PI * 2); ctx.fill();
    if (star.r > .96) { ctx.strokeStyle = `rgba(176,214,246,${opacity * .4})`; ctx.lineWidth = .4; ctx.beginPath(); ctx.moveTo(star.x * width - 3, star.y * height * .89); ctx.lineTo(star.x * width + 3, star.y * height * .89); ctx.moveTo(star.x * width, star.y * height * .89 - 3); ctx.lineTo(star.x * width, star.y * height * .89 + 3); ctx.stroke(); }
  }

  // A distant horizon anchors the floating character in a spacious scene.
  const horizonY = height * 1.5, rx = width * .8, ry = height * .73;
  ctx.save(); ctx.translate(width * .57, horizonY); ctx.scale(1, ry / rx);
  const haze = ctx.createRadialGradient(0, 0, rx * .94, 0, 0, rx * 1.03);
  haze.addColorStop(0, '#09152100'); haze.addColorStop(.64, '#639ac614'); haze.addColorStop(.72, '#9bdef92b'); haze.addColorStop(.78, '#5089b50b'); haze.addColorStop(1, '#050c1500');
  ctx.fillStyle = haze; ctx.beginPath(); ctx.arc(0, 0, rx * 1.03, 0, Math.PI * 2); ctx.fill();
  ctx.strokeStyle = '#8fc8e527'; ctx.lineWidth = .6; ctx.beginPath(); ctx.arc(0, 0, rx, Math.PI, Math.PI * 2); ctx.stroke(); ctx.restore();

  glow(cx, cy, size * 2.35, [[0, '#82d0ff13'], [.4, '#4389c311'], [1, '#16325300']]);
  ctx.save(); ctx.translate(cx, cy); ctx.scale(breath, breath);

  // Project two gently twisted ribbons in 3D; depth controls light and width.
  const segments = [];
  const rotation = t * (think ? .55 : .14);
  for (let ribbon = 0; ribbon < 2; ribbon++) {
    let last;
    for (let i = 0; i <= 220; i++) {
      const a = i / 220 * Math.PI * 2;
      const shape = 1 + .055 * Math.sin(a * 3 + t * .5);
      let x = Math.cos(a) * size * shape;
      let y = Math.sin(a) * size * .80 * shape;
      let z = Math.sin(a * 2 + ribbon * Math.PI + t * .22) * size * .25;
      const tilt = ribbon === 0 ? -.57 : .7;
      const x1 = x * Math.cos(tilt) - y * Math.sin(tilt);
      const y1 = x * Math.sin(tilt) + y * Math.cos(tilt);
      const depthAngle = (ribbon === 0 ? .86 : -.91) + Math.sin(rotation) * .25;
      x = x1 * Math.cos(depthAngle) + z * Math.sin(depthAngle);
      z = -x1 * Math.sin(depthAngle) + z * Math.cos(depthAngle);
      y = y1;
      const perspective = 480 / (480 + z);
      const point = { x: x * perspective, y: y * perspective, z, a };
      if (last) segments.push({ from: last, to: point, z: (last.z + z) / 2, ribbon });
      last = point;
    }
  }
  segments.sort((a, b) => b.z - a.z);
  const drawSegments = (front) => {
    for (const seg of segments) {
      if ((seg.z < 0) !== front) continue;
      const luminosity = (1 - seg.z / (size * 1.5)) * .5;
      ctx.beginPath(); ctx.moveTo(seg.from.x, seg.from.y); ctx.lineTo(seg.to.x, seg.to.y);
      ctx.lineCap = 'round';
      ctx.strokeStyle = `rgba(${think ? '181,177,255' : '111,198,250'},${luminosity * .13})`; ctx.lineWidth = 15; ctx.stroke();
      ctx.strokeStyle = `rgba(109,194,249,${luminosity * .28})`; ctx.lineWidth = 6; ctx.stroke();
      ctx.strokeStyle = `rgba(${seg.ribbon ? '169,223,255' : '201,240,255'},${Math.min(.95, luminosity + .15)})`; ctx.lineWidth = 1.5 + luminosity * 1.4; ctx.stroke();
    }
  };
  drawSegments(false);
  const coreSize = size * .51;
  glow(0, 0, coreSize * 1.7, [[0, '#b8eaff26'], [.45, '#76bbf12b'], [1, '#3a81ba00']]);
  const core = ctx.createRadialGradient(-coreSize * .3, -coreSize * .4, 1, 0, 0, coreSize);
  core.addColorStop(0, '#ddf7ffb0'); core.addColorStop(.25, '#8dd4f978'); core.addColorStop(.62, '#3e7dad45'); core.addColorStop(.9, '#1839564a'); core.addColorStop(1, '#8bc8ec74');
  ctx.fillStyle = core; ctx.beginPath(); ctx.arc(0, 0, coreSize, 0, Math.PI * 2); ctx.fill();
  // A subtle V-shaped light signature gives the companion its own identity.
  ctx.beginPath(); ctx.moveTo(-size * .23, -size * .1); ctx.bezierCurveTo(-size * .13, -size * .06, -size * .06, size * .16, 0, size * .2); ctx.bezierCurveTo(size * .06, size * .16, size * .13, -size * .06, size * .23, -size * .1);
  ctx.strokeStyle = '#c7f1ff'; ctx.lineWidth = 2.8; ctx.shadowColor = '#9cdfff'; ctx.shadowBlur = 15; ctx.stroke(); ctx.shadowBlur = 0;
  drawSegments(true);

  // A fine orbital plane and a few moving satellites establish spatial depth.
  ctx.save(); ctx.rotate(-.15); ctx.strokeStyle = '#91ccf021'; ctx.lineWidth = .7;
  ctx.beginPath(); ctx.ellipse(0, 12, size * 1.56, size * .32, 0, 0, Math.PI * 2); ctx.stroke();
  for (let i = 0; i < 3; i++) {
    const a = t * .22 + i * 2.09;
    const x = Math.cos(a) * size * 1.56, y = Math.sin(a) * size * .32 + 12;
    glow(x, y, 7, [[0, '#cff2ffd0'], [.2, '#9bd9ff6a'], [1, '#9bd9ff00']]);
  }
  ctx.restore();
  if (state.phase === 'acting') {
    const progress = state.reduced ? .5 : (t * .65) % 1;
    ctx.strokeStyle = `rgba(152,221,255,${(1 - progress) * .5})`; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.ellipse(0, 0, size * (1.1 + progress), size * (.8 + progress * .6), -.2, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.restore();
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

function setPhase(phase, text) {
  state.phase = phase;
  $('#orb-state').textContent = phaseLabels[phase];
  if (text) $('#response-text').textContent = text;
  document.body.classList.toggle('busy', !['idle', 'clarify', 'blocked'].includes(phase));
}
function updateVehicle() {
  $('#temperature').innerHTML = `${state.temp}<span>°</span>`;
  $('#window-label').textContent = state.window ? 'Đang mở' : 'Đang đóng';
  $('#window-toggle').setAttribute('aria-pressed', String(state.window));
  $('#window-toggle').setAttribute('aria-label', state.window ? 'Đóng cửa sổ bên tài' : 'Mở cửa sổ bên tài');
  $('#window-percent').textContent = state.window ? '100%' : '0%';
  $('#window-note').textContent = state.window ? 'Đón một chút không khí mới' : 'Một khoảng yên tĩnh riêng';
  $('#music-card').classList.toggle('playing', state.music);
  $('#music-toggle').textContent = state.music ? 'Ⅱ' : '▶';
  $('#music-toggle').setAttribute('aria-label', state.music ? 'Dừng nhạc mô phỏng' : 'Phát nhạc mô phỏng');
  $('#music-note').textContent = state.music ? 'Đang phát · trạng thái mô phỏng' : 'Để tâm trí được thảnh thơi';
}
function highlight(id) { $(id).classList.add('highlight'); setTimeout(() => $(id).classList.remove('highlight'), 2600); }
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
async function ensureTtsAudioContext() {
  ttsAudioContext ||= new AudioContext();
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
async function speak(text, turnId = crypto.randomUUID()) {
  if (!state.sound) return;
  stopPlayback();
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  if (state.ttsAvailable) {
    const playback = { controller: new AbortController(), sources: new Set(), cancelled: false, streamDone: false, played: false, nextStart: 0 };
    playback.done = new Promise(resolve => { playback.finish = resolve; });
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
        source.buffer = buffer; source.connect(context.destination);
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
      playback.streamDone = true;
      if (!playback.sources.size) playback.finish();
      await playback.done;
      if (currentPlayback === playback) currentPlayback = null;
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
  document.querySelectorAll('.suggestions button, .stepper button, #window-toggle, #music-toggle, #drive-toggle, #demo-mic, .send-button').forEach(button => { button.disabled = locked; });
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
      case 'climate': state.temp = action.value; message = `Mình đã đặt nhiệt độ xe mô phỏng ở ${state.temp} độ. Hy vọng bạn thấy dễ chịu hơn.`; highlight('#climate-card'); $('#climate-note').textContent = 'ViVi vừa điều chỉnh cho bạn'; break;
      case 'window': state.window = action.value; message = `Mình đã ${state.window ? 'mở' : 'đóng'} cửa sổ bên tài trên xe mô phỏng.`; highlight('#window-card'); break;
      case 'music': state.music = action.value; message = state.music ? 'Đã chuyển nhạc sang trạng thái phát trong demo. Một chút bình yên cho hành trình.' : 'Mình đã dừng nhạc mô phỏng.'; highlight('#music-card'); break;
      case 'manual': openPanel('manual'); message = 'Mình đã mở cẩm nang minh họa. Bản demo chưa kết nối tài liệu hướng dẫn chính thức.'; break;
      case 'status': openPanel('vehicle'); message = `Xe mô phỏng còn 82% pin, nhiệt độ cài đặt ${state.temp} độ và cửa sổ bên tài ${state.window ? 'đang mở' : 'đang đóng'}.`; break;
    }
    updateVehicle(); state.progress = 3;
    setPhase('speaking', message); say(message); await wait(2300);
    setPhase('idle');
  } finally { state.busy = false; lockControls(false); document.body.classList.remove('busy'); }
}

function applyBackendState(vehicle) {
  state.temp = vehicle.temperature_celsius;
  state.window = vehicle.window_driver_percent > 0;
  state.music = vehicle.media_playing;
  state.driving = vehicle.driving;
  updateVehicle();
}

async function runBackendCommand(command, turnId = crypto.randomUUID()) {
  if (state.busy || !command.trim()) return;
  state.busy = true; state.lastCommand = command; lockControls(true); $('#command-input').value = '';
  try {
    setPhase('thinking', `“${command}”`);
    const response = await fetch(`${API_BASE}/api/v1/turn`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript: command, session_id: sessionId, turn_id: turnId, vehicle_state: { temperature_celsius: state.temp, window_driver_percent: state.window ? 100 : 0, media_playing: state.music, driving: state.driving, battery_percent: 82, range_km: 328 } })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Backend không phản hồi');
    setPhase('validating', 'Safety gateway đã kiểm tra yêu cầu.');
    await wait(180);
    applyBackendState(payload.vehicle_state);
    if (payload.action.intent === 'manual.search' && payload.status === 'verified') openPanel('manual');
    const phase = payload.status === 'verified' ? 'speaking' : payload.status;
    setPhase(phase, payload.message);
    if (payload.status === 'verified') {
      const card = payload.action.intent.startsWith('climate.') ? '#climate-card' : payload.action.intent.startsWith('window.') ? '#window-card' : payload.action.intent.startsWith('media.') ? '#music-card' : null;
      if (card) highlight(card);
    }
    await speak(payload.message, turnId);
    await wait(500);
    setPhase('idle');
  } catch (error) {
    setPhase('blocked', `Không kết nối được pipeline local: ${error.message}`);
  } finally {
    state.busy = false; lockControls(false); document.body.classList.remove('busy');
  }
}

function runCommand(command, voiceDemo = false) {
  return state.backendAvailable ? runBackendCommand(command) : runLocalCommand(command, voiceDemo);
}

let recorder = null, recorderStream = null, recorderChunks = [], audioContext = null, silenceFrame = null;
function stopRecording() {
  if (recorder?.state === 'recording') recorder.stop();
}
async function submitRecording(blob, turnId) {
  lockControls(true); setPhase('transcribing', 'PhoWhisper đang chuyển giọng nói thành văn bản…');
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
  if (!state.sttAvailable) { setPhase('blocked', 'PhoWhisper chưa được cài. Xem requirements-ai.txt.'); return; }
  if (recorder?.state === 'recording') { stopRecording(); return; }
  if (state.busy) return;
  try {
    recorderStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    recorderChunks = [];
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
    recorder = new MediaRecorder(recorderStream, mimeType ? { mimeType } : undefined);
    const turnId = crypto.randomUUID();
    recorder.ondataavailable = event => { if (event.data.size) recorderChunks.push(event.data); };
    recorder.onstop = async () => {
      $('#demo-mic').classList.remove('recording'); $('#demo-mic').setAttribute('aria-label', 'Bắt đầu thu âm');
      cancelAnimationFrame(silenceFrame); recorderStream?.getTracks().forEach(track => track.stop()); await audioContext?.close(); audioContext = null;
      const blob = new Blob(recorderChunks, { type: recorder.mimeType || 'audio/webm' }); recorder = null;
      await submitRecording(blob, turnId);
    };
    recorder.start(250); $('#demo-mic').classList.add('recording'); $('#demo-mic').setAttribute('aria-label', 'Dừng thu âm');
    setPhase('listening', 'Mình đang nghe… Nhấn mic lần nữa để dừng.');
    audioContext = new AudioContext();
    const source = audioContext.createMediaStreamSource(recorderStream), analyser = audioContext.createAnalyser(), samples = new Uint8Array(512);
    analyser.fftSize = 1024; source.connect(analyser);
    let heardSpeech = false, silentSince = 0, startedAt = performance.now();
    const monitor = () => {
      analyser.getByteTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / samples.length);
      if (rms > .035) { heardSpeech = true; silentSince = 0; }
      else if (heardSpeech && !silentSince) silentSince = performance.now();
      if ((silentSince && performance.now() - silentSince > 1400) || performance.now() - startedAt > 15000) stopRecording();
      else silenceFrame = requestAnimationFrame(monitor);
    };
    monitor();
  } catch (error) { setPhase('blocked', `Không mở được microphone: ${error.message}`); }
}

async function checkBackend() {
  try {
    const response = await fetch(`${API_BASE}/api/v1/health`); if (!response.ok) throw new Error();
    const health = await response.json(); state.backendAvailable = true; state.sttAvailable = health.stt.available; state.ttsAvailable = health.tts.available;
    $('#backend-status').classList.remove('offline'); $('#backend-status').innerHTML = `<i></i> Local · ${health.llm.provider}`;
    $('#runtime-note').innerHTML = `<span class="mini-dot"></span> STT ${health.stt.available ? 'sẵn sàng' : 'chưa cài'} · TTS ${health.tts.available ? 'Mai Chi' : 'chưa cài'} · LLM ${health.llm.provider}`;
  } catch {
    state.backendAvailable = false; $('#backend-status').classList.add('offline'); $('#backend-status').innerHTML = '<i></i> Local backend offline';
    $('#runtime-note').innerHTML = '<span class="mini-dot"></span> Chế độ prototype · hãy chạy python run.py';
  }
}

$('#command-form').addEventListener('submit', event => { event.preventDefault(); runCommand($('#command-input').value); });
document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', () => runCommand(button.dataset.command)));
$('#demo-mic').addEventListener('click', startRecording);
document.querySelectorAll('[data-temp]').forEach(button => button.addEventListener('click', () => runCommand(`Đặt nhiệt độ ${state.temp + Number(button.dataset.temp)} độ`)));
$('#window-toggle').addEventListener('click', () => runCommand(state.window ? 'Đóng cửa sổ bên tài' : 'Mở cửa sổ bên tài'));
$('#music-toggle').addEventListener('click', () => runCommand(state.music ? 'Dừng nhạc' : 'Phát nhạc thư giãn'));
$('#drive-toggle').addEventListener('click', () => {
  state.driving = !state.driving;
  $('#drive-toggle').setAttribute('aria-pressed', String(state.driving));
  $('#drive-toggle span').textContent = state.driving ? 'Đang lái xe · Demo' : 'Đang đỗ xe';
  document.body.classList.toggle('driving', state.driving);
  setPhase('idle', state.driving ? 'Chuyển động đã dịu lại. Mình đồng hành cùng bạn.' : 'Đã về chế độ đỗ xe. Không gian ViVi được mở rộng.');
});
$('#sound-toggle').addEventListener('click', () => {
  state.sound = !state.sound;
  $('#sound-toggle').setAttribute('aria-pressed', String(state.sound));
  $('#sound-toggle span').textContent = `Mai Chi: ${state.sound ? 'bật' : 'tắt'}`;
  if (state.sound) speak('Mình là ViVi, sẵn sàng đồng hành cùng bạn.'); else { stopPlayback(); if ('speechSynthesis' in window) speechSynthesis.cancel(); }
});

const dialog = $('#info-dialog');
function openPanel(view) {
  if (view === 'home') { if (dialog.open) dialog.close(); document.querySelectorAll('.rail-main .rail-button').forEach(button => button.classList.toggle('active', button.dataset.view === 'home')); return; }
  document.querySelectorAll('.rail-main .rail-button').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  const contents = {
    vehicle: `<h2>Không gian của bạn</h2><p>Digital Twin · trạng thái được mô phỏng trong phiên trải nghiệm này.</p><div class="detail-row"><span>Pin còn lại</span><strong>82% / 328 km</strong></div><div class="detail-row"><span>Nhiệt độ cài đặt</span><strong>${state.temp}°C</strong></div><div class="detail-row"><span>Cửa sổ bên tài</span><strong>${state.window ? 'Đang mở' : 'Đang đóng'}</strong></div><div class="detail-row"><span>Multimedia</span><strong>${state.music ? 'Đang phát (mô phỏng)' : 'Đang dừng'}</strong></div><div class="detail-row"><span>Chế độ</span><strong>${state.driving ? 'Đang lái xe (demo)' : 'Đang đỗ xe'}</strong></div><p class="dialog-note">Chưa kết nối xe thật, cảm biến hoặc dịch vụ VinFast.</p>`,
    journey: '<h2>Mỗi hành trình, một khám phá.</h2><p>Không gian dành cho địa điểm yêu thích, trạm sạc và chỉ đường trong phiên bản tiếp theo.</p><div class="detail-row"><span>Điểm đến</span><strong>Chưa thiết lập</strong></div><div class="detail-row"><span>Dịch vụ bản đồ</span><strong>Chưa kết nối</strong></div><p class="dialog-note">Prototype hiện tập trung vào trải nghiệm trợ lý và các thao tác cabin.</p>',
    manual: '<h2>Hiểu chiếc xe của bạn</h2><p>Đây là bản xem trước cách ViVi trình bày một câu trả lời có nguồn tham khảo.</p><div class="detail-row"><span>Ví dụ câu hỏi</span><strong>Điều chỉnh nhiệt độ thế nào?</strong></div><p>Trong demo, bạn có thể nói “Tôi hơi lạnh” để tăng nhiệt độ cài đặt thêm 2°C, hoặc nhập “Đặt nhiệt độ 25 độ”. ViVi kiểm tra giới hạn 16–30°C trước khi cập nhật xe mô phỏng.</p><div class="detail-row"><span>Nguồn của nội dung này</span><strong>Quy tắc prototype ViVi</strong></div><p class="dialog-note">Chưa tích hợp RAG hoặc manual VinFast. Nội dung này chỉ mô tả cách vận hành bản demo.</p><button class="dialog-action" id="try-climate">Thử “Tôi hơi lạnh” ↗</button>',
    settings: `<h2>Nhịp điệu của ViVi</h2><p>Điều chỉnh không gian theo sự thoải mái của bạn.</p><label class="setting-label"><input id="reduce-motion" type="checkbox" ${state.reduced ? 'checked' : ''}> Giảm chuyển động</label><p class="dialog-note">Giữ nguyên hình khối và ánh sáng, dừng chuyển động lơ lửng, xoay quỹ đạo và hiệu ứng âm thanh dạng sóng. Chế độ lái xe tự động làm dịu chuyển động.</p><div class="detail-row"><span>Tương tác giọng nói</span><strong>${state.sttAvailable ? 'PhoWhisper local' : 'Chưa cài AI runtime'}</strong></div><p class="dialog-note">Nhấn mic một lần để bắt đầu. ViVi tự dừng khi bạn im lặng hoặc khi bạn nhấn mic lần nữa. Audio và transcript được lưu cục bộ trong thư mục data cho đến khi bạn tự xóa.</p>`
  };
  $('#dialog-content').innerHTML = contents[view];
  if (!dialog.open) dialog.showModal();
  $('#try-climate')?.addEventListener('click', () => { dialog.close(); runCommand('Tôi hơi lạnh'); });
  $('#reduce-motion')?.addEventListener('change', event => { state.reduced = event.target.checked; document.body.classList.toggle('reduced-motion', state.reduced); });
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => openPanel(button.dataset.view)));
$('.settings-button').addEventListener('click', () => openPanel('settings'));
$('#close-dialog').addEventListener('click', () => dialog.close());
dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
dialog.addEventListener('close', () => { document.querySelectorAll('.rail-main .rail-button').forEach(button => button.classList.toggle('active', button.dataset.view === 'home')); });
motionPreference.addEventListener('change', event => { state.reduced = event.matches; document.body.classList.toggle('reduced-motion', state.reduced); });
document.body.classList.toggle('reduced-motion', state.reduced);
function updateClock() { $('#clock').textContent = new Intl.DateTimeFormat('vi-VN', { hour: '2-digit', minute: '2-digit' }).format(new Date()); }
updateClock(); setInterval(updateClock, 60000); updateVehicle(); checkBackend();
