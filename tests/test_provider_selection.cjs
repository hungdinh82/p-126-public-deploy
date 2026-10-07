const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');

const app = readFileSync(join(__dirname, '..', 'app.js'), 'utf8');
const start = app.indexOf('function resolveLlmProvider(');
const end = app.indexOf('async function checkBackend()', start);
assert.ok(start >= 0 && end > start);
const resolve = runInNewContext(`${app.slice(start, end)}; resolveLlmProvider`);
const options = [
  { provider: 'rules', available: true },
  { provider: 'google', available: true },
  { provider: 'local', available: false },
];

test('legacy saved rules cannot mask configured Google', () => {
  assert.equal(resolve(options, 'google', 'rules', null), 'google');
});
test('changed backend default invalidates an earlier UI override', () => {
  assert.equal(resolve(options, 'google', 'rules', 'rules'), 'google');
  assert.equal(resolve(options, 'rules', 'google', 'google'), 'rules');
});
test('explicit UI override persists while backend default stays the same', () => {
  assert.equal(resolve(options, 'google', 'rules', 'google'), 'rules');
});
test('unavailable saved provider returns to configured default', () => {
  assert.equal(resolve(options, 'google', 'local', 'google'), 'google');
});
test('first visit uses backend default; unavailable default uses an available provider', () => {
  assert.equal(resolve(options, 'google', null, null), 'google');
  assert.equal(resolve(options, 'local', null, null), 'rules');
});
