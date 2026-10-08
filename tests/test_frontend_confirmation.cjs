const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');

const app = readFileSync(join(__dirname, '..', 'app.js'), 'utf8');

test('vehicle confirmation stays in the voice and chat conversation', () => {
  assert.doesNotMatch(app, /window\.confirm\s*\(/);
  assert.match(app, /payload\.status === 'confirmation_required' \? 'confirm'/);
  assert.match(app, /confirm: 'Mình chờ bạn xác nhận'/);
});

test('an explicit body control click approves only its bound action', () => {
  const controls = app.slice(
    app.indexOf("$('#command-form').addEventListener"),
    app.indexOf('async function setDemoDriving')
  );
  assert.match(app, /async function runBackendCommand\(command, turnId = makeId\(\), \{ autoConfirm = false \} = \{\}\)/);
  assert.match(app, /autoConfirm && payload\.status === 'confirmation_required'/);
  assert.match(app, /\/api\/v1\/confirmations\/\$\{payload\.confirmation\.confirmation_id\}/);
  assert.equal((controls.match(/\{ autoConfirm: true \}/g) || []).length, 4);
  assert.match(app, /\$\('#command-form'\)[^\n]+runCommand\(\$\('#command-input'\)\.value\)/);
});
