// Neon border: two glowing arcs that travel around a rounded element.
// Vanilla port of the Originkit "Neon Border" reference in styleUI.
const GLOW_LAYERS = [
  { blur: 8, opacity: .5, reach: .3 },
  { blur: 15, opacity: .3, reach: .6 },
  { blur: 57, opacity: .18, reach: 1 }
];
const MAX_GLOW_BLUR = 57, MAX_GLOW_REACH = 36, ARC_SAMPLES = 24, MIN_ARC = .015;
const SLOWEST_CYCLE = 30, FASTEST_CYCLE = 4, SLOWEST_STEP = 3, FASTEST_STEP = .35;
const BAND_MASK = 'linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0)';

function rgba(hex, alpha) {
  const value = parseInt(hex.replace('#', '').padEnd(6, '0').slice(0, 6), 16);
  return `rgba(${(value >> 16) & 255},${(value >> 8) & 255},${value & 255},${Math.max(0, Math.min(1, alpha)).toFixed(3)})`;
}
function perimeterPoint(u, w, h) {
  const d = (((u % 1) + 1) % 1) * 2 * (w + h);
  if (d < w) return [d, 0];
  if (d < w + h) return [w, d - w];
  if (d < w * 2 + h) return [w - (d - w - h), h];
  return [0, h - (d - w * 2 - h)];
}
function cornerLap(k, w, h) {
  const p = 2 * (w + h);
  const at = [0, w / p, (w + h) / p, (w * 2 + h) / p];
  return Math.floor(k / 4) + at[((k % 4) + 4) % 4];
}
function perimeterAngle(u, w, h) {
  const [x, y] = perimeterPoint(u, w, h);
  return Math.atan2(x - w / 2, h / 2 - y) * 180 / Math.PI;
}
function buildArc(lap, lengthPct, w, h, color) {
  const fw = w > 0 ? w : 100, fh = h > 0 ? h : 100;
  const length = Math.max(0, Math.min(100, lengthPct));
  const span = Math.max(MIN_ARC, length / 100 * .5), solid = length / 100;
  const stops = [];
  let base = 0, previous = 0, accumulated = 0;
  for (let i = 0; i <= ARC_SAMPLES; i++) {
    const f = i / ARC_SAMPLES;
    const angle = perimeterAngle(lap + (f - .5) * span, fw, fh);
    if (i === 0) base = angle;
    else {
      let delta = angle - previous;
      while (delta > 180) delta -= 360;
      while (delta < -180) delta += 360;
      accumulated += delta;
    }
    previous = angle;
    const t = Math.abs(f - .5) * 2;
    const k = solid >= 1 ? 1 : t <= solid ? 1 : 1 - (t - solid) / (1 - solid);
    stops.push(`${rgba(color, k * k * (3 - 2 * k))} ${accumulated.toFixed(2)}deg`);
  }
  stops.push(`${rgba(color, 0)} ${accumulated.toFixed(2)}deg`, `${rgba(color, 0)} 360deg`);
  return `conic-gradient(from ${base.toFixed(2)}deg at 50% 50%, ${stops.join(', ')})`;
}
function bezier([x1, y1, x2, y2]) {
  const curve = (a, b, t) => { const u = 1 - t; return 3 * u * u * t * a + 3 * u * t * t * b + t * t * t; };
  return (t) => {
    const x = Math.max(0, Math.min(1, t));
    let s = x;
    for (let i = 0; i < 8; i++) {
      const u = 1 - s;
      const dx = 3 * u * u * x1 + 6 * u * s * (x2 - x1) + 3 * s * s * (1 - x2);
      if (Math.abs(dx) < 1e-6) break;
      s = Math.max(0, Math.min(1, s - (curve(x1, x2, s) - x) / dx));
    }
    return curve(y1, y2, s);
  };
}
const stepEase = bezier([.72, .16, .18, 1.05]);
const glideEase = bezier([.65, 0, .35, 1]);

