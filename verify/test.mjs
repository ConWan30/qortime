// node verify/test.mjs : JCS parity + JS verifier against the Python-produced vectors.
import { readFileSync } from "node:fs";
import { canonicalize, sha256Hex, verifyReceipt, challengeEvidenceErrors, challengeConsistencyPartials, inputProofAdvisory, checkDelegation,
         corroborationView,
         deviceKeyView,
         revocationsProblem, receiptBodyHash, verifyInclusion, verifyLogEntries, verifyLogHead, loadAllowlist } from "./verify.js";
import { REFEREE_REVOCATIONS } from "./referee_revocations.inc.js";
const v = JSON.parse(readFileSync(new URL("../tests/vectors/jcs_vector.json", import.meta.url)));
let fails = 0;
const ok = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
ok(canonicalize(v.input) === v.canonical, "jcs canonical parity with python");
ok((await sha256Hex(v.input)) === v.sha256, "jcs sha256 parity with python");
const d = JSON.parse(readFileSync(new URL("../tests/vectors/demo_receipt_dual.json", import.meta.url)));
let r = await verifyReceipt(d.receipt, d.referee_pub);
ok(r.status === "SELF_ATTESTED" && r.trust_tier === "dual-attested", "ephemeral demo referee => SELF_ATTESTED in JS");
ok((await verifyReceipt(d.receipt, null)).status === "SELF_ATTESTED", "unpinned ephemeral referee => SELF_ATTESTED");
const t = structuredClone(d.receipt); t.result.home_score = 99;
ok((await verifyReceipt(t, d.referee_pub)).status === "FAIL", "tampered score => FAIL");
const t2 = structuredClone(d.receipt); t2.checkpoints.home.splice(3, 1);
ok((await verifyReceipt(t2, d.referee_pub)).status === "FAIL", "dropped checkpoint => FAIL");
const t3 = structuredClone(d.receipt); t3.trust_tier = "dual-attested"; t3.match.mode = "single";
ok((await verifyReceipt(t3, d.referee_pub)).status === "FAIL", "tier upgrade => FAIL");
const a = JSON.parse(readFileSync(new URL("../tests/vectors/agent_replay_receipt.json", import.meta.url)));
r = await verifyReceipt(a.receipt, a.referee_pub);
ok(r.status === "SELF_ATTESTED" && r.trust_tier === "single-attested", "agent replay ephemeral referee => SELF_ATTESTED in JS");
const bareOk = { issued_ms: 1000, deadline_ms: 20000, result: { ok: true, reason: "ok" } };
const ev = challengeEvidenceErrors(bareOk, []);
ok(ev.includes("challenge_missing_hid_sha256") && ev.includes("challenge_timing_incomplete")
   && ev.includes("challenge_not_bound_to_live_checkpoint"),
   "challengeEvidenceErrors rejects bare result.ok without HID/timing/checkpoint bind");
const good = {
  issued_ms: 1000, deadline_ms: 20000,
  result: { ok: true, reason: "ok", pause_press_ms: 1500, done_ms: 2000, latency_ms: 80,
            hid_sha256: "a".repeat(64) },
};
ok(challengeEvidenceErrors(good, [{ referee_ts_ms: 1600 }]).length === 0,
   "challengeEvidenceErrors accepts HID+timing+live checkpoint");
const oldWide = structuredClone(good); oldWide.result.latency_ms = 1700;
ok(challengeEvidenceErrors(oldWide, [{ referee_ts_ms: 1600 }]).includes("challenge_latency_out_of_range"),
   "a receipt with no result.rule still rejects 1700 ms");
const rule2 = structuredClone(good); rule2.result.latency_ms = 1700; rule2.result.rule = 2;
ok(challengeEvidenceErrors(rule2, [{ referee_ts_ms: 1600 }]).length === 0,
   "result.rule 2 accepts 1700 ms");
const bogus = structuredClone(good); bogus.result.latency_ms = 1700; bogus.result.rule = 9;
const bogusErr = challengeEvidenceErrors(bogus, [{ referee_ts_ms: 1600 }]);
ok(bogusErr.includes("challenge_rule_unknown") && bogusErr.includes("challenge_latency_out_of_range"),
   "an unknown rule does not widen the 600 ms ceiling");
