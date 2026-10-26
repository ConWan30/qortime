// Verifier page rendering and motion. Builds DOM from receipt-view.js; animates transform and opacity only.
import { verifyReceipt, receiptBodyHash, verifyInclusion, verifyLogHead, MAX_LATENCY_MS, RULE2_MAX_LATENCY_MS } from "./verify.js";
import { acceptLowerOnly } from "./lower-only.js";
import { buildPanels, buildSteps, describeSidecar, explainCode, goldAllowed, verdictLabel } from "./receipt-view.js";

export const RM = matchMedia("(prefers-reduced-motion: reduce)");
const REFEREE_API = (() => {
  const q = new URLSearchParams(location.search).get("referee_api");
  return q && /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(q) ? q : "https://qortime-referee-production.up.railway.app";
})();
const STEP_MS = 70;
const CHIP = { pass: "checks out", partial: "partial", fail: "fails", absent: "not provided", unchecked: "not checked", info: "shown as given" };
const MARK = { pass: "●", partial: "◐", fail: "✕", absent: "○", unchecked: "·", info: "◇" };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
const show = (v) => (v === undefined || v === null || v === "" ? h("span", { class: "np" }, "not provided") : typeof v === "boolean" ? (v ? "yes" : "no") : String(v));
const short = (x) => (typeof x === "string" && x.length > 16 ? `${x.slice(0, 12)}…` : x);

function chip(state, text) { return h("span", { class: `chip s-${state}` }, h("i", { "aria-hidden": "true" }), text || CHIP[state]); }
function facts(rows) {
  return h("dl", { class: "facts" }, rows.map(([k, v]) => [h("dt", null, k), h("dd", null, show(v))]));
}
function codes(list) {
  if (!list || !list.length) return null;
  return h("ul", { class: "codes" }, list.slice(0, 24).map((c) => {
    const x = explainCode(c);
    return h("li", null, x.text, x.code ? h("code", null, x.code) : null);
  }));
}
function meter(value, max, { floor = null, tone = "", label = "" } = {}) {
  const f = Math.max(0, Math.min(1, (value || 0) / max));
  return h("div", { class: `meter ${tone}`, role: "img", "aria-label": label },
    h("i", { style: { "--f": f.toFixed(4) } }),
    floor != null ? h("b", { style: { "--x": (floor / max).toFixed(4) }, "aria-hidden": "true" }) : null);
}

/* ---------------- verdict + steps ---------------- */
const STEP_TITLES = ["Receipt format qor-receipt/v0", "Pinned referee key", "Referee signature over the receipt", "Referee key on the trusted list",
  "Player signatures", "Official review block", "Checkpoint hash chain", "Nonce challenges", "Motion correlation",
  "Two-player timeline agreement", "Result is consistent", "Tier claim matches"];

export function stepRow(title, i) {
  return h("li", { class: "step", "data-state": "queued", style: { "--i": String(i) } },
    h("span", { class: "mk", "aria-hidden": "true" }, h("i", { class: "q" }), h("i", { class: "m" })),
    h("span", { class: "st" }, h("b", null, title), h("span", { class: "sv" }, "queued")),
    h("span", { class: "why" }));
}

export function setChecking(ui) {
  ui.verdict.dataset.tone = "checking";
  ui.verdict.classList.remove("revealed");
  ui.word.textContent = "CHECKING";
  ui.line.textContent = "The verifier is running in this browser. No result yet.";
  ui.tags.replaceChildren();
  ui.seal.replaceChildren();
  ui.steps.classList.remove("revealed");
  ui.steps.replaceChildren(...STEP_TITLES.map(stepRow));
  ui.stepsWrap.hidden = false;
  ui.panels.replaceChildren();
  ui.panelsWrap.hidden = true;
}

export function setIdle(ui, line) {
  ui.verdict.dataset.tone = "idle";
  ui.verdict.classList.remove("revealed");
  morphText(ui.word, "NOT CHECKED");
  ui.line.textContent = line;
  ui.tags.replaceChildren();
  ui.seal.replaceChildren();
  ui.steps.replaceChildren();
  ui.stepsWrap.hidden = true;
  ui.panels.replaceChildren();
  ui.panelsWrap.hidden = true;
}

