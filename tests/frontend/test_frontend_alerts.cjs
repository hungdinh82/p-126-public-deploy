const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

const source = readFileSync('app.js', 'utf8');

test('active alerts render and announce even if navigation synchronization fails', async () => {
  const rendered = [], announced = [];
  const sandbox = {
    API_BASE: '',
    sessionId: 'driver',
    state: { backendAvailable: true },
    encodeURIComponent,
    fetch: async () => ({
      ok: true,
      json: async () => ({
        vehicle_state: { battery_percent: 15 },
        active_alerts: [{ alert_id: 'battery-low', message: 'Pin xe còn 15 phần trăm.' }],
      }),
    }),
    applyBackendState: state => rendered.push(['state', state]),
    renderAlerts: snapshot => rendered.push(['alerts', snapshot.active_alerts]),
    announceNewAlerts: alerts => announced.push(alerts),
    synchronizeNavigation: async () => { throw new Error('navigation unavailable'); },
  };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(source.indexOf('async function pollVehicleAlerts'), source.indexOf('function highlight')), sandbox);

  await sandbox.pollVehicleAlerts();

  assert.deepEqual(rendered, [
    ['state', { battery_percent: 15 }],
    ['alerts', [{ alert_id: 'battery-low', message: 'Pin xe còn 15 phần trăm.' }]],
  ]);
  assert.deepEqual(announced, [[{ alert_id: 'battery-low', message: 'Pin xe còn 15 phần trăm.' }]]);
});
