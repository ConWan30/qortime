// Display model for the verifier page. Reads a receipt and the output of verifyReceipt; it never re-decides a verdict.
// Every state below comes from a verifier code or a field that is present in the receipt. Missing data is "absent".
import { MOTION_MIN, REVIEW_KEY_KIND, SCHEMA } from "./verify.js";

export const STATES = ["pass", "partial", "fail", "absent", "unchecked", "info"];
const KNOWN_TOP = new Set(["schema", "match", "result", "players", "referee", "challenges", "checkpoints", "agreement",
  "signals", "trust_tier", "review", "signatures"]);
const KNOWN_SIGNALS = new Set(["motion_permille", "motion_basis", "input"]);
const KNOWN_REVIEW = new Set(["type", "reviewed_ms", "submission", "commit", "capture_referee", "bundle", "checks", "anchors",
  "corroboration", "final_reread", "scope", "not_proven", "device_key", "post_sign_errors"]);
const KNOWN_CHECKS = {
  bundle_structure: "Bundle structure",
  capture_chain: "Capture chain countersignatures",
  video_bytes: "Video bytes",
  final_reread: "Final re-read by the referee",
  challenges: "Official challenges",
  motion: "Motion correlation",
  input_integrity: "Controller log integrity",
};
const PHASES = ["kickoff", "halftime", "end"];
const PHASE_LABEL = { kickoff: "Game start", halftime: "Second half", end: "Q4" };

/** Product name only. Instance ids and local paths stay off the page. The receipt bytes are unchanged. */
export function publicDevice(value) {
  if (typeof value !== "string") return null;
  const name = value.split(" id=")[0].trim();
  if (!name || name.includes("\\") || name.includes("HID#") || name.includes("@device") || name.includes("/Users/") || name.includes(":/")) return null;
  return name;
}

/** Human label for a verdict. Status codes stay in the raw output; this copy never says "attested". */
export function verdictLabel(status) {
  return {
    VERIFIED: { word: "VERIFIED", line: "Referee-verified result", tone: "gold" },
    PARTIAL: { word: "PARTIAL", line: "Trusted referee, but some checks are not covered. Not a verified result.", tone: "warn" },
    SELF_ATTESTED: { word: "SELF-SIGNED", line: "Signed on the player's own PC, not by a trusted QorTime referee. Not a verified result.", tone: "warn" },
    FAIL: { word: "FAIL", line: "This receipt does not verify. Nothing in it is a verified result.", tone: "bad" },
  }[status] || { word: String(status || "UNKNOWN"), line: "Unknown status.", tone: "bad" };
}

