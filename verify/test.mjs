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
const withFinals = corroborationView({ corroboration: { status: "disagreed", reasons: ["final_score_mismatch"],
  finals: { home: [14, 7], away: [14, 3], bad: [1] } } });
ok(withFinals && withFinals.finals.home[0] === 14 && withFinals.finals.home[1] === 7
   && withFinals.finals.away[1] === 3 && !withFinals.finals.bad && withFinals.affects_verdict === false,
   "corroboration view keeps both finals and drops a bad pair");
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
ok(deviceKeyView({ device_key: { status: "tpm_attested", affects_verdict: true, detail: "Attested by TPM" } }) === null,
   "tpm_attested is not a displayable status");
const claimedView = deviceKeyView({ device_key: { status: "tpm_claimed", affects_verdict: true, detail: "attacker text" } });
ok(claimedView && claimedView.status === "tpm_claimed" && claimedView.affects_verdict === false
   && /unverified/i.test(claimedView.detail),
   "tpm_claimed says the TPM claim is unverified and cannot affect the verdict");
ok((await verifyReceipt(rv.receipt, rv.referee_pub, { allowlist: rv.allowlist })).status === "VERIFIED"
   && !(await verifyReceipt(rv.receipt, rv.referee_pub, { allowlist: rv.allowlist })).device_key,
   "a review receipt with no device sidecar stays VERIFIED");

// ---- verifier page display model: explains verifyReceipt, never re-decides it ----
const { buildPanels, buildSteps, describeSidecar, explainCode, goldAllowed, publicDevice, verdictLabel } = await import("./receipt-view.js");
const { readdirSync } = await import("node:fs");
const fxDir = new URL("../tests/vectors/viewer/", import.meta.url);
const fixtures = readdirSync(fxDir).filter((f) => f.endsWith(".json")).sort();
ok(fixtures.length >= 4, "viewer fixtures are present");
const panelOf = (panels, id) => panels.find((p) => p.id === id);
for (const name of fixtures) {
  const fx = JSON.parse(readFileSync(new URL(name, fxDir)));
  const fv = await verifyReceipt(fx.receipt, fx.referee_pub, { allowlist: fx.allowlist });
  ok(fv.status === fx.expect, `${name}: JS verifier returns ${fx.expect}`);
  const panels = buildPanels(fx.receipt, fv, {});
  const res = panelOf(panels, "result");
  ok(res.gold === (fv.status === "VERIFIED") && goldAllowed(fv) === (fv.status === "VERIFIED"), `${name}: gold only when VERIFIED`);
  ok(fv.status === "VERIFIED" || res.state !== "pass", `${name}: result panel is not a pass unless VERIFIED`);
  const { steps } = buildSteps(fx.receipt, fv);
  ok(fv.status !== "FAIL" || steps.some((s) => s.state === "fail"), `${name}: a FAIL shows at least one failed step`);
  ok(steps.every((s) => s.codes.every((c) => [...fv.errors, ...fv.partial].includes(c))), `${name}: every step code comes from the verifier`);
}
const loadFx = (n) => JSON.parse(readFileSync(new URL(n, fxDir)));
const tampered = loadFx("fail-tampered-after-signing.json");
const tv = await verifyReceipt(tampered.receipt, tampered.referee_pub, { allowlist: tampered.allowlist });
const tp = buildPanels(tampered.receipt, tv, {});
ok(panelOf(tp, "device").state === "unchecked" && !panelOf(tp, "device").device, "device key in a bad-signature receipt is not shown as a status");
ok(panelOf(tp, "corroboration").state === "unchecked", "corroboration in a bad-signature receipt is not shown as a status");
ok(panelOf(tp, "review").state === "fail", "review block in a bad-signature receipt is not trusted");
const weak = loadFx("partial-motion-low-device-invalid-disagreed.json");
const wv = await verifyReceipt(weak.receipt, weak.referee_pub, { allowlist: weak.allowlist });
const wp = buildPanels(weak.receipt, wv, {});
ok(panelOf(wp, "device").state === "fail" && panelOf(wp, "corroboration").state === "fail" && panelOf(wp, "motion").state === "partial",
   "invalid device key, disagreement and low motion each show their own state");