function morphText(el, text) {
  if (el.textContent === text) return;
  if (RM.matches || !el.animate) { el.textContent = text; return; }
  const out = el.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-6px)" }], { duration: 120, easing: "ease-in" });
  out.onfinish = () => {
    el.textContent = text;
    el.animate([{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }], { duration: 260, easing: "cubic-bezier(.2,.8,.2,1)" });
  };
}

export function setError(ui, title, line) {
  ui.verdict.dataset.tone = "err";
  ui.verdict.style.setProperty("--after", "0ms");
  morphText(ui.word, title);
  ui.line.textContent = line;
  ui.tags.replaceChildren(chip("fail", "verifier did not run"));
  ui.seal.replaceChildren();
  ui.steps.replaceChildren(stepRow("Receipt could be read", 0));
  const row = ui.steps.firstChild;
  row.dataset.state = "fail"; row.querySelector(".sv").textContent = "fails"; row.querySelector(".why").textContent = line;
  ui.steps.classList.add("revealed");
  ui.verdict.classList.add("revealed");
  ui.panelsWrap.hidden = true;
}

export function paintResult(ui, receipt, v, extras) {
  const { steps } = buildSteps(receipt, v);
  const rows = steps.map((s, i) => {
    const row = stepRow(s.title, i);
    row.dataset.state = s.state;
    row.querySelector(".m").textContent = MARK[s.state];
    row.querySelector(".sv").textContent = CHIP[s.state];
    const why = row.querySelector(".why");
    if (s.note) why.append(h("span", null, s.note));
    const list = codes(s.codes);
    if (list) why.append(list);
    return row;
  });
  ui.steps.replaceChildren(...rows);
  void ui.steps.offsetWidth;
  ui.steps.classList.add("revealed");
  const lab = verdictLabel(v.status);
  const gold = goldAllowed(v);
  ui.verdict.style.setProperty("--after", `${RM.matches ? 0 : rows.length * STEP_MS + 160}ms`);
  ui.verdict.dataset.tone = gold ? "gold" : lab.tone;
  ui.word.textContent = lab.word;
  ui.line.textContent = lab.line;
  const tier = v.trust_tier === "dual-attested" ? "two-player tier" : v.trust_tier === "single-attested" ? "one-player tier" : "no tier";
  ui.tags.replaceChildren(chip(v.referee_trust === "active" ? "pass" : v.referee_trust === "revoked" ? "fail" : "partial",
    `referee ${({ active: "trusted", revoked: "revoked", untrusted: "not trusted" })[v.referee_trust] || "unknown"}${v.referee_id ? ` · ${v.referee_id}` : ""}`),
  chip("info", tier), v.verification_basis === "official-post-hoc-review" ? chip("info", "official post-hoc review") : null);
  ui.seal.replaceChildren();
  if (gold) ui.seal.append(goldSeal());
  void ui.verdict.offsetWidth;
  ui.verdict.classList.add("revealed");
  ui.panels.replaceChildren(...buildPanels(receipt, v, extras).map((p, i) => panel(p, i, receipt, v)));
  ui.panelsWrap.hidden = false;
  observe(ui.panels.children);
}

function goldSeal() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 64 64"); svg.setAttribute("aria-hidden", "true"); svg.classList.add("seal-svg");
  svg.innerHTML = '<circle cx="32" cy="32" r="28" class="ring"/><circle cx="32" cy="32" r="22" class="disc"/><path d="M21 33l8 8 15-17" class="tick"/>';
  return h("span", { class: "seal" }, svg, h("span", { class: "sheen", "aria-hidden": "true" }));
}

/* ---------------- panels ---------------- */
function panelShell(p, i, chipText, ...body) {
  return h("section", { class: `panel reveal s-${p.state}`, id: `p-${p.id}`, "data-state": p.state, style: { "--i": String(i % 6) }, "aria-label": p.title },
    h("header", null, h("h3", null, p.title), chip(p.state, chipText)),
    ...body,
    p.note ? h("p", { class: "pnote" }, p.note) : null);
}

