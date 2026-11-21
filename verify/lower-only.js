// Guard for display-layer adjustments (hackathon, Oct 26 - Nov 4 2026). An adjusted result is accepted only if its
// status equals the verifier's, or is PARTIAL while the verifier said VERIFIED. Nothing can raise a status or turn a
// result into FAIL / SELF_ATTESTED from outside verify.js.
export function acceptLowerOnly(v, a) {
  if (!v || !a || typeof a !== "object") return v;
  if (a.status === v.status) return a;
  if (v.status === "VERIFIED" && a.status === "PARTIAL") return a;
  return v;
}