ok(panelOf(wp, "input").device === "Xbox Wireless Controller (USB)" && panelOf(wp, "capture").facts.some(([k, v]) => k === "platform" && v === "xbox-series"),
   "controller and platform are shown as written, not assumed");
const generic = loadFx("verified-software-single-sided-unknown-evidence.json");
const gv = await verifyReceipt(generic.receipt, generic.referee_pub, { allowlist: generic.allowlist });
const gp = buildPanels(generic.receipt, gv, {});
const unk = panelOf(gp, "unknown");
ok(unk.state === "info" && unk.items.some((x) => x.where === "review.checks" && x.key === "example_vendor_probe")
   && unk.items.some((x) => x.where === "review" && x.key === "example_sidecar"), "unknown evidence is listed raw, not dropped");
ok(panelOf(gp, "review").checks.some((c) => c.name === "example_vendor_probe" && !c.known), "unknown review check is labelled unknown");
const plain = buildPanels(rv.receipt, untouched, {});
for (const id of ["device", "corroboration", "handles"]) ok(panelOf(plain, id).state === "absent" && /^Not provided/.test(panelOf(plain, id).note), `missing ${id} renders as not provided`);
const dualP = buildPanels(d.receipt, await verifyReceipt(d.receipt, d.referee_pub), {});
ok(panelOf(dualP, "result").gold === false && panelOf(dualP, "agreement").state !== "absent", "self-signed dual receipt: no gold, agreement shown");
for (const s of ["VERIFIED", "PARTIAL", "SELF_ATTESTED", "FAIL", "WHATEVER"]) {
  const l = verdictLabel(s);
  ok(!/attest|hardware.verified/i.test(`${l.word} ${l.line}`), `verdict copy for ${s} avoids attested wording`);
  ok((l.tone === "gold") === (s === "VERIFIED"), `only VERIFIED gets the gold tone (${s})`);
}
for (const c of ["referee_local_self_attested", "home:key_kind_claim_unverified:tpm-attested", "dual_timelines_disagree", "home:cp[3]:gap_too_long"]) {
  const x = explainCode(c);
  ok(!/attest/i.test(x.text) && (x.code === null || !/attest/i.test(x.code)), `explanation for ${c} avoids attested wording`);
}
ok(explainCode("no_haptic_events").text === "No haptic events were recorded.", "missing IMU bursts are labeled haptic events");
ok(explainCode("haptic_low_confidence_only").text === "Haptic events were recorded, but none with high confidence.",
   "low-confidence IMU bursts are labeled haptic events");
ok(!/rumble/i.test(`${explainCode("no_haptic_events").text} ${explainCode("haptic_low_confidence_only").text}`),
   "haptic explanations do not call the bursts rumble");
const odd = describeSidecar("vendor_probe.v3.json", JSON.stringify({ schema: "acme-probe/v3", samples: [1, 2] }));
ok(odd.state === "info" && odd.family === "acme-probe" && odd.version === "v3" && odd.known === null && odd.fields.length === 2,
   "an unknown sidecar is identified by schema and listed generically");
ok(describeSidecar("bad.json", "{not json").state === "fail", "an unreadable sidecar is flagged, not thrown");
ok(describeSidecar("device_attest.v0.json", JSON.stringify({ schema: "qorcap-device-attest/v0", key_provider: "windows-tpm" })).known !== null,
   "device key sidecar is recognized");
ok(describeSidecar("inputs.jsonl", '{"t_ms":1}\n{"t_ms":2}\n').rows === 2, "JSON lines sidecars are counted");
const proposal = '{"schema":"qorcap-ai-proposal/v0","proposal_id":"p1","abstain":false,"value":{"away":7,"home":14}}\n';
const prop = describeSidecar("ai_proposals.jsonl", proposal);
ok(prop.state === "info" && prop.known && prop.known.includes("Not a label") && prop.summary.includes("not a training label"),
   "ai proposal sidecar is info, not a verdict");
const outcome = describeSidecar("ai_outcomes.jsonl", '{"schema":"qorcap-ai-outcome/v0","outcome":"confirmed"}\n{"schema":"qorcap-ai-outcome/v0","outcome":"contradicted"}\n');
ok(outcome.state === "info" && /confirmed is not a pass/.test(outcome.summary) && outcome.state !== "pass",
   "confirmed outcome is not rendered as pass");
