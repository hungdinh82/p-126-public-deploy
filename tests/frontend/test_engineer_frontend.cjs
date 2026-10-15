const assert = require('node:assert/strict');
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const js = fs.readFileSync('engineer.js', 'utf8');
assert.match(js, /escapeHtml/);
assert.match(js, /textContent/);
assert.doesNotMatch(js, /innerHTML\s*=\s*[^;]*\bpayload\b/);
execFileSync(process.execPath, ['--check', 'engineer.js']);
