(() => {
  const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fine = matchMedia('(pointer: fine)').matches;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // 1. Monotonic QPC clock readout in hero bar
  const qpcEl = $('#qpc-clock');
  if (qpcEl) {
    const baseUs = 4819204000;
    const t0 = performance.now();
    const tick = () => {
      const us = baseUs + Math.round((performance.now() - t0) * 1000);
      qpcEl.textContent = `${String(us).replace(/\B(?=(\d{3})+(?!\d))/g, ',')} us`;
      if (!RM) requestAnimationFrame(tick);
    };
    tick();
  }

  // 2. Lazy WebGL Chrono-Loom initialization with fallback
  const glCanvas = $('#gl');
  if (glCanvas) {
    let glOK = false;
    try {
      const c = document.createElement('canvas');
      glOK = !!(c.getContext('webgl2') || c.getContext('webgl'));
    } catch {
      glOK = false;
    }
    if (!glOK || RM) {
      document.documentElement.classList.add('no-gl');
    } else {
      const startGL = () =>
        import('./gl.js')
          .then((m) => m.init(glCanvas, { fine }))
          .catch(() => document.documentElement.classList.add('no-gl'));
      (window.requestIdleCallback || ((f) => setTimeout(f, 50)))(startGL, { timeout: 400 });
    }
  }

  // 3. Pointer spotlight tracking for .beam-card elements
  if (fine) {
    $$('.beam-card').forEach((card) => {
      card.addEventListener(
        'pointermove',
        (e) => {
          const r = card.getBoundingClientRect();
          card.style.setProperty('--mx', `${(e.clientX - r.left).toFixed(1)}px`);
          card.style.setProperty('--my', `${(e.clientY - r.top).toFixed(1)}px`);
        },
        { passive: true }
      );
    });
  }

  // 4. Interactive Hero Trio Switchboard
  const pills = $$('.sw-pill[data-trio-mode]');
  if (pills.length) {
    const activate = (pill) => {
      pills.forEach((p) => p.classList.toggle('is-active', p === pill));
      window.__qorGL?.setMode?.(pill.dataset.trioMode);
    };
    pills.forEach((pill) => {
      pill.addEventListener('click', () => activate(pill));
      if (fine) pill.addEventListener('mouseenter', () => activate(pill));
    });
  }

  // 5. Interactive Tamper-Wave & Hash-Stamp Simulator
  const lab = $('#tamper-lab');
  if (lab) {
    const cells = $$('.lab-cell', lab);
    const lanesEl = $('.lab-lanes', lab);
    const verdictEl = $('#lab-verdict', lab);
    const diffEl = $('#lab-diff', lab);
    const pinBtn = $('#lab-pin', lab);
    const resetBtn = $('#lab-reset', lab);
    let brokenFrom = null;
    let keyPinned = true;

    const INTACT_SHA = 'd58dec24464c947a75a441ec4eba426e78c7d185bdbd66081346ca69d1ba6c70';
    const MUTATED_SHA = '9f14a8c0e77b2104d2c98011fe8a3c4b9120fa6d44e1bc93a82c4170e9012f3a';

    const renderLab = () => {
      cells.forEach((cell) => {
        const idx = Number(cell.dataset.idx);
        const lane = cell.dataset.lane;
        const origBreak = brokenFrom && brokenFrom.lane === lane && brokenFrom.idx === idx;
        const cascBreak = brokenFrom && brokenFrom.lane === lane && idx > brokenFrom.idx;
        cell.classList.toggle('is-origin-break', Boolean(origBreak));
        cell.classList.toggle('is-cascade-break', Boolean(cascBreak));
      });

      if (brokenFrom) {
        verdictEl.dataset.state = 'broken';
        verdictEl.textContent = 'CHAIN BROKEN · FIRST BREAK DETECTED';
        diffEl.classList.add('is-broken');
        diffEl.innerHTML = `<span>First break: <code>${brokenFrom.label}</code> (1 byte flipped → every later link fails SHA-256)</span><span>computed: <code>${MUTATED_SHA.slice(0, 20)}…</code> vs expected <code>${INTACT_SHA.slice(0, 20)}…</code></span>`;
      } else if (keyPinned) {
        verdictEl.dataset.state = 'pass';
        verdictEl.textContent = 'LOCAL CHAIN INTACT · EXTERNAL HASH STAMP VERIFIED';
        diffEl.classList.remove('is-broken');
        diffEl.innerHTML = `<span>18/18 blocks + Ed25519 stamp <code>key_id 10ba682c8ad13513</code> (pinned)</span><span>receipt_sha256: <code>${INTACT_SHA.slice(0, 28)}…</code></span>`;
      } else {
        verdictEl.dataset.state = 'local';
        verdictEl.textContent = 'LOCAL CHAIN INTACT · NO EXTERNAL ANCHOR';
        diffEl.classList.remove('is-broken');
        diffEl.innerHTML = `<span>18/18 bytes match local receipt (external stamp key unpinned — server trust only)</span><span>receipt_sha256: <code>${INTACT_SHA.slice(0, 28)}…</code></span>`;
      }
      if (pinBtn) {
        pinBtn.setAttribute('aria-pressed', keyPinned ? 'true' : 'false');
        pinBtn.textContent = keyPinned ? 'Pinned QorTime Key: 10ba682c8ad13513' : 'Pin QorTime Key (currently unpinned)';
      }
    };

    cells.forEach((cell) => {
      cell.addEventListener('click', () => {
        const idx = Number(cell.dataset.idx);
        const lane = cell.dataset.lane;
        const label = cell.dataset.label || cell.textContent.trim();
        if (brokenFrom && brokenFrom.lane === lane && brokenFrom.idx === idx) {
          brokenFrom = null;
        } else {
          brokenFrom = { lane, idx, label };
        }
        renderLab();
      });
    });

    pinBtn?.addEventListener('click', () => {
      keyPinned = !keyPinned;
      renderLab();
    });

    resetBtn?.addEventListener('click', () => {
      brokenFrom = null;
      lanesEl?.classList.remove('is-scanning');
      void lanesEl?.offsetWidth;
      lanesEl?.classList.add('is-scanning');
      renderLab();
    });

    renderLab();
  }

  // 6. Live 1 kHz Controller Oscilloscope (#scope-canvas)
  const scope = $('#scope-canvas');
  if (scope) {
    const ctx = scope.getContext('2d');
    let visible = true;
    let t0 = performance.now();
    const drawScope = (now) => {
      if (!ctx) return;
      const w = (scope.width = scope.clientWidth * Math.min(window.devicePixelRatio || 1, 2));
      const h = (scope.height = scope.clientHeight * Math.min(window.devicePixelRatio || 1, 2));
      const t = (now - t0) / 1000;
      ctx.clearRect(0, 0, w, h);

      // Baseline grid
      ctx.strokeStyle = 'rgba(79, 124, 255, 0.16)';
      ctx.lineWidth = 1;
      for (let g = 1; g < 4; g++) {
        const y = (h / 4) * g;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }

      const drawTrace = (yBase, color, freq, amp, phase) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        for (let x = 0; x <= w; x += 3) {
          const u = x / w;
          const v =
            Math.sin(u * freq + t * 4.2 + phase) * amp +
            Math.sin(u * freq * 2.7 - t * 6.5) * (amp * 0.35);
          const y = yBase + v;
          if (x === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      };

      drawTrace(h * 0.28, '#2ef2ff', 14, h * 0.11, 0);
      drawTrace(h * 0.56, '#4f7cff', 20, h * 0.1, 1.4);
      drawTrace(h * 0.82, '#f0f4ff', 9, h * 0.08, 2.8);

      if (visible && !RM) requestAnimationFrame(drawScope);
    };

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(([e]) => {
        visible = e.isIntersecting;
        if (visible && !RM) requestAnimationFrame(drawScope);
      }).observe(scope);
    }
    requestAnimationFrame(drawScope);
  }

  // 7. Interactive Quickstart Stepper on /watch/
  const stepper = $('#qs-stepper');
  if (stepper) {
    const tabs = $$('.stepper-tab', stepper);
    const cmdOut = $('#qs-cmd', stepper);
    const descOut = $('#qs-desc', stepper);
    const copyBtn = $('#qs-copy', stepper);
    const STEPS = [
      {
        cmd: 'qorcap.exe preflight --hand-dir D:\\QorCap\\practice-hand --bundle-dir "C:\\Users\\Contr\\Documents\\QorCap Practice"',
        desc: 'Read-only check: verifies disk headroom, folder permissions, and listen-only DualSense / DualSense Edge USB visibility before recording.',
      },
      {
        cmd: 'Start practice capture (QorCap 0.3.43+) -> writes hand_timeline.v0.jsonl + hand_raw.v0.bin + frame-chain.jsonl',
        desc: 'Zero injected DLLs and zero console modifications. Segments and 1 kHz controller chunks are SHA-256 chained on one monotonic clock.',
      },
      {
        cmd: 'qorcap.exe hash-stamp --url <QORTIME_URL> --trusted-key <KEY_ID> <SESSION_DIR>',
        desc: 'Opt-in: posts only SHA-256 session heads (zero video, zero PII) to /aux/v1/hash-stamp and verifies the returned Ed25519 signature.',
      },
      {
        cmd: 'GET /api/matches/<match_id>/moment-bundle.tar?t=12  (or Click Card -> Moment bundle (.tar))',
        desc: 'Packages unedited ±6 s video segments, frame chain, receipt, scrubbed hand_timeline.moment.jsonl (device IDs stripped), and optional hash_stamp.v1.json.',
      },
      {
        cmd: 'node tools/check_bundle.mjs <clip>.qormoment.tar --trusted-stamp-key <KEY_ID>',
        desc: 'Or drop the .qormoment.tar directly into /check in any browser (zero uploads) -> LOCAL CHAIN INTACT · EXTERNAL HASH STAMP VERIFIED.',
      },
    ];

    const selectStep = (idx) => {
      const s = STEPS[idx] || STEPS[0];
      tabs.forEach((b, bi) => b.setAttribute('aria-selected', bi === idx ? 'true' : 'false'));
      if (cmdOut) cmdOut.textContent = s.cmd;
      if (descOut) descOut.textContent = s.desc;
    };

    tabs.forEach((btn, i) => btn.addEventListener('click', () => selectStep(i)));
    copyBtn?.addEventListener('click', async () => {
      try {
        await navigator.clipboard?.writeText(cmdOut?.textContent || '');
        copyBtn.textContent = 'Copied';
        setTimeout(() => {
          copyBtn.textContent = 'Copy';
        }, 1600);
      } catch {
        copyBtn.textContent = 'Copy manually';
      }
    });
    selectStep(0);
  }

  // 8. GSAP + ScrollTrigger choreography (skipped automatically under prefers-reduced-motion)
  if (RM || !window.gsap) return;
  const { gsap } = window;
  if (window.ScrollTrigger) gsap.registerPlugin(ScrollTrigger);

  if (window.Lenis && window.ScrollTrigger) {
    const lenis = new Lenis({ lerp: 0.09, smoothWheel: true });
    lenis.on('scroll', ScrollTrigger.update);
    gsap.ticker.add((t) => lenis.raf(t * 1000));
    gsap.ticker.lagSmoothing(0);
  }

  if ($$('h1 .line').length) {
    gsap.from('h1 .line', {
      yPercent: 45,
      opacity: 0,
      duration: 0.9,
      ease: 'expo.out',
      stagger: 0.09,
    });
  }

  if (window.ScrollTrigger) {
    $$('[data-stagger-group]').forEach((group) => {
      const items = $$('[data-stagger-item]', group);
      if (!items.length) return;
      gsap.from(items, {
        y: 26,
        opacity: 0,
        duration: 0.75,
        ease: 'expo.out',
        stagger: 0.08,
        scrollTrigger: { trigger: group, start: 'top 86%', once: true },
      });
    });
  }
})();
