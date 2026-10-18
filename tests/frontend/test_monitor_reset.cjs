const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('web/monitor.js', 'utf8');

function setup(fetch) {
  const nodes = new Map();
  const values = new Map([['vivi-session-id', 'user'], ['vivi-command-history', '["old"]']]);
  const context = vm.createContext({
    fetch, Date, JSON, Error,
    localStorage: {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)},
    $: key => {
      if (!nodes.has(key)) nodes.set(key, {addEventListener: (_, handler) => nodes.get(key).click = handler});
      return nodes.get(key);
    },
    updateTurnList() {}, updateTurnDetail() {}, drawChart() {}, compactSample: item => item,
    setConnection() {}, updateHardware() {}, updateRetainedCommands() {}, updateTurns() {},
  });
  vm.runInContext(`let samples = [1], turns = [1], selectedTurnId = 'old', listSignature = '', detailSignature = '', resetAt = 0;
    let pollInFlight = false, resetInFlight = false, resetGeneration = 0;
    const STORAGE_KEY = 'samples', RESET_KEY = 'reset', MAX_SAMPLES = 240;
    ${source.slice(source.indexOf('async function poll()'), source.indexOf("$('#metric').addEventListener"))}`, context);
  return {nodes, values, context, click: () => nodes.get('#reset').click()};
}

test('monitor waits for backend deletion before clearing local history', async () => {
  let resolve;
  let request;
  const view = setup((url, options) => {
    request = {url, options};
    return new Promise(done => resolve = done);
  });
  const pending = view.click();
  assert.equal(view.values.get('vivi-command-history'), '["old"]');
  assert.equal(view.nodes.get('#reset').disabled, true);
  assert.equal(request.url, '/api/v1/history/reset');
  assert.equal(JSON.parse(request.options.body).session_id, 'user');
  resolve({ok: true, json: async () => ({deleted_turns: 8})});
  await pending;
  assert.equal(view.values.has('vivi-command-history'), false);
  assert.ok(view.values.has('vivi-history-reset'));
  assert.equal(view.nodes.get('#reset').disabled, false);
});

test('busy backend preserves history and shows the failure', async () => {
  const view = setup(async () => ({ok: false, json: async () => ({detail: 'Đợi lượt đang xử lý'})}));
  await view.click();
  assert.equal(view.values.get('vivi-command-history'), '["old"]');
  assert.match(view.nodes.get('#updated').textContent, /Đợi lượt/);
  assert.equal(view.values.has('vivi-history-reset'), false);
});

test('metrics requested before reset cannot restore old samples', async () => {
  let resolveMetrics;
  const view = setup(url => url.endsWith('/metrics') ? new Promise(done => resolveMetrics = done)
    : Promise.resolve({ok: true, json: async () => ({deleted_turns: 1})}));
  const poll = vm.runInContext('poll()', view.context);
  await view.click();
  resolveMetrics({ok: true, json: async () => ({history: [{command: 'old'}]})});
  await poll;
  assert.equal(vm.runInContext('samples.length', view.context), 0);
});