function panel(p, i, receipt, v) {
  switch (p.id) {
    case "result": return resultPanel(p, i);
    case "chain": return chainPanel(p, i);
    case "challenges": return challengePanel(p, i);
    case "motion": return motionPanel(p, i);
    case "input": return inputPanel(p, i);
    case "device": return devicePanel(p, i);
    case "corroboration": return corroborationPanel(p, i);
    case "review": return reviewPanel(p, i);
    case "handles": return handlesPanel(p, i);
    case "unknown": return unknownPanel(p, i);
    case "sidecars": return sidecarPanel(p, i);
    default: return panelShell(p, i, null, p.facts ? facts(p.facts) : null);
  }
}

function resultPanel(p, i) {
  const s = p.score;
  const txt = p.state === "pass" ? "referee-verified" : p.state === "fail" ? "not a verified result" : p.state === "absent" ? null : "not a verified result";
  return panelShell(p, i, txt,
    s ? h("div", { class: `score${p.gold ? " gold" : ""}` },
      h("span", null, h("small", null, "away"), h("b", null, show(s.away_score ?? s.away))),
      h("i", { "aria-hidden": "true" }, "–"),
      h("span", null, h("small", null, "home"), h("b", null, show(s.home_score ?? s.home))),
      h("em", null, `Q${s.quarter ?? "?"} · winner ${s.winner ?? "not given"}`)) : null,
    facts(p.facts));
}

const LINKS = 56;
function chainPanel(p, i) {
  const rows = p.chains.map((c) => {
    const per = Math.max(1, Math.ceil(c.n / LINKS));
    const buckets = Math.ceil(c.n / per);
    const cracked = new Set(c.cracks.map((x) => Math.floor(x.i / per)));
    const strip = h("div", { class: "links", role: "img", "aria-label": `${c.n} checkpoints, ${c.cracks.length} cracked` },
      Array.from({ length: buckets }, (_, k) => h("span", { class: `lk${cracked.has(k) ? " crack" : ""}`, style: { "--k": String(k) } })));
    if (c.tail) strip.append(h("span", { class: "lk tailgap", title: "chain ends before match end" }));
    return h("div", { class: "chainrow" },
      h("div", { class: "ch-h" }, h("b", null, c.role), h("span", null, `${c.n} checkpoints${c.span_s != null ? ` · ${Math.round(c.span_s / 60)} min` : ""}${per > 1 ? ` · each mark is ${per}` : ""}`),
        c.head ? h("code", null, `head ${short(c.head)}`) : null),
      strip,
      c.cracks.length ? h("ul", { class: "codes" }, c.cracks.slice(0, 8).map((x) => h("li", null, `checkpoint ${x.i}: ${x.why.map((w) => explainCode(w).text).join(" ")}`, h("code", null, `${c.role}:cp[${x.i}]:${x.why.join(",")}`)))) : null,
      c.tail ? h("p", { class: "pnote bad" }, explainCode("chain_ends_before_match_end").text) : null);
  });
  const crackN = p.chains.reduce((n, c) => n + c.cracks.length, 0);
  return panelShell(p, i, p.state === "fail" ? `${crackN} cracked` : null, rows.length ? rows : null);
}

function challengePanel(p, i) {
  const grid = h("div", { class: "cgrid" }, p.cells.map((c, k) => {
    const ceil = c.rule === 2 ? RULE2_MAX_LATENCY_MS : MAX_LATENCY_MS;
    return h("div", { class: `cell s-${c.state}`, style: { "--k": String(k) } },
      h("div", { class: "cell-h" }, h("b", null, c.label), h("span", null, c.role), chip(c.state)),
      c.present ? h("div", { class: "pat" }, c.pattern.map((b) => h("kbd", null, b))) : h("p", { class: "np" }, "No challenge for this phase."),
      c.present && Number.isInteger(c.latency_ms) ? [meter(c.latency_ms, ceil, { tone: c.latency_ms > ceil ? "bad" : "", label: `latency ${c.latency_ms} ms of ${ceil}` }),
        h("small", null, `pause menu ${c.latency_ms} ms after the press · rule v${c.rule} ceiling ${ceil} ms`)] : null,
      c.present ? h("small", null, `${c.official ? "nonce from a referee token" : "nonce not from a referee token"}${c.reverified ? ` · re-run by ${c.reverified}` : ""}`) : null,
      codes(c.codes.filter((x) => !/challenge_missing/.test(x))));
  }));
  return panelShell(p, i, null, p.cells.length ? grid : null);
}