function band(element, inset, padding, radius) {
  Object.assign(element.style, {
    position: 'absolute', inset: `${inset}px`, boxSizing: 'border-box', padding: `${padding}px`,
    borderRadius: `${radius}px`, background: 'var(--arc)',
    webkitMask: BAND_MASK, webkitMaskComposite: 'xor', mask: BAND_MASK, maskComposite: 'exclude'
  });
  return element;
}

export function neonBorder(host, options = {}) {
  const settings = { color: '#B4C0D8', thickness: 2, borderSize: 50, glow: 70, movement: 'continuous', speed: 8, ...options };
  const root = document.createElement('div');
  root.className = 'neon-border';
  root.setAttribute('aria-hidden', 'true');
  Object.assign(root.style, { position: 'absolute', inset: '0', pointerEvents: 'none', borderRadius: 'inherit' });
  host.append(root);
  const size = { w: 0, h: 0 };
  let groups = [];

  function build() {
    const box = host.getBoundingClientRect();
    size.w = box.width; size.h = box.height;
    const radius = Math.min(parseFloat(getComputedStyle(host).borderTopLeftRadius) || 0, Math.min(size.w, size.h) / 2);
    const thick = Math.max(1, Math.min(10, settings.thickness));
    const amount = Math.max(0, Math.min(100, settings.glow)) / 100;
    const glowOuter = 10 + MAX_GLOW_REACH + MAX_GLOW_BLUR * 2;
    root.replaceChildren();
    groups = [0, .5].map(start => {
      const group = document.createElement('div');
      Object.assign(group.style, { position: 'absolute', inset: '0', overflow: 'visible', pointerEvents: 'none' });
      group.style.setProperty('--arc', buildArc(start, settings.borderSize, size.w, size.h, settings.color));
      if (amount > 0) {
        for (const layer of GLOW_LAYERS) {
          const reach = thick + amount * MAX_GLOW_REACH * layer.reach;
          const glow = band(document.createElement('div'), -glowOuter, glowOuter, radius + glowOuter);
          Object.assign(glow.style, { background: 'none', opacity: layer.opacity, mixBlendMode: 'plus-lighter', filter: `blur(${layer.blur}px)` });
          glow.append(band(document.createElement('div'), glowOuter - reach, reach, radius + reach));
          group.append(glow);
        }
      }
      for (let copy = 0; copy < 2; copy++) {
        const edge = band(document.createElement('div'), -thick, thick, radius + thick);
        edge.style.mixBlendMode = 'plus-lighter';
        group.append(edge);
      }
      root.append(group);
      return { group, start };
    });
  }
  const observer = new ResizeObserver(build);
  observer.observe(host);
  build();

  let frame = 0, last = performance.now(), corner = 0, stepT = 0;
  function tick() {
    const now = performance.now();
    frame = requestAnimationFrame(tick);
    const dt = Math.min(.05, Math.max(0, (now - last) / 1000));
    last = now;
    const speed = Math.max(0, Math.min(20, settings.speed));
    if (!speed || document.hidden || !host.isConnected || host.offsetParent === null) return;
    const step = settings.movement === 'step';
    const beat = step
      ? SLOWEST_STEP + (FASTEST_STEP - SLOWEST_STEP) * (speed - 1) / 19
      : (SLOWEST_CYCLE + (FASTEST_CYCLE - SLOWEST_CYCLE) * (speed - 1) / 19) / 4;
    stepT += dt / beat;
    while (stepT >= 1) { stepT -= 1; corner += 1; }
    const eased = step ? stepEase(Math.min(1, stepT * 2)) : glideEase(stepT);
    const from = cornerLap(corner, size.w || 100, size.h || 100), to = cornerLap(corner + 1, size.w || 100, size.h || 100);
    const lap = from + (to - from) * eased;
    for (const { group, start } of groups) group.style.setProperty('--arc', buildArc(lap + start, settings.borderSize, size.w, size.h, settings.color));
  }
  frame = requestAnimationFrame(tick);

  return {
    set(next) {
      const rebuild = next.thickness !== undefined || next.glow !== undefined;
      Object.assign(settings, next);
      if (rebuild) build();
    },
    destroy() { cancelAnimationFrame(frame); observer.disconnect(); root.remove(); }
  };
}
