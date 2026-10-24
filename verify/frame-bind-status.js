// Status vocabulary for qorcap-frame-bind/v0. Same set as
// qor_receipt.review.frame_bind.FRAME_BIND_STATUSES.
//
// verify/verify.js does not walk frames.jsonl or readers.json and does not read
// these constants when it decides VERIFIED / PARTIAL / FAIL / SELF_ATTESTED.
// Porting frame_bind_status into the browser is a follow-up.

export const FRAME_BIND_STATUSES = Object.freeze(["verified", "missing", "stripped", "broken"]);
export const FRAME_BIND_VERIFIED = FRAME_BIND_STATUSES[0];

// True only for a string primitive equal to FRAME_BIND_VERIFIED.
// new String("verified") is typeof "object" and is not verified.
export function isFrameBindVerified(value) {
  return typeof value === "string" && value === FRAME_BIND_VERIFIED;
}
