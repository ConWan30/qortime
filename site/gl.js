import * as THREE from './vendor/three.module.min.js';

const CY = new THREE.Color('#2ef2ff');
const BL = new THREE.Color('#4f7cff');
const WH = new THREE.Color('#f0f4ff');
const ease = (t) => 1 - Math.pow(1 - Math.min(Math.max(t, 0), 1), 4);
const clamp01 = (t) => Math.min(Math.max(t, 0), 1);

const MODES = {
  receipts: { speed: 1.0, tiltY: 0.35, tiltX: 0.25, strandAmp: 0.9, ringColor: CY },
  capture: { speed: 1.55, tiltY: 0.55, tiltX: 0.18, strandAmp: 1.35, ringColor: BL },
  watch: { speed: 1.25, tiltY: 0.42, tiltX: 0.32, strandAmp: 1.15, ringColor: CY },
};

export function init(canvas, { fine }) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, fine ? 1.75 : 1.5));
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(35, 1, 0.1, 100);
  cam.position.set(0, 0, 11.4);

  const root = new THREE.Group();
  scene.add(root);
  const seal = new THREE.Group();
  root.add(seal);

  const add = (m) => ((m.blending = THREE.AdditiveBlending), (m.transparent = true), (m.depthWrite = false), m);

  // Glow radial texture
  const gc = document.createElement('canvas');
  gc.width = gc.height = 128;
  const gx = gc.getContext('2d');
  const gr = gx.createRadialGradient(64, 64, 0, 64, 64, 64);
  gr.addColorStop(0, 'rgba(255,255,255,1)');
  gr.addColorStop(0.28, 'rgba(255,255,255,.38)');
  gr.addColorStop(1, 'rgba(255,255,255,0)');
  gx.fillStyle = gr;
  gx.fillRect(0, 0, 128, 128);
  const glowTex = new THREE.CanvasTexture(gc);

  // Outer Chrono-Loom segmented ring
  const R = 2.15;
  const N = 36;
  const segs = [];
  for (let i = 0; i < N; i++) {
    const g = new THREE.TorusGeometry(R, 0.095, 12, 24, ((Math.PI * 2) / N) * 0.94);
    const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: CY.clone().lerp(WH, 0.16) }));
    const halo = new THREE.Mesh(
      new THREE.TorusGeometry(R, 0.28, 8, 24, ((Math.PI * 2) / N) * 0.94),
      add(new THREE.MeshBasicMaterial({ color: CY, opacity: 0.045 }))
    );
    const s = new THREE.Group();
    s.add(m, halo);
    s.rotation.z = (i / N) * Math.PI * 2;
    s.userData = {
      from: new THREE.Vector3((Math.random() - 0.5) * 14, (Math.random() - 0.5) * 10, (Math.random() - 0.2) * 8),
      rz: (Math.random() - 0.5) * 6,
      d: Math.random() * 0.55,
    };
    seal.add(s);
    segs.push(s);
  }

  // Orbital SHA-256 lattice hex nodes around the seal
  const orbitRing = new THREE.Group();
  seal.add(orbitRing);
  const hexNodes = [];
  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * Math.PI * 2;
    const node = new THREE.Mesh(
      new THREE.OctahedronGeometry(0.11, 0),
      add(new THREE.MeshBasicMaterial({ color: i % 2 ? BL : CY, opacity: 0.88 }))
    );
    node.position.set(Math.cos(a) * (R * 1.24), Math.sin(a) * (R * 1.24), 0.15);
    orbitRing.add(node);
    hexNodes.push(node);
  }

  const inner = new THREE.Mesh(
    new THREE.RingGeometry(R * 0.77, R * 0.776, 128),
    add(new THREE.MeshBasicMaterial({ color: CY, opacity: 0.35, side: THREE.DoubleSide }))
  );
  seal.add(inner);

  const ticks = [];
  for (let i = 0; i < 12; i++) {
    const big = i % 3 === 0;
    const t = new THREE.Mesh(
      new THREE.PlaneGeometry(big ? 0.095 : 0.045, big ? 0.38 : 0.18),
      new THREE.MeshBasicMaterial({ color: big ? CY : new THREE.Color('#6f8db0'), transparent: true })
    );
    const a = (i / 12) * Math.PI * 2;
    t.position.set(Math.sin(a) * R * 0.89, Math.cos(a) * R * 0.89, 0);
    t.rotation.z = -a;
    seal.add(t);
    ticks.push(t);
  }

  const mkHand = (len, w, col) => {
    const p = new THREE.Group();
    const h = new THREE.Mesh(new THREE.PlaneGeometry(w, len), new THREE.MeshBasicMaterial({ color: col, transparent: true }));
    h.position.y = len / 2;
    p.add(h);
    const glow = new THREE.Sprite(add(new THREE.SpriteMaterial({ map: glowTex, color: col, opacity: 0.5 })));
    glow.scale.set(w * 6, len * 1.4, 1);
    glow.position.y = len / 2;
    p.add(glow);
    return p;
  };
  const whiteHand = mkHand(R * 0.62, 0.18, WH);
  whiteHand.position.z = 0.02;
  seal.add(whiteHand);
  const accentHand = mkHand(R * 1.2, 0.24, BL);
  accentHand.position.z = 0.04;
  seal.add(accentHand);
  const hub = new THREE.Mesh(new THREE.CircleGeometry(0.22, 32), new THREE.MeshBasicMaterial({ color: BL }));
  hub.position.z = 0.06;
  seal.add(hub);

  const flash = new THREE.Sprite(add(new THREE.SpriteMaterial({ map: glowTex, color: new THREE.Color('#d6e2ff'), opacity: 0 })));
  flash.position.z = 0.1;
  seal.add(flash);
  const shocks = [0, 1, 2].map((i) => {
    const m = new THREE.Mesh(
      new THREE.RingGeometry(0.96, 1, 128),
      add(new THREE.MeshBasicMaterial({ color: i ? CY : BL, opacity: 0, side: THREE.DoubleSide }))
    );
    seal.add(m);
    return m;
  });
  const backGlow = new THREE.Sprite(add(new THREE.SpriteMaterial({ map: glowTex, color: CY, opacity: 0.16 })));
  backGlow.scale.set(11, 11, 1);
  backGlow.position.z = -1;
  seal.add(backGlow);

  // Three-strand braided telemetry waveform (Capture BL, Anchor WH, Watch CY)
  const P = 2850;
  const pos = new Float32Array(P * 3);
  const seed = new Float32Array(P);
  const col = new Float32Array(P * 3);
  const palette = [BL, CY, WH];
  for (let i = 0; i < P; i++) {
    seed[i] = Math.random();
    const lane = i % 3;
    const c = palette[lane].clone();
    col.set([c.r, c.g, c.b], i * 3);
    pos[i * 3] = lane;
  }
  const pg = new THREE.BufferGeometry();
  pg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  pg.setAttribute('color', new THREE.BufferAttribute(col, 3));
  pg.setAttribute('seed', new THREE.BufferAttribute(seed, 1));
  const pm = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    vertexColors: true,
    uniforms: { uT: { value: 0 }, uAmp: { value: 0.8 }, uPx: { value: renderer.getPixelRatio() } },
    vertexShader: `attribute float seed; uniform float uT,uAmp,uPx; varying vec3 vC; varying float vA;
      void main(){
        float strand = position.x;
        float x = mod(seed * 32.0 + uT * (1.3 + strand * 0.25), 32.0) - 16.0;
        float phase = strand * 2.094395 + uT * 1.9;
        float wave = sin(x * 0.55 + phase) * 0.72 * uAmp + cos(x * 1.4 - uT * 2.2) * 0.18 * uAmp;
        float depth = cos(x * 0.55 + phase) * 0.9 - 2.4;
        vec3 p = vec3(x, wave - 0.15, depth);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = (2.2 + fract(seed * 19.0) * 3.8) * uPx * (9.2 / max(1.5, -mv.z));
        vC = color;
        vA = smoothstep(16.0, 9.5, abs(x));
      }`,
    fragmentShader: `varying vec3 vC; varying float vA;
      void main(){
        float d = length(gl_PointCoord - 0.5);
        if(d > 0.5) discard;
        gl_FragColor = vec4(vC, pow(1.0 - d * 2.0, 1.5) * vA * 0.85);
      }`,
  });
  const pts = new THREE.Points(pg, pm);
  pts.frustumCulled = false;
  root.add(pts);

  let modeCfg = MODES.receipts;
  let simTime = 0;
  window.__qorGL = {
    setMode(mode) {
      if (MODES[mode]) modeCfg = MODES[mode];
    },
  };

  let W = 1;
  let H = 1;
  const resize = () => {
    W = canvas.clientWidth;
    H = canvas.clientHeight;
    renderer.setSize(W, H, false);
    cam.aspect = W / Math.max(1, H);
    cam.updateProjectionMatrix();
    const mobile = W < 900;
    seal.position.set(mobile ? 0 : 2.85 * Math.min(1.22, cam.aspect / 1.6), mobile ? 1.8 : 0.15, 0);
    seal.scale.setScalar(mobile ? Math.min(0.54, cam.aspect * 0.82) : 0.76);
  };
  resize();
  addEventListener('resize', resize);

  let mx = 0;
  let my = 0;
  let tx = 0;
  let ty = 0;
  let scrollK = 0;
  let visible = true;
  let raf = 0;
  if (fine) {
    addEventListener(
      'pointermove',
      (e) => {
        mx = e.clientX / innerWidth - 0.5;
        my = e.clientY / innerHeight - 0.5;
      },
      { passive: true }
    );
  }
  addEventListener('scroll', () => {
    scrollK = clamp01(scrollY / Math.max(1, innerHeight));
  }, { passive: true });
  new IntersectionObserver(([e]) => {
    visible = e.isIntersecting;
    if (visible && !raf) {
      last = performance.now();
      raf = requestAnimationFrame(loop);
    }
  }).observe(canvas);

  const LOCK = Math.PI * 0.75;
  let last = performance.now();
  let cycleStart = 0;

  function loop(now) {
    raf = 0;
    if (!visible) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    simTime += dt * modeCfg.speed;
    const t = simTime;
    const c = t - cycleStart;

    segs.forEach((s) => {
      const k = ease((c - s.userData.d) / 1.5);
      s.position.copy(s.userData.from).multiplyScalar(1 - k);
      s.rotation.x = s.userData.rz * (1 - k);
      s.scale.setScalar(0.42 + 0.58 * k);
    });
    orbitRing.rotation.z = t * 0.24;
    hexNodes.forEach((n, idx) => {
      n.rotation.y = t * 1.4 + idx;
      n.scale.setScalar(0.85 + Math.sin(t * 2.5 + idx) * 0.22);
    });
    ticks.forEach((m, i) => (m.material.opacity = ease((c - 1.3 - i * 0.04) / 0.4)));
    inner.material.opacity = 0.35 * ease((c - 1.1) / 0.8);

    const sw = clamp01((c - 1.8) / 1.9);
    const sweepE = 1 - Math.pow(1 - sw, 3);
    let ang = -(sweepE * (Math.PI * 4 + LOCK));
    const lt = c - 3.7;
    if (lt > 0) ang = -LOCK + Math.sin(lt * 26) * Math.exp(-lt * 7) * 0.085;
    accentHand.rotation.z = ang;
    accentHand.children.forEach((ch) => (ch.material.opacity = ease((c - 1.6) / 0.4) * (ch.isSprite ? 0.5 : 1)));
    whiteHand.rotation.z = -sweepE * Math.PI * 2;
    whiteHand.children.forEach((ch) => (ch.material.opacity = ease((c - 1.4) / 0.4) * (ch.isSprite ? 0.5 : 1)));
    hub.scale.setScalar(ease((c - 1.5) / 0.3) + (lt > 0 ? Math.exp(-lt * 6) * 0.55 : 0));

    const fl = lt > 0 ? Math.exp(-lt * 5) : 0;
    flash.material.opacity = fl;
    flash.scale.setScalar(2 + 8.5 * (1 - fl));
    shocks.forEach((m, i) => {
      const k = lt - i * 0.12;
      if (k > 0 && k < 1.8) {
        const e = ease(k / 1.8);
        m.scale.setScalar(R * (1 + e * 2.7));
        m.material.opacity = (1 - e) * 0.85;
      } else {
        m.material.opacity = 0;
      }
    });
    backGlow.material.opacity = 0.14 + fl * 0.36 + Math.sin(t * 2) * 0.02;
    pm.uniforms.uT.value = t;
    pm.uniforms.uAmp.value += (modeCfg.strandAmp - pm.uniforms.uAmp.value) * 0.08;
    if (c > 11.5) cycleStart = t;

    tx += (mx - tx) * 0.05;
    ty += (my - ty) * 0.05;
    root.rotation.y = tx * modeCfg.tiltY;
    root.rotation.x = ty * modeCfg.tiltX;
    seal.rotation.z = Math.sin(t * 0.3) * 0.03 - scrollK * 0.75;
    root.position.y = scrollK * 2.1;
    cam.position.z = 11.4 + scrollK * 3.8;

    renderer.render(scene, cam);
    raf = requestAnimationFrame(loop);
  }
  raf = requestAnimationFrame(loop);
}
