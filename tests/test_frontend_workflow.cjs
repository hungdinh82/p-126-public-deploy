const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = readFileSync('app.js', 'utf8');

function harness(events) {
  const requests = [], speech = [], phases = [], container = {hidden: true, items: [], replaceChildren() {this.items = [];}, append(item) {this.items.push(item);}};
  const final = {status: 'confirmation_required', message: 'Chờ xác nhận cửa kính', route: 'vehicle', vehicle_state: {}, diagnostics: {effective_provider: 'rules', observation_available: false}, steps: [
    {capability: 'media.play', status: 'completed', result: {message: 'Đã bật nhạc'}},
    {capability: 'window.set_position', status: 'waiting_confirmation'},
    {capability: 'door.set_open', status: 'pending'}
  ]};
  const sandbox = { AbortController, TextDecoder, Uint8Array,
    state: {busy: false, sound: false, llmProvider: 'google'}, API_BASE: 'http://test', sessionId: 'driver', DOCK_BY_INTENT: {},
    document: {createElement: () => ({}), body: {classList: {remove() {}}}},
    $: selector => selector === '#turn-steps' ? container : {value: ''},
    handleSpeakerCommand: () => false, rememberEdgeCommand() {}, lockControls() {},
    setPhase: (...args) => phases.push(args), updateModelStatus() {}, say() {},
    applyBackendState() {throw new Error('No new observation must preserve UI car state');},
    speak: async text => speech.push(text), wait: async () => {},
    fetch: async (url, options) => {
      requests.push(JSON.parse(options.body));
      let sent = false;
      return {ok: true, body: {getReader: () => ({read: async () => {
        if (sent) return {done: true};
        sent = true;
        return {done: false, value: new TextEncoder().encode([...events, {type: 'final', turn_id: 'new', sequence: 3, response: final}].map(e => JSON.stringify(e)).join('\n') + '\n')};
      }})}};
    }
  };
  vm.createContext(sandbox);
  vm.runInContext('let activeTurn = null;\n' + source.slice(source.indexOf('async function runBackendCommand'), source.indexOf('function runCommand')), sandbox);
  return {sandbox, requests, speech, phases, container};
}

test('typed control reaches backend intact and partial steps stay visible', async () => {
  const h = harness([{type: 'provider', provider: 'rules', turn_id: 'new', sequence: 1}]);
  await vm.runInContext("runBackendCommand('Mở kính', 'new', {action: {capability: 'window.set_position', arguments: {zone: 'driver', position_percent: 50}}})", h.sandbox);
  assert.deepEqual(h.requests[0].action, {capability: 'window.set_position', arguments: {zone: 'driver', position_percent: 50}});
  assert.equal(h.container.hidden, false);
  assert.match(h.container.items[0].textContent, /Đã xong: Đã bật nhạc/);
  assert.match(h.container.items[1].textContent, /Chờ xác nhận/);
  assert.match(h.container.items[2].textContent, /Chưa chạy/);
  assert.equal(h.sandbox.state.effectiveProvider, 'rules');
});

test('old turn speech and duplicate sequence are ignored after barge-in', async () => {
  const h = harness([
    {type: 'speech', text: 'OLD AUDIO', turn_id: 'old', sequence: 1},
    {type: 'provider', provider: 'rules', turn_id: 'new', sequence: 2},
    {type: 'speech', text: 'DUPLICATE AUDIO', turn_id: 'new', sequence: 1}
  ]);
  await vm.runInContext("runBackendCommand('Mở kính', 'new')", h.sandbox);
  assert.deepEqual(h.speech, ['Chờ xác nhận cửa kính']);
});
