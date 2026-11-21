// Verifier auto-pin. Built during the Meta AI Developer Hackathon (Oct 26 - Nov 4 2026). See verify/AUTO_PIN.md.
//
// Problem: a shared receipt link with no ?referee= shows PARTIAL (referee_key_not_pinned) even when the receipt is
// signed by a QorTime referee on the built-in trusted list. Fix: when the pin box is empty, pick the pin ONLY from the
// built-in allowlist, never from the URL or the receipt alone.
//
// Safety rules (tests in verify/auto-pin.test.mjs):
//   * verify.js and verifyReceipt() are unchanged. Auto-pin only chooses the `refereePub` argument a person could
//     already type by hand, so it can never produce a result a manual pin of the same key would not.
//   * A key qualifies only if it is (a) an ACTIVE row on the built-in allowlist, or (b) an online key with a VALID,
//     unrevoked delegation from an allowlisted root (the same checkDelegation() verifyReceipt uses).
//   * Unknown, revoked, local self-attested, referee-equals-player and malformed keys return pin = null, so the call
//     to verifyReceipt is byte-for-byte what it was before (unknown referees behave exactly as before).
//   * Auto-pin never removes errors: a tampered receipt from a trusted referee still FAILs.
import { checkDelegation, lookupReferee, REVIEW_KEY_KIND } from "./verify.js";
import { TRUSTED_REFEREES } from "./trusted_referees.inc.js";
import { REFEREE_REVOCATIONS } from "./referee_revocations.inc.js";

const HEX64 = /^[0-9a-fA-F]{64}$/;
const LOCAL_PREFIX = "local-self-attested";
export const AUTO_PIN_NOTE = "Referee key pinned automatically from QorTime's built-in trusted list.";

/** Returns { pin: hex|null, source: "allowlist"|"delegation"|null, reason } . Never throws. */
export async function autoPinFor(receipt, { allowlist = TRUSTED_REFEREES, revocations = REFEREE_REVOCATIONS, nowMs = null } = {}) {
  try {
    const none = (reason) => ({ pin: null, source: null, reason });
    if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) return none("not_a_receipt");
    const ref = receipt.referee;
    if (!ref || typeof ref !== "object") return none("no_referee");
    const pub = ref.pubkey;
    if (typeof pub !== "string" || !HEX64.test(pub)) return none("referee_pubkey_malformed");
    const want = pub.toLowerCase();
    if (String(ref.key_kind || "").startsWith(LOCAL_PREFIX)) return none("local_self_attested");
    const players = Array.isArray(receipt.players) ? receipt.players : [];
    if (players.some((p) => p && String(p.pubkey || "").toLowerCase() === want)) return none("referee_same_as_player");
    const row = lookupReferee(want, allowlist);
    if (row) {
      return row.status === "active" ? { pin: want, source: "allowlist", reason: `allowlisted:${row.id}` } : none(`not_active:${row.status}`);
    }
    if (ref.delegation == null) return none("not_on_allowlist");
    if (ref.key_kind !== REVIEW_KEY_KIND) return none("delegation_scope");
    const d = await checkDelegation(ref.delegation, want, (receipt.review || {}).reviewed_ms, { allowlist, revocations, nowMs });
    if (!d.info) return none(`delegation_invalid:${d.problems.join(",")}`);
    return { pin: want, source: "delegation", reason: `delegated:${d.info.id}:serial${d.info.serial}:from:${d.info.delegated_by}` };
  } catch {
    return { pin: null, source: null, reason: "error" };
  }
}