const PLAIN = {
  schema: "The schema is not qor-receipt/v0.",
  referee_key_mismatch: "The pinned key is not the key that signed this receipt.",
  referee_key_not_pinned: "No referee key was pinned on this page.",
  referee_pin_not_allowlisted: "The pinned key is not on QorTime's built-in trusted list.",
  bad_referee_signature: "The referee signature does not check against the receipt bytes.",
  referee_not_on_allowlist: "The signing key is not on QorTime's built-in trusted list.",
  referee_local_self_attested: "The signing key is a local key on the player's own PC.",
  referee_same_as_player: "The referee key is the same as a player key.",
  delegation_scope: "The delegated key is not scoped for official reviews.",
  no_players: "The receipt lists no players.",
  dual_missing_roles: "Dual mode needs both a home and an away player.",
  dual_timelines_disagree: "The two players' timelines disagree.",
  winner_inconsistent: "The winner does not follow from the score.",
  score_not_nonneg_int: "A score is not a whole number.",
  quarter_not_final: "The result is not from Q4 or later.",
  quarter_invalid: "The quarter is not valid.",
  bad_result_method: "The result method is not one the verifier knows.",
  trust_tier_claim_wrong: "The tier written in the receipt is not the tier the verifier computes.",
  review_missing_or_wrong_type: "The official review block is missing or of the wrong type.",
  review_capture_referee_missing: "The review does not name the capture key.",
  review_capture_referee_is_official_key: "The capture key is the official key.",
  review_checks_missing: "The review lists no checks.",
  gap_too_long: "Two checkpoints are more than 10 s apart.",
  clock_skew: "A checkpoint timestamp is more than 3 s from its countersign.",
  prev_mismatch: "A checkpoint does not link to the one before it.",
  hash_mismatch: "A checkpoint hash does not recompute.",
  seq_gap: "Checkpoint numbers skip.",
  bad_referee_sig: "A checkpoint countersignature does not check.",
  referee_time_backwards: "A countersign time goes backwards.",
  chain_ends_before_match_end: "The chain stops more than 10 s before the match ended.",
  no_checkpoints: "No checkpoints for this player.",
  challenge_missing: "No challenge for this phase.",
  challenge_unanswered: "The challenge was not answered.",
  pattern_too_short: "The challenge pattern is too short.",
  pattern_not_from_nonce: "The pattern does not come from the nonce.",
  challenge_nonce_not_official: "The nonce was not issued by an official referee key.",
  challenge_missing_hid_sha256: "No controller-log hash for the answer.",
  challenge_window_too_long: "The challenge window is longer than 120 s.",
  challenge_timing_incomplete: "The answer timing is incomplete.",
  challenge_pause_outside_window: "The pause press is outside the window.",
  challenge_done_before_pause: "The pattern finished before the pause.",
  challenge_rule_unknown: "The measurement rule is unknown.",
  challenge_latency_out_of_range: "The pause menu showed up later than the rule allows.",
  challenge_not_bound_to_live_checkpoint: "No checkpoint was countersigned near the challenge.",
  challenge_rule_mixed: "Challenges mix rule v1 and rule v2.",
  challenge_latency_inconsistent: "Rule v2 latencies differ by more than 300 ms.",
  motion_signal_missing: "No motion correlation in the receipt.",
  motion_correlation_low: `Motion correlation is below the verifier's floor of ${MOTION_MIN}.`,
  key_kind_claim_unverified: "The player key claims a TPM. That claim is unverified.",
  revocations_missing: "The revocation list is missing.",
  revocations_bad_signature: "The revocation list is not signed by the root.",
  final_score_mismatch: "The two final scores differ.",
  final_score_missing: "A final score is missing.",
  score_timeline_missing: "A signed score timeline is missing.",
  unsigned_score_timeline: "The score timeline is missing, or its signature did not check.",
  timeline_result_mismatch: "The last score event does not match that recording's final score.",
  timeline_file_mismatch: "timeline.jsonl does not match the signed score events.",
  pairing_unverified: "The pairing token was not signed by this referee.",
  role_conflict: "Both submissions claimed the same role.",
  same_player_key: "Both recordings were signed by the same player key.",
  same_checkpoint_head: "Both recordings share a checkpoint head.",
  shared_challenge_nonce: "The recordings share a challenge nonce.",
  copied_challenge_response: "A challenge response was copied from the other recording.",
  state_sequence_differs: "The two recordings saw different score changes, or saw them in a different order.",
  device_signature_invalid: "The device signature over the chain head does not check.",
  schema_unexpected: "The device key file has a schema this referee does not expect.",
  non_exportable_missing: "The device key file does not say whether the key can be exported.",
  device_fields_unreadable: "The device key file could not be read.",
  chain_head_missing: "The device key file has no chain head to sign.",
  exportable_key: "The bundle says this key can be exported.",
  key_provider_unknown: "The key provider is not one this referee recognizes.",
  human_score_low: "Human-likeness is below the advisory floor of 400.",
  human_score_missing: "No human-likeness score.",
  alignment_not_above_chance: "Controller timing is not clearly above chance against the video.",
  alignment_missing: "No controller-to-video alignment.",
  no_haptic_events: "No rumble events were recorded.",
  haptic_low_confidence_only: "Rumble events were recorded, but none with high confidence.",
  inputs_sha256_missing: "No hash of the controller log.",
};
const DEVICE_NOTE = {
  tpm_claimed: "Device signature valid. The bundle claims a TPM key. That TPM claim is unverified. Does not change the receipt verdict.",
  software: "Device signature valid for a software key. Does not change the receipt verdict.",
  invalid: "The device signature did not check. Flagged. Does not change the receipt verdict.",
};

/** Plain-language text and a code that is safe to print beside it (codes that say "attested" stay in the raw output). */
export function explainCode(code) {
  const s = String(code);
  const parts = s.split(":");
  let key = parts.find((p) => PLAIN[p]) || parts[parts.length - 1];
  if (s.startsWith("review_check_failed:")) return { text: `Review check failed: ${checkLabel(parts[1])}.`, code: s };
  if (s.startsWith("review_check_missing:")) return { text: `Review check missing: ${checkLabel(parts[1])}.`, code: s };
  if (s.startsWith("review_check_bad_state:")) return { text: `Review check has an unknown state: ${checkLabel(parts[1])}.`, code: s };
  if (s.startsWith("review_partial:")) return { text: `Review check is partial: ${checkLabel(parts[1])}.`, code: s };
  if (s.startsWith("referee_revoked:")) return { text: "The signing key is revoked.", code: s };
  if (s.startsWith("delegation_revoked:")) return { text: "The delegation certificate is revoked.", code: s };
  if (s.startsWith("delegation_caps_status:")) return { text: "The delegation does not allow this status, so it was capped.", code: s };
  if (s.startsWith("delegation_")) return { text: "The delegation certificate does not check.", code: s };
  if (s.includes(":challenge_failed:")) return { text: "The challenge answer failed.", code: s };
  if (s.startsWith("malformed:")) return { text: "The receipt is malformed. The verifier stopped.", code: s };
  if (s.includes("bad_player_signature")) return { text: "A player signature does not check.", code: s };
  if (s.includes("missing_player_signature")) return { text: "A player signature is missing.", code: s };
  if (s.includes("duplicate_player_role")) return { text: "Two players claim the same role.", code: s };
  if (s.includes("bad_player_role")) return { text: "A player role is not home or away.", code: s };
  if (s.includes("referee_key_kind_claim_unverified")) return { text: "The referee key claims a TPM. That claim is unverified.", code: s };
  if (s.includes("time_differs")) return { text: "A score change is further apart than the tolerance.", code: s };
  const text = PLAIN[key] || "Verifier code.";
  return { text, code: /attested/i.test(s) ? null : s };
}

