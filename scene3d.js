// 3D space for the main screen: the VinFast VF8 model resting in a quiet,
// deep-space studio while parked, and an abstract road through the stars
// while driving. The model is the full-detail ModelVF8/ source,
// meshopt-compressed in three precisions; ?model=hq|lite picks a lighter one.
//   vf8-lossless.glb  float geometry, identical to the source (8.7MB, default)
//   vf8-hq.glb        16-bit positions, 12-bit normals (5.5MB)
//   vf8.glb           14-bit positions, 8-bit normals (2.8MB)
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { mergeGeometries, mergeVertices } from 'three/addons/utils/BufferGeometryUtils.js';

const MODEL_FILES = { lossless: 'vf8-lossless.glb', hq: 'vf8-hq.glb', lite: 'vf8.glb' };
const MODEL_URL = `./assets/${MODEL_FILES[new URLSearchParams(location.search).get('model')] || MODEL_FILES.lossless}?v=3`;
const STARLIGHT = 0xb4c0d8;
const BACKGROUND = 0x04050a;
const BADGES = /^Object0(04|11|19)$/;   // front V logo (two parts) and rear V + lettering
const REAR_BADGE = 'Object019';
const CAR_LENGTH = 4.75;   // VF8 length in metres; the model is scaled to it
const TAU = Math.PI * 2;

