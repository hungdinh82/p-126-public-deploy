// "ViVi ơi" / "Hey ViVi" wake word, detected in the browser: no audio leaves the
// device until the phrase is heard. It is openWakeWord's streaming front end
// (melspectrogram → speech embedding) followed by the trained vivi.onnx head,
// run with onnxruntime-web; scripts/train_wake.py produces the head.

const ORT_VERSION = '1.20.1';
const ORT_BASE = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/`;
const RATE = 16000;
const CHUNK = 1280;            // 80 ms: one embedding frame
const MEL_CONTEXT = 160 * 3;   // extra samples the melspectrogram needs before a chunk
const MEL_WINDOW = 76;         // mel frames per embedding
const FEATURES = 16;           // embedding frames per classifier input (1.28 s)

let ortLoading = null;
function loadOrt() {
  ortLoading ||= new Promise((resolve, reject) => {
    if (window.ort) return resolve(window.ort);
    const script = Object.assign(document.createElement('script'), { src: `${ORT_BASE}ort.min.js`, async: true });
    script.onload = () => {
      window.ort.env.wasm.wasmPaths = ORT_BASE;
      // One thread: threaded WASM needs cross-origin isolation the page does not have.
      window.ort.env.wasm.numThreads = 1;
      resolve(window.ort);
    };
    script.onerror = () => reject(new Error('không tải được onnxruntime-web'));
    document.head.append(script);
  });
  return ortLoading;
}

/** Scores 80 ms chunks of 16 kHz int16 audio; one score per chunk once 1.3 s has been heard. */
export class WakeDetector {
  static async load(base = './wake/') {
    const ort = await loadOrt();
    const session = name => ort.InferenceSession.create(`${base}${name}`, { executionProviders: ['wasm'] });
    const [mel, embedding, head] = await Promise.all(['melspectrogram.onnx', 'embedding_model.onnx', 'vivi.onnx'].map(session));
    return new WakeDetector(ort, mel, embedding, head);
  }

  constructor(ort, mel, embedding, head) {
    Object.assign(this, { ort, mel, embedding, head });
    this.reset();
  }

  reset() {
    this.raw = new Float32Array(CHUNK + MEL_CONTEXT);
    // openWakeWord starts its mel buffer with ones; matching it keeps the first frames comparable.
    this.mels = Array.from({ length: MEL_WINDOW }, () => new Float32Array(32).fill(1));
    this.features = [];
  }

  async push(chunk) {
    const { ort } = this;
    this.raw.copyWithin(0, CHUNK);
    this.raw.set(chunk, MEL_CONTEXT);
    const melOut = await this.mel.run({ input: new ort.Tensor('float32', this.raw.slice(), [1, this.raw.length]) });
    const mel = Object.values(melOut)[0].data;
    for (let frame = 0; frame < mel.length / 32; frame++) {
      this.mels.push(mel.slice(frame * 32, frame * 32 + 32).map(value => value / 10 + 2));
    }
    this.mels.splice(0, this.mels.length - MEL_WINDOW);
    const window = new Float32Array(MEL_WINDOW * 32);
    this.mels.forEach((frame, index) => window.set(frame, index * 32));
    const embeddingOut = await this.embedding.run({ input_1: new ort.Tensor('float32', window, [1, MEL_WINDOW, 32, 1]) });
    this.features.push(Float32Array.from(Object.values(embeddingOut)[0].data));
    this.features.splice(0, this.features.length - FEATURES);
    if (this.features.length < FEATURES) return 0;
    const input = new Float32Array(FEATURES * 96);
    this.features.forEach((frame, index) => input.set(frame, index * 96));
    const scoreOut = await this.head.run({ x: new ort.Tensor('float32', input, [1, FEATURES, 96]) });
    return Object.values(scoreOut)[0].data[0];
  }
}

// Resamples the microphone to 16 kHz (box filter, enough below 8 kHz for the mel
// front end) and posts 80 ms int16 chunks to the page.
const TAP = `
class WakeTap extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.step = options.processorOptions.rate / ${RATE};
    this.chunk = new Int16Array(${CHUNK}); this.filled = 0;
    this.sum = 0; this.count = 0; this.edge = this.step;
  }
  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      this.sum += input[i]; this.count++;
      if (this.count >= this.edge) {
        const value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.chunk[this.filled++] = value * 32767;
        this.edge += this.step - this.count; this.sum = 0; this.count = 0;
        if (this.filled === ${CHUNK}) { this.port.postMessage(this.chunk.slice()); this.filled = 0; }
      }
    }
    return true;
  }
}
registerProcessor('wake-tap', WakeTap);`;

/**
 * Listens on the microphone and calls onWake when the phrase is heard.
 * canListen() gates detection (busy turn, recording, ViVi speaking); a short
 * quiet period after it turns true keeps ViVi's own voice tail from waking it.
 */
export async function startWakeWord({ base, threshold = .6, cooldown = 2000, canListen = () => true, onWake, onScore }) {
  const detector = await WakeDetector.load(base);
  // Same processing as the recorder and record.html, so the model hears what it was given.
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  const context = new AudioContext();
  await context.audioWorklet.addModule(URL.createObjectURL(new Blob([TAP], { type: 'text/javascript' })));
  const source = context.createMediaStreamSource(stream);
  const tap = new AudioWorkletNode(context, 'wake-tap', { processorOptions: { rate: context.sampleRate } });
  source.connect(tap);
  let quietUntil = performance.now() + 1500, running = true, queue = Promise.resolve(), backlog = 0;
  tap.port.onmessage = ({ data }) => {
    // Drop audio rather than fall behind on a slow device: a late wake is worse than a missed one.
    if (backlog > 6) return;
    backlog++;
    queue = queue.then(async () => {
      const score = await detector.push(data);
      backlog--;
      if (!running) return;
      onScore?.(score);
      const now = performance.now();
      if (!canListen()) { quietUntil = now + 1000; return; }
      if (score >= threshold && now >= quietUntil) {
        quietUntil = now + cooldown;
        onWake(score);
      }
    }).catch(() => { backlog--; });
  };
  // Browsers start audio suspended until the page has been touched.
  const resume = () => context.state === 'suspended' && context.resume();
  for (const type of ['pointerdown', 'keydown']) document.addEventListener(type, resume);
  resume();
  return {
    get suspended() { return context.state === 'suspended'; },
    stop() {
      running = false;
      for (const type of ['pointerdown', 'keydown']) document.removeEventListener(type, resume);
      stream.getTracks().forEach(track => track.stop());
      context.close();
    },
  };
}
