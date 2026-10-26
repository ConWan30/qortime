// HACKATHON (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026): where the verifier looks for advisory sidecars.
// 1. a dropped file (handled by the page), 2. the sibling of a same-origin ?receipt= URL, 3. for an official-review
// receipt only, the referee's copy at /v1/advisory/{SHA-256(JCS(receipt))}/{name}, written automatically after the
// official verdict. Whatever is fetched still has to pass the advisory-key + receipt-hash checks in
// ai-ruling-view.js / score-crosscheck-view.js, and can never raise a verdict (lower-only.js). Missing = "Muse unchecked".
import { sha256Hex } from "./verify.js";

export const ADVISORY_NAMES = ["ai_ruling.json", "score_crosscheck.json"];
const DEFAULT_API = "https://qortime-referee-production.up.railway.app";

export function refereeApi(search = (typeof location !== "undefined" ? location.search : "")) {
  const q = new URLSearchParams(search).get("referee_api");
  return q && /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(q) ? q : DEFAULT_API;
}

export function isOfficialReview(receipt) {
  return !!(receipt && typeof receipt === "object" && receipt.review && typeof receipt.review === "object"
    && receipt.referee && receipt.referee.key_kind === "qortime-official-review");
}

export async function advisoryUrl(receipt, name, api = refereeApi()) {
  if (!ADVISORY_NAMES.includes(name) || !isOfficialReview(receipt)) return null;
  return `${api.replace(/\/$/, "")}/v1/advisory/${await sha256Hex(receipt)}/${name}`;
}

// Text of the advisory file or null. Never throws.
export async function fetchAdvisoryText(siblingUrl, receipt, name, { fetchFn = globalThis.fetch, api } = {}) {
  const get = async (u) => {
    if (!u) return null;
    try { const res = await fetchFn(u); return res && res.ok ? await res.text() : null; } catch { return null; }
  };
  const t = await get(siblingUrl);
  if (t != null) return t;
  try { return await get(await advisoryUrl(receipt, name, api || refereeApi())); } catch { return null; }
}
