const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('app.js', 'utf8');

test('barge-in aborts the old request, cancels backend turn, and unlocks microphone', async () => {
  const requests = [];
  let stopped = 0, unlocked = false, speechCancelled = 0;
  const sandbox = {
    state: {busy: true}, AbortController,
    window: {speechSynthesis: {cancel: () => speechCancelled++}},
    speechSynthesis: {cancel: () => speechCancelled++},
    stopPlayback: () => stopped++, lockControls: value => {unlocked = !value;},
    API_BASE: 'http://test', sessionId: 'driver',
    fetch: (url, options) => {requests.push({url, body: JSON.parse(options.body)}); return Promise.resolve({});}
  };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(source.indexOf('let activeTurn = null;'), source.indexOf('async function runBackendCommand')), sandbox);
  vm.runInContext("globalThis.old = {turnId: 'turn/one', controller: new AbortController()}; activeTurn = old; interruptActiveTurn(); globalThis.active = activeTurn;", sandbox);
  assert.equal(sandbox.old.controller.signal.aborted, true);
  assert.equal(sandbox.active, null);
  assert.equal(sandbox.state.busy, false);
  assert.equal(stopped, 1);
  assert.equal(speechCancelled, 1);
  assert.equal(unlocked, true);
  assert.equal(requests[0].url, 'http://test/api/v2/turns/turn%2Fone/cancel');
  assert.deepEqual(requests[0].body, {session_id: 'driver'});
});
