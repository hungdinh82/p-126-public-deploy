// ViVi's voice mark: layered sine waves that breathe while idle, follow the
// microphone while listening, ripple while thinking and follow ViVi's own
// voice while speaking.
const LAYERS = [
  { color: '214,222,238', frequency: 1.6, speed: 1.9, amplitude: 1, offset: 0, width: 1.8 },
  { color: '150,164,198', frequency: 2.3, speed: -2.6, amplitude: .72, offset: 1.7, width: 1.3 },
  { color: '128,116,168', frequency: 3.1, speed: 3.3, amplitude: .5, offset: 3.4, width: 1.1 }
];
const WARN = '214,174,120';

export function createWave(canvas, { level = () => 0, onLevel = () => {} } = {}) {
  const ctx = canvas.getContext('2d');
  const state = { phase: 'idle', time: 0, smooth: 0, last: performance.now(), reduced: false, width: 1, height: 1 };

  function resize() {
    const box = canvas.getBoundingClientRect();
    const ratio = Math.min(devicePixelRatio || 1, 2);
    state.width = box.width || 1; state.height = box.height || 1;
    canvas.width = Math.round(state.width * ratio); canvas.height = Math.round(state.height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }
  new ResizeObserver(resize).observe(canvas);
  resize();

  function energy() {
    const { phase, time } = state;
    if (phase === 'listening' || phase === 'speaking') return .14 + state.smooth * .86;
    if (['thinking', 'transcribing', 'validating', 'acting', 'synthesizing'].includes(phase)) return .3 + Math.sin(time * 3.2) * .1;
    return .1 + Math.sin(time * 1.3) * .04;
  }

  function draw() {
    const now = performance.now();
    requestAnimationFrame(draw);
    const dt = Math.max(0, Math.min(.05, (now - state.last) / 1000));
    state.last = now;
    if (document.hidden) return;
    state.time += state.reduced ? 0 : dt;
    const raw = ['listening', 'speaking'].includes(state.phase) ? Math.max(0, Math.min(1, level(state.phase))) : 0;
    // Fast attack, slow release keeps the wave lively without flicker.
    state.smooth += (raw - state.smooth) * (raw > state.smooth ? .45 : .08);
    onLevel(state.phase === 'speaking' ? state.smooth : 0);

    const { width, height, time } = state;
    const middle = height / 2;
    const amplitude = height * .4 * (state.reduced ? .25 : energy());
    const thinking = ['thinking', 'transcribing', 'validating', 'acting', 'synthesizing'].includes(state.phase);
    const warn = state.phase === 'blocked' || state.phase === 'unverified';
    ctx.clearRect(0, 0, width, height);
    ctx.globalCompositeOperation = 'lighter';
    ctx.lineCap = 'round';
    for (const layer of LAYERS) {
      const color = warn ? WARN : layer.color;
      ctx.beginPath();
      for (let x = 0; x <= width; x += 2) {
        const u = x / width;
        // Taper both ends so the wave floats inside the capsule.
        const envelope = (1 - (2 * u - 1) ** 2) ** 2;
        // While thinking a bright packet sweeps across instead of following sound.
        const sweep = thinking ? .45 + .55 * Math.exp(-(((u - ((time * .55) % 1.4) + .2)) ** 2) / .02) : 1;
        const y = middle + Math.sin(u * Math.PI * 2 * layer.frequency + time * layer.speed + layer.offset) * amplitude * layer.amplitude * envelope * sweep;
        if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = `rgba(${color},.8)`;
      ctx.lineWidth = layer.width;
      ctx.shadowColor = `rgba(${color},.6)`;
      ctx.shadowBlur = 6;
      ctx.stroke();
    }
    ctx.shadowBlur = 0;
    ctx.globalCompositeOperation = 'source-over';
  }
  requestAnimationFrame(draw);

  return {
    setPhase(phase) { state.phase = phase; },
    setReduced(reduced) { state.reduced = reduced; }
  };
}
