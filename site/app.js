(() => {
  const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fine = matchMedia('(pointer: fine)').matches;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  if (RM) document.documentElement.classList.add('rm');

  // digit columns (final value is the resting state)
  $$('.dg').forEach(d => {
    const v = +d.dataset.v, col = document.createElement('span'); col.className = 'col';
    for (let i = 0; i < 30; i++) { const b = document.createElement('b'); b.textContent = i % 10; col.appendChild(b); }
    d.appendChild(col); d._v = v + 20; col.style.transform = `translateY(-${d._v}em)`;
  });
  $('#scorebug')?.classList.add('locked');
  $$('.g').forEach(g => g.classList.add('on'));

  // hero WebGL (lazy, with fallback)
  const canvas = $('#gl');
  const glOK = (() => { try { const c = document.createElement('canvas'); return !!(c.getContext('webgl2') || c.getContext('webgl')); } catch { return false; } })();
  if (!glOK || RM) document.documentElement.classList.add('no-gl');
  else {
    const start = () => import('./gl.js').then(m => m.init(canvas, { fine })).catch(() => document.documentElement.classList.add('no-gl'));
    (window.requestIdleCallback || (f => setTimeout(f, 60)))(start, { timeout: 400 });
  }

  if (RM || !window.gsap) return; // calm static version: everything already in its final state
  const { gsap } = window; gsap.registerPlugin(ScrollTrigger);

  // smooth scroll
  let lenis;
  if (window.Lenis) {
    lenis = new Lenis({ lerp: 0.09, smoothWheel: true });
    lenis.on('scroll', ScrollTrigger.update);
    gsap.ticker.add(t => lenis.raf(t * 1000)); gsap.ticker.lagSmoothing(0);
    $$('a[href^="#"]').forEach(a => a.addEventListener('click', e => { const t = $(a.getAttribute('href')); if (t) { e.preventDefault(); lenis.scrollTo(t, { offset: -10 }); } }));
  }

  // cursor glow + magnetic buttons
  if (fine) {
    document.body.classList.add('has-cursor');
    const c = $('.cursor'), qx = gsap.quickTo(c, 'x', { duration: .5, ease: 'power3' }), qy = gsap.quickTo(c, 'y', { duration: .5, ease: 'power3' });
    addEventListener('pointermove', e => { qx(e.clientX); qy(e.clientY); }, { passive: true });
    $$('.mag').forEach(b => {
      const inner = b.querySelector('span');
      b.addEventListener('pointermove', e => { const r = b.getBoundingClientRect(), x = e.clientX - r.left - r.width / 2, y = e.clientY - r.top - r.height / 2;
        gsap.to(b, { x: x * .3, y: y * .4, duration: .4, ease: 'power3' }); inner && gsap.to(inner, { x: x * .15, y: y * .2, duration: .4, ease: 'power3' }); });
      b.addEventListener('pointerleave', () => { gsap.to([b, inner].filter(Boolean), { x: 0, y: 0, duration: .8, ease: 'elastic.out(1,.4)' }); });
    });
    $$('.card').forEach(cd => { cd.addEventListener('pointermove', e => { const r = cd.getBoundingClientRect();
      gsap.to(cd, { rotateY: ((e.clientX - r.left) / r.width - .5) * 8, rotateX: -((e.clientY - r.top) / r.height - .5) * 8, transformPerspective: 900, duration: .5 }); });
      cd.addEventListener('pointerleave', () => gsap.to(cd, { rotateX: 0, rotateY: 0, duration: .8 })); });
  }

  // kinetic hero type
  $$('h1 .line').forEach(l => { const t = l.textContent; l.textContent = ''; [...t].forEach(ch => { const s = document.createElement('span'); s.className = 'ch'; s.textContent = ch === ' ' ? '\u00a0' : ch; l.appendChild(s); }); });
  gsap.timeline({ delay: .2 })
    .from('h1 .ch', { yPercent: 115, rotate: 8, opacity: 0, duration: 1.1, ease: 'expo.out', stagger: .035 })
    .from('.reveal-up', { y: 30, opacity: 0, duration: 1, ease: 'expo.out', stagger: .12 }, .5)
    .from('.nav', { y: -80, opacity: 0, duration: 1, ease: 'expo.out' }, .3);
  gsap.to('.hero-copy', { yPercent: -30, opacity: .2, ease: 'none', scrollTrigger: { trigger: '.hero', start: 'top top', end: 'bottom top', scrub: true } });

  // split headings
  $$('.split').forEach(h => {
    const words = h.textContent.trim().split(/\s+/); h.textContent = '';
    words.forEach((w, i) => { const o = document.createElement('span'); o.className = 'w'; const n = document.createElement('span'); n.textContent = w; o.appendChild(n); h.appendChild(o); if (i < words.length - 1) h.appendChild(document.createTextNode(' ')); });
    gsap.from(h.querySelectorAll('.w>span'), { yPercent: 110, duration: 1, ease: 'expo.out', stagger: .06, scrollTrigger: { trigger: h, start: 'top 85%' } });
  });
  $$('.scene-copy p:not(.num), .ticks, .tiers, .cards .card, .limits, .cta p, .cta .ctas, .vm-panel').forEach(el =>
    gsap.from(el, { y: 40, opacity: 0, duration: 1, ease: 'expo.out', scrollTrigger: { trigger: el, start: 'top 90%' } }));

  const mobile = innerWidth < 900;
  const pin = (sel, len) => ({ trigger: sel, start: 'top top', end: '+=' + (mobile ? len * .7 : len) + '%', scrub: 1, pin: sel + ' .pin', anticipatePin: 1 });

  // 01 scorebug digit-by-digit then sealed
  const sb = $('#scorebug');
  const t1 = gsap.timeline({ scrollTrigger: { ...pin('#capture', 140), onUpdate: s => sb.classList.toggle('locked', s.progress > .62) } });
  t1.from(sb, { scale: .85, rotateX: 30, opacity: 0, duration: .6, transformPerspective: 800 })
    .fromTo('.scan', { x: 0 }, { x: () => sb.offsetWidth + 200, duration: 1.2, ease: 'none', repeat: 1 }, .3)
    .from('.ocr-bar i', { scaleX: 0, duration: 2.2, ease: 'none' }, .3)
    .from('.ocr-st', { opacity: 0, duration: .2 }, 2.4);
  $$('.dg').forEach((d, i) => t1.fromTo(d.firstChild, { y: 0 }, { y: () => -d._v * d.offsetHeight, duration: .9, ease: 'power3.inOut' }, .5 + i * .35));
  t1.from('.sb-time,.sb-clock em', { opacity: 0, duration: .3 }, 1.9)
    .from('#sbseal', { scale: 3.2, opacity: 0, rotate: -40, duration: .5, ease: 'power4.in' }, 2.6)
    .fromTo('#sbshock', { scale: .3, opacity: .9 }, { scale: 6, opacity: 0, duration: .8, ease: 'power2.out' }, 3.1)
    .to(sb, { x: 6, duration: .05, yoyo: true, repeat: 5 }, 3.1)
    .to({}, { duration: .8 });

  // 02 button challenge rhythm
  const gl = $$('.g'); gl.forEach(g => g.classList.remove('on'));
  const wave = $('#pwave'); const spikes = [];
  const t2 = gsap.timeline({ scrollTrigger: pin('#challenge', 160) });
  t2.from('.pad-wrap', { y: 60, opacity: 0, duration: .5 }).from('.paused', { opacity: 0, duration: .2, repeat: 3, yoyo: true }, .3);
  const drawWave = p => { let d = 'M0 45'; for (let x = 0; x <= 600; x += 6) { let y = Math.sin(x * .05 + p * 20) * 3; spikes.forEach(s => { if (p >= s) { const k = Math.exp(-((x - (s - .2) * 1000) ** 2) / 300); y += k * -38 * Math.cos(x * .3); } }); d += ` L${x} ${(45 + y).toFixed(1)}`; } wave.setAttribute('d', d); };
  gl.forEach((g, i) => { const at = .9 + i * .45; spikes.push(.2 + i * .07);
    t2.call(() => g.classList.toggle('on', t2.time() >= at), null, at)
      .fromTo(g, { scale: 1 }, { scale: 1.18, duration: .12, yoyo: true, repeat: 1, ease: 'power2.out' }, at); });
  t2.eventCallback('onUpdate', () => { const t = t2.time(); gl.forEach((g, i) => g.classList.toggle('on', t >= .9 + i * .45)); drawWave(t2.progress() * .65); });
  t2.from('#winbar', { scaleX: 0, duration: 2.6, ease: 'none' }, .9).from('#winok', { opacity: 0, x: -10, duration: .3 }, 3.6).to({}, { duration: .8 });
  drawWave(0);

  // 03 checkpoint chain, horizontal
  const track = $('#chaintrack'); const cps = $$('.cp', track);
  const t3 = gsap.timeline({ scrollTrigger: { ...pin('#chain', 220), invalidateOnRefresh: true } });
  t3.to(track, { x: () => Math.min(0, innerWidth * .9 - track.scrollWidth - 40), duration: cps.length, ease: 'none' }, 0)
    .fromTo('.chain-line', { scaleX: 0, transformOrigin: 'left' }, { scaleX: 1, duration: cps.length, ease: 'none' }, 0);
  cps.forEach((c, i) => t3.fromTo(c, { y: 40, opacity: 0 }, { y: 0, opacity: 1, duration: .4 }, i * .8).fromTo(c.querySelector('.ref'), { scale: 3, opacity: 0, rotate: -30 }, { scale: 1, opacity: 1, rotate: 8, duration: .25, ease: 'power4.in' }, i * .8 + .4));

  // 04 receipt prints, hash types itself, signatures, stamp
  const hash = $('#hash'), full = hash.dataset.full;
  const t4 = gsap.timeline({ scrollTrigger: pin('#receipt', 170) });
  const typer = { n: 0 };
  t4.fromTo('#paper', { clipPath: 'inset(0 0 100% 0)', y: -40 }, { clipPath: 'inset(0 0 0% 0)', y: 0, duration: 1.4, ease: 'none' })
    .fromTo(typer, { n: 0 }, { n: full.length, duration: 1.4, ease: 'none', onStart: () => hash.classList.add('typing'),
      onUpdate: () => { const n = Math.round(typer.n); hash.textContent = full.slice(0, n); hash.classList.toggle('typing', n < full.length); } }, 1.2);
  $$('.sigs li').forEach((li, i) => t4.from(li, { x: -20, opacity: 0, duration: .3 }, 2.7 + i * .35).from(li.querySelector('em'), { scale: 0, duration: .2, ease: 'back.out(3)' }, 2.9 + i * .35));
  t4.from('#pstamp', { scale: 3, opacity: 0, rotate: 30, duration: .4, ease: 'power4.in' }, 4).to('.paper', { x: 4, duration: .05, yoyo: true, repeat: 3 }, 4.4).to({}, { duration: .6 });

  // 05 verifier badge flips
  const t5 = gsap.timeline({ scrollTrigger: pin('#verify', 120) });
  t5.from('#flip', { y: 60, opacity: 0, duration: .4 })
    .fromTo('.flip-in', { rotateY: 0 }, { rotateY: 180, duration: 1, ease: 'power3.inOut' }, .8)
    .fromTo('.face.back', { boxShadow: '0 0 0 rgba(61,255,168,0)' }, { boxShadow: '0 0 120px rgba(61,255,168,.45)', duration: .3, yoyo: true, repeat: 1 }, 1.7)
    .from('#verify .badges .badge', { y: 20, opacity: 0, stagger: .15, duration: .3 }, 1.9).to({}, { duration: .5 });

  // subtle parallax
  $$('.cards').forEach(a => gsap.fromTo(a, { yPercent: 6 }, { yPercent: -6, ease: 'none', scrollTrigger: { trigger: a, start: 'top bottom', end: 'bottom top', scrub: true } }));
  addEventListener('load', () => ScrollTrigger.refresh());
})();