function checkLabel(name) { return KNOWN_CHECKS[name] || String(name || "").replace(/_/g, " "); }

const isObj = (x) => !!x && typeof x === "object" && !Array.isArray(x);
const roleOf = (code) => (/^(home|away):/.exec(code) || [])[1] || null;

function pick(codes, test) { return codes.filter(test); }
function stateFrom(errs, parts, ran) {
  if (errs.length) return "fail";
  if (parts.length) return "partial";
  return ran ? "pass" : "unchecked";
}

/**
 * Ordered checks, mirroring the order verifyReceipt runs them. Each step carries the verifier codes that set it.
 * `stopped` is true when the verifier returned early (schema) or threw (malformed), so later steps were not run.
 */
export function buildSteps(receipt, v) {
  const errors = (v && v.errors) || [];
  const partial = (v && v.partial) || [];
  const stopped = errors.includes("schema") || errors.some((e) => String(e).startsWith("malformed:"));
  const r = isObj(receipt) ? receipt : {};
  const used = new Set();
  const take = (list, test) => { const out = pick(list, test); out.forEach((c) => used.add(c)); return out; };
  const steps = [];
  const add = (id, title, errs, parts, opts = {}) => {
    const ran = opts.applies !== false && !stopped;
    let state = opts.applies === false ? "absent" : stateFrom(errs, parts, ran);
    if (stopped && !errs.length && !parts.length && opts.applies !== false) state = "unchecked";
    steps.push({ id, title, state, codes: [...errs, ...parts], note: opts.note || "" });
  };
  const schemaErr = take(errors, (e) => e === "schema");
  add("schema", "Receipt format qor-receipt/v0", schemaErr, []);
  if (schemaErr.length) steps[0].state = "fail";
  else if (r.schema === SCHEMA) steps[0].state = "pass";
  const pin = take(errors, (e) => e === "referee_key_mismatch");
  const pinP = take(partial, (p) => p === "referee_key_not_pinned" || p === "referee_pin_not_allowlisted");
  add("pin", "Pinned referee key", pin, pinP);
  add("refsig", "Referee signature over the receipt", take(errors, (e) => e === "bad_referee_signature"), []);
  const trustP = take(partial, (p) => /^referee_(not_on_allowlist|revoked|local_self_attested|same_as_player|key_kind_claim_unverified)/.test(p)
    || p.startsWith("delegation_") || p.startsWith("revocations_"));
  add("trust", "Referee key on the trusted list", [], trustP,
    { note: v && v.delegation ? `Delegated key, serial ${v.delegation.serial} from root ${v.delegation.delegated_by}.` : "" });
  if (v && v.referee_trust !== "active" && !trustP.length && !stopped) {
    steps[steps.length - 1].state = "partial";
    steps[steps.length - 1].note = "Not trusted for this check. See the pinned key above.";
  }
  const pErr = take(errors, (e) => /player_signature|^no_players$|bad_player_role|duplicate_player_role/.test(e));
  const pPart = take(partial, (p) => /:key_kind_claim_unverified:/.test(p) && !p.startsWith("referee_"));
  add("players", "Player signatures", pErr, pPart);
  const review = r.referee && r.referee.key_kind === REVIEW_KEY_KIND;
  add("review", "Official review block", take(errors, (e) => e.startsWith("review_")), take(partial, (p) => p.startsWith("review_partial:")),
    { applies: !!review, note: review ? "" : "Not provided. This receipt has no official post-hoc review." });
  add("chain", "Checkpoint hash chain", take(errors, (e) => /:(cp\[\d+\]:|chain_ends_before_match_end|no_checkpoints)/.test(e)), []);
  const chErr = take(errors, (e) => /:(kickoff|halftime|end):/.test(e));
  const chPart = take(partial, (p) => /:(kickoff|halftime|end):|challenge_missing|challenge_unanswered|challenge_rule_mixed|challenge_latency_inconsistent/.test(p));
  add("challenges", "Nonce challenges", chErr, chPart);
  add("motion", "Motion correlation", [], take(partial, (p) => /motion_(signal_missing|correlation_low)/.test(p)));
  const dual = r.match && r.match.mode === "dual";
  add("dual", "Two-player timeline agreement", take(errors, (e) => e === "dual_missing_roles" || e === "dual_timelines_disagree"), [],
    { applies: !!dual, note: dual ? "" : "Not applicable. Single-console receipt." });
  add("result", "Result is consistent", take(errors, (e) => ["winner_inconsistent", "score_not_nonneg_int", "quarter_not_final",
    "quarter_invalid", "bad_result_method"].includes(e)), []);
  add("tier", "Tier claim matches", take(errors, (e) => e === "trust_tier_claim_wrong"), []);
  const restE = errors.filter((e) => !used.has(e));
  const restP = partial.filter((p) => !used.has(p));
  if (restE.length || restP.length) add("other", "Other verifier findings", restE, restP);
  return { steps, stopped };
}

