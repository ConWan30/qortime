// Advisory AI explanation panel. Built during the Meta AI Developer Hackathon (Oct 26 - Nov 4 2026); see HACKATHON.md.
//
// Shows ai_ruling.json (qortime.ai_ruling.v0, written by Meta Muse Spark) ONLY when all of these hold:
//   * advisory_signature verifies under a key in ai_advisory_keys.inc.js (key_kind qortime-ai-advisory),
//   * that key is NOT a referee key (not on the trusted list, not the receipt's referee),
//   * receipt_jcs_sha256 equals SHA-256(JCS(this receipt)),
//   * verdict_echo.status equals the status verify.js just computed (a stale explanation is hidden),
//   * affects_verdict === false.
// Otherwise nothing is shown. The panel never changes the verdict, never uses gold, and renders text with textContent.
import * as ed from "./vendor/noble-ed25519.js";
import { canonicalize, sha256Hex, lookupReferee } from "./verify.js";
import { AI_ADVISORY_KEYS } from "./ai_advisory_keys.inc.js";

export const AI_SCHEMA = "qortime.ai_ruling.v0";
export const ADV_KIND = "qortime-ai-advisory";
const enc = new TextEncoder();
const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const unhex = (s) => Uint8Array.from(String(s).match(/../g) || [], (x) => parseInt(x, 16));
const isHex = (s, n) => typeof s === "string" && new RegExp(`^[0-9a-fA-F]{${n}}$`).test(s);
const without = (o, k) => Object.fromEntries(Object.entries(o).filter(([x]) => x !== k));
const kidOf = async (pub) => hex(new Uint8Array(await crypto.subtle.digest("SHA-256", unhex(pub)))).slice(0, 16);

/** Generic checks for any sidecar signed with the AI advisory key (also used by score-crosscheck-view.js). Never throws. */
export async function advisorySignatureProblems(doc, receipt, schema, { keys = AI_ADVISORY_KEYS } = {}) {
  const p = [];
  try {
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) return ["not_an_object"];
    if (doc.schema !== schema) p.push("schema");
    if (["signature", "signatures", "referee", "referee_ai"].some((k) => k in doc)) p.push("forbidden_field");
    const ak = doc.advisory_key || {}, sg = doc.advisory_signature || {};
    const pub = String(ak.pubkey || "").toLowerCase();
    if (ak.key_kind !== ADV_KIND) p.push("key_kind");
    const row = ((keys || {}).keys || []).find((r) => String(r.pubkey || "").toLowerCase() === pub);
    if (!row || row.status !== "active" || row.key_kind !== ADV_KIND) p.push("advisory_key_not_allowlisted");
    if (pub && (lookupReferee(pub) || pub === String((receipt && receipt.referee && receipt.referee.pubkey) || "").toLowerCase())) p.push("advisory_key_is_a_referee_key");
    let sigOk = false;
    if (isHex(pub, 64) && sg.alg === "Ed25519" && isHex(sg.sig, 128) && sg.key_id === (await kidOf(pub))) {
      try { sigOk = await ed.verifyAsync(unhex(sg.sig), enc.encode(canonicalize(without(doc, "advisory_signature"))), unhex(pub)); } catch { sigOk = false; }
    }
    if (!sigOk) p.push("bad_advisory_signature");
    if (!receipt || doc.receipt_jcs_sha256 !== (await sha256Hex(receipt))) p.push("receipt_mismatch");
  } catch (e) {
    p.push(`error:${e && e.name ? e.name : "error"}`);
  }
  return p;
}

/** Returns { ok, problems, text, model }. Never throws. */
export async function checkAiRuling(doc, receipt, status, { keys = AI_ADVISORY_KEYS } = {}) {
  const p = await advisorySignatureProblems(doc, receipt, AI_SCHEMA, { keys });
  if (p.length === 1 && p[0] === "not_an_object") return { ok: false, problems: p };
  if (doc.affects_verdict !== false) p.unshift("affects_verdict_not_false");
  if (!doc.verdict_echo || doc.verdict_echo.status !== status) p.push("verdict_echo_stale");
  if (typeof doc.explanation !== "string" || !doc.explanation.trim()) p.push("no_explanation");
  return p.length ? { ok: false, problems: p } : { ok: true, problems: [], text: doc.explanation.trim(), model: String(doc.model || "Muse Spark") };
}

/** DOM for a checked ruling (res.ok must be true). Plain text only; no gold. */
export function aiRulingPanel(res) {
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const sec = el("section", "panel reveal s-info ai-panel");
  sec.id = "p-ai"; sec.dataset.state = "info"; sec.setAttribute("aria-label", "AI explanation (advisory)");
  const head = el("header");
  head.append(el("h3", null, "AI explanation"));
  const chip = el("span", "chip s-info"); const dot = el("i"); dot.setAttribute("aria-hidden", "true"); chip.append(dot, "advisory");
  head.append(chip);
  sec.append(head);
  const body = el("div", "ai-text");
  for (const para of res.text.split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean).slice(0, 8)) body.append(el("p", null, para));
  sec.append(body);
  sec.append(el("p", "pnote", `Written by Meta ${res.model} from the referee's results and signed by QorTime's AI advisory key, which is not a referee key. It does not decide or change the verdict above.`));
  return sec;
}
