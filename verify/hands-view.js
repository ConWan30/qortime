// Show Your Hands (display layer). Built during the Meta AI Developer Hackathon (Oct 26 - Nov 4 2026).
//
// hands.json (qortime.hands.v0) is written by qor_receipt/hands/sidecar.py. It is LOWER-ONLY and HIDDEN BY DEFAULT:
//   * shown only when its AI advisory signature, advisory key, and receipt binding all check out;
//   * a valid file with cap PARTIAL turns VERIFIED into PARTIAL; nothing here can raise a status or FAIL;
//   * no file, or an invalid one => no panel at all and the status exactly as verify.js computed it.
// verify.js is unchanged; gold still requires the final status VERIFIED.
import { advisorySignatureProblems } from "./ai-ruling-view.js";

export const HANDS_SCHEMA = "qortime.hands.v0";
export const HANDS_CODE = "show_your_hands_flags";
const BAD_KEY = /path|instance|serial|(^|_)(vid|pid)($|_)|gamertag|psn|device_id|hid_id|usb_id/i;
const BAD_VAL = /HID\\|USB\\|VID_|PID_|\\\\\?\\|&MI_/i;

function identifying(x) {
  if (Array.isArray(x)) return x.some(identifying);
  if (x && typeof x === "object") return Object.entries(x).some(([k, v]) => BAD_KEY.test(k) || identifying(v));
  return typeof x === "string" && BAD_VAL.test(x);
}

const flagsIn = (clips) => clips.filter((c) => c && c.scorable === true).reduce((n, c) => n + (Array.isArray(c.flags) ? c.flags.length : 0), 0);

/** { ok, problems, cap, inputs, clips, scorable, flags, summary }. Never throws. */
export async function checkHands(doc, receipt, opts = {}) {
  const p = await advisorySignatureProblems(doc, receipt, HANDS_SCHEMA, opts);
  if (p.length === 1 && p[0] === "not_an_object") return { ok: false, problems: p };
  if (doc.affects_verdict !== "lower_only" || doc.max_cap !== "PARTIAL") p.push("not_lower_only");
  if (doc.cap !== null && doc.cap !== "PARTIAL") p.push("bad_cap");
  const clips = Array.isArray(doc.clips) ? doc.clips : [];
  if (doc.cap === "PARTIAL" && (doc.inputs_verified !== true || flagsIn(clips) < 2)) p.push("cap_inconsistent");
  if (identifying(doc)) p.push("identifying_data");
  if (p.length) return { ok: false, problems: p };
  const inp = doc.inputs && typeof doc.inputs === "object" ? doc.inputs : {};
  return { ok: true, problems: [], cap: doc.cap, inputsVerified: doc.inputs_verified === true,
           reports: inp.reports | 0, chunks: inp.chunks | 0, clips: clips.length,
           scorable: clips.filter((c) => c && c.scorable === true).length, flags: flagsIn(clips),
           why: [...new Set(clips.map((c) => c && c.why).filter(Boolean))], summary: String(doc.summary || "") };
}

/** Copy of the verifier output with Show Your Hands applied (lower-only). Input is never mutated. */
export function applyHands(v, h) {
  if (!v || typeof v !== "object") return v;
  const out = { ...v, partial: [...(v.partial || [])] };
  if (!h || !h.ok) return out;
  out.show_your_hands = h.cap ? "flagged" : h.scorable ? "clean" : "not_scored";
  if (h.cap === "PARTIAL" && out.status === "VERIFIED") { out.status = "PARTIAL"; out.partial.push(HANDS_CODE); }
  return out;
}

const WHY = { too_few_anchors: "too few confirmed play starts to line the video up with the inputs",
              implausible_lag: "the video and input clocks did not line up", alignment_residual_too_large: "the video and input clocks did not line up" };

/** Panel DOM, or null when there is nothing valid to show (the panel stays hidden). Never gold. */
export function handsPanel(h) {
  if (!h || !h.ok) return null;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const state = !h.inputsVerified ? "unchecked" : h.cap ? "partial" : h.scorable ? "pass" : "unchecked";
  const sec = el("section", `panel reveal s-${state} xc-panel hands-panel`);
  sec.id = "p-hands"; sec.dataset.state = state; sec.setAttribute("aria-label", "Show Your Hands");
  const head = el("header"); head.append(el("h3", null, "Show Your Hands"));
  const chip = el("span", `chip s-${state}`); const dot = el("i"); dot.setAttribute("aria-hidden", "true");
  chip.append(dot, !h.inputsVerified ? "Inputs unverified" : h.cap ? "Flags found" : h.scorable ? "Presses match" : "Inputs match");
  head.append(chip); sec.append(head);
  const n = (x) => Number(x).toLocaleString("en-US");
  if (h.inputsVerified) sec.append(el("p", "xc-line", `Controller inputs match the receipt: ${n(h.reports)} reports in ${n(h.chunks)} chunks, all committed by its checkpoints.`));
  const ptp = h.cap ? `Press-to-pixel: ${h.flags} flag(s) on the highlight clips. The result is shown as PARTIAL, not VERIFIED.`
    : h.scorable ? `Press-to-pixel: ${h.scorable} of ${h.clips} highlight clip(s) checked, no flags.`
    : `Press-to-pixel: not scored on these ${h.clips} highlight clips (${h.why.map((w) => WHY[w] || w).join("; ") || "nothing to align"}). No penalty.`;
  sec.append(el("p", "xc-line", ptp));
  sec.append(el("p", "pnote", "Show Your Hands checks that on-screen play starts have a controller press behind them. Meta Muse Spark only decides which on-screen motion is a real play start, so it can only remove candidates. This can lower VERIFIED to PARTIAL, never raise a status or fail a match. Inputs show a USB controller in sync with the video, not who held it. Signed by QorTime's AI advisory key, not a referee key."));
  return sec;
}