function segmentsFromErrors(errors, role, n) {
  const cracks = new Map();
  for (const e of errors || []) {
    const m = new RegExp(`^${role}:cp\\[(\\d+)\\]:(\\w+)`).exec(e);
    if (m && +m[1] < n) cracks.set(+m[1], [...(cracks.get(+m[1]) || []), m[2]]);
  }
  return cracks;
}

/** Evidence panels. Each panel has a state from real data and a list of facts. */
export function buildPanels(receipt, v, extras = {}) {
  const r = isObj(receipt) ? receipt : {};
  const errors = (v && v.errors) || [];
  const partial = (v && v.partial) || [];
  const status = v && v.status;
  const sigOk = !errors.includes("bad_referee_signature") && !errors.includes("schema");
  const stopped = errors.includes("schema") || errors.some((e) => String(e).startsWith("malformed:"));
  const panels = [];
  const m = isObj(r.match) ? r.match : {};
  const res = isObj(r.result) ? r.result : {};
  const sig = isObj(r.signals) ? r.signals : {};
  const rv = isObj(r.review) ? r.review : null;

  // result
  const resErr = errors.filter((e) => ["winner_inconsistent", "score_not_nonneg_int", "quarter_not_final", "quarter_invalid", "bad_result_method"].includes(e));
  panels.push({
    id: "result", title: "Result", state: !Object.keys(res).length ? "absent" : resErr.length || status === "FAIL" ? "fail" : status === "VERIFIED" ? "pass" : "info",
    gold: status === "VERIFIED",
    score: Object.keys(res).length ? { away: res.away_score, home: res.home_score, quarter: res.quarter, winner: res.winner } : null,
    facts: [
      ["method", res.method], ["final basis", res.final_basis], ["final confirmed", res.final_confirmed],
      ["last scorebug read", isObj(res.scorebug_last) ? `home ${res.scorebug_last.home_score} – away ${res.scorebug_last.away_score}${res.scorebug_last.quarter != null ? ` · Q${res.scorebug_last.quarter}` : ""}` : undefined],
      ["referee re-read", rv && isObj(rv.final_reread) ? `home ${rv.final_reread.home} – away ${rv.final_reread.away} · ${rv.final_reread.basis || "basis not given"} · confirmed ${rv.final_reread.final_confirmed}` : undefined],
      ...(Array.isArray(res.notes) ? res.notes.map((n) => ["note", n]) : []),
    ],
    note: status === "VERIFIED" ? "Gold because the verifier returned VERIFIED." : "Not a verified result. Digits are not gold.",
  });

  // hash chain, per role
  const cps = isObj(r.checkpoints) ? r.checkpoints : {};
  const chains = [];
  for (const role of ["home", "away"]) {
    const rows = Array.isArray(cps[role]) ? cps[role] : null;
    if (!rows) continue;
    const cracks = segmentsFromErrors(errors, role, rows.length);
    const tail = errors.includes(`${role}:chain_ends_before_match_end`);
    const first = rows[0] || {}, last = rows[rows.length - 1] || {};
    chains.push({ role, n: rows.length, cracks: [...cracks.entries()].map(([i, why]) => ({ i, why })), tail,
      head: typeof last.hash === "string" ? last.hash : null,
      span_s: Number.isFinite(last.t_ms - first.t_ms) ? Math.round((last.t_ms - first.t_ms) / 1000) : null });
  }
  const chainErrs = errors.filter((e) => /:(cp\[\d+\]:|chain_ends_before_match_end|no_checkpoints)/.test(e));
  panels.push({ id: "chain", title: "Hash chain", chains,
    state: !chains.length ? "absent" : chainErrs.length ? "fail" : stopped ? "unchecked" : "pass",
    note: rv ? "Countersigned by the capture key named in the official review." : "Countersigned by the key in referee.pubkey. Each link is about 2 s of recording." });

  // challenges
  const ch = Array.isArray(r.challenges) ? r.challenges : [];
  const roles = (Array.isArray(r.players) ? r.players : []).map((p) => p && p.role).filter((x) => x === "home" || x === "away");
  const cells = [];
  for (const role of roles) {
    for (const ph of PHASES) {
      const c = ch.find((x) => x && x.role === role && x.phase === ph);
      const codes = [...errors, ...partial].filter((x) => x.startsWith(`${role}:${ph}:`) || x === `${role}:challenge_missing:${ph}` || x === `${role}:challenge_unanswered:${ph}`);
      const hard = errors.some((x) => x.startsWith(`${role}:${ph}:`));
      const res2 = c && isObj(c.result) ? c.result : null;
      cells.push({ role, phase: ph, label: PHASE_LABEL[ph], present: !!c, pattern: c && Array.isArray(c.pattern) ? c.pattern.map(String) : [],
        latency_ms: res2 ? res2.latency_ms : null, rule: res2 ? (res2.rule ?? 1) : null, official: !!(c && c.issuer),
        reverified: res2 ? res2.reverified_by || null : null, reason: res2 ? res2.reason : null,
        state: !c ? "absent" : hard ? "fail" : codes.length ? "partial" : stopped ? "unchecked" : "pass", codes });
    }
  }
  const chState = !cells.length ? "absent" : cells.some((c) => c.state === "fail") ? "fail"
    : cells.every((c) => c.state === "absent") ? "absent" : cells.some((c) => c.state !== "pass") ? "partial" : "pass";
  panels.push({ id: "challenges", title: "Nonce challenges", cells, state: chState,
    note: "Button names are as recorded in the receipt. The referee checks timing and the controller log, not which pad was used." });

  // motion
  const mp = isObj(sig.motion_permille) ? sig.motion_permille : {};
  const motion = roles.map((role) => ({ role, value: Number.isInteger(mp[role]) ? mp[role] : null,
    state: !Number.isInteger(mp[role]) ? "absent" : mp[role] < MOTION_MIN ? "partial" : "pass" }));
  const motionBasis = typeof sig.motion_basis === "string" ? sig.motion_basis : "";
  const motionNote = motionBasis.includes("motion-rule/v3")
    ? "Controller report rate against frame-difference energy on a stratified random spread of at most 24 media segments drawn after commit, timed with the fragment PTS clock. Bins dropped only for referee-measured scorebug absence, end menu, final screen, or challenge pause. Supporting evidence, not proof."
    : "Controller report rate against frame-difference energy. Supporting evidence, not proof.";
  panels.push({ id: "motion", title: "Motion correlation", motion, floor: MOTION_MIN, basis: sig.motion_basis || null,
    state: !motion.length || motion.every((x) => x.state === "absent") ? "absent" : motion.some((x) => x.state === "partial" || x.state === "absent") ? "partial" : "pass",
    note: motionNote });

  // controller input (advisory)
  const inp = isObj(sig.input) ? sig.input : null;
  const adv = v && v.input_advisory;
  const integ = rv && isObj(rv.checks) && isObj(rv.checks.input_integrity) ? rv.checks.input_integrity : null;
  panels.push({ id: "input", title: "Controller evidence", advisory: true,
    state: !inp ? "absent" : integ && integ.state === "fail" ? "fail" : adv && adv.ok === false ? "partial" : "info",
    device: publicDevice(inp && inp.device),
    human: inp && Number.isInteger(inp.human_permille) ? inp.human_permille : null, human_label: inp && inp.human_label,
    align: inp && Number.isInteger(inp.match_permille) && Number.isInteger(inp.chance_permille) ? { match: inp.match_permille, chance: inp.chance_permille } : null,
    haptics: inp && Number.isInteger(inp.haptic_events) ? { events: inp.haptic_events, high: inp.haptic_high_confidence } : null,
    facts: inp ? [["inputs sha256", inp.inputs_sha256 ? "present" : undefined], ["inputs chain head", inp.inputs_chain_head ? "present" : undefined],
      ["haptics sha256", inp.haptics_sha256 ? "present" : undefined], ["offset to video", Number.isInteger(inp.offset_ms) ? `${inp.offset_ms} ms` : undefined],
      ["referee log check", integ ? integ.state : undefined]] : [],
    flags: adv ? adv.flags || [] : [],
    note: "Advisory evidence, not proof. It shows controller activity in sync with the video, not who held the controller." });

  // device key (review.device_key, only when the referee signature checks)
  const dk = v && v.device_key;
  const dkHidden = !dk && rv && isObj(rv.device_key);
  panels.push({ id: "device", title: "Device key", device: dk || null,
    state: dk ? (dk.status === "invalid" ? "fail" : dk.status === "tpm_claimed" ? "partial" : "info") : dkHidden ? "unchecked" : "absent",
    note: dk ? DEVICE_NOTE[dk.status] || "Does not change the receipt verdict." : dkHidden ? "Present in the receipt but not shown: the referee signature did not check." : "Not provided. No device_attest.v0.json was reviewed for this receipt." });

  // corroboration
  const co = v && v.corroboration;
  const coHidden = !co && rv && isObj(rv.corroboration);
  panels.push({ id: "corroboration", title: "Two-sided corroboration", corroboration: co || null,
    state: co ? (co.status === "corroborated" ? "pass" : co.status === "disagreed" ? "fail" : "info") : coHidden ? "unchecked" : "absent",
    note: co ? "A separate indicator. It does not change the verdict." : coHidden ? "Present in the receipt but not shown: the referee signature did not check." : "Not provided. No pairing was reviewed for this receipt." });

  // official review
  if (rv) {
    const checks = isObj(rv.checks) ? Object.entries(rv.checks).map(([name, c]) => ({ name, label: checkLabel(name), known: name in KNOWN_CHECKS,
      state: isObj(c) && ["pass", "partial", "fail"].includes(c.state) ? c.state : "fail", detail: isObj(c) ? c.detail : null,
      value: name === "final_reread" ? rereadValue(c) : null })) : [];
    const vb = rv.checks && isObj(rv.checks.video_bytes) && isObj(rv.checks.video_bytes.detail) ? rv.checks.video_bytes.detail : null;
    const an = isObj(rv.anchors) ? rv.anchors : null;
    panels.push({ id: "review", title: "Official review", checks,
      state: errors.some((e) => e.startsWith("review_")) || !sigOk ? "fail" : partial.some((p) => p.startsWith("review_partial:")) ? "partial" : "pass",
      video: vb ? { verified: vb.segments_verified, total: vb.segments_total, bytes_verified: vb.bytes_verified, bytes_total: vb.bytes_total,
        method: vb.method, random: vb.random_segments, p10: vb.p_undetected_if_missing_10pct } : null,
      anchors: an ? { valid: an.valid, invalid: an.invalid } : null,
      scope: typeof rv.scope === "string" ? rv.scope : null,
      not_proven: Array.isArray(rv.not_proven) ? rv.not_proven.filter((x) => typeof x === "string") : [],
      bundle: isObj(rv.bundle) ? { qorcap_version: rv.bundle.qorcap_version, segments: rv.bundle.segments, schema: rv.bundle.schema } : null,
      note: sigOk ? "Checks the referee ran on the uploaded bundle. Signed with the receipt." : "The referee signature did not check, so these entries are not trusted." });
  } else {
    panels.push({ id: "review", title: "Official review", state: "absent", checks: [],
      note: "Not provided. This receipt was not reviewed by the official referee." });
  }

  // referee key
  const ref = isObj(r.referee) ? r.referee : {};
  panels.push({ id: "referee", title: "Referee key", state: v && v.referee_trust === "active" ? "pass" : v && v.referee_trust === "revoked" ? "fail" : Object.keys(ref).length ? "partial" : "absent",
    facts: [["key id", ref.key_id], ["trusted list", v ? ({ active: "on the list, active", revoked: "revoked", untrusted: "not on the list" }[v.referee_trust] || v.referee_trust) : undefined],
      ["referee id", v && v.referee_id], ["official review key", ref.key_kind === REVIEW_KEY_KIND ? "yes" : "no"],
      ["delegation", v && v.delegation ? `serial ${v.delegation.serial} · root ${v.delegation.delegated_by} · valid until ${new Date(v.delegation.not_after_ms).toISOString().slice(0, 10)}` : (ref.delegation ? "present, did not check" : undefined)]],
    note: "Only a key on QorTime's built-in list can produce VERIFIED." });

  // agreement (dual)
  const ag = isObj(r.agreement) ? r.agreement : null;
  const dual = m.mode === "dual";
  panels.push({ id: "agreement", title: "Two-player timeline", state: !dual ? "absent" : errors.includes("dual_timelines_disagree") || errors.includes("dual_missing_roles") ? "fail" : ag && ag.timelines_agree === true ? "pass" : "partial",
    facts: dual && ag ? [["timelines agree", ag.timelines_agree], ...(Array.isArray(ag.mismatches) ? ag.mismatches.slice(0, 8).map((x) => ["mismatch", x]) : [])] : [],
    note: dual ? "Both players recorded the same match." : "Not applicable. Single-console receipt." });

  // on-screen handles
  const oh = isObj(m.observed_handles) ? m.observed_handles : null;
  panels.push({ id: "handles", title: "On-screen names", state: oh ? "info" : "absent",
    sides: oh ? ["away", "home"].filter((s) => isObj(oh[s])).map((s) => ({ side: s, handle: oh[s].handle, pattern: oh[s].pattern,
      confidence: oh[s].confidence_permille, sightings: oh[s].sightings, agree: oh[s].agree, screen_side: oh[s].screen_side })) : [],
    facts: oh ? [["platform names", oh.platform], ["reader", oh.reader]] : [],
    note: oh ? (typeof oh.claims === "string" ? oh.claims : "On-screen evidence, not account proof.") : "Not provided. No on-screen names in this receipt." });

  // capture
  panels.push({ id: "capture", title: "Capture", state: Object.keys(m).length ? "info" : "absent",
    facts: [["match id", m.match_id], ["game", m.game], ["platform", m.platform], ["scorebug profile", m.scorebug_profile], ["mode", m.mode],
      ["length", Number.isFinite(m.ended_ms - m.started_ms) ? `${Math.round((m.ended_ms - m.started_ms) / 1000)} s` : undefined],
      ["qorcap", rv && isObj(rv.bundle) ? rv.bundle.qorcap_version : undefined],
      ["controller", publicDevice(inp && inp.device) || undefined]],
    note: "Shown as written in the receipt. Any HDMI console and any controller name QorCap records are listed as given." });

  // unknown evidence: fields the page does not know are shown raw, never dropped and never scored
  const unknown = [];
  for (const k of Object.keys(r)) if (!KNOWN_TOP.has(k)) unknown.push({ where: "receipt", key: k, value: r[k] });
  for (const k of Object.keys(sig)) if (!KNOWN_SIGNALS.has(k)) unknown.push({ where: "signals", key: k, value: sig[k] });
  if (rv) {
    for (const k of Object.keys(rv)) if (!KNOWN_REVIEW.has(k)) unknown.push({ where: "review", key: k, value: rv[k] });
    if (isObj(rv.checks)) for (const k of Object.keys(rv.checks)) if (!(k in KNOWN_CHECKS)) unknown.push({ where: "review.checks", key: k, value: rv.checks[k] });
  }
  if (isObj(res)) for (const k of Object.keys(res)) if (!["home_score", "away_score", "quarter", "final_t_ms", "winner", "method", "final_basis", "final_confirmed", "scorebug_last", "notes"].includes(k)) unknown.push({ where: "result", key: k, value: res[k] });
  panels.push({ id: "unknown", title: "Other evidence", state: unknown.length ? "info" : "absent", items: unknown,
    note: unknown.length ? "Fields this page does not know. Shown as written. They do not change the verdict on this page." : "None. Every field in this receipt is one the page knows." });

  if (extras.sidecars) panels.push(sidecarPanel(extras.sidecars));
  return panels;
}