const consol = describeSidecar("consolidation.json", JSON.stringify({ schema: "qorcap-ai-consolidation/v0", decision: "promoted" }));
ok(consol.state === "info" && /promoted is not VERIFIED/.test(consol.summary) && !/gold|VERIFIED result/.test(consol.known),
   "promoted consolidation is not VERIFIED");
const stance = describeSidecar("ai_stance.json", JSON.stringify({ schema: "qorcap-ai-stance/v0", stance: "accept", note: "Model stance. The receipt verdict stays with the referee." }));
ok(stance.state === "info" && stance.summary.includes("receipt verdict stays with the referee"),
   "model stance stays separate from the receipt verdict");
const future = describeSidecar("ai_proposals.jsonl", '{"schema":"qorcap-ai-proposal/v9","proposal_id":"p9"}\n');
ok(future.known === null && future.family === "qorcap-ai-proposal" && future.version === "v9" && future.state === "info",
   "an unknown learning-sidecar version falls back to the generic panel");
const withValue = structuredClone(rv.receipt);
withValue.review.checks.final_reread.value = { away: 7, home: 14, quarter: 4 };
const vp = buildPanels(withValue, untouched, {});
const fr = panelOf(vp, "review").checks.find((c) => c.name === "final_reread");
ok(fr && fr.state === "pass" && fr.value && fr.value.away === 7 && fr.value.home === 14 && fr.value.quarter === 4,
   "final_reread value is shown beside the existing check state");
ok(panelOf(vp, "result").gold === true && untouched.status === "VERIFIED", "gold on this fixture still comes from VERIFIED");
ok(panelOf(buildPanels(withValue, { ...untouched, status: "FAIL" }, {}), "result").gold === false,
   "a structured re-read does not mint gold when the verifier said FAIL");
const live170123 = JSON.parse(readFileSync(new URL("../site/matches/170123/receipt.json", import.meta.url)));
const liveBare = await verifyReceipt(live170123, null);
ok(liveBare.status === "PARTIAL" && liveBare.errors.length === 0 && liveBare.partial.length === 1 && liveBare.partial[0] === "referee_key_not_pinned",
   "match 170123 with no pinned key is PARTIAL only because the key is not pinned");
const livePin = await verifyReceipt(live170123, live170123.referee.pubkey);
ok(livePin.status === "VERIFIED" && livePin.errors.length === 0 && livePin.partial.length === 0, "match 170123 with its referee key pinned is VERIFIED");
ok(livePin.referee_id === "qortime-online-s2" && livePin.delegation && livePin.delegation.serial === 2 && livePin.delegation.delegated_by === "qortime-v1",
   "match 170123 delegation serial 2 from qortime-v1");
ok(livePin.review && livePin.review.segments_verified === 166 && livePin.review.segments_total === 525 && livePin.review.video_method === "spot-check",
   "match 170123 spot-check is 166/525");
ok(Array.isArray(livePin.review.not_proven) && livePin.review.not_proven.length === 4, "match 170123 keeps the receipt not_proven list");
const livePanels = buildPanels(live170123, livePin, {});
const liveReview = panelOf(livePanels, "review");
ok(liveReview && liveReview.state === "pass" && liveReview.not_proven.length === 4 && liveReview.checks.every((c) => c.state === "pass") && liveReview.checks.length === 7,
   "match 170123 review panel is pass with 7 passing checks and the not_proven list");
ok(panelOf(livePanels, "result").gold === true, "match 170123 result panel is gold only from VERIFIED");
ok(publicDevice("DualSense Edge (USB) id=instance:\\\\?\\HID#VID_054C") === "DualSense Edge (USB)", "a controller instance id is not shown");
ok(publicDevice("C:\\Users\\player\\pad") === null, "a local path is not shown as a controller name");
ok(panelOf(livePanels, "input").device === "DualSense Edge (USB)", "match 170123 shows the controller product name only");
const liveCtrl = panelOf(livePanels, "capture").facts.find((row) => row[0] === "controller");
ok(liveCtrl && liveCtrl[1] === "DualSense Edge (USB)" && !String(liveCtrl[1]).includes("HID#") && !String(liveCtrl[1]).includes("\\"),
   "match 170123 capture panel does not show the instance id");
process.exit(fails ? 1 : 0);

