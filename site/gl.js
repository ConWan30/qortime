import * as THREE from './vendor/three.module.min.js';
const CY = new THREE.Color('#2ef2ff'), AC = new THREE.Color('#4f7cff');
const ease = t => 1 - Math.pow(1 - Math.min(Math.max(t, 0), 1), 4);
const clamp01 = t => Math.min(Math.max(t, 0), 1);

export function init(canvas, { fine }) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, fine ? 1.75 : 1.5)); // phones: ~27% fewer pixels for the full-screen hero
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(35, 1, .1, 100); cam.position.set(0, 0, 11);
  const root = new THREE.Group(); scene.add(root);
  const seal = new THREE.Group(); root.add(seal);
  const add = (m) => (m.blending = THREE.AdditiveBlending, m.transparent = true, m.depthWrite = false, m);

  // glow sprite texture
  const gc = document.createElement('canvas'); gc.width = gc.height = 128; const gx = gc.getContext('2d');
  const gr = gx.createRadialGradient(64, 64, 0, 64, 64, 64); gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(.25, 'rgba(255,255,255,.35)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
  gx.fillStyle = gr; gx.fillRect(0, 0, 128, 128); const glowTex = new THREE.CanvasTexture(gc);

  // ring built from segments that fly in
  const R = 2.2, N = 36, segs = [];
  for (let i = 0; i < N; i++) {
    const g = new THREE.TorusGeometry(R, .11, 12, 24, (Math.PI * 2) / N * .96);
    const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: CY.clone().lerp(new THREE.Color('#ffffff'), .15) }));
    const halo = new THREE.Mesh(new THREE.TorusGeometry(R, .3, 8, 24, (Math.PI * 2) / N * .96), add(new THREE.MeshBasicMaterial({ color: CY, opacity: .045 })));
    const s = new THREE.Group(); s.add(m, halo); s.rotation.z = (i / N) * Math.PI * 2;
    s.userData = { from: new THREE.Vector3((Math.random() - .5) * 14, (Math.random() - .5) * 10, (Math.random() - .2) * 8), rz: (Math.random() - .5) * 6, d: Math.random() * .6 };
    seal.add(s); segs.push(s);
  }
  // inner thin ring + ticks
  const inner = new THREE.Mesh(new THREE.RingGeometry(R * .78, R * .785, 128), add(new THREE.MeshBasicMaterial({ color: CY, opacity: .35, side: THREE.DoubleSide })));
  seal.add(inner);
  const ticks = [];
  for (let i = 0; i < 12; i++) {
    const big = i % 3 === 0, t = new THREE.Mesh(new THREE.PlaneGeometry(big ? .1 : .05, big ? .42 : .2), new THREE.MeshBasicMaterial({ color: big ? CY : new THREE.Color('#6f8db0'), transparent: true }));
    const a = (i / 12) * Math.PI * 2; t.position.set(Math.sin(a) * R * .9, Math.cos(a) * R * .9, 0); t.rotation.z = -a; seal.add(t); ticks.push(t);
  }
  // hands
  const mkHand = (len, w, col) => { const p = new THREE.Group(); const h = new THREE.Mesh(new THREE.PlaneGeometry(w, len), new THREE.MeshBasicMaterial({ color: col, transparent: true })); h.position.y = len / 2; p.add(h);
    const glow = new THREE.Sprite(add(new THREE.SpriteMaterial({ map: glowTex, color: col, opacity: .5 }))); glow.scale.set(w * 6, len * 1.4, 1); glow.position.y = len / 2; p.add(glow); return p; };
  const white = mkHand(R * .62, .2, new THREE.Color('#ffffff')); white.position.z = .02; seal.add(white);
  const accentHand = mkHand(R * 1.22, .26, AC); accentHand.position.z = .04; seal.add(accentHand);
  const hub = new THREE.Mesh(new THREE.CircleGeometry(.24, 32), new THREE.MeshBasicMaterial({ color: AC })); hub.position.z = .06; seal.add(hub);
  // flash + shockwaves
  const flash = new THREE.Sprite(add(new THREE.SpriteMaterial({ map: glowTex, color: new THREE.Color('#d6e2ff'), opacity: 0 }))); flash.position.z = .1; seal.add(flash);
  const shocks = [0, 1, 2].map(i => { const m = new THREE.Mesh(new THREE.RingGeometry(.96, 1, 128), add(new THREE.MeshBasicMaterial({ color: i ? CY : AC, opacity: 0, side: THREE.DoubleSide }))); seal.add(m); return m; });
  const backGlow = new THREE.Sprite(add(new THREE.SpriteMaterial({ map: glowTex, color: CY, opacity: .18 }))); backGlow.scale.set(11, 11, 1); backGlow.position.z = -1; seal.add(backGlow);

  // particle waveform (input chain)
  const P = 2600, pos = new Float32Array(P * 3), seed = new Float32Array(P), col = new Float32Array(P * 3);
  for (let i = 0; i < P; i++) { seed[i] = Math.random(); const c = CY.clone().lerp(AC, Math.random() ** 2); col.set([c.r, c.g, c.b], i * 3); }
  const pg = new THREE.BufferGeometry(); pg.setAttribute('position', new THREE.BufferAttribute(pos, 3)); pg.setAttribute('color', new THREE.BufferAttribute(col, 3)); pg.setAttribute('seed', new THREE.BufferAttribute(seed, 1));
  const pm = new THREE.ShaderMaterial({ transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, vertexColors: true,
    uniforms: { uT: { value: 0 }, uAmp: { value: 0 }, uPx: { value: renderer.getPixelRatio() } },
    vertexShader: `attribute float seed; uniform float uT,uAmp,uPx; varying vec3 vC; varying float vA;
      void main(){ float x = mod(seed*30.0 + uT*1.6, 30.0) - 15.0; float lane = fract(seed*91.7)-.5;
        float y = (sin(x*.9+uT*2.)*.55 + sin(x*2.3-uT*3.1)*.22 + step(.82,fract(x*.37+uT*.4))*sin(x*9.)*.6)*uAmp + lane*.35;
        vec3 p = vec3(x, y-.2, -2.5 + lane*2.0); vec4 mv = modelViewMatrix*vec4(p,1.); gl_Position = projectionMatrix*mv;
        gl_PointSize = (2.0+fract(seed*13.)*4.0)*uPx*(9.0/-mv.z); vC = color; vA = smoothstep(15.,9.,abs(x)); }`,
    fragmentShader: `varying vec3 vC; varying float vA; void main(){ float d = length(gl_PointCoord-.5); if(d>.5) discard; gl_FragColor = vec4(vC, pow(1.-d*2.,1.5)*vA); }` });
  const pts = new THREE.Points(pg, pm); pts.frustumCulled = false; root.add(pts);

  // layout
  let W = 1, H = 1;
  const resize = () => { W = canvas.clientWidth; H = canvas.clientHeight; renderer.setSize(W, H, false); cam.aspect = W / H; cam.updateProjectionMatrix();
    const mobile = W < 900; seal.position.set(mobile ? 0 : 2.9 * Math.min(1.25, cam.aspect / 1.6), mobile ? 1.9 : 0, 0); seal.scale.setScalar(mobile ? Math.min(.55, cam.aspect * .85) : .78); };
  resize(); addEventListener('resize', resize);

  let mx = 0, my = 0, tx = 0, ty = 0, scrollK = 0, visible = true, raf = 0;
  if (fine) addEventListener('pointermove', e => { mx = e.clientX / innerWidth - .5; my = e.clientY / innerHeight - .5; }, { passive: true });
  addEventListener('scroll', () => { scrollK = clamp01(scrollY / innerHeight); }, { passive: true });
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; if (visible && !raf) { last = performance.now(); raf = requestAnimationFrame(loop); } }).observe(canvas);

  const LOCK = Math.PI * .75; // accent hand points to the lower-right corner, like a Q tail
  let t0 = performance.now(), last = t0, cycleStart = 0;
  function loop(now) {
    raf = 0; if (!visible) return;
    const t = (now - t0) / 1000; last = now;
    const c = t - cycleStart;
    // assembly 0..2.2s
    segs.forEach(s => { const k = ease((c - s.userData.d) / 1.6); s.position.copy(s.userData.from).multiplyScalar(1 - k); s.rotation.x = s.userData.rz * (1 - k); s.children[0].material.opacity = 1; s.scale.setScalar(.4 + .6 * k); });
    ticks.forEach((m, i) => m.material.opacity = ease((c - 1.4 - i * .05) / .4));
    inner.material.opacity = .35 * ease((c - 1.2) / .8);
    // sweep 2.0..4.0, lock at 4.0
    const sw = clamp01((c - 2.0) / 2.0), sweepE = 1 - Math.pow(1 - sw, 3);
    let ang = -(sweepE * (Math.PI * 4 + LOCK));
    const lt = c - 4.0;
    if (lt > 0) ang = -LOCK + Math.sin(lt * 26) * Math.exp(-lt * 7) * .09; // overshoot snap
    accentHand.rotation.z = ang; accentHand.children.forEach(ch => ch.material.opacity = ease((c - 1.8) / .4) * (ch.isSprite ? .5 : 1));
    white.rotation.z = -sweepE * Math.PI * 2; white.children.forEach(ch => ch.material.opacity = ease((c - 1.6) / .4) * (ch.isSprite ? .5 : 1));
    hub.scale.setScalar(ease((c - 1.6) / .3) + (lt > 0 ? Math.exp(-lt * 6) * .6 : 0));
    // flash + shockwave
    const fl = lt > 0 ? Math.exp(-lt * 5) : 0; flash.material.opacity = fl; flash.scale.setScalar(2 + 9 * (1 - fl));
    shocks.forEach((m, i) => { const k = lt - i * .12; if (k > 0 && k < 1.8) { const e = ease(k / 1.8); m.scale.setScalar(R * (1 + e * 2.8)); m.material.opacity = (1 - e) * .9; } else m.material.opacity = 0; });
    backGlow.material.opacity = .14 + fl * .4 + Math.sin(t * 2) * .02;
    // input waveform energises around the lock
    pm.uniforms.uT.value = t; pm.uniforms.uAmp.value = .6 + 1.2 * (lt > 0 ? Math.exp(-lt * 1.5) : clamp01(c / 2) * .4);
    if (c > 11) cycleStart = t; // replay the seal every ~11s
    // parallax + scroll exit
    tx += (mx - tx) * .05; ty += (my - ty) * .05;
    root.rotation.y = tx * .35; root.rotation.x = ty * .25;
    seal.rotation.z = Math.sin(t * .3) * .03 - scrollK * .8; root.position.y = scrollK * 2.2; cam.position.z = 11 + scrollK * 4;
    renderer.render(scene, cam);
    raf = requestAnimationFrame(loop);
  }
  raf = requestAnimationFrame(loop);
}
