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
  assert.equal(requests[0].url, 'http://test/api/v1/turns/turn%2Fone/cancel');
  assert.deepEqual(requests[0].body, {session_id: 'driver'});
});

test('old speech waits during upload and STT, then only the newest revision plays', async () => {
  const played = [];
  const sandbox = {
    state: {busy: false}, recorder: null,
    localStorage: {getItem: () => null, setItem: () => {}}, played,
  };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(source.indexOf('let activeTurn = null;'),
                              source.indexOf('let currentNavigationRevision')), sandbox);
  vm.runInContext(`
    presentVoiceResponse = async payload => played.push(payload.turn_id);
    spokenTurnQueue.push({id:'old-input', ids:['old-input'], revision:1,
                          response:{turn_id:'old', response_revision:1}});
    pendingVoiceUploads = 1;
  `, sandbox);
  await vm.runInContext('drainSpokenTurns()', sandbox);
  assert.deepEqual(played, []);
  vm.runInContext(`
    pendingVoiceUploads = 0;
    voiceInputJobs.set('new-input', {transcript:false});
  `, sandbox);
  await vm.runInContext('drainSpokenTurns()', sandbox);
  assert.deepEqual(played, []);
  vm.runInContext(`
    voiceInputJobs.get('new-input').transcript = true;
    latestVoiceRevision = 2;
    spokenTurnQueue.push({id:'new-input', ids:['new-input'], revision:2,
                          response:{turn_id:'new', response_revision:2}});
  `, sandbox);
  await vm.runInContext('drainSpokenTurns()', sandbox);
  assert.deepEqual(played, ['new']);
});
