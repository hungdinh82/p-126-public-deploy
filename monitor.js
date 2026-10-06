const $ = selector => document.querySelector(selector);
const canvas = $('#chart');
const context = canvas.getContext('2d');
const STORAGE_KEY = 'vivi-monitor-samples-v2';
const RESET_KEY = 'vivi-monitor-reset-at';
const MAX_SAMPLES = 240;
const compactSample = item => ({
  timestamp: item.timestamp,
  memory: item.memory,
  host: item.host,
  gpu: item.gpu,
  disk: item.disk,
});
let samples;
try {
  samples = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]').slice(-MAX_SAMPLES);
} catch {
  samples = [];
}
let resetAt = Number(localStorage.getItem(RESET_KEY) || 0);
let turns = [];
let selectedTurnId = null;
let listSignature = '';
let detailSignature = '';
let pollInFlight = false;

const bytes = value => value == null ? '—' : `${(value / 1048576).toFixed(0)} MB`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const turnKey = turn => `${turn.session_id || ''}:${turn.turn_id || ''}`;
const percent = value => value == null ? '—' : `${Number(value).toFixed(1)}%`;

function drawChart() {
  const width = canvas.clientWidth;
  const height = 280;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(width * ratio);
  const pixelHeight = Math.round(height * ratio);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const metric = $('#metric').value;
  const getValue = sample => {
    if (metric === 'memory') return sample.memory?.percent;
    if (metric === 'cpu') return sample.host?.load_1m;
    if (metric === 'disk') return sample.disk?.percent;
    const gpu = sample.gpu?.devices?.[0];
    if (metric === 'gpu_memory') return gpu?.memory_total_mb ? gpu.memory_used_mb / gpu.memory_total_mb * 100 : null;
    return gpu?.peak_usage_percent_20s ?? gpu?.usage_percent;
  };
  const valid = samples.map(sample => ({ sample, value: getValue(sample) })).filter(point => Number.isFinite(point.value));
  const maximum = metric === 'cpu' ? Math.max(1, ...valid.map(point => point.value)) : 100;

  context.strokeStyle = '#a3d5fc20';
  context.fillStyle = '#7893aa';
  context.font = '10px sans-serif';
  for (let index = 0; index < 5; index += 1) {
    const y = 20 + index * 60;
    context.beginPath();
    context.moveTo(42, y);
    context.lineTo(width - 8, y);
    context.stroke();
    const label = metric === 'cpu' ? (maximum * (1 - index / 4)).toFixed(1) : `${100 - index * 25}%`;
    context.fillText(label, 5, y + 4);
  }
  if (valid.length < 2) return;
  context.strokeStyle = '#9bdcff';
  context.lineWidth = 2;
  context.beginPath();
  valid.forEach((point, index) => {
    const x = 42 + index * (width - 50) / Math.max(1, valid.length - 1);
    const y = 260 - point.value / maximum * 240;
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.stroke();
}

function updateHardware(item) {
  const gpu = item.gpu?.devices?.[0];
  $('#ram-value').textContent = percent(item.memory?.percent);
  $('#ram-detail').textContent = `${bytes(item.memory?.used_bytes)} / ${bytes(item.memory?.total_bytes)}`;
  $('#cpu-value').textContent = item.host?.load_1m ?? '—';
  $('#cpu-detail').textContent = `${item.host?.cpu_count ?? '—'} core · load average`;
  $('#gpu-value').textContent = gpu ? percent(gpu.usage_percent) : 'N/A';
  const gpuTemperature = Number.isFinite(gpu?.temperature_c) ? `${gpu.temperature_c}°C` : 'nhiệt độ N/A';
  const gpuMemory = Number.isFinite(gpu?.memory_used_mb) && Number.isFinite(gpu?.memory_total_mb)
    ? `VRAM ${gpu.memory_used_mb}/${gpu.memory_total_mb} MB`
    : 'bộ nhớ dùng chung với RAM';
  $('#gpu-detail').textContent = gpu
    ? `${gpu.name} · ${gpuTemperature} · ${gpuMemory} · peak 20s ${gpu.peak_usage_percent_20s ?? gpu.usage_percent}%`
    : 'Không có GPU telemetry; đang dùng CPU';
  $('#disk-value').textContent = percent(item.disk?.percent);
  $('#disk-detail').textContent = `${bytes(item.disk?.free_bytes)} còn trống`;
  const device = item.device || {};
  const kinds = { jetson: 'NVIDIA Jetson', nvidia_rtx: 'NVIDIA RTX', nvidia_gpu: 'NVIDIA GPU', cpu: 'CPU' };
  $('#device-info').textContent = `Thiết bị: ${kinds[device.kind] || device.kind || 'chưa xác định'}${device.name ? ` · ${device.name}` : ''}${device.gpu_backend ? ` · ${device.gpu_backend}` : ''}`;
  $('#updated').textContent = `Cập nhật ${new Date(item.timestamp * 1000).toLocaleTimeString('vi-VN')}`;
  updateComponents(item.components || {});
}

function updateComponents(components) {
  const labels = { orchestration: 'Điều phối', stt: 'Nhận dạng giọng nói', tts: 'Tổng hợp giọng nói', vehicle: 'Vehicle adapter', rag: 'Cẩm nang RAG' };
  const entries = Object.entries(components);
  $('#components').innerHTML = entries.length
    ? entries.map(([name, item]) => {
      const status = item.status === 'ready' ? 'ready' : 'offline';
      return `<div class="component"><span>${esc(labels[name] || name)}</span><strong class="${status}">${status === 'ready' ? 'Sẵn sàng' : 'Ngoại tuyến'}</strong><small>${esc(item.detail)}</small></div>`;
    }).join('')
    : '<div class="empty">Không có trạng thái component.</div>';
}

function updateRetainedCommands(enabled) {
  const panel = $('#retained-command-panel');
  panel.hidden = !enabled;
  if (!enabled) return;
  let commands = [];
  try {
    const stored = JSON.parse(localStorage.getItem('vivi-command-history') || '[]');
    if (Array.isArray(stored)) commands = stored.slice(0, 12);
  } catch {
    commands = [];
  }
  $('#retained-command-list').innerHTML = commands.length
    ? commands.map(command => `<div class="row"><span>${new Date(command.at).toLocaleTimeString('vi-VN')}</span><strong>${esc(command.text)}</strong></div>`).join('')
    : '<div class="empty">Chưa có lệnh được lưu.</div>';
}

function updateTurnList() {
  const signature = turns.map(turnKey).join('|');
  const list = $('#command-list');
  if (signature !== listSignature) {
    listSignature = signature;
    const previousScroll = list.scrollTop;
    if (!turns.length) {
      list.innerHTML = '<div class="empty">Chưa có lượt chạy kể từ lần reset.</div>';
    } else {
      list.innerHTML = turns.slice().reverse().map(turn => {
        const key = turnKey(turn);
        const selected = key === selectedTurnId ? ' selected' : '';
        const elapsed = turn.input_to_output_ms ?? turn.end_to_end_ms;
        const duration = elapsed == null ? 'đang xử lý' : `${Number(elapsed).toFixed(0)} ms`;
        const time = turn.received_at ? new Date(turn.received_at * 1000).toLocaleTimeString('vi-VN') : '—';
        return `<button type="button" class="command-item${selected}" data-turn-key="${esc(key)}"><strong class="turn-command">${esc(turn.command || '(không có transcript)')}</strong><small class="turn-meta">${time} · ${esc(turn.status)} · ${duration}</small></button>`;
      }).join('');
    }
    list.scrollTop = previousScroll;
    list.querySelectorAll('[data-turn-key]').forEach(button => button.addEventListener('click', () => {
      selectedTurnId = button.dataset.turnKey;
      list.querySelectorAll('.command-item').forEach(item => item.classList.toggle('selected', item === button));
      detailSignature = '';
      updateTurnDetail();
    }));
  }
  list.querySelectorAll('[data-turn-key]').forEach(button => {
    const turn = turns.find(item => turnKey(item) === button.dataset.turnKey);
    if (!turn) return;
    const elapsed = turn.input_to_output_ms ?? turn.end_to_end_ms;
    const duration = elapsed == null ? 'đang xử lý' : `${Number(elapsed).toFixed(0)} ms`;
    const time = turn.received_at ? new Date(turn.received_at * 1000).toLocaleTimeString('vi-VN') : '—';
    button.querySelector('.turn-command').textContent = turn.command || '(không có transcript)';
    button.querySelector('.turn-meta').textContent = `${time} · ${turn.status} · ${duration}`;
    button.classList.toggle('selected', turnKey(turn) === selectedTurnId);
  });
  $('#turn-count').textContent = String(turns.length);
}

function updateTurnDetail() {
  const turn = turns.find(item => turnKey(item) === selectedTurnId);
  if (!turn) {
    $('#turn-status').textContent = 'Chọn một lượt';
    $('#turn-detail').innerHTML = '<div class="empty">Thời gian STT, pipeline, model và TTS sẽ hiện tại đây.</div>';
    detailSignature = '';
    return;
  }
  const tts = turn.tts || [];
  const pipeline = turn.timings_ms || {};
  const signature = JSON.stringify([turn.status, turn.received_at, turn.output_at, turn.end_to_end_ms, turn.input_to_output_ms, turn.input_to_audio_ready_ms, turn.stt_ms, pipeline, tts]);
  if (signature === detailSignature) return;
  detailSignature = signature;
  const modelMs = pipeline.generate ?? null;
  const classifierMs = pipeline.classify_intent ?? null;
  const stages = Object.entries(pipeline).filter(([name]) => name !== 'total')
    .map(([name, value]) => `<div class="row"><span>Pipeline · ${esc(name)}</span><strong>${Number(value).toFixed(1)} ms</strong></div>`).join('');
  const ttsRows = tts.length ? tts.map((part, index) => `<div class="row"><span>TTS ${index + 1} · first chunk</span><strong>${part.time_to_first_chunk_ms ?? '—'} ms</strong></div><div class="row"><span>TTS ${index + 1} · synthesis</span><strong>${part.synthesis_ms ?? '—'} ms</strong></div><div class="row"><span>TTS ${index + 1} · audio duration</span><strong>${part.audio_duration_ms ?? '—'} ms</strong></div>`).join('') : '<div class="row"><span>TTS</span><strong>Không phát TTS trong lượt này</strong></div>';
  $('#turn-status').textContent = `${turn.status} · ${turn.end_to_end_ms == null ? 'đang xử lý' : `${turn.end_to_end_ms} ms`}`;
  $('#turn-detail').innerHTML = `<div class="row"><span>Lệnh</span><strong>${esc(turn.command)}</strong></div><div class="row"><span>Thời điểm nhận</span><strong>${turn.received_at ? new Date(turn.received_at * 1000).toLocaleTimeString('vi-VN') : '—'}</strong></div><div class="row"><span>Input → output</span><strong>${turn.input_to_output_ms != null ? `${turn.input_to_output_ms} ms (STT + pipeline)` : turn.end_to_end_ms == null ? '—' : `${turn.end_to_end_ms} ms (pipeline API)`}</strong></div><div class="row"><span>Input → TTS audio sẵn sàng</span><strong>${turn.input_to_audio_ready_ms == null ? 'TTS chưa phát/hoàn tất' : `${turn.input_to_audio_ready_ms} ms`}</strong></div><div class="row"><span>STT</span><strong>${turn.stt_ms == null ? 'Không có audio STT' : `${turn.stt_ms} ms`}</strong></div><div class="row"><span>Pipeline tổng</span><strong>${pipeline.total == null ? '—' : `${pipeline.total} ms`}</strong></div><div class="row"><span>Model generate</span><strong>${modelMs == null ? 'Không gọi generate' : `${modelMs} ms`}</strong></div><div class="row"><span>Model classify</span><strong>${classifierMs == null ? '—' : `${classifierMs} ms`}</strong></div>${stages}${ttsRows}`;
}

function updateTurns(item) {
  turns = (item.history || []).filter(turn => !resetAt || !turn.received_at || turn.received_at * 1000 >= resetAt);
  if (!turns.some(turn => turnKey(turn) === selectedTurnId)) {
    selectedTurnId = turns.length ? turnKey(turns[turns.length - 1]) : null;
    listSignature = '';
    detailSignature = '';
  }
  updateTurnList();
  updateTurnDetail();
}

async function poll() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const response = await fetch('/api/v1/metrics', { cache: 'no-store' });
    if (!response.ok) throw new Error(`metrics returned ${response.status}`);
    const item = await response.json();
    samples.push(compactSample(item));
    samples.splice(0, Math.max(0, samples.length - MAX_SAMPLES));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(samples));
    updateHardware(item);
    updateRetainedCommands(Boolean(item.storage?.transcripts));
    updateTurns(item);
    drawChart();
  } catch {
    $('#updated').textContent = 'Backend offline';
  } finally {
    pollInFlight = false;
  }
}

$('#reset').addEventListener('click', () => {
  samples.length = 0;
  turns = [];
  selectedTurnId = null;
  listSignature = '';
  detailSignature = '';
  resetAt = Date.now();
  localStorage.setItem(STORAGE_KEY, '[]');
  localStorage.setItem(RESET_KEY, String(resetAt));
  localStorage.removeItem('vivi-command-history');
  $('#retained-command-list').innerHTML = '<div class="empty">Chưa có lệnh được lưu.</div>';
  updateTurnList();
  updateTurnDetail();
  drawChart();
});
$('#metric').addEventListener('change', drawChart);
window.addEventListener('resize', drawChart);
poll();
window.setInterval(poll, 1000);