function motionPanel(p, i) {
  return panelShell(p, i, null, p.motion.map((m) => h("div", { class: "gauge" },
    h("div", { class: "g-h" }, h("b", null, m.role), h("span", null, m.value == null ? "not provided" : `${m.value} / 1000`), chip(m.state, m.state === "partial" ? "below floor" : null)),
    m.value == null ? null : meter(Math.max(0, m.value), 1000, { floor: p.floor, tone: m.state === "partial" ? "warn" : "", label: `motion ${m.value}, floor ${p.floor}` }))),
  p.basis ? h("p", { class: "pnote" }, p.basis) : null);
}

function inputPanel(p, i) {
  if (p.state === "absent") return panelShell(p, i, null);
  return panelShell(p, i, p.state === "info" ? "advisory" : p.state === "partial" ? "advisory · weak" : null,
    h("div", { class: "device-line" }, h("span", { class: "lbl" }, "controller"), p.device ? h("b", null, p.device) : h("span", { class: "np" }, "name not recorded")),
    h("div", { class: "gauge" }, h("div", { class: "g-h" }, h("b", null, "human-likeness"), h("span", null, p.human == null ? "not provided" : `${p.human} / 1000${p.human_label ? ` · ${p.human_label}` : ""}`)),
      p.human == null ? null : meter(p.human, 1000, { floor: 400, label: `human ${p.human}` })),
    h("div", { class: "gauge" }, h("div", { class: "g-h" }, h("b", null, "alignment to video"), h("span", null, p.align ? `${p.align.match} vs chance ${p.align.chance}` : "not provided")),
      p.align ? h("div", { class: "pair" }, meter(p.align.match, 1000, { label: `match ${p.align.match}` }), meter(p.align.chance, 1000, { tone: "dim", label: `chance ${p.align.chance}` })) : null),
    h("div", { class: "gauge" }, h("div", { class: "g-h" }, h("b", null, "haptics"), h("span", null, p.haptics ? `${p.haptics.events} event${p.haptics.events === 1 ? "" : "s"} · ${p.haptics.high ?? "?"} high confidence` : "not provided")),
      p.haptics && p.haptics.events ? meter(p.haptics.high || 0, p.haptics.events, { label: "high-confidence share" }) : null),
    facts(p.facts),
    codes(p.flags));
}

function devicePanel(p, i) {
  const d = p.device;
  const text = d ? { tpm_claimed: "TPM claimed (unverified)", software: "software key", invalid: "invalid" }[d.status] : null;
  return panelShell(p, i, text,
    d ? facts([["key provider", d.key_provider], ["algorithm", d.algorithm], ["non-exportable", d.non_exportable], ["sidecar schema", d.schema]]) : null,
    d ? codes(d.flags) : null);
}

function corroborationPanel(p, i) {
  const c = p.corroboration;
  if (!c) return panelShell(p, i, null);
  const text = { corroborated: "corroborated", disagreed: "disagreed", single_sided: "single-sided" }[c.status];
  const fin = (r) => (c.finals && c.finals[r] ? `home ${c.finals[r][0]} – away ${c.finals[r][1]}` : null);
  const side = (r) => h("div", { class: `side${c.sides && c.sides[r] ? "" : " ghost"}` }, h("small", null, `${r} recording`),
    h("b", null, c.sides && c.sides[r] ? fin(r) || "final not given" : "no partner"),
    c.sides && c.sides[r] ? h("span", null, `own checks ${c.sides[r].own_checks}`) : null);
  return panelShell(p, i, text,
    h("div", { class: `pairviz ${c.status}` }, side("home"), h("span", { class: "bridge", "aria-hidden": "true" }, h("i"), h("i")), side("away")),
    facts([["pairing", c.pairing_id], ["score-change tolerance", Number.isInteger(c.tolerance_ms) ? `${c.tolerance_ms} ms, no clock offset removed` : undefined]]),
    codes(c.reasons),
    ["home", "away"].filter((r) => c.sides && c.sides[r] && c.sides[r].own_checks !== "pass").map((r) =>
      h("p", { class: "pnote bad" }, `${r} own checks: ${c.sides[r].own_checks}. Corroboration does not repair a receipt that fails on its own.`)));
}