function canvasTexture(size, paint) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  paint(canvas.getContext('2d'), size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

// Sky dome: near-black space with a faint, slowly drifting nebula band.
const SKY_VERTEX = `
varying vec3 vDir;
void main() {
  vDir = position;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;
const SKY_FRAGMENT = `
uniform vec3 ground;
uniform vec3 horizon;
uniform vec3 zenith;
uniform vec3 nebulaA;
uniform vec3 nebulaB;
uniform float time;
varying vec3 vDir;
float hash(vec3 p) { return fract(sin(dot(p, vec3(127.1, 311.7, 74.7))) * 43758.5453); }
float noise(vec3 p) {
  vec3 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(hash(i), hash(i + vec3(1, 0, 0)), f.x), mix(hash(i + vec3(0, 1, 0)), hash(i + vec3(1, 1, 0)), f.x), f.y),
             mix(mix(hash(i + vec3(0, 0, 1)), hash(i + vec3(1, 0, 1)), f.x), mix(hash(i + vec3(0, 1, 1)), hash(i + vec3(1, 1, 1)), f.x), f.y), f.z);
}
float fbm(vec3 p) {
  float sum = 0.0, amp = 0.5;
  for (int i = 0; i < 5; i++) { sum += noise(p) * amp; p *= 2.03; amp *= 0.5; }
  return sum;
}
void main() {
  vec3 d = normalize(vDir);
  vec3 color = mix(horizon, zenith, smoothstep(0.0, 0.35, d.y));
  // A tilted band, like the Milky Way seen from a dark road.
  float band = exp(-pow((d.y - 0.14 - 0.1 * d.x) / 0.16, 2.0));
  vec3 drift = vec3(time * 0.004, 0.0, 0.0);
  color += nebulaA * smoothstep(0.42, 0.85, fbm(d * 2.4 + drift)) * band;
  color += nebulaB * smoothstep(0.5, 0.95, fbm(d * 4.6 + 7.0 - drift)) * band;
  gl_FragColor = vec4(mix(ground, color, smoothstep(-0.01, 0.03, d.y)), 1.0);
}`;
function buildSky() {
  const material = new THREE.ShaderMaterial({
    uniforms: {
      ground: { value: new THREE.Color(BACKGROUND) },
      horizon: { value: new THREE.Color(0x0d1020) },
      zenith: { value: new THREE.Color(0x020309) },
      nebulaA: { value: new THREE.Color(0x262c52) },
      nebulaB: { value: new THREE.Color(0x33284a) },
      time: { value: 0 }
    },
    vertexShader: SKY_VERTEX, fragmentShader: SKY_FRAGMENT,
    side: THREE.BackSide, depthWrite: false
  });
  return new THREE.Mesh(new THREE.SphereGeometry(120, 48, 24), material);
}

const STAR_VERTEX = `
attribute float size;
attribute float phase;
uniform float time;
uniform float scale;
varying float vAlpha;
void main() {
  vec4 view = modelViewMatrix * vec4(position, 1.0);
  gl_PointSize = size * scale;
  // Stars dim towards the horizon and twinkle slowly.
  vAlpha = smoothstep(0.0, 0.06, normalize(position).y) * (0.6 + 0.4 * sin(time * (0.4 + phase) + phase * 40.0));
  gl_Position = projectionMatrix * view;
}`;
const STAR_FRAGMENT = `
uniform vec3 color;
uniform float brightness;
varying float vAlpha;
void main() {
  float d = length(gl_PointCoord - 0.5);
  gl_FragColor = vec4(color * smoothstep(0.5, 0.0, d) * vAlpha * brightness, 1.0);
}`;
function buildStars(count = 1600) {
  let seed = 7;
  const random = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  const positions = new Float32Array(count * 3), sizes = new Float32Array(count), phases = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    // Upper hemisphere only; the floor hides the rest.
    const theta = random() * TAU, y = Math.pow(random(), 1.8), r = Math.sqrt(1 - y * y), radius = 100;
    positions.set([Math.cos(theta) * r * radius, y * radius, Math.sin(theta) * r * radius], i * 3);
    sizes[i] = random() < .05 ? 3 + random() * 1.6 : 1.2 + random() * 1.6;
    phases[i] = random();
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('size', new THREE.BufferAttribute(sizes, 1));
  geometry.setAttribute('phase', new THREE.BufferAttribute(phases, 1));
  const material = new THREE.ShaderMaterial({
    uniforms: { time: { value: 0 }, scale: { value: 1 }, color: { value: new THREE.Color(0xdfe6f5) }, brightness: { value: 1.2 } },
    vertexShader: STAR_VERTEX, fragmentShader: STAR_FRAGMENT,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending
  });
  return new THREE.Points(geometry, material);
}

// Paint and lights tuned for a pearl-white car under cool studio light.
function styleCar(model) {
  const leds = new Set(), tails = new Set();
  const paint = new THREE.MeshPhysicalMaterial({ name: 'v__xe__1', color: 0xe8ebf0, roughness: .3, metalness: .05, clearcoat: 1, clearcoatRoughness: .25, envMapIntensity: 1.1 });
  const glass = new THREE.MeshPhysicalMaterial({ color: 0x07090e, roughness: .3, metalness: 0, specularIntensity: .6, transparent: true, opacity: .78, envMapIntensity: 1.2 });
  // VinFast badges: the source's matte grey "chrome" disappears under our dim
  // lighting. The front V sits on black trim, so it gets bright polished metal;
  // the rear V, VINFAST lettering and VF8 sit on white paint, so they get dark
  // graphite chrome. Polygon offset keeps the thin rear letters off the paint.
  const badgeLight = new THREE.MeshStandardMaterial({ color: 0xf4f6fa, metalness: 1, roughness: .22, envMapIntensity: 3, emissive: 0x9aa3b8, emissiveIntensity: .35 });
  const badgeDark = new THREE.MeshStandardMaterial({ color: 0x23272f, metalness: .85, roughness: .28, envMapIntensity: 1.6, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
  const tailLens = new THREE.MeshPhysicalMaterial({ color: 0x07080b, roughness: .2, metalness: 0, specularIntensity: .7, transparent: true, opacity: .82, depthWrite: false, envMapIntensity: 1.3 });
  const turnLens = new THREE.MeshPhysicalMaterial({ color: 0x0b0a09, roughness: .2, metalness: 0, specularIntensity: .7, envMapIntensity: 1.3 });
  const reflector = new THREE.MeshStandardMaterial({ color: 0x3a0707, roughness: .35, metalness: 0 });
  model.traverse(node => {
    if (!node.isMesh) return;
    if (BADGES.test(node.name)) { node.material = node.name === REAR_BADGE ? badgeDark : badgeLight; return; }
    const material = node.material;
    switch (material.name) {
      case 'v__xe__1':
        node.material = paint;
        break;
      case 'windowglass':
        node.material = glass;
        break;
      case 'orangeglass__2':
        // Rear turn signals (Object007) blend into the smoked tail lens; the
        // mirror indicators keep their amber.
        if (node.name === 'Object007') { node.material = turnLens; break; }
        material.transmission = 0; material.transparent = true; material.opacity = .85;
        break;
      case 'LED_tr_ng__1':
        material.emissive.set(0xe4ebff);
        material.toneMapped = false;
        leds.add(material);
        break;
      case 'led__1':
        // The source paints the whole rear cluster in one red material. Keep
        // only the LED strips and bars lit, under a dark smoked outer lens.
        if (/^misc_f/.test(node.name)) node.material = tailLens;
        else if (node.name === 'Object009') node.material = reflector;
        else {
          material.color.set(0x2a0505);
          material.emissive.set(0xff2a2a);
          material.toneMapped = false;
          tails.add(material);
        }
        break;
      case 'fallback Material':
        node.visible = false;   // lens-flare cards from the source scene
        break;
    }
  });
  return (drive, voice, reveal) => {
    for (const led of leds) led.emissiveIntensity = (.55 + drive * .9 + voice * 2.2) * reveal;
    for (const tail of tails) tail.emissiveIntensity = (3.6 + drive * 3) * reveal;   // dimmed by the smoked lens
  };
}

// Wheel parts become children of one pivot per wheel so they can roll.
// Brake callipers (material "phanh") stay fixed to the body.
function rigWheels(model) {
  model.updateMatrixWorld(true);
  const box = new THREE.Box3(), centers = [];
  model.traverse(node => {
    if (!node.isMesh || !/^tire/.test(node.name)) return;
    const center = box.setFromObject(node).getCenter(new THREE.Vector3());
    if (!centers.some(other => other.distanceTo(center) < .3)) centers.push(center);
  });
  const pivots = centers.map(center => {
    const pivot = new THREE.Group();
    pivot.position.copy(center);
    model.add(pivot);
    return pivot;
  });
  pivots.forEach(pivot => pivot.updateMatrixWorld(true));
  const parts = [];
  model.traverse(node => { if (node.isMesh && /^(tire|disk|log0)/.test(node.name) && node.material.name !== 'phanh') parts.push(node); });
  for (const part of parts) {
    const center = box.setFromObject(part).getCenter(new THREE.Vector3());
    const nearest = pivots.reduce((best, pivot) => pivot.position.distanceTo(center) < best.position.distanceTo(center) ? pivot : best, pivots[0]);
    if (nearest && nearest.position.distanceTo(center) < .6) nearest.attach(part);
  }
  const tire = parts.find(part => /^tire/.test(part.name));
  return { pivots, radius: tire ? box.setFromObject(tire).getSize(new THREE.Vector3()).y / 2 : .36 };
}

async function loadCar() {
  const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
  const gltf = await loader.loadAsync(MODEL_URL);
  const model = gltf.scene;
  const glow = styleCar(model);
  const wheels = rigWheels(model);
  // Centre the car on the origin, wheels on the floor, scaled to real size.
  // The model already faces -Z, the direction the road scrolls towards.
  const bounds = new THREE.Box3().setFromObject(model);
  const size = bounds.getSize(new THREE.Vector3()), center = bounds.getCenter(new THREE.Vector3());
  const scale = CAR_LENGTH / size.z;
  const car = new THREE.Group();
  model.position.set(-center.x, -bounds.min.y, -center.z);
  car.add(model);
  car.scale.setScalar(scale);
  car.updateMatrixWorld(true);
  // The door is lifted out before the rear repaint splits bodyshell materials.
  const driverDoor = rigDriverDoor(car);
  paintRear(car);
  const driverWindow = rigDriverWindow(car, driverDoor);
  wheels.radius *= scale;
  return { car, wheels, glow, driverDoor, driverWindow };
}

// Split a mesh into its connected pieces (vertices welded by position) and
// sort its triangles into those of the pieces `pick` accepts and the rest.
// Pieces are judged by their centre and bounds in the car's frame (metres, origin under
// the car, rear towards +Z).
function splitPieces(mesh, pick) {
  const geometry = mesh.geometry, index = geometry.index.array, position = geometry.attributes.position;
  const weld = new Map(), root = new Int32Array(position.count);
  for (let i = 0; i < position.count; i++) {
    const key = `${position.getX(i)},${position.getY(i)},${position.getZ(i)}`;
    if (!weld.has(key)) weld.set(key, i);
    root[i] = weld.get(key);
  }
  const parent = Int32Array.from(root, (_, i) => i);
  const find = x => { while (parent[x] !== x) x = parent[x] = parent[parent[x]]; return x; };
  for (let i = 0; i < index.length; i += 3) {
    const a = find(root[index[i]]), b = find(root[index[i + 1]]), c = find(root[index[i + 2]]);
    parent[a] = b; parent[find(c)] = find(b);
  }
  const boxes = new Map(), point = new THREE.Vector3();
  for (let i = 0; i < index.length; i++) {
    const piece = find(root[index[i]]);
    if (!boxes.has(piece)) boxes.set(piece, new THREE.Box3());
    boxes.get(piece).expandByPoint(point.fromBufferAttribute(position, index[i]).applyMatrix4(mesh.matrixWorld));
  }
  const chosen = new Set(), bounds = new THREE.Box3();
  for (const [piece, box] of boxes) if (pick(box.getCenter(new THREE.Vector3()), box)) { chosen.add(piece); bounds.union(box); }
  const keep = [], swap = [];
  for (let i = 0; i < index.length; i += 3) (chosen.has(find(root[index[i]])) ? swap : keep).push(index[i], index[i + 1], index[i + 2]);
  return { keep, swap, bounds };
}

// Give the pieces `pick` accepts a second material.
function repaint(mesh, pick, material) {
  const { keep, swap } = splitPieces(mesh, pick);
  if (!swap.length) return;
  const geometry = mesh.geometry;
  geometry.setIndex([...keep, ...swap]);
  geometry.clearGroups();
  geometry.addGroup(0, keep.length, 0);
  geometry.addGroup(keep.length, swap.length, 1);
  mesh.material = [mesh.material, material];
}

// Lift the pieces `pick` accepts out of `mesh` into a new mesh under `parent`,
// keeping where they sit. Returns the new mesh, or null when nothing matched.
// The new mesh shares the source's vertex buffer, so its geometry bounds cover
// the whole source; `userData.bounds` holds its real bounds in the car frame.
function detachPieces(mesh, pick, parent, material = mesh.material) {
  const { keep, swap, bounds } = splitPieces(mesh, pick);
  if (!swap.length) return null;
  mesh.geometry.setIndex(keep);
  const geometry = new THREE.BufferGeometry();
  for (const [name, attribute] of Object.entries(mesh.geometry.attributes)) geometry.setAttribute(name, attribute);
  geometry.setIndex(swap);
  const part = new THREE.Mesh(geometry, material);
  part.name = `${mesh.name}_moving`;
  part.userData.bounds = bounds;
  part.applyMatrix4(mesh.matrixWorld);
  parent.updateMatrixWorld(true);
  parent.attach(part);
  return part;
}

// The driver's (front left) door: skin, frame, trim, handle, mirror and
// chrome, swung about a hinge at its front edge. The source model has no
// separate door, so its pieces are picked by where they sit (car frame).
const DOOR_MESHES = /^(bodyshell|interior|betaparts|Object016|Object018|Object021|indicator_lf)$/;
const DOOR_ANGLE = THREE.MathUtils.degToRad(62);
function inDriverDoor(center, box) {
  return center.x < -.6 && box.min.x > -1.12 && box.min.z > -.97 && box.max.z < .3 && center.z < .14 && box.max.y > .3;
}
function rigDriverDoor(car) {
  car.updateMatrixWorld(true);
  const meshes = [];
  car.traverse(node => { if (node.isMesh && DOOR_MESHES.test(node.name) && !Array.isArray(node.material)) meshes.push(node); });
  const hinge = new THREE.Group();
  car.add(hinge);
  // The hinge sits on the door's front edge, just inside its outer skin; the
  // mirror sticks out further, so only the panel below it sets the skin line.
  const bounds = new THREE.Box3();
  const pick = (center, box) => {
    if (!inDriverDoor(center, box)) return false;
    if (box.max.y < 1.05) bounds.union(box);
    return true;
  };
  const parts = meshes.map(mesh => detachPieces(mesh, pick, hinge)).filter(Boolean);
  if (!parts.length) { car.remove(hinge); return null; }
  const pivot = new THREE.Vector3(bounds.min.x + .06, 0, bounds.min.z + .04);
  car.worldToLocal(pivot);
  hinge.position.copy(pivot);
  for (const part of parts) part.position.sub(pivot);
  hinge.updateMatrixWorld(true);
  return hinge;
}

// The driver's (front left) side window, lifted out of the shared glass mesh
// so it can wind down. It slides along the glass plane, which leans inwards
// towards the roof, so it disappears into the door like the real one.
const WINDOW_TRAVEL = .42;
const WINDOW_SLIDE = new THREE.Vector3(-.32, -1, 0).normalize();
function rigDriverWindow(car, door) {
  const glass = car.getObjectByName('windscreen');
  if (!glass) return null;
  const pick = c => c.x < -.6 && Math.abs(c.z + .24) < .12 && c.y > 1.15;
  // The glass below the window sill would show through the door skin, so it is
  // clipped there. The car only ever turns about Y, so a level plane holds.
  const material = glass.material.clone();
  const slider = new THREE.Group();
  (door || car).add(slider);
  const pane = detachPieces(glass, pick, slider, material);
  if (!pane) return null;
  pane.name = 'window_driver';
  const sill = pane.userData.bounds.min.y;
  material.clippingPlanes = [new THREE.Plane(new THREE.Vector3(0, 1, 0), -(sill - .005))];
  // The car group is scaled to metres; the slider moves in its local units.
  return open => slider.position.copy(WINDOW_SLIDE).multiplyScalar(open * WINDOW_TRAVEL / car.scale.x);
}

// As on the real VF8, the panel around the rear V and the inside of both lamp
// clusters are gloss black, and the V on it is bright chrome (the lettering
// below stays dark graphite on the white paint).
const REAR_BLACK = [[0, 1.021, 2.235], [-.768, 1.029, 2.025], [.768, 1.029, 2.025], [-.753, 1.059, 2.115], [.753, 1.059, 2.115]];
function paintRear(car) {
  const black = new THREE.MeshPhysicalMaterial({ color: 0x040506, roughness: .18, metalness: 0, clearcoat: 1, clearcoatRoughness: .25, envMapIntensity: 1.2 });
  const chrome = new THREE.MeshStandardMaterial({ color: 0xf4f6fa, metalness: 1, roughness: .22, envMapIntensity: 3, emissive: 0x9aa3b8, emissiveIntensity: .35, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
  car.traverse(node => {
    if (node.name === 'bodyshell') repaint(node, c => REAR_BLACK.some(([x, y, z]) => Math.hypot(c.x - x, c.y - y, c.z - z) < .03), black);
    if (node.name === REAR_BADGE) repaint(node, c => c.y > .95, chrome);
  });
}

function buildFloor() {
  const floor = new THREE.Group();
  const disc = new THREE.Mesh(
    new THREE.CircleGeometry(40, 96),
    new THREE.MeshStandardMaterial({ color: 0x030408, roughness: .9, metalness: 0, envMapIntensity: .02 })
  );
  disc.rotation.x = -Math.PI / 2;
  floor.add(disc);
  // Soft pool of cool light the car rests in.
  const pool = new THREE.Mesh(
    new THREE.PlaneGeometry(16, 16),
    new THREE.MeshBasicMaterial({
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      map: canvasTexture(512, (ctx, size) => {
        const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
        gradient.addColorStop(0, 'rgba(150,165,200,.2)'); gradient.addColorStop(.3, 'rgba(110,122,160,.09)');
        gradient.addColorStop(.65, 'rgba(60,68,100,.025)'); gradient.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = gradient; ctx.fillRect(0, 0, size, size);
      })
    })
  );
  pool.rotation.x = -Math.PI / 2; pool.position.y = .004;
  floor.add(pool);
  const shadow = new THREE.Mesh(
    new THREE.PlaneGeometry(6.4, 3.4),
    new THREE.MeshBasicMaterial({
      transparent: true, depthWrite: false,
      map: canvasTexture(256, (ctx, size) => {
        const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
        gradient.addColorStop(0, 'rgba(0,0,0,.92)'); gradient.addColorStop(.55, 'rgba(0,0,0,.6)'); gradient.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = gradient; ctx.fillRect(0, 0, size, size);
      })
    })
  );
  shadow.rotation.set(-Math.PI / 2, 0, Math.PI / 2); shadow.position.y = .006;
  floor.add(shadow);
  // Orbit lines: a few thin circles, one dashed, fading out with distance.
  const orbits = new THREE.Mesh(
    new THREE.PlaneGeometry(30, 30),
    new THREE.MeshBasicMaterial({
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      map: canvasTexture(1024, (ctx, size) => {
        const center = size / 2, unit = size / 30;
        ctx.strokeStyle = 'rgba(180,192,216,.16)';
        for (const [radius, width, dash] of [[3.9, 1.2, []], [5.6, .8, [3, 9]], [8.4, .7, []], [12.5, .6, [2, 14]]]) {
          ctx.lineWidth = width; ctx.setLineDash(dash);
          ctx.beginPath(); ctx.arc(center, center, radius * unit, 0, TAU); ctx.stroke();
        }
        ctx.globalCompositeOperation = 'destination-in';
        const fade = ctx.createRadialGradient(center, center, 0, center, center, center);
        fade.addColorStop(0, 'rgba(0,0,0,1)'); fade.addColorStop(.45, 'rgba(0,0,0,.55)'); fade.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = fade; ctx.fillRect(0, 0, size, size);
      })
    })
  );
  orbits.rotation.x = -Math.PI / 2; orbits.position.y = .008;
  floor.add(orbits);
  // A faint ring that brightens with ViVi's voice.
  const halo = new THREE.Mesh(
    new THREE.RingGeometry(3.4, 4.4, 160),
    new THREE.MeshBasicMaterial({
      transparent: true, opacity: .3, depthWrite: false, blending: THREE.AdditiveBlending,
      map: canvasTexture(256, (ctx, size) => {
        const gradient = ctx.createLinearGradient(0, 0, size, 0);
        gradient.addColorStop(0, 'rgba(180,192,216,0)'); gradient.addColorStop(.5, 'rgba(180,192,216,.18)'); gradient.addColorStop(1, 'rgba(180,192,216,0)');
        ctx.fillStyle = gradient; ctx.fillRect(0, 0, size, size);
      })
    })
  );
  // RingGeometry UVs run across the ring, so the gradient becomes a soft band.
  halo.rotation.x = -Math.PI / 2; halo.position.y = .01;
  floor.add(halo);
  return { floor, pool, orbits, halo };
}

// --- Road path --------------------------------------------------------------
// The road follows a path in metres on a local plane (x east, -z north) with
// compass headings in degrees. A planned route gives the real shape of the road
// ahead; without one the road runs straight on.
const PATH_STEP = 2, PATH_SMOOTHING = 5;
function straightPath(heading) {
  const radians = THREE.MathUtils.degToRad(heading);
  return { at(distance, out) { out.x = Math.sin(radians) * distance; out.z = -Math.cos(radians) * distance; out.h = heading; return out; } };
}
// Resampled every 2 m along the route's own distances (so the car stays in step
// with the map marker) and smoothed, so street corners become drivable bends.
function routePath(coordinates, cumulative) {
  const [lng0, lat0] = coordinates[0];
  const kx = 111320 * Math.cos(THREE.MathUtils.degToRad(lat0)), kz = 110540;
  const length = cumulative.at(-1);
  const count = Math.max(2, Math.ceil(length / PATH_STEP) + 1);
  let xs = new Float64Array(count), zs = new Float64Array(count);
  for (let index = 0, segment = 0; index < count; index++) {
    const distance = Math.min(length, index * PATH_STEP);
    while (segment < cumulative.length - 2 && cumulative[segment + 1] < distance) segment++;
    const t = Math.min(1, (distance - cumulative[segment]) / (cumulative[segment + 1] - cumulative[segment] || 1));
    const [a, b] = [coordinates[segment], coordinates[Math.min(segment + 1, coordinates.length - 1)]];
    xs[index] = (a[0] + (b[0] - a[0]) * t - lng0) * kx;
    zs[index] = -(a[1] + (b[1] - a[1]) * t - lat0) * kz;
  }
  const smooth = values => {
    const result = new Float64Array(count);
    for (let index = 0; index < count; index++) {
      let sum = 0;
      for (let offset = -PATH_SMOOTHING; offset <= PATH_SMOOTHING; offset++) sum += values[Math.max(0, Math.min(count - 1, index + offset))];
      result[index] = sum / (2 * PATH_SMOOTHING + 1);
    }
    return result;
  };
  for (let pass = 0; pass < 2; pass++) { xs = smooth(xs); zs = smooth(zs); }
  // Past either end the last segment carries on in a straight line.
  const point = (distance, out) => {
    const index = Math.max(0, Math.min(count - 2, Math.floor(distance / PATH_STEP)));
    const t = distance / PATH_STEP - index;
    out.x = xs[index] + (xs[index + 1] - xs[index]) * t;
    out.z = zs[index] + (zs[index + 1] - zs[index]) * t;
    return out;
  };
  const behind = {}, ahead = {};
  return {
    at(distance, out) {
      point(distance, out); point(distance - 3, behind); point(distance + 3, ahead);
      out.h = THREE.MathUtils.radToDeg(Math.atan2(ahead.x - behind.x, behind.z - ahead.z));
      return out;
    }
  };
}

const ROAD_BEHIND = 40, ROAD_AHEAD = 200;
const ROAD_SAMPLES = (ROAD_BEHIND + ROAD_AHEAD) / PATH_STEP + 1;
const ROAD_HALF_WIDTH = 5, EDGES = [-4.6, 4.6], EDGE_WIDTH = .06;
const LANE_LINES = [-1.55, 1.55], DASH_SPACING = 6, DASH_LENGTH = 2.4, DASH_WIDTH = .1;
const POSTS = [-6, 6], POST_SPACING = 14;
// Guide path: a glowing band the width of the car laid along the route ahead,
// like a self-driving display showing the lane it has picked.
const GUIDE_BEHIND = 1, GUIDE_AHEAD = 52, GUIDE_HALF_WIDTH = .95, GUIDE_COLOR = 0x8fb2ff;
const GUIDE_SAMPLES = (GUIDE_BEHIND + GUIDE_AHEAD) / 1 + 1;
const MAX_DASHES = LANE_LINES.length * (Math.ceil((ROAD_BEHIND + ROAD_AHEAD) / DASH_SPACING) + 1);
const MAX_POSTS = POSTS.length * (Math.ceil((ROAD_BEHIND + ROAD_AHEAD) / POST_SPACING) + 1);
// Ribbons rewritten every frame: per strip, a left/right vertex pair per sample.
function stripGeometry(strips, segments) {
  const geometry = new THREE.BufferGeometry();
  const vertices = strips * (segments + 1) * 2;
  geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(vertices * 3), 3).setUsage(THREE.DynamicDrawUsage));
  geometry.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(vertices * 3).map((_, index) => index % 3 === 1 ? 1 : 0), 3));
  const index = [];
  for (let strip = 0; strip < strips; strip++) {
    for (let segment = 0; segment < segments; segment++) {
      const a = (strip * (segments + 1) + segment) * 2;
      index.push(a, a + 1, a + 2, a + 2, a + 1, a + 3);
    }
  }
  geometry.setIndex(index);
  return geometry;
}
function ribbon(geometry, material) {
  const mesh = new THREE.Mesh(geometry, material);
  mesh.frustumCulled = false;
  return mesh;
}

function buildRoad() {
  const road = new THREE.Group();
  const asphalt = ribbon(stripGeometry(1, ROAD_SAMPLES - 1), new THREE.MeshStandardMaterial({ color: 0x020307, roughness: .9, metalness: 0, transparent: true, opacity: 0, envMapIntensity: .02 }));
  road.add(asphalt);
  const edgeMaterial = new THREE.MeshBasicMaterial({ color: STARLIGHT, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
  const edges = ribbon(stripGeometry(EDGES.length, ROAD_SAMPLES - 1), edgeMaterial);
  road.add(edges);
  const dashMaterial = new THREE.MeshBasicMaterial({ color: 0x8e97ab, transparent: true, opacity: 0, depthWrite: false, side: THREE.DoubleSide });
  const dashes = ribbon(stripGeometry(MAX_DASHES, 1), dashMaterial);
  road.add(dashes);
  const postMaterial = new THREE.MeshBasicMaterial({ color: STARLIGHT, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false });
  const posts = new THREE.InstancedMesh(new THREE.BoxGeometry(.04, 1.1, .04).translate(0, .55, 0), postMaterial, MAX_POSTS);
  posts.frustumCulled = false;
  road.add(posts);
  // Light streaks rushing past, like stars at warp.
  const streakMaterial = new THREE.MeshBasicMaterial({ color: 0xdfe6f5, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false });
  const streaks = new THREE.InstancedMesh(new THREE.BoxGeometry(.016, .016, 4), streakMaterial, 46);
  let seed = 11;
  const random = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  const streakSeeds = Array.from({ length: streaks.count }, () => ({ x: (random() < .5 ? -1 : 1) * (6.5 + random() * 9), y: .4 + random() * 5, z: random() * 120, speed: .8 + random() * .9 }));
  road.add(streaks);
  // Upcoming manoeuvre: a chevron lying on the road ahead.
  const chevronShape = new THREE.Shape();
  chevronShape.moveTo(0, 1.3); chevronShape.lineTo(1.1, -.1); chevronShape.lineTo(.62, -.1); chevronShape.lineTo(0, .66);
  chevronShape.lineTo(-.62, -.1); chevronShape.lineTo(-1.1, -.1); chevronShape.lineTo(0, 1.3);
  const chevronMaterial = new THREE.MeshBasicMaterial({ color: STARLIGHT, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
  const chevron = new THREE.Group();
  for (let index = 0; index < 3; index++) {
    const piece = new THREE.Mesh(new THREE.ShapeGeometry(chevronShape), chevronMaterial);
    piece.rotation.x = -Math.PI / 2; piece.position.z = -index * 1.25;
    chevron.add(piece);
  }
  chevron.position.set(0, .02, -16);
  road.add(chevron);
  const guideGeometry = stripGeometry(1, GUIDE_SAMPLES - 1);
  // u runs across the band, v from the car (0) to the far end (1).
  guideGeometry.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(GUIDE_SAMPLES * 4).map((_, index) => index % 2 ? Math.floor(index / 4) / (GUIDE_SAMPLES - 1) : (index >> 1) % 2), 2));
  const guideMaterial = new THREE.MeshBasicMaterial({
    color: GUIDE_COLOR, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
    map: canvasTexture(256, (ctx, size) => {
      // Bright rims with a faint fill, fading out down the road.
      const across = ctx.createLinearGradient(0, 0, size, 0);
      for (const [stop, alpha] of [[0, 0], [.04, .95], [.12, .3], [.5, .16], [.88, .3], [.96, .95], [1, 0]]) across.addColorStop(stop, `rgba(255,255,255,${alpha})`);
      ctx.fillStyle = across; ctx.fillRect(0, 0, size, size);
      const along = ctx.createLinearGradient(0, size, 0, 0);
      for (const [stop, alpha] of [[0, 0], [.06, 1], [.45, .75], [1, 0]]) along.addColorStop(stop, `rgba(0,0,0,${alpha})`);
      ctx.globalCompositeOperation = 'destination-in';
      ctx.fillStyle = along; ctx.fillRect(0, 0, size, size);
    })
  });
  const guide = ribbon(guideGeometry, guideMaterial);
  road.add(guide);
  // Headlight beams on the road, faded in while driving.
  const beams = new THREE.Mesh(
    new THREE.PlaneGeometry(3.4, 9),
    new THREE.MeshBasicMaterial({
      transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending,
      map: canvasTexture(256, (ctx, size) => {
        const gradient = ctx.createLinearGradient(0, size, 0, 0);
        gradient.addColorStop(0, 'rgba(220,228,245,.4)'); gradient.addColorStop(1, 'rgba(220,228,245,0)');
        ctx.fillStyle = gradient;
        ctx.filter = `blur(${size / 14}px)`;
        ctx.beginPath(); ctx.moveTo(size * .4, size); ctx.lineTo(size * .12, size * .1); ctx.lineTo(size * .88, size * .1); ctx.lineTo(size * .6, size); ctx.fill();
      })
    })
  );
  beams.rotation.x = -Math.PI / 2; beams.position.set(0, .012, -CAR_LENGTH / 2 - 4.5);
  return { road, asphalt, edges, edgeMaterial, dashes, dashMaterial, posts, postMaterial, streaks, streakMaterial, streakSeeds, chevron, chevronMaterial, guide, guideMaterial, beams };
}

// --- Traffic --------------------------------------------------------------
// Simulated cars for context, like a self-driving display, on a one-way road
// of three lanes (-1 left, 0 centre, 1 right). They mostly drive a little
// slower than us, so the VinFast changes lanes to get past them.
const TRAFFIC_LANE = 3.1, TRAFFIC_GAP = 14, TRAFFIC_LANES = [-1, 0, 1];
const TRAFFIC = { count: 3, ahead: [30, 120], spawn: [90, 140], behind: -34 };
// Our car moves over when a car is this close ahead in its lane, and keeps at
// least this much room when a traffic car would otherwise run into it.
const AVOID_AHEAD = 42, ROOM_AHEAD = 9, ROOM_BEHIND = 8;
// Two body styles, each a side profile extruded across the car's width with
// rounded shoulders. Glass, cladding and tyres are vertex colours, so a style
// stays one draw call however many cars use it.
const TRAFFIC_MODELS = {
  // Compact SUV: high hood, raked windscreen, long flat roof, upright tailgate.
  suv: {
    profile: shape => shape
      .moveTo(-2.3, .38)
      .quadraticCurveTo(-2.38, .7, -2.25, .86)            // front face
      .quadraticCurveTo(-1.6, .98, -1.05, 1.02)           // hood
      .quadraticCurveTo(-.75, 1.4, -.42, 1.58)            // windscreen
      .lineTo(1.5, 1.6)                                   // roof
      .quadraticCurveTo(1.95, 1.61, 2.1, 1.48)            // roof spoiler
      .lineTo(2.3, 1.05)                                  // tailgate glass
      .quadraticCurveTo(2.36, .6, 2.28, .38)              // tail
      .lineTo(-2.3, .38),
    cabin: [1, 1.6, .2], glass: [1.06, 1.53, .8], cladding: .52, sill: .38,
    wheel: { radius: .4, x: .8, z: [-1.48, 1.45] },
    tail: [1.4, .05, [0], .98, 2.36], head: [.45, .05, [-.58, .58], .8, -2.35]
  },
  // Saloon: low nose, long hood, arched roof and a short separate boot.
  sedan: {
    profile: shape => shape
      .moveTo(-2.35, .3)
      .quadraticCurveTo(-2.42, .55, -2.28, .68)           // nose
      .quadraticCurveTo(-1.6, .8, -.95, .86)              // hood
      .quadraticCurveTo(-.55, 1.25, -.2, 1.42)            // windscreen
      .quadraticCurveTo(.4, 1.48, .95, 1.4)               // roof
      .quadraticCurveTo(1.45, 1.1, 1.75, .98)             // rear window
      .lineTo(2.3, .94)                                   // boot lid
      .quadraticCurveTo(2.4, .6, 2.32, .3)                // tail
      .lineTo(-2.35, .3),
    cabin: [.88, 1.45, .28], glass: [.92, 1.42, .93], cladding: 0, sill: .3,
    wheel: { radius: .34, x: .8, z: [-1.45, 1.5] },
    tail: [.5, .06, [-.52, .52], .84, 2.38], head: [.42, .045, [-.6, .6], .6, -2.4]
  }
};
function trafficCarGeometry(model) {
  const width = 1.9, shoulder = .22, depth = width - 2 * shoulder;
  let body = new THREE.ExtrudeGeometry(model.profile(new THREE.Shape()), { depth, bevelEnabled: true, bevelThickness: shoulder, bevelSize: .06, bevelSegments: 5, curveSegments: 16 })
    .rotateY(-Math.PI / 2).translate(depth / 2, 0, 0);
  // Pull the cabin in above the beltline and taper nose and tail in plan view,
  // then weld the seams so the paint shades smoothly.
  const [cabinFrom, cabinTo, cabinPull] = model.cabin;
  const position = body.attributes.position;
  for (let index = 0; index < position.count; index++) {
    const y = position.getY(index), z = position.getZ(index);
    const cabin = THREE.MathUtils.smoothstep(y, cabinFrom, cabinTo), taper = (Math.abs(z) / 2.4) ** 4;
    const tuck = 1 - THREE.MathUtils.smoothstep(y, model.sill, model.sill + .22);   // sides roll under at the sills
    position.setX(index, position.getX(index) * (1 - cabinPull * cabin) * (1 - .1 * taper) * (1 - .06 * tuck));
  }
  body.deleteAttribute('uv'); body.deleteAttribute('normal');
  body = mergeVertices(body, 1e-4);
  body.computeVertexNormals();
  const paint = new THREE.Color(0x6b7386), glass = new THREE.Color(0x0b0e16), cladding = new THREE.Color(0x1a1d24), tyre = new THREE.Color(0x07080b);
  const tint = (geometry, pick) => {
    const points = geometry.attributes.position, colors = new Float32Array(points.count * 3);
    for (let index = 0; index < points.count; index++) pick(points.getY(index), index).toArray(colors, index * 3);
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    return geometry;
  };
  // Glass is the steep band between beltline and roof; the roof stays painted.
  const [glassFrom, glassTo, steepness] = model.glass;
  const normals = body.attributes.normal;
  tint(body, (y, index) => y < model.cladding ? cladding : y > glassFrom && y < glassTo && Math.abs(normals.getY(index)) < steepness ? glass : paint);
  const { radius, x: track, z: axles } = model.wheel;
  const wheels = [];
  for (const x of [-track, track]) for (const z of axles) {
    const wheel = new THREE.CylinderGeometry(radius, radius, .26, 22).rotateZ(Math.PI / 2).translate(x, radius, z);
    wheel.deleteAttribute('uv');
    wheels.push(tint(wheel, () => tyre));
  }
  return mergeGeometries([body, ...wheels]);
}
function buildTraffic() {
  const group = new THREE.Group();
  const lamps = ([width, height, xs, y, z]) => mergeGeometries(xs.map(x => new THREE.BoxGeometry(width, height, .05).translate(x, y, z)));
  const fade = { transparent: true, opacity: 0 };
  const materials = [
    new THREE.MeshStandardMaterial({ vertexColors: true, roughness: .38, metalness: .45, envMapIntensity: .9, ...fade }),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(2.6, .28, .32), toneMapped: false, ...fade }),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(1.8, 1.9, 2.2), toneMapped: false, ...fade })
  ];
  const total = TRAFFIC.count;
  // Per body style: the car itself, tail lamps and headlamps.
  const styles = Object.values(TRAFFIC_MODELS).map(model => [trafficCarGeometry(model), lamps(model.tail), lamps(model.head)].map((geometry, part) => {
    const mesh = new THREE.InstancedMesh(geometry, materials[part], total);
    mesh.frustumCulled = false;
    group.add(mesh);
    return mesh;
  }));
  let seed = 7;
  const random = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  const cars = Array.from({ length: total }, () => ({ lane: 0, offset: 0, pace: 1, style: 0 }));
  // Puts a car somewhere in [from, to] metres from us in one of `lanes`, clear
  // of the cars already in that lane.
  function spawn(car, distance, from, to, lanes = TRAFFIC_LANES) {
    car.lane = lanes[Math.floor(random() * lanes.length)];
    for (let attempt = 0; attempt < 8; attempt++) {
      const at = distance + from + random() * (to - from);
      if (cars.every(other => other === car || other.lane !== car.lane || Math.abs(other.at - at) > TRAFFIC_GAP)) { car.at = at; break; }
      car.at = distance + to + attempt * TRAFFIC_GAP;
    }
    car.pace = .7 + random() * .25;
    car.offset = (random() - .5) * .4;
    car.style = Math.floor(random() * styles.length);
  }
  function reset(distance) {
    for (const car of cars) car.at = -Infinity;
    for (const car of cars) spawn(car, distance, ...TRAFFIC.ahead);
  }
  return { group, styles, materials, cars, spawn, reset, last: null };
}

const ease = (current, target, rate, dt) => current + (target - current) * (1 - Math.exp(-rate * dt));

export function createScene(canvas, options = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.75));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = .9;
  renderer.localClippingEnabled = true;   // the driver's window sinks into the door

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(BACKGROUND);
  scene.fog = new THREE.Fog(BACKGROUND, 18, 54);   // reaches further while driving
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), .04).texture;
  scene.environmentIntensity = .38;
  pmrem.dispose();

  const sky = buildSky();
  scene.add(sky);
  const stars = buildStars();
  scene.add(stars);

  // Cool top light like a studio softbox, with two rim lights to trace the
  // silhouette against the dark.
  const hemisphere = new THREE.HemisphereLight(0xb9c6e6, 0x05060b, .45);
  const key = new THREE.DirectionalLight(0xf2f5ff, 2.2);
  key.position.set(1.5, 9, 3);
  const rimLeft = new THREE.DirectionalLight(0x8fa3d6, 2.4);
  rimLeft.position.set(-7, 3, -6);
  const rimRight = new THREE.DirectionalLight(0x9a8fc4, 1.2);
  rimRight.position.set(7, 2.5, 5);
  const lights = [[hemisphere, .3], [key, 1.5], [rimLeft, 2], [rimRight, .9]];
  for (const [light] of lights) scene.add(light);

  const floor = buildFloor();
  scene.add(floor.floor);
  const road = buildRoad();
  scene.add(road.road);
  const traffic = buildTraffic();
  scene.add(traffic.group);
  const carPivot = new THREE.Group();
  carPivot.add(road.beams);
  scene.add(carPivot);
  let vehicle = null;
  loadCar()
    .then(loaded => { vehicle = loaded; carPivot.add(loaded.car); })
    .catch(error => console.warn('VF8 model unavailable:', error));

  const camera = new THREE.PerspectiveCamera(32, 1, .1, 260);
  const look = new THREE.Vector3(0, .7, 0);
  // Bloom only catches the LEDs and the brightest stars; tone mapping happens
  // in the output pass.
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), .32, .28, 1.25);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());
  const desired = { position: new THREE.Vector3(), look: new THREE.Vector3() };

  const state = {
    mode: 'parked', drive: 0, speed: 0, displaySpeed: 0, distance: 0,
    orbit: -2.35, orbitVelocity: 0, dragging: false, lastX: 0,
    voice: 0, turn: null, turnStrength: 0, yaw: 0, reveal: 0,
    route: null, routeDistance: 0, routeLive: false, onRoute: false, straight: null, carHeading: 0, camHeading: 0, chevronTurn: 0, lane: 0, laneOffset: 0, laneVelocity: 0, laneHold: 0, lastDistance: 0, forward: 0, laneYaw: 0, camLane: 0, guideLane: 0, window: 0, windowOpen: 0, door: 0, doorOpen: 0,
    reduced: Boolean(options.reduced), paused: false, time: 0, last: performance.now(), frame: 0
  };

  const matrix = new THREE.Matrix4();
  // The road is rebuilt every frame in the chase camera's frame: the car sits at
  // the origin and the camera's heading points down -Z.
  const carPoint = { x: 0, z: 0, h: 0 }, point = { x: 0, z: 0, h: 0, rx: 1, rz: 0, rel: 0 };
  const view = { heading: 0, cos: 1, sin: 0 };
  function place(path, distance) {
    path.at(distance, point);
    const dx = point.x - carPoint.x, dz = point.z - carPoint.z;
    point.x = dx * view.cos + dz * view.sin;
    point.z = -dx * view.sin + dz * view.cos;
    point.rel = THREE.MathUtils.degToRad(point.h - view.heading);
    point.rx = Math.cos(point.rel); point.rz = Math.sin(point.rel);   // unit vector to the right
    return point;
  }
  function writePair(array, vertex, at, left, right, y) {
    const index = vertex * 3;
    array[index] = at.x + at.rx * left; array[index + 1] = y; array[index + 2] = at.z + at.rz * left;
    array[index + 3] = at.x + at.rx * right; array[index + 4] = y; array[index + 5] = at.z + at.rz * right;
  }
  function layoutRoad(path, distance) {
    const asphalt = road.asphalt.geometry.attributes.position, edges = road.edges.geometry.attributes.position;
    for (let sample = 0; sample < ROAD_SAMPLES; sample++) {
      place(path, distance - ROAD_BEHIND + sample * PATH_STEP);
      writePair(asphalt.array, sample * 2, point, -ROAD_HALF_WIDTH, ROAD_HALF_WIDTH, .002);
      EDGES.forEach((x, strip) => writePair(edges.array, (strip * ROAD_SAMPLES + sample) * 2, point, x - EDGE_WIDTH / 2, x + EDGE_WIDTH / 2, .008));
    }
    asphalt.needsUpdate = edges.needsUpdate = true;
    const guide = road.guide.geometry.attributes.position;
    // The guide bends toward the lane the car is heading for.
    const laneTarget = state.guideLane;
    for (let sample = 0; sample < GUIDE_SAMPLES; sample++) {
      place(path, distance - GUIDE_BEHIND + sample);
      const lateral = state.laneOffset + (laneTarget - state.laneOffset) * THREE.MathUtils.smoothstep(sample - GUIDE_BEHIND, 2, 22);
      writePair(guide.array, sample * 2, point, lateral - GUIDE_HALF_WIDTH, lateral + GUIDE_HALF_WIDTH, .011);
    }
    guide.needsUpdate = true;
    // Dashes and posts are pinned to distances along the road, so they pass by.
    const dashes = road.dashes.geometry.attributes.position;
    let count = 0;
    for (let mark = Math.ceil((distance - ROAD_BEHIND) / DASH_SPACING) * DASH_SPACING; mark + DASH_LENGTH <= distance + ROAD_AHEAD; mark += DASH_SPACING) {
      for (const lane of LANE_LINES) {
        if (count >= MAX_DASHES) break;
        writePair(dashes.array, count * 4, place(path, mark), lane - DASH_WIDTH / 2, lane + DASH_WIDTH / 2, .009);
        writePair(dashes.array, count * 4 + 2, place(path, mark + DASH_LENGTH), lane - DASH_WIDTH / 2, lane + DASH_WIDTH / 2, .009);
        count++;
      }
    }
    dashes.needsUpdate = true;
    road.dashes.geometry.setDrawRange(0, count * 6);
    count = 0;
    for (let mark = Math.ceil((distance - ROAD_BEHIND) / POST_SPACING) * POST_SPACING; mark <= distance + ROAD_AHEAD; mark += POST_SPACING) {
      place(path, mark);
      for (const x of POSTS) {
        if (count >= MAX_POSTS) break;
        matrix.makeTranslation(point.x + point.rx * x, 0, point.z + point.rz * x);
        road.posts.setMatrixAt(count++, matrix);
      }
    }
    road.posts.count = count;
    road.posts.instanceMatrix.needsUpdate = true;
    // The manoeuvre chevron lies on the road just ahead, turned toward the turn.
    place(path, distance + 16);
    road.chevron.position.set(point.x + point.rx * laneTarget, .02, point.z + point.rz * laneTarget);
    road.chevron.rotation.y = -point.rel + state.chevronTurn;
    road.streakSeeds.forEach((streak, streakIndex) => {
      const z = 24 - ((streak.z + distance * streak.speed * 1.6) % 120);
      matrix.makeTranslation(streak.x, streak.y, z);
      road.streaks.setMatrixAt(streakIndex, matrix);
    });
    road.streaks.instanceMatrix.needsUpdate = true;
  }

  // Traffic moves with the distance we cover, at a pace close to ours. Cars
  // that drop out of view respawn: far ahead, or behind us out of our lane.
  const lanesAt = offset => TRAFFIC_LANES.filter(lane => Math.abs(lane * TRAFFIC_LANE - offset) < 2.4);
  function layoutTraffic(path, distance) {
    const step = traffic.last === null ? Infinity : distance - traffic.last;
    traffic.last = distance;
    if (Math.abs(step) > 30) traffic.reset(distance);
    else {
      const ours = lanesAt(state.laneOffset);
      for (const car of traffic.cars) {
        car.at += step * car.pace;
        const ahead = car.at - distance;
        if (ahead < TRAFFIC.behind) traffic.spawn(car, distance, ...TRAFFIC.spawn);
        else if (ahead > TRAFFIC.spawn[1] + 20) traffic.spawn(car, distance, TRAFFIC.behind + 2, TRAFFIC.behind + 6, TRAFFIC_LANES.filter(lane => !ours.includes(lane)));
      }
      // Cars keep their distance within a lane: a faster car closes up on the
      // one ahead and then follows it rather than driving through it. Our car
      // counts too, in whichever lanes it straddles; when it cannot get round
      // a car, that car is nudged along ahead of it.
      for (const lane of TRAFFIC_LANES) {
        const queue = traffic.cars.filter(car => car.lane === lane).sort((a, b) => b.at - a.at);
        if (ours.includes(lane)) {
          for (const car of queue) {
            const gap = car.at - distance;
            if (gap >= 0 && gap < ROOM_AHEAD) car.at = distance + ROOM_AHEAD;
            else if (gap < 0 && gap > -ROOM_BEHIND) car.at = distance - ROOM_BEHIND;
          }
        }
        for (let index = 1; index < queue.length; index++) {
          const leader = queue[index - 1], car = queue[index];
          if (leader.at - car.at >= TRAFFIC_GAP) continue;
          car.at = leader.at - TRAFFIC_GAP;
          car.pace = Math.min(car.pace, leader.pace);
        }
      }
    }
    const counts = traffic.styles.map(() => 0);
    for (const car of traffic.cars) {
      place(path, car.at);
      const x = car.lane * TRAFFIC_LANE + car.offset;
      matrix.makeRotationY(-point.rel).setPosition(point.x + point.rx * x, 0, point.z + point.rz * x);
      for (const mesh of traffic.styles[car.style]) mesh.setMatrixAt(counts[car.style], matrix);
      counts[car.style]++;
    }
    traffic.styles.forEach((meshes, style) => meshes.forEach(mesh => { mesh.count = counts[style]; mesh.instanceMatrix.needsUpdate = true; }));
  }

  // Lane keeping for our car: if a slower car is close ahead in its lane, move
  // to a free neighbouring lane; drift back to the centre once it is clear.
  // The lateral move is a critically damped spring, so it eases in and out.
  const laneFree = (lane, distance, behind, ahead) => traffic.cars.every(car => car.lane !== lane || car.at - distance < -behind || car.at - distance > ahead);
  function steer(distance, dt, driving) {
    if (driving) {
      if (!laneFree(state.lane, distance, 0, AVOID_AHEAD)) {
        // Overtake on the left first, as Vietnamese road rules expect.
        const free = (state.lane === 0 ? [-1, 1] : [0]).find(lane => laneFree(lane, distance, 10, AVOID_AHEAD + 10));
        if (free !== undefined) { state.lane = free; state.laneHold = 0; }
      // Only head back with plenty of room, so the car does not weave.
      } else if (state.lane !== 0 && (state.laneHold += dt) > 2.5 && laneFree(0, distance, 12, AVOID_AHEAD * 2)) state.lane = 0;
    }
    // The guide swings over to the new lane quickly but not in a single frame.
    state.guideLane = ease(state.guideLane, state.lane * TRAFFIC_LANE, 4, dt);
    const target = state.lane * TRAFFIC_LANE;
    state.laneVelocity += ((target - state.laneOffset) * 3.2 - state.laneVelocity * 3.6) * dt;
    state.laneOffset += state.laneVelocity * dt;
  }

  function resize() {
    const width = canvas.clientWidth || innerWidth, height = canvas.clientHeight || innerHeight;
    renderer.setSize(width, height, false);
    composer.setSize(width, height);
    camera.aspect = width / height;
    // Shift the optical centre up so the car sits above the assistant and dock.
    camera.setViewOffset(width, height, 0, height * .09, width, height);
    camera.updateProjectionMatrix();
    stars.material.uniforms.scale.value = renderer.getPixelRatio() * Math.max(1, height / 900);
  }
  new ResizeObserver(resize).observe(canvas);
  resize();

  // Drag to turn the parked car; driving keeps the chase camera fixed.
  canvas.addEventListener('pointerdown', event => { if (state.mode !== 'parked') return; state.dragging = true; state.lastX = event.clientX; canvas.setPointerCapture(event.pointerId); });
  canvas.addEventListener('pointermove', event => {
    if (!state.dragging) return;
    const delta = (event.clientX - state.lastX) / canvas.clientWidth;
    state.lastX = event.clientX;
    state.orbit -= delta * 3.2;
    state.orbitVelocity = -delta * 3.2 * 60;
  });
  const release = () => { state.dragging = false; };
  canvas.addEventListener('pointerup', release);
  canvas.addEventListener('pointercancel', release);

  function frame() {
    const now = performance.now();
    state.frame = requestAnimationFrame(frame);
    const dt = Math.max(0, Math.min(.05, (now - state.last) / 1000));
    state.last = now;
    if (state.paused || document.hidden) return;
    state.time += dt;
    const driving = state.mode === 'driving';
    state.drive = ease(state.drive, driving ? 1 : 0, 2.2, dt);
    state.displaySpeed = ease(state.displaySpeed, driving ? state.speed : 0, 1.6, dt);
    // The car is lit up gently once the model has loaded.
    state.reveal = vehicle ? (state.reduced ? 1 : ease(state.reveal, 1, 1.4, dt)) : 0;
    const metersPerSecond = state.displaySpeed / 3.6;
    // On a planned route the road keeps pace with the map's simulation; otherwise
    // scenery scrolls a bit faster than real speed so city pace reads as motion.
    // A paused route stays on screen while the road fades out.
    const onRoute = Boolean(state.route) && (state.routeLive || !driving);
    if (!onRoute && (state.onRoute || !state.straight)) state.straight = straightPath(state.carHeading);
    state.onRoute = onRoute;
    const path = onRoute ? state.route : state.straight;
    if (onRoute) state.distance = state.routeDistance; else state.distance += metersPerSecond * dt * 1.8;

    const drive = state.drive, parked = 1 - drive, reveal = state.reveal;
    if (vehicle) {
      for (const wheel of vehicle.wheels.pivots) wheel.rotation.x -= metersPerSecond * 1.8 * dt / vehicle.wheels.radius;
      vehicle.glow(drive, state.voice, reveal);
      // About 1.5 s from closed to fully open, like a real window motor.
      state.windowOpen = state.reduced ? state.window : ease(state.windowOpen, state.window, 2.4, dt);
      vehicle.driverWindow?.(state.windowOpen);
      state.doorOpen = state.reduced ? state.door : ease(state.doorOpen, state.door, 2.6, dt);
      if (vehicle.driverDoor) vehicle.driverDoor.rotation.y = -DOOR_ANGLE * state.doorOpen;
    }
    for (const [light, intensity] of lights) light.intensity = intensity * (.15 + reveal * .85);
    const twinkle = state.reduced ? 0 : state.time;
    sky.material.uniforms.time.value = twinkle;
    stars.material.uniforms.time.value = twinkle;
    floor.pool.material.opacity = .4 + reveal * .45 + state.voice * .15;
    floor.orbits.material.opacity = parked;
    floor.halo.material.opacity = (.25 + state.voice * .6 + (state.reduced ? 0 : Math.sin(state.time * 1.1) * .05)) * parked;
    // See further down the road while driving, so turns show up in time.
    scene.fog.far = 54 + 46 * drive;
    road.asphalt.material.opacity = drive;
    road.edgeMaterial.opacity = .4 * drive;
    road.dashMaterial.opacity = .25 * drive;
    road.postMaterial.opacity = .3 * drive;
    road.streakMaterial.opacity = state.reduced ? 0 : Math.min(.45, state.displaySpeed / 90) * drive;
    road.beams.material.opacity = .28 * drive;
    for (const material of traffic.materials) material.opacity = drive;
    traffic.group.visible = drive > .01;
    road.guideMaterial.opacity = (state.reduced ? .7 : .62 + Math.sin(state.time * 2.2) * .08) * drive;

    // Manoeuvre cue: chevrons glow and point toward the coming turn.
    state.turnStrength = ease(state.turnStrength, driving && state.turn !== null ? 1 : 0, 3, dt);
    road.chevronMaterial.opacity = state.turnStrength * (.45 + Math.sin(state.time * 4) * .15);
    state.chevronTurn = ease(state.chevronTurn, -(state.turn || 0) * Math.PI / 2, 4, dt);
    // Without a route the car only leans toward a coming turn.
    state.yaw = ease(state.yaw, driving && !onRoute ? (state.turn || 0) * -.06 * state.turnStrength : 0, 1.5, dt);
    // Route following: the car drives along the planned route's shape and the
    // chase camera swings round after it, so bends and corners show up ahead.
    // The route distance arrives from the map simulation's own frame loop, so
    // per-frame steps are uneven; smooth the speed before steering with it.
    if (dt > 0) state.forward = ease(state.forward, Math.max(0, state.distance - state.lastDistance) / dt, 3, dt);
    state.lastDistance = state.distance;
    steer(state.distance, dt, driving);
    path.at(state.distance, carPoint);
    // Our car sits in its lane: shift the point we draw everything around.
    const across = THREE.MathUtils.degToRad(carPoint.h);
    carPoint.x += Math.cos(across) * state.laneOffset; carPoint.z += Math.sin(across) * state.laneOffset;
    // Steering into a lane change: the nose points along the car's actual path.
    state.laneYaw = ease(state.laneYaw, THREE.MathUtils.clamp(-Math.atan2(state.laneVelocity * 2.2, Math.max(state.forward, 6)), -.3, .3), 8, dt);
    // The chase camera trails the car sideways, so a lane change reads as the
    // car moving across rather than the road sliding under it.
    state.camLane = ease(state.camLane, state.laneOffset, 2.2, dt);
    const trail = (state.camLane - state.laneOffset) * drive;
    state.carHeading += ((carPoint.h - state.carHeading) % 360 + 540) % 360 - 180;
    state.camHeading = driving ? ease(state.camHeading, state.carHeading, 1.4, dt) : state.carHeading;
    view.heading = state.camHeading;
    view.cos = Math.cos(THREE.MathUtils.degToRad(view.heading)); view.sin = Math.sin(THREE.MathUtils.degToRad(view.heading));
    layoutRoad(path, state.distance);
    layoutTraffic(path, state.distance);
    const bend = THREE.MathUtils.degToRad(state.carHeading - state.camHeading) * drive;
    carPivot.rotation.y = state.yaw + state.laneYaw * drive - bend;
    carPivot.position.y = driving && !state.reduced ? Math.sin(state.time * 11) * .006 * Math.min(1, state.displaySpeed / 40) : 0;

    if (!state.dragging && !state.reduced) {
      state.orbitVelocity *= Math.exp(-dt * 2.5);
      state.orbit += (state.orbitVelocity + .05) * dt * parked;
    }
    const aspect = camera.aspect;
    // Keep the whole car in frame on narrow (portrait) screens.
    const fit = Math.max(1, 1.35 / aspect);
    const radius = 11.2 * fit;
    const parkedPosition = new THREE.Vector3(Math.sin(state.orbit) * radius, 1.7 + fit * .7, Math.cos(state.orbit) * radius);
    const chasePosition = new THREE.Vector3(state.yaw * 8 + trail, 3.6 + fit * .8, 13.5 * fit);
    desired.position.copy(parkedPosition).lerp(chasePosition, drive);
    desired.look.set(0, .62 - (fit - 1) * .3, 0).lerp(new THREE.Vector3(trail * .6, .2, -5), drive);
    if (state.mode === 'parked' && drive < .02) camera.position.copy(desired.position);
    else camera.position.lerp(desired.position, 1 - Math.exp(-dt * 3));
    look.lerp(desired.look, 1 - Math.exp(-dt * 3));
    camera.lookAt(look);
    // The sky and stars travel with the camera so they read as infinitely far.
    sky.position.copy(camera.position);
    stars.position.copy(camera.position);
    composer.render(dt);
  }
  state.frame = requestAnimationFrame(frame);

  const api = {
    setMode(mode) { state.mode = mode === 'driving' ? 'driving' : 'parked'; },
    setSpeed(kmh) { state.speed = Math.max(0, Number(kmh) || 0); },
    // -1 left, 0 straight, 1 right, null for no upcoming manoeuvre.
    setTurn(direction) { state.turn = direction; },
    // Planned route as [lng, lat] points with cumulative distances in metres,
    // or null; the drive simulation then reports how far along it the car is.
    setRoute(route) {
      state.route = route?.coordinates?.length > 1 ? routePath(route.coordinates, route.cumulative) : null;
      state.routeLive = false;
    },
    // Metres travelled along the route, or null when the simulation stops.
    setRouteDistance(distance) {
      state.routeLive = Number.isFinite(distance);
      if (state.routeLive) state.routeDistance = distance;
    },
    // Driver's window opening, 0 (closed) to 100 (fully open).
    setWindow(percent) { state.window = Math.max(0, Math.min(100, Number(percent) || 0)) / 100; },
    setDoor(open) { state.door = open ? 1 : 0; },
    setVoiceLevel(level) { state.voice = Math.max(0, Math.min(1, level)); },
    setPaused(paused) { if (state.paused && !paused) state.last = performance.now(); state.paused = paused; },
    setReduced(reduced) { state.reduced = reduced; }
  };
  window.__vivi3d = { scene, camera, renderer, state, api, traffic };
  return api;
}
