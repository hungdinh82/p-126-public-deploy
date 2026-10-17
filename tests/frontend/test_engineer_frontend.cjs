const assert = require('node:assert/strict');
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const vm = require('node:vm');
const js = fs.readFileSync('engineer.js', 'utf8');
assert.match(js, /escapeHtml/);
assert.match(js, /textContent/);
assert.doesNotMatch(js, /innerHTML\s*=\s*[^;]*\bpayload\b/);
execFileSync(process.execPath, ['--check', 'engineer.js']);

let renderedHtml = '';
const routeFlow = { scrollLeft: 0 };
const target = {
  classList: { remove() {} },
  querySelector(selector) {
    return selector === '.route-flow' && renderedHtml.includes('class="route-flow"') ? routeFlow : null;
  },
  get innerHTML() { return renderedHtml; },
  set innerHTML(value) {
    renderedHtml = value;
    routeFlow.scrollLeft = 0;
  },
};
const routeFixture = {
  routes: [{
    transcript: 'tính năng adas của xe',
    turn_id: 'turn-route',
    status: 'verified',
    effective_provider: 'openrouter',
    llm_attempts: [
      { phase: 'interpret', attempt: 1, provider: 'openrouter', location: 'cloud', status: 'served', served_model: 'qwen' },
      { phase: 'generate', attempt: 1, provider: 'openrouter', location: 'cloud', status: 'served', served_model: 'qwen' },
    ],
    stages: {
      tools: { provider: 'manual.search', location: 'laptop', status: 'served' },
      rag: { provider: 'sqlite', location: 'laptop', status: 'served', detail: '3 nguồn' },
      grounding: { provider: 'citation-validator', location: 'laptop', status: 'served', detail: '3 nguồn đã kiểm chứng' },
      response: { provider: 'turn-response', location: 'laptop', status: 'verified' },
      tts: { provider: 'vieneu', voice: 'Tưởng Vy', location: 'cloud', status: 'served' },
      output: { provider: 'driver-ui', location: 'laptop', status: 'verified' },
    },
  }],
};
const sandbox = {
  document: { querySelector: (selector) => selector === '#ai-route' ? target : null },
  routeFixture,
};
vm.createContext(sandbox);
const renderSource = js.slice(0, js.indexOf('async function refreshAiRoutes'));
vm.runInContext(`${renderSource}\nrenderAiRoutes(routeFixture);`, sandbox);

const orderedLabels = [
  'Điều phối',
  'LLM hiểu lệnh #1',
  'Tools',
  'RAG',
  'LLM sinh câu trả lời #1',
  'Kiểm chứng nguồn',
  'Response',
];
let previousIndex = -1;
for (const label of orderedLabels) {
  const index = target.innerHTML.indexOf(label);
  assert.ok(index > previousIndex, `${label} phải đứng sau stage trước đó`);
  previousIndex = index;
}
assert.match(target.innerHTML, /route-delivery/);
assert.match(target.innerHTML, /TTS/);
assert.match(target.innerHTML, /vieneu · Tưởng Vy/);
assert.doesNotMatch(target.innerHTML, /TTS[\s\S]*?model chưa được response xác nhận[\s\S]*?Driver UI/);
assert.match(target.innerHTML, /Driver UI/);

routeFlow.scrollLeft = 240;
vm.runInContext('renderAiRoutes(routeFixture);', sandbox);
assert.equal(routeFlow.scrollLeft, 240, 'refresh route không được kéo thanh cuộn ngang về đầu');