function reviewPanel(p, i) {
  if (p.state === "absent") return panelShell(p, i, null);
  return panelShell(p, i, null,
    h("ul", { class: "checks" }, p.checks.map((c, k) => h("li", { class: `s-${c.state}`, style: { "--k": String(k) } },
      h("span", { class: "mk", "aria-hidden": "true" }, MARK[c.state]), h("b", null, c.label), chip(c.state, c.known ? null : `${c.state} · unknown check`),
      typeof c.detail === "string" ? h("small", null, c.detail) : null,
      c.value ? h("small", null, `structured re-read away ${c.value.away}, home ${c.value.home}${c.value.quarter == null ? "" : `, quarter ${c.value.quarter}`}. The check state stays ${c.state}.`) : null))),
    p.video ? h("div", { class: "gauge" }, h("div", { class: "g-h" }, h("b", null, "video segments hashed by the referee"),
      h("span", null, `${p.video.verified ?? "?"} / ${p.video.total ?? "?"} · ${p.video.method || "method not given"}`)),
    meter(p.video.verified || 0, p.video.total || 1, { label: "segments verified" })) : null,
    facts([["anchors", p.anchors ? `${p.anchors.valid} valid · ${p.anchors.invalid} invalid` : undefined],
      ["random segments", p.video ? p.video.random : undefined], ["missed if 10% absent", p.video ? p.video.p10 : undefined],
      ["qorcap", p.bundle && p.bundle.qorcap_version], ["bundle segments", p.bundle && p.bundle.segments]]),
    p.scope ? h("p", { class: "pnote" }, p.scope) : null,
    p.not_proven.length ? h("details", { class: "np-list" }, h("summary", null, "What this review does not prove"), h("ul", null, p.not_proven.map((x) => h("li", null, x)))) : null);
}

function handlesPanel(p, i) {
  return panelShell(p, i, p.state === "info" ? "on screen, not accounts" : null,
    p.sides.length ? h("div", { class: "handles" }, p.sides.map((s) => h("div", { class: "side" }, h("small", null, `${s.side}${s.screen_side ? ` · ${s.screen_side} of screen` : ""}`),
      h("b", null, s.handle || s.pattern || "unread"), h("span", null, `confidence ${s.confidence ?? "?"} / 1000 · ${s.agree ?? "?"} of ${s.sightings ?? "?"} sightings agree`)))) : null,
    p.facts.length ? facts(p.facts) : null);
}

function rawPreview(value) {
  let s;
  try { s = JSON.stringify(value, null, 1); } catch { s = String(value); }
  return s.length > 700 ? `${s.slice(0, 700)}\n…` : s;
}
function unknownPanel(p, i) {
  return panelShell(p, i, p.items.length ? `${p.items.length} unrecognized` : "none",
    p.items.map((x) => h("details", { class: "raw" }, h("summary", null, h("code", null, `${x.where}.${x.key}`)), h("pre", null, rawPreview(x.value)))));
}
function sidecarPanel(p, i) {
  return panelShell(p, i, p.files.length ? `${p.files.length} attached · not checked here` : null,
    p.files.map((f) => h("div", { class: `file s-${f.state}` },
      h("div", { class: "g-h" }, h("b", null, f.name), h("code", null, f.schema || "no schema field"), chip(f.state === "fail" ? "fail" : "info", f.state === "fail" ? "unreadable" : f.known ? "known type" : "unknown type")),
      h("p", { class: "pnote" }, f.error || f.known || "Unrecognized evidence type. Fields are listed as written."),
      f.summary ? h("p", { class: "pnote" }, f.summary) : null,
      f.fields ? h("div", { class: "fields" }, f.fields.map((x) => h("span", null, h("code", null, x.key), ` ${x.type}`))) : null,
      f.rows != null ? h("p", { class: "pnote" }, `${f.rows} JSON lines.`) : null)));
}