const hid = "a".repeat(64);
const claim = (phase, lat, rule) => ({ role: "home", phase, result: { ok: true, latency_ms: lat, rule } });
ok(challengeConsistencyPartials([claim("kickoff", 1556, 2), claim("halftime", 1535, 2), claim("end", 1715, 2)], "home").length === 0,
   "110028 rule-2 latencies are consistent");
ok(challengeConsistencyPartials([claim("kickoff", 1556, 2), claim("halftime", 1535, 2), claim("end", 2200, 2)], "home")
   .includes("home:challenge_latency_inconsistent"),
   "a jittered rule-2 claim is challenge_latency_inconsistent");
ok(challengeConsistencyPartials([claim("kickoff", 80, 1), claim("halftime", 1556, 2)], "home").includes("home:challenge_rule_mixed"),
   "mixed rule ids are challenge_rule_mixed");
const advOk = inputProofAdvisory({ input: { human_permille: 700, match_permille: 500, chance_permille: 100,
  haptic_events: 3, haptic_high_confidence: 2, inputs_sha256: "c".repeat(64) } });
ok(advOk && advOk.ok, "inputProofAdvisory accepts strong human/align/haptic signals");
const advBad = inputProofAdvisory({ input: { human_permille: 50, match_permille: 10, chance_permille: 200, haptic_events: 0 } });
ok(advBad && !advBad.ok && advBad.flags.includes("human_score_low"), "inputProofAdvisory flags weak signals (advisory only)");
// Official post-hoc review receipt (Python-produced, signed by a TEST key that only the vector's allowlist trusts).
const rv = JSON.parse(readFileSync(new URL("../tests/vectors/review_receipt.json", import.meta.url)));
r = await verifyReceipt(rv.receipt, rv.referee_pub, { allowlist: rv.allowlist });
ok(r.status === "VERIFIED" && r.verification_basis === "official-post-hoc-review", "review receipt + allowlisted key => VERIFIED in JS");
ok((await verifyReceipt(rv.receipt, rv.referee_pub)).status === "SELF_ATTESTED", "review receipt by a key not on the built-in allowlist => SELF_ATTESTED");
const rt = structuredClone(rv.receipt); rt.review.checks.video_bytes.detail.method = "full";
ok((await verifyReceipt(rt, rv.referee_pub, { allowlist: rv.allowlist })).status === "FAIL", "tampered review block => FAIL");
const rt2 = structuredClone(rv.receipt); rt2.challenges[0].issuer.issued_ms -= 1;
r = await verifyReceipt(rt2, rv.referee_pub, { allowlist: rv.allowlist });
ok(r.status === "FAIL", "tampered challenge token (covered by referee signature) => FAIL");
// ---- delegated online referee key (root-signed cert), revocation, transparency log
const dv = JSON.parse(readFileSync(new URL("../tests/vectors/delegated_review_receipt.json", import.meta.url)));
const dopt = { allowlist: dv.allowlist, revocations: dv.revocations };
r = await verifyReceipt(dv.receipt, dv.referee_pub, dopt);
ok(r.status === "VERIFIED" && r.delegation && r.delegation.serial === 1, "delegated service key + root cert => VERIFIED in JS");
ok((await verifyReceipt(dv.receipt, dv.referee_pub)).status === "SELF_ATTESTED", "delegation from a root not on the built-in allowlist => SELF_ATTESTED");
r = await verifyReceipt(dv.receipt, dv.referee_pub, { allowlist: dv.allowlist, revocations: dv.revoked_list });
ok(r.status === "SELF_ATTESTED" && r.partial.includes("delegation_revoked:1"), "revoked delegation serial => SELF_ATTESTED");
const forgedRevs = structuredClone(dv.revocations); forgedRevs.revoked = [];  forgedRevs.version = 99;
r = await verifyReceipt(dv.receipt, dv.referee_pub, { allowlist: dv.allowlist, revocations: forgedRevs });
ok(r.status === "SELF_ATTESTED" && r.partial.includes("revocations_bad_signature"), "unsigned/forged revocation list => fail closed");
r = await verifyReceipt(dv.receipt, dv.referee_pub, { allowlist: dv.allowlist, revocations: null });
ok(r.status === "SELF_ATTESTED" && r.partial.includes("revocations_missing"), "missing revocation list => fail closed");
const cert = dv.receipt.referee.delegation;
let dd = await checkDelegation(cert, dv.referee_pub, cert.not_after_ms + 1, { ...dopt, nowMs: cert.not_after_ms + 2 });
ok(!dd.info && dd.problems.includes("delegation_expired"), "signing time after not_after => delegation_expired");
dd = await checkDelegation(cert, dv.referee_pub, cert.not_before_ms - 1, dopt);
ok(!dd.info && dd.problems.includes("delegation_not_yet_valid"), "signing time before not_before => not yet valid");
const wide = structuredClone(cert); wide.not_after_ms += 86400000;
dd = await checkDelegation(wide, dv.referee_pub, dv.receipt.review.reviewed_ms, dopt);
ok(!dd.info && dd.problems.includes("delegation_bad_signature"), "widened cert window => bad root signature");
dd = await checkDelegation(cert, "00".repeat(32), dv.receipt.review.reviewed_ms, dopt);
ok(!dd.info && dd.problems.includes("delegation_key_mismatch"), "cert for another key => mismatch");
const rx = structuredClone(dv.receipt); rx.referee.delegation.not_after_ms += 1;
ok((await verifyReceipt(rx, dv.referee_pub, dopt)).status === "FAIL", "tampered embedded cert (covered by service sig) => FAIL");
ok((await revocationsProblem(REFEREE_REVOCATIONS, loadAllowlist())) === null, "shipped revocation list is signed by the built-in root");
const bh = await receiptBodyHash(dv.receipt);
ok((await verifyLogHead(dv.log_head, dv.referee_pub)) && (await verifyLogEntries(dv.log_entries)).length === 0, "transparency log head signature + chain");
ok((await verifyInclusion(bh, dv.log_entries, dv.log_head)).length === 0, "receipt inclusion in transparency log (JS)");
const le = structuredClone(dv.log_entries); le[0].body_sha256 = "f".repeat(64);
ok((await verifyInclusion(bh, le, dv.log_head)).length > 0, "rewritten log entry => inclusion fails");
// legacy root-signed review receipt (no delegation) still VERIFIED with the shipped revocation list loaded
ok((await verifyReceipt(rv.receipt, rv.referee_pub, { allowlist: rv.allowlist })).status === "VERIFIED", "legacy direct-signed review receipt still VERIFIED");
const forged = structuredClone(d.receipt);
forged.result.home_score = 99;
forged.review = { corroboration: { status: "corroborated", reasons: [], affects_verdict: true } };
const forgedV = await verifyReceipt(forged, d.referee_pub);
ok(forgedV.status === "FAIL" && !forgedV.corroboration, "forged corroboration on a bad signature does not display or upgrade");
const view = corroborationView({ corroboration: { status: "disagreed", reasons: ["final_score_mismatch"], affects_verdict: true, tolerance_ms: 5000 } });
ok(view && view.status === "disagreed" && view.affects_verdict === false && view.reasons[0] === "final_score_mismatch",
   "corroboration view keeps the disagreement and forces affects_verdict false");
