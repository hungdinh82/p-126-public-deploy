const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
const format = (value) => value === null || value === undefined ? 'Chưa đo' : escapeHtml(value);
let overviewData;

async function api(path, options) {
  const response = await fetch(`/api/v1/engineer${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(options?.headers || {}) } });
  if (response.status === 403) { window.location.assign('/'); throw new Error('Phiên demo đã hết hạn.'); }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || 'Không thể tải dữ liệu.');
  return payload;
}
function setNotice(message) { $('#notice').textContent = message; }
function card(label, value) { return `<article class="card"><small>${escapeHtml(label)}</small><strong>${format(value)}</strong></article>`; }
function renderOverview(data) {
  overviewData = data;
  const health = data.health; const metrics = data.metrics;
  $('#overview').classList.remove('loading');
  $('#overview').innerHTML = [
    card('Gateway', `${data.connectivity.effective.mode} · Internet ${data.connectivity.effective.internet} · Jetson ${data.connectivity.effective.jetson}`),
    card('STT', `${health.stt.provider} · ${health.stt.device || 'device n/a'} · ${health.stt.available ? 'ready' : 'offline'}`),
    card('LLM', `${health.llm.provider} · ${health.llm.options.find((item) => item.provider === health.llm.provider)?.model || 'n/a'}`),
    card('TTS', `${health.tts.provider} · ${health.tts.voice || 'n/a'} · ${health.tts.device || 'n/a'}`),
    card('RAG', `${health.orchestration.retrieval_mode} · handbook ${health.orchestration.handbook_available ? 'ready' : 'unavailable'} · ${health.orchestration.local_only ? 'local-only' : 'network allowed'}`),
    card('Vehicle', `${health.vehicle.provider} · ${health.vehicle.vehicle_id} · ${health.vehicle.connected ? 'connected' : 'offline'}`),
    card('Uptime', `${metrics.uptime_seconds}s · CPU ${metrics.host.cpu_count} · RAM ${metrics.memory.percent ?? 'n/a'}%`),
    card('Device', `${metrics.device.kind} · ${metrics.device.name}`),
    card('Pipeline gần nhất', metrics.pipeline.status),
  ].join('');
  const banner = $('#connectivity-banner');
  banner.hidden = !data.connectivity.simulated;
  banner.className = data.connectivity.simulated ? 'panel' : '';
  banner.textContent = data.connectivity.simulated
    ? `ĐANG GIẢ LẬP: ${data.connectivity.override} · trạng thái thực Internet ${data.connectivity.actual.internet}, Jetson ${data.connectivity.actual.jetson}`
    : '';
  const history = metrics.history.slice(-5).reverse().map((item) => `<div class="history"><b>${format(item.status)}</b> · ${format(item.end_to_end_ms)} ms <span class="muted">${format(item.grounding_status || 'grounding n/a')}</span></div>`).join('') || '<p class="muted">Chưa có lượt chạy. Eval intent accuracy: Chưa đo.</p>';
  $('#pipeline').classList.remove('loading');
  $('#pipeline').innerHTML = `<p>End-to-end: <b>${format(metrics.pipeline.end_to_end_ms)} ms</b> · STT: <b>${format(metrics.history.at(-1)?.stt_ms)} ms</b> · TTS first chunk: <b>${format(metrics.tts.time_to_first_chunk_ms)} ms</b></p><p>Stages: ${format(JSON.stringify(metrics.pipeline.timings_ms || {}))}</p>${history}`;
}
function routeModel(item) {
  if (item.provider === 'rules') return 'không gọi model';
  if (item.provider === 'orchestrator') return 'code router';
  if (item.served_model) return escapeHtml(item.served_model);
  if (item.status === 'calling' && item.requested_model) return `${escapeHtml(item.requested_model)} <span class="model-proof">đang yêu cầu</span>`;
  return '<span class="model-unconfirmed">model chưa được response xác nhận</span>';
}
function serviceNode(label, item) {
  if (!item) return '';
  const detail = item.detail ? ` · ${escapeHtml(item.detail)}` : '';
  return `<div class="route-node ${escapeHtml(item.status || 'unknown')}"><small>${escapeHtml(label)}</small><b>${escapeHtml(item.provider || 'unknown')}</b><span>${escapeHtml(item.location || 'unknown')} · ${escapeHtml(item.status || 'unknown')}${detail}</span></div>`;
}
function routeNode(label, item) {
  if (!item) return '';
  const latency = item.latency_ms === null || item.latency_ms === undefined ? '' : ` · ${escapeHtml(item.latency_ms)} ms`;
  const requested = item.served_model && item.requested_model && item.served_model !== item.requested_model
    ? `<div class="route-requested">Yêu cầu: ${escapeHtml(item.requested_model)}</div>` : '';
  return `<div class="route-node ${escapeHtml(item.status || 'unknown')}"><small>${escapeHtml(label)}</small><b>${escapeHtml(item.provider || 'unknown')} · ${routeModel(item)}</b><span>${escapeHtml(item.location || 'unknown')} · ${escapeHtml(item.status || 'unknown')}${latency}</span>${requested}</div>`;
}
function ttsNode(item) {
  if (!item) return '';
  const voice = item.voice ? ` · ${escapeHtml(item.voice)}` : '';
  const latency = item.latency_ms === null || item.latency_ms === undefined ? '' : ` · ${escapeHtml(item.latency_ms)} ms`;
  return `<div class="route-node ${escapeHtml(item.status || 'unknown')}"><small>TTS</small><b>${escapeHtml(item.provider || 'unknown')}${voice}</b><span>${escapeHtml(item.location || 'unknown')} · ${escapeHtml(item.status || 'unknown')}${latency}</span></div>`;
}
function connectRouteNodes(nodes) {
  return nodes.filter(Boolean).join('<span class="route-arrow">→</span>');
}
function renderDelivery(stages) {
  const consumers = [
    ttsNode(stages.tts),
    stages.output ? serviceNode('Driver UI', stages.output) : '',
  ].filter(Boolean);
  if (!consumers.length) return '';
  return `<div class="route-delivery"><small>Phân phối phản hồi</small><div class="route-branches">${consumers.join('')}</div></div>`;
}
function renderAiRoutes(data) {
  const target = $('#ai-route');
  target.classList.remove('loading');
  const previousScrollLeft = target.querySelector('.route-flow')?.scrollLeft || 0;
  const route = data.routes?.[0];
  if (!route) { target.innerHTML = '<p class="muted">Chưa có lượt xử lý.</p>'; return; }
  const stages = route.stages || {};
  const attempts = route.llm_attempts || [];
  const interpretAttempts = attempts.filter((item) => item.phase !== 'generate');
  const generationAttempts = attempts.filter((item) => item.phase === 'generate');
  const interpretation = interpretAttempts.length
    ? interpretAttempts.map((item, index) => routeNode(`LLM hiểu lệnh #${index + 1}`, item))
    : [route.effective_provider === 'rules'
      ? routeNode('Hiểu lệnh', { provider: 'rules', location: 'laptop', status: 'served' })
      : routeNode('LLM hiểu lệnh', { provider: route.requested_provider || 'pending', location: 'unknown', status: route.status === 'running' ? 'calling' : 'unknown' })];
  const generation = generationAttempts.map((item, index) => routeNode(`LLM sinh câu trả lời #${index + 1}`, item));
  const coreNodes = [
    stages.stt ? routeNode('STT', stages.stt) : '',
    routeNode('Điều phối', { provider: 'orchestrator', location: 'laptop', status: route.status === 'running' ? 'calling' : 'served' }),
    ...interpretation,
    stages.tools ? serviceNode('Tools', stages.tools) : '',
    stages.rag ? serviceNode('RAG', stages.rag) : '',
    ...generation,
    stages.grounding ? serviceNode('Kiểm chứng nguồn', stages.grounding) : '',
    stages.response ? serviceNode('Response', stages.response) : '',
  ];
  const delivery = renderDelivery(stages);
  const flow = `${connectRouteNodes(coreNodes)}${delivery ? `<span class="route-arrow">→</span>${delivery}` : ''}`;
  target.innerHTML = `<div class="route-head"><div><b>${escapeHtml(route.transcript || 'Yêu cầu không có transcript')}</b><br><span class="muted">${escapeHtml(route.turn_id)} · ${escapeHtml(route.status)}</span></div><span class="pill ${route.status === 'running' ? 'live' : ''}">${route.status === 'running' ? 'đang xử lý' : escapeHtml(route.status)}</span></div><div class="route-flow">${flow}</div>`;
  target.querySelector('.route-flow').scrollLeft = previousScrollLeft;
}
async function refreshAiRoutes() { renderAiRoutes(await api('/ai-routes')); }
function renderFleet(data) {
  $('#fleet').classList.remove('loading');
  $('#fleet').innerHTML = data.vehicles.map((vehicle) => {
    const state = vehicle.state;
    if (!state) return `<article class="vehicle"><div class="vehicle-head"><h3>${escapeHtml(vehicle.vehicle_id || 'runtime vehicle')}</h3><span class="pill unavailable">unavailable</span></div><p class="source">${escapeHtml(vehicle.source)}</p><p class="muted">Không đọc được state hiện tại.</p></article>`;
    const tires = Object.entries(state.tire_pressures_kpa).map(([key, value]) => `${escapeHtml(key)} ${format(value)} kPa`).join(' · ');
    const doors = Object.entries(state.door_states).filter(([, item]) => item.open).map(([key]) => key).join(', ') || 'đã đóng';
    const alerts = vehicle.alerts.map((alert) => `${escapeHtml(alert.severity)}: ${escapeHtml(alert.code)}`).join(' · ') || 'Không có cảnh báo active';
    return `<article class="vehicle"><div class="vehicle-head"><h3>${escapeHtml(vehicle.vehicle_id)}</h3><span class="pill ${escapeHtml(vehicle.availability)}">${escapeHtml(vehicle.availability)}</span></div><p class="source">${escapeHtml(vehicle.source)} · ${vehicle.online ? 'online' : 'offline'} · stale ${format(vehicle.stale_seconds)}s</p><div class="state-grid"><span><b>Pin / range</b>${format(state.battery_percent)}% / ${format(state.range_km)} km</span><span><b>Cabin / powertrain</b>${format(state.temperature_celsius)}° / ${format(state.powertrain_temperature_celsius)}°</span><span><b>Power / cửa</b>${format(state.power_state)} / ${escapeHtml(doors)}</span><span><b>Software</b>${escapeHtml(vehicle.software_profile)}</span></div><p class="source">Lốp: ${tires}</p><p class="alerts">${alerts}</p></article>`;
  }).join('');
  $('#scenario-vehicle').innerHTML = data.vehicles.map((vehicle) => `<option value="${escapeHtml(vehicle.vehicle_id || 'demo-car-1')}">${escapeHtml(vehicle.vehicle_id || 'demo-car-1')} — ${escapeHtml(vehicle.source)}</option>`).join('');
}
function renderScenarios(data) { $('#scenarios').innerHTML = data.scenarios.map((scenario, index) => `<label><input type="radio" name="scenario" value="${escapeHtml(scenario.id)}" ${index === 0 ? 'checked' : ''}>${escapeHtml(scenario.label)}${scenario.simulated_only ? ' (simulation)' : ''}</label>`).join(''); }
function renderConnectivity(data) { $('#connectivity-scenarios').innerHTML = data.scenarios.map((scenario) => `<label><input type="radio" name="connectivity-scenario" value="${escapeHtml(scenario.id)}" ${scenario.id === data.selected ? 'checked' : ''}>${escapeHtml(scenario.label)}</label>`).join(''); }
function renderModels(data) { $('#models').classList.remove('loading'); $('#models').innerHTML = data.profiles.map((profile) => `<div class="profile"><div><b>${escapeHtml(profile.label)}</b><br><span class="muted">${escapeHtml(profile.detail)} · ${escapeHtml(profile.mode)} · ${profile.verified ? 'đã kiểm chứng' : 'Chưa kiểm chứng'}</span></div><button data-profile="${escapeHtml(profile.id)}">${profile.id === data.selected_profile ? 'Đang chọn' : 'Chọn'}</button></div>`).join(''); document.querySelectorAll('[data-profile]').forEach((button) => button.addEventListener('click', async () => { try { await api('/models/apply', { method: 'POST', body: JSON.stringify({ profile_id: button.dataset.profile }) }); await refresh(); } catch (error) { setNotice(error.message); } })); }
function renderOta(data) { $('#ota-history').innerHTML = data.history.map((item) => `<div class="history"><b>${escapeHtml(item.result)}</b> · ${escapeHtml(item.states.join(' → '))} <span class="muted">simulation</span></div>`).join('') || '<p class="muted">Chưa có lượt OTA.</p>'; }
function renderAudit(data) { $('#audit').classList.remove('loading'); $('#audit').innerHTML = data.events.map((item) => `<div class="audit-row"><b>${escapeHtml(item.action)}</b> · ${escapeHtml(item.target)}<br><span class="muted">${escapeHtml(item.occurred_at)} · ${escapeHtml(item.actor)} · ${escapeHtml(item.detail)}</span></div>`).join('') || '<p class="muted">Chưa có thao tác trong phiên Engineer.</p>'; }
async function refresh() { setNotice('Đang đồng bộ runtime…'); try { const [overview, routes, fleet, scenarios, connectivity, models, ota, audit] = await Promise.all([api('/overview'), api('/ai-routes'), api('/fleet'), api('/scenarios'), api('/connectivity/scenarios'), api('/models'), api('/ota'), api('/audit')]); renderOverview(overview); renderAiRoutes(routes); renderFleet(fleet); renderScenarios(scenarios); renderConnectivity(connectivity); renderModels(models); renderOta(ota); renderAudit(audit); setNotice(`Đã đồng bộ ${new Date().toLocaleTimeString('vi-VN')}`); } catch (error) { setNotice(error.message); } }
$('#refresh').addEventListener('click', refresh);
$('#switch-role').addEventListener('click', async () => { await fetch('/api/v1/session', { method: 'DELETE' }); window.location.assign('/'); });
$('#scenario-form').addEventListener('submit', async (event) => { event.preventDefault(); const vehicleId = $('#scenario-vehicle').value; const scenario = document.querySelector('input[name="scenario"]:checked').value; try { const payload = await api(`/vehicles/${encodeURIComponent(vehicleId)}/scenario`, { method: 'POST', body: JSON.stringify({ scenario }) }); setNotice(`${payload.vehicle_id}: ${payload.scenario} (${payload.mode})`); await refresh(); } catch (error) { setNotice(error.message); } });
$('#connectivity-form').addEventListener('submit', async (event) => { event.preventDefault(); const scenario = document.querySelector('input[name="connectivity-scenario"]:checked').value; try { await api('/connectivity/scenario', { method: 'POST', body: JSON.stringify({ scenario }) }); await refresh(); } catch (error) { setNotice(error.message); } });
document.querySelectorAll('[data-ota]').forEach((button) => button.addEventListener('click', async () => { try { await api('/ota/deploy', { method: 'POST', body: JSON.stringify({ outcome: button.dataset.ota }) }); await refresh(); } catch (error) { setNotice(error.message); } }));
refresh();
setInterval(() => refreshAiRoutes().catch(() => {}), 1000);