const SIDECARS = {
  "qorcap-device-attest": "Device key file. The referee reads it into review.device_key. Strongest status: TPM claimed (unverified).",
  "qorcap-score-timeline": "Signed score timeline. The referee uses it for two-sided corroboration.",
  "qortime-pairing": "Referee-signed pairing token that links two players' submissions.",
  "qorcap-bundle": "QorCap bundle summary.",
  "qortime-corroboration": "Corroboration view written by the referee.",
  "qortime-challenge": "Referee-signed challenge token.",
  "qortime-anchor": "Referee-signed chain-head timestamp.",
  "qortime-trusted-referees": "Trusted referee list.",
  "qor-receipt": "A receipt. Paste it in the box above to verify it.",
};

// On-device learning files. v0 only. A later version is an unknown sidecar and stays on the generic panel.
// These rows are info. confirmed is not a pass. promoted is not VERIFIED.
const AI_V0 = {
  "qorcap-ai-proposal/v0": "On-device proposal. Not a label and not a verdict.",
  "qorcap-ai-outcome/v0": "Proposal joined to review checks or owner review. confirmed is not a pass and not VERIFIED.",
  "qorcap-ai-consolidation/v0": "Offline learning batch. promoted is not VERIFIED. discarded is not a receipt failure.",
  "qorcap-ai-stance/v0": "Model stance. The receipt verdict stays with the referee. Not a label.",
  "qorcap-ai-label-view/v0": "Label view for the learning join. Labels come from review checks or owner review. Not a verdict.",
};
const AI_FAMILIES = new Set(Object.keys(AI_V0).map((id) => id.split("/")[0]));