ok(corroborationView({ corroboration: { status: "upgraded" } }) === null, "unknown corroboration status is dropped");
const untouched = await verifyReceipt(rv.receipt, rv.referee_pub, { allowlist: rv.allowlist });
ok(untouched.status === "VERIFIED" && !untouched.corroboration, "a review receipt with no corroboration block stays VERIFIED");
const forgedDevice = structuredClone(d.receipt);
forgedDevice.result.home_score = 99;
forgedDevice.review = { device_key: { status: "tpm_attested", flags: [], affects_verdict: true } };
const forgedDeviceV = await verifyReceipt(forgedDevice, d.referee_pub);
ok(forgedDeviceV.status === "FAIL" && !forgedDeviceV.device_key, "forged device_key on a bad signature does not display or upgrade");
const deviceView = deviceKeyView({ device_key: { status: "invalid", flags: ["device_signature_invalid"], affects_verdict: true } });
ok(deviceView && deviceView.status === "invalid" && deviceView.affects_verdict === false
   && deviceView.flags[0] === "device_signature_invalid",
   "invalid device key stays flagged and cannot affect the verdict");
ok(deviceKeyView({ device_key: { status: "trusted" } }) === null, "unknown device_key status is dropped");
ok((await verifyReceipt(rv.receipt, rv.referee_pub, { allowlist: rv.allowlist })).status === "VERIFIED"
   && !(await verifyReceipt(rv.receipt, rv.referee_pub, { allowlist: rv.allowlist })).device_key,
   "a review receipt with no device sidecar stays VERIFIED");
process.exit(fails ? 1 : 0);