/* ---------------- transparency log (opt-in network call) ---------------- */
export function logPanel(getReceipt) {
  const state = h("div", { class: "logstate" });
  const btn = h("button", { type: "button", class: "btn ghost", id: "logGo" }, "Look up this receipt in the referee's public log");
  const sec = h("section", { class: "panel reveal s-absent", id: "p-log", "data-state": "absent", "aria-label": "Transparency log" },
    h("header", null, h("h3", null, "Transparency log"), chip("absent", "not checked")),
    btn, state,
    h("p", { class: "pnote" }, "Opt-in. Sends only the receipt's body hash to the official referee. Inclusion does not change the verdict."));
  const set = (st, text, extra) => {
    sec.dataset.state = st; sec.className = `panel reveal in s-${st}`;
    sec.querySelector("header .chip").replaceWith(chip(st, text));
    state.replaceChildren(...(extra || []));
  };
  btn.onclick = async () => {
    const r = getReceipt();
    if (!r) { set("fail", "no receipt", [h("p", null, "Paste a receipt first.")]); return; }
    btn.disabled = true; btn.setAttribute("aria-busy", "true");
    set("unchecked", "checking", [h("p", { class: "pending" }, "Asking the referee…")]);
    try {
      const bh = await receiptBodyHash(r);
      const res = await fetch(`${REFEREE_API}/v1/log/inclusion/${bh}`);
      if (res.status === 404) set("absent", "not in the log", [h("p", null, "The referee's log has no entry for this receipt body."), h("code", null, bh)]);
      else if (!res.ok) set("fail", `referee said ${res.status}`, []);
      else {
        const doc = await res.json();
        const pub = r.referee && r.referee.pubkey;
        const errs = await verifyInclusion(bh, doc.entries || [], doc.head || {});
        const headOk = pub ? await verifyLogHead(doc.head, String(pub).toLowerCase()) : false;
        if (!errs.length && headOk) set("pass", "included", [facts([["entry", doc.seq], ["log size", doc.head && doc.head.size], ["head signed by", "this receipt's referee key"]])]);
        else set("fail", "proof does not check", [codes([...errs, ...(headOk ? [] : ["log_head_signature"])])]);
      }
    } catch {
      set("unchecked", "unreachable", [h("p", null, "The referee could not be reached from this browser.")]);
    } finally { btn.disabled = false; btn.removeAttribute("aria-busy"); }
  };
  return sec;
}

/* ---------------- scroll reveal ---------------- */
let io = null;
export function observe(nodes) {
  const list = [...nodes];
  if (RM.matches || !("IntersectionObserver" in window)) { list.forEach((n) => n.classList.add("in")); return; }
  io = io || new IntersectionObserver((entries) => entries.forEach((e) => {
    if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }), { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
  list.forEach((n) => io.observe(n));
}

export async function runVerify(ui, text, pin, extras) {
  let r;
  try { r = JSON.parse(text); } catch {
    setError(ui, "CAN'T CHECK", "That is not valid JSON, so the verifier did not run.");
    return { receipt: null, v: null };
  }
  if (!r || typeof r !== "object" || Array.isArray(r)) {
    setError(ui, "CAN'T CHECK", "That JSON is not a receipt object, so the verifier did not run.");
    return { receipt: null, v: null };
  }
  setChecking(ui);
  const started = performance.now();
  let v;
  try { v = await verifyReceipt(r, pin || null); } catch (e) {
    setError(ui, "CAN'T CHECK", `The verifier stopped: ${e && e.name ? e.name : "error"}.`);
    return { receipt: r, v: null };
  }
  // Optional display-layer adjustment (lower-only signals such as the Muse score cross-check). It may only lower the
  // status: anything that tries to raise it, or throws, is ignored and the verifier's own result is shown.
  if (extras && typeof extras.adjust === "function") {
    try { v = acceptLowerOnly(v, await extras.adjust(r, v)); } catch { /* keep v */ }
  }
  const wait = RM.matches ? 0 : Math.max(0, 320 - (performance.now() - started));
  if (wait) await new Promise((res) => setTimeout(res, wait));
  paintResult(ui, r, v, extras);
  return { receipt: r, v };
}

export { describeSidecar };
