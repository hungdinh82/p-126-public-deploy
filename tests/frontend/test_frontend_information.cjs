const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = readFileSync('web/app.js', 'utf8');

function harness() {
  const classes = new Set();
  const container = { items: [], hidden: true, replaceChildren() { this.items = []; },
    append(item) { this.items.push(item); }, get childElementCount() { return this.items.length; } };
  const response = { classList: { toggle(name, on) { on ? classes.add(name) : classes.delete(name); } } };
  const sandbox = { URL, document: { createElement: () => ({}) }, $: selector => selector === '#response' ? response : container };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(source.indexOf('function renderAnswerSources'), source.indexOf('function renderLiveCaption')), sandbox);
  return { sandbox, container, classes };
}

test('web sources render as safe links and untrusted titles stay literal text', () => {
  const h = harness();
  h.sandbox.evidence = [{ source_url: 'https://example.org/place', title: '<img src=x onerror=alert(1)>', valid_date: '2026-10-12' },
    { source_url: 'javascript:alert(1)', title: 'unsafe' }];
  vm.runInContext("renderAnswerSources(evidence, 'web')", h.sandbox);
  assert.equal(h.container.items.length, 1);
  assert.equal(h.container.items[0].textContent, '<img src=x onerror=alert(1)> · 2026-10-12');
  assert.equal(h.container.items[0].rel, 'noopener noreferrer');
  assert.equal(h.container.hidden, false);
  assert.equal(h.classes.has('expanded-answer'), true);
});

test('starting a new answer removes previous sources and expanded layout', () => {
  const h = harness();
  vm.runInContext("renderAnswerSources([{source_url:'https://example.org/', title:'Old'}], 'creative'); renderAnswerSources()", h.sandbox);
  assert.equal(h.container.hidden, true);
  assert.equal(h.container.items.length, 0);
  assert.equal(h.classes.has('expanded-answer'), false);
});