function schemaIdOf(row) {
  if (!isObj(row)) return null;
  return [row.schema, row.type, row.schema_id].find((x) => typeof x === "string") || null;
}

function classifySchema(id) {
  if (typeof id !== "string" || !id) return { schema: null, family: null, version: null, known: null };
  const slash = id.indexOf("/");
  const family = slash < 0 ? id : id.slice(0, slash);
  const version = slash < 0 ? null : id.slice(slash + 1);
  if (AI_FAMILIES.has(family)) {
    return { schema: id, family, version, known: AI_V0[id] || null };
  }
  return { schema: id, family, version, known: SIDECARS[family] || null };
}

/** Structured final_reread.value, or null. Display only. It does not set the check state. */
function rereadValue(c) {
  if (!isObj(c) || !isObj(c.value)) return null;
  const away = c.value.away, home = c.value.home, quarter = c.value.quarter;
  if (!Number.isInteger(away) || !Number.isInteger(home) || away < 0 || home < 0) return null;
  return { away, home, quarter: Number.isInteger(quarter) && quarter >= 0 ? quarter : null };
}

function aiSummary(family, doc, rows) {
  if (family === "qorcap-ai-proposal") {
    const n = rows ? rows.length : 1;
    const abstain = (rows || [doc]).filter((x) => isObj(x) && x.abstain === true).length;
    return `${n} proposal${n === 1 ? "" : "s"}${abstain ? `, ${abstain} abstaining` : ""}. A proposal is not a training label.`;
  }
  if (family === "qorcap-ai-outcome") {
    const list = rows || [doc];
    const n = (name) => list.filter((x) => isObj(x) && x.outcome === name).length;
    return `${list.length} outcomes: confirmed ${n("confirmed")}, contradicted ${n("contradicted")}, unverifiable ${n("unverifiable")}. confirmed is not a pass.`;
  }
  if (family === "qorcap-ai-consolidation" && isObj(doc)) {
    const decision = doc.decision === "promoted" || doc.decision === "discarded" ? doc.decision : "not given";
    const tail = decision === "promoted" ? "promoted is not VERIFIED." : decision === "discarded" ? "discarded is not a receipt failure." : "Shown as written.";
    return `decision ${decision}. ${tail}`;
  }
  if (family === "qorcap-ai-stance" && isObj(doc)) {
    const stance = ["accept", "dispute", "abstain"].includes(doc.stance) ? doc.stance : "not given";
    return `stance ${stance}. The receipt verdict stays with the referee.`;
  }
  if (family === "qorcap-ai-label-view" && isObj(doc)) {
    const n = isObj(doc.checks) ? Object.keys(doc.checks).length : 0;
    const source = doc.source === "referee" || doc.source === "human_review" ? doc.source : "not given";
    return `${n} checks from ${source}. detail prose is not a label.`;
  }
  return null;
}

