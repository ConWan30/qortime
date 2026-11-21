// Muse Spark score cross-check (display layer). Built during the Meta AI Developer Hackathon (Oct 26 - Nov 4 2026).
//
// score_crosscheck.json (qortime.score_crosscheck.v0) is written by qor_receipt/ai_ruling/score_crosscheck.py: Meta Muse
// Spark re-reads the scorebug after each locked score. It is LOWER-ONLY:
//   * applied only when its AI advisory signature, advisory key, and receipt binding all check out;
//   * agreement keeps the status; a disagreement turns VERIFIED into PARTIAL. Nothing here can raise a status or FAIL;
//   * no valid sidecar => "Muse unchecked": the verifier's status is shown exactly as verify.js computed it.
// verify.js is unchanged; this adjusts only what the page displays, and gold still requires the final status VERIFIED.
import { advisorySignatureProblems } from "./ai-ruling-view.js";

export const XC_SCHEMA = "qortime.score_crosscheck.v0";
export const XC_CODE = "muse_score_crosscheck_disagrees";

/** { ok, problems, cap, agree, disagree, checked, summary, cost_micro_usd }. Never throws. */
export async function checkCrosscheck(doc, receipt, opts = {}) {
  const p = await advisorySignatureProblems(doc, receipt, XC_SCHEMA, opts);
  if (p.length === 1 && p[0] === "not_an_object") return { ok: false, problems: p };
  if (doc.affects_verdict !== "lower_only" || doc.max_cap !== "PARTIAL") p.push("not_lower_only");
  if (doc.cap !== null && doc.cap !== "PARTIAL") p.push("bad_cap");
  const locks = Array.isArray(doc.locks) ? doc.locks : [];
  if ((doc.cap === "PARTIAL") !== locks.some((l) => l && l.result === "disagree")) p.push("cap_inconsistent");
  if (p.length) return { ok: false, problems: p };
  const n = (r) => locks.filter((l) => l && l.result === r).length;
  return { ok: true, problems: [], cap: doc.cap, agree: n("agree"), disagree: n("disagree"), checked: n("agree") + n("disagree"),
           summary: String(doc.summary || ""), model: String(doc.model || "muse-spark"), cost_micro_usd: doc.cost_micro_usd | 0 };
}

/** Copy of the verifier output with the cross-check applied (lower-only). Input is never mutated. */
export function applyCrosscheck(v, xc) {
  if (!v || typeof v !== "object") return v;
  const out = { ...v, partial: [...(v.partial || [])] };
  if (!xc || !xc.ok) { out.muse_crosscheck = "unchecked"; return out; }
  if (xc.cap === "PARTIAL" && out.status === "VERIFIED") { out.status = "PARTIAL"; out.partial.push(XC_CODE); }
  out.muse_crosscheck = xc.cap === "PARTIAL" ? "disagree" : xc.checked ? "agree" : "unchecked";
  return out;
}

/** Panel DOM. xc may be null (=> "Muse unchecked"). Never gold. */
export function crosscheckPanel(xc) {
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const state = !xc || !xc.ok || !xc.checked ? "unchecked" : xc.cap ? "partial" : "pass";
  const sec = el("section", `panel reveal s-${state} xc-panel`);
  sec.id = "p-muse-xc"; sec.dataset.state = state; sec.setAttribute("aria-label", "Muse Spark score cross-check");
  const head = el("header"); head.append(el("h3", null, "Score cross-check"));
  const chip = el("span", `chip s-${state}`); const dot = el("i"); dot.setAttribute("aria-hidden", "true");
  chip.append(dot, state === "pass" ? "Muse agrees" : state === "partial" ? "Muse disagrees" : "Muse unchecked");
  head.append(chip); sec.append(head);
  const line = state === "unchecked"
    ? "Muse unchecked: local scorebug reader only. No penalty."
    : `${xc.summary}.${xc.cap ? " The result is shown as PARTIAL, not VERIFIED." : ""}`;
  sec.append(el("p", "xc-line", line));
  sec.append(el("p", "pnote", "Meta Muse Spark independently re-reads the scorebug just after each locked score. Agreement keeps the status; a disagreement can only lower VERIFIED to PARTIAL. It never raises a status or fails a match. Signed by QorTime's AI advisory key, not a referee key."));
  return sec;
}
