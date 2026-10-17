const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const context = vm.createContext({TextDecoder, Blob, $: () => ({})});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../jetson_ai/static/streaming.js'), 'utf8'), context);

function response(chunks, beforeRead = () => {}) {
  let index = 0;
  return {body: {getReader: () => ({
    async read() {beforeRead(index); return index < chunks.length ? {value: chunks[index++], done: false} : {done: true};},
    async cancel() {}, releaseLock() {},
  })}};
}

test('SSE renders complete events before EOF and preserves Vietnamese split across network bytes', async () => {
  const data = new TextEncoder().encode('event: delta\r\ndata: {"text":"Xin chào"}\r\n\r\nevent: done\ndata: {}\n\n');
  const frames = [];
  await context.readSSE(response([...data].map(byte => Uint8Array.of(byte))), (event, raw) => {
    frames.push([event, JSON.parse(raw)]);
  });
  assert.equal(frames[0][0], 'delta');
  assert.equal(frames[0][1].text, 'Xin chào');
  assert.equal(frames[1][0], 'done');
  let received = false;
  await context.readSSE(response([new TextEncoder().encode('data: {}\n\n')], index => {
    if (index === 1) assert.equal(received, true);
  }), () => {received = true;});
});

test('SSE rejects truncated frames instead of reporting success', async () => {
  await assert.rejects(context.readSSE(response([new TextEncoder().encode('data: {"text":')]), () => {}), /ngắt/);
});

test('PCM replay WAV preserves streamed bytes and sample rate', async () => {
  const bytes = new Uint8Array(await context.pcmWav(Uint8Array.of(1, 0, 255, 127), 48000).arrayBuffer());
  const view = new DataView(bytes.buffer);
  assert.equal(view.getUint32(24, true), 48000);
  assert.equal(view.getUint32(40, true), 4);
  assert.deepEqual([...bytes.slice(44)], [1, 0, 255, 127]);
});