/** Parse local evidence files (sidecars). Identification only: none of them is checked in this browser. */
export function describeSidecar(name, text) {
  const out = { name: String(name || "file"), bytes: typeof text === "string" ? text.length : 0, state: "info" };
  if (/^[0-9a-f]{64}$/i.test(String(text || "").trim())) {
    out.kind = "public key"; out.schema = null;
    out.known = "An Ed25519 public key. Pinning it does not make it trusted; only a key on QorTime's built-in list can produce VERIFIED.";
    return out;
  }
  let doc;
  try { doc = JSON.parse(text); } catch {
    const lines = String(text || "").split(/\r?\n/).filter(Boolean);
    let rows;
    try { rows = lines.map((l) => JSON.parse(l)); }
    catch { out.state = "fail"; out.error = "Not JSON or JSON lines."; return out; }
    out.kind = "jsonl"; out.rows = rows.length;
    const ids = [...new Set(rows.map(schemaIdOf).filter(Boolean))];
    const id = ids.length === 1 ? ids[0] : null;
    Object.assign(out, classifySchema(id));
    if (ids.length > 1) { out.schema = null; out.family = null; out.version = null; out.known = null; }
    if (out.known) out.summary = aiSummary(out.family, null, rows);
    return out;
  }
  if (!isObj(doc)) { out.kind = Array.isArray(doc) ? "array" : typeof doc; out.known = null; return out; }
  Object.assign(out, classifySchema(schemaIdOf(doc)));
  out.fields = Object.keys(doc).slice(0, 40).map((k) => ({ key: k, type: Array.isArray(doc[k]) ? `list of ${doc[k].length}` : doc[k] === null ? "null" : typeof doc[k] }));
  if (out.known && AI_FAMILIES.has(out.family)) out.summary = aiSummary(out.family, doc, null);
  if (out.family === "qorcap-score-timeline") {
    const ev = isObj(doc.signals) && isObj(doc.signals.score_timeline) && Array.isArray(doc.signals.score_timeline.events) ? doc.signals.score_timeline.events : [];
    out.summary = `${ev.length} score events in the file${ev.length ? `, last home ${ev[ev.length - 1].home} – away ${ev[ev.length - 1].away}` : ""}.`;
  } else if (out.family === "qorcap-device-attest") {
    out.summary = `Key provider ${doc.key_provider ?? "not given"}, algorithm ${doc.algorithm ?? "not given"}, non-exportable ${doc.non_exportable ?? "not given"}.`;
  } else if (out.family === "qortime-pairing") {
    out.summary = `Pairing ${typeof doc.pairing_id === "string" ? doc.pairing_id : "not given"}.`;
  }
  return out;
}

function sidecarPanel(list) {
  return { id: "sidecars", title: "Evidence files", state: list.length ? "info" : "absent", files: list,
    note: list.length ? "Identified by their schema field. These files are not checked in this browser. The referee checks them and signs what it found into the receipt." : "None attached." };
}

/** Pure check used by tests: gold is allowed only when the verifier said VERIFIED. */
export function goldAllowed(v) { return !!v && v.status === "VERIFIED"; }
