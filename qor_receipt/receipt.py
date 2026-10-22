"""Build / sign / verify qor-receipt v0.

Signed bytes = JCS(receipt without "signatures"). Every listed player and the
referee sign the same bytes. Verdicts follow the QorTroller PORT-CERT honesty
rule: any broken evidence => FAIL; missing evidence => PARTIAL; VERIFIED only
when everything was checked **and** the referee pubkey is on the built-in
trusted-referee allowlist. Unknown / local / URL-pinned keys are at most
SELF_ATTESTED. The trust tier is computed, never trusted.
"""
from __future__ import annotations

import copy
from typing import Any

from qor_receipt import SCHEMA
from qor_receipt import delegation as D
from qor_receipt.challenge import (
    DEFAULT_LEN,
    MAX_WINDOW_MS,
    PHASES,
    RULE_V1,
    RULE_V2,
    deviations_over,
    latency_ceiling_ms,
    pattern_for,
    result_rule_id,
)
from qor_receipt.checkpoints import verify_chain
from qor_receipt.jcs import canonicalize
from qor_receipt.keys import KeyProvider, kid, verify  # noqa: F401
from qor_receipt.review.corroboration import corroboration_view
from qor_receipt.review.device_attest import device_key_view
from qor_receipt.review.policy import CHALLENGE_TOKEN_TYPE, CHECK_STATES, FAIL_CHECK, PARTIAL_CHECK, REVIEW_KEY_KIND, REVIEW_TYPE
from qor_receipt.trusted_referees import is_trusted_active, load_allowlist, lookup

VERIFIED, PARTIAL, SELF_ATTESTED, FAIL = "VERIFIED", "PARTIAL", "SELF_ATTESTED", "FAIL"
DUAL, SINGLE = "dual-attested", "single-attested"
MOTION_MIN_PERMILLE = 300
RESULT_METHODS = frozenset({"agreed_scorebug_final", "end_menu_read", "last_scorebug_read_unconfirmed"})
UNCONFIRMED = "last_scorebug_read_unconfirmed"


def winner_for(home: int, away: int) -> str:
    """``result.winner`` for a score: "home", "away" or "tie" (equal scores)."""
    return "home" if home > away else "away" if away > home else "tie"


def result_errors(res: dict) -> list[str]:
    """Result consistency (receipt.v0, 2026-10-17 rules):
    * winner must follow the score: home / away / "tie" on equal scores (a legacy "away" on a tie is still accepted,
      that is what drafts said before ties existed);
    * a final (agreed_scorebug_final / end_menu_read) has integer scores and quarter >= 4 (5 = OT);
    * an unconfirmed last read keeps the quarter it showed (>= 1) or null, and may carry null scores with winner
      "unknown" when nothing was read at all (never an invented 0-0 / Q4 / "away")."""
    errs: list[str] = []
    unconfirmed = res.get("method") == UNCONFIRMED
    h, a, q = res.get("home_score"), res.get("away_score"), res.get("quarter")
    if unconfirmed and h is None and a is None:
        if res.get("winner") != "unknown":
            errs.append("winner_inconsistent")
    elif not _is_nonneg_int(h) or not _is_nonneg_int(a):
        errs.append("score_not_nonneg_int")
    else:
        w = winner_for(h, a)
        if res.get("winner") != w and not (w == "tie" and res.get("winner") == "away"):
            errs.append("winner_inconsistent")
    q_ok = isinstance(q, int) and not isinstance(q, bool)
    if unconfirmed:
        if q is not None and (not q_ok or q < 1):
            errs.append("quarter_invalid")
    elif not q_ok or q < 4:
        errs.append("quarter_not_final")
    return errs
ROLES = frozenset({"home", "away"})
LOCAL_REFEREE_PREFIXES = ("local-self-attested",)
UNVERIFIED_KIND_CLAIMS = frozenset({"tpm"})


def signing_bytes(receipt: dict[str, Any]) -> bytes:
    return canonicalize({k: v for k, v in receipt.items() if k != "signatures"})


def compute_tier(body: dict[str, Any]) -> str:
    roles = {p["role"] for p in body.get("players", [])}
    ag = body.get("agreement") or {}
    if body["match"].get("mode") == "dual" and roles == {"home", "away"} and ag.get("timelines_agree") is True:
        return DUAL
    return SINGLE


def build(*, match: dict, result: dict, players: list[dict], referee: dict, challenges: list[dict],
          checkpoints: dict, agreement: dict, signals: dict) -> dict[str, Any]:
    body = {"schema": SCHEMA, "match": match, "result": result, "players": players, "referee": referee,
            "challenges": challenges, "checkpoints": checkpoints, "agreement": agreement, "signals": signals}
    body["trust_tier"] = compute_tier(body)
    body["signatures"] = {"players": {}, "referee": None}
    return body


def sign_player(receipt: dict, role: str, key: KeyProvider) -> dict:
    r = copy.deepcopy(receipt)
    r["signatures"]["players"][role] = {"key_id": key.key_id, "sig": key.sign(signing_bytes(r))}
    return r


def sign_referee(receipt: dict, key: KeyProvider) -> dict:
    r = copy.deepcopy(receipt)
    r["signatures"]["referee"] = {"key_id": key.key_id, "sig": key.sign(signing_bytes(r))}
    return r


def _is_nonneg_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0



def _challenge_evidence_errors(c: dict[str, Any], cps: list[dict[str, Any]]) -> list[str]:
    """Evidence required when result.ok is claimed. Without HID bytes in the receipt we cannot
    re-run verify_response; we require a HID commitment, sane timing, and a countersigned
    checkpoint whose referee clock falls inside the challenge window (referee was live)."""
    errs: list[str] = []
    res = c.get("result") or {}
    hid = res.get("hid_sha256")
    if not isinstance(hid, str) or len(hid) != 64 or any(ch not in "0123456789abcdef" for ch in hid.lower()):
        errs.append("challenge_missing_hid_sha256")
    try:
        issued, deadline = int(c["issued_ms"]), int(c["deadline_ms"])
        pause = res.get("pause_press_ms")
        done = res.get("done_ms")
        lat = res.get("latency_ms")
    except (KeyError, TypeError, ValueError):
        return errs + ["challenge_timing_malformed"]
    # v0.3.11: windows are 90 s (pause-capable moments); anything over MAX_WINDOW_MS is not a timed challenge.
    if not (0 < deadline - issued <= MAX_WINDOW_MS):
        errs.append("challenge_window_too_long")
    if pause is None or done is None or lat is None:
        errs.append("challenge_timing_incomplete")
    else:
        if not (issued <= int(pause) <= deadline):
            errs.append("challenge_pause_outside_window")
        if int(done) < int(pause):
            errs.append("challenge_done_before_pause")
        # Absent rule is v1 (0..600). Rule 2 is the capture-path ceiling. Any other value is not a
        # widening: it is flagged and judged against the v1 ceiling.
        rule_id = result_rule_id(res)
        if rule_id is None:
            errs.append("challenge_rule_unknown")
        if not (0 <= int(lat) <= latency_ceiling_ms(rule_id)):
            errs.append("challenge_latency_out_of_range")
    # Live referee: at least one countersigned checkpoint stamped during the challenge window.
    win_lo, win_hi = issued - 3_000, deadline + 5_000
    if not any(isinstance(cp.get("referee_ts_ms"), int) and win_lo <= cp["referee_ts_ms"] <= win_hi for cp in (cps or [])):
        errs.append("challenge_not_bound_to_live_checkpoint")
    return errs


def challenge_consistency_partials(challenges: list[dict[str, Any]], role: str) -> list[str]:
    """Backstop when a signed receipt claims several rule-v2 successes that do not agree.

    The referee flips outliers to ``ok`` false before signing, which is a failed challenge.
    This catches a receipt that still claims them ok: the verdict stays PARTIAL. Mixed rule
    ids on one role are the same kind of partial. Rule v1 latencies are not compared here;
    their ceiling is already 600 ms.
    """
    ok: list[dict[str, Any]] = []
    for c in challenges or []:
        if c.get("role") != role:
            continue
        res = c.get("result") or {}
        if res.get("ok"):
            ok.append(res)
    rules = [result_rule_id(res) for res in ok]
    out: list[str] = []
    known = [r for r in rules if r in (RULE_V1, RULE_V2)]
    if RULE_V1 in known and RULE_V2 in known:
        out.append(f"{role}:challenge_rule_mixed")
    lats = []
    for res in ok:
        if result_rule_id(res) != RULE_V2:
            continue
        lat = res.get("latency_ms")
        if isinstance(lat, int) and not isinstance(lat, bool):
            lats.append(int(lat))
    if deviations_over(lats):
        out.append(f"{role}:challenge_latency_inconsistent")
    return out


def _is_hex64(v: Any) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(ch in "0123456789abcdef" for ch in v.lower())


def official_challenge_ok(c: dict[str, Any], *, allowlist_path: str | None = None,
                          delegate_pub: str | None = None) -> bool:
    """Review receipts: the challenge nonce must come from a token signed by an allowlisted active referee key,
    issued for exactly this nonce / phase / window (so the pattern could not be known before issued_ms)."""
    tok = c.get("issuer")
    if not isinstance(tok, dict) or tok.get("type") != CHALLENGE_TOKEN_TYPE or not isinstance(tok.get("sig"), str):
        return False
    for k in ("nonce", "phase", "issued_ms", "deadline_ms"):
        if tok.get(k) != c.get(k):
            return False
    from qor_receipt.trusted_referees import load_allowlist

    payload = canonicalize({k: v for k, v in tok.items() if k != "sig"})
    # Delegated service key (already validated against the root-signed cert by the caller) may sign tokens too.
    if delegate_pub and tok.get("key_id") == kid(delegate_pub) and verify(delegate_pub, tok["sig"], payload):
        return True
    for row in load_allowlist(allowlist_path).get("referees") or []:
        pub = str(row.get("pubkey", "")).lower()
        if row.get("status") == "active" and tok.get("key_id") == kid(pub) and verify(pub, tok["sig"], payload):
            return True
    return False


def _review_problems(r: dict[str, Any], ref: dict[str, Any]) -> tuple[str | None, list[str], list[str]]:
    """Validate the ``review`` block of an official post-hoc review receipt. Returns (capture_pubkey, errors, partial)."""
    rv = r.get("review")
    if not isinstance(rv, dict) or rv.get("type") != REVIEW_TYPE:
        return None, ["review_missing_or_wrong_type"], []
    cap = (rv.get("capture_referee") or {}).get("pubkey")
    errs: list[str] = []
    part: list[str] = []
    if not _is_hex64(cap):
        errs.append("review_capture_referee_missing")
        cap = None
    elif cap.lower() == str(ref.get("pubkey", "")).lower():
        errs.append("review_capture_referee_is_official_key")
    checks = rv.get("checks")
    if not isinstance(checks, dict) or not checks:
        errs.append("review_checks_missing")
        checks = {}
    for name in ("video_bytes", "final_reread", "capture_chain", "bundle_structure"):
        if name not in checks:
            errs.append(f"review_check_missing:{name}")
    for name, c in sorted(checks.items()):
        st = c.get("state") if isinstance(c, dict) else None
        if st not in CHECK_STATES:
            errs.append(f"review_check_bad_state:{name}")
        elif st == FAIL_CHECK:
            errs.append(f"review_check_failed:{name}")
        elif st == PARTIAL_CHECK:
            part.append(f"review_partial:{name}")
    return cap, errs, part


# Advisory thresholds for signals.input (QorCap input_proof) — never gates VERIFIED alone.
INPUT_HUMAN_MIN = 400          # human_permille: below = "machine-like / low"
INPUT_ALIGN_EDGE = 50          # match_permille must beat chance_permille by this margin
INPUT_HAPTIC_MIN = 1           # at least one high-confidence haptic if haptics claimed


def _input_proof_advisory(signals: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fold QorCap ``signals.input`` into verify as signed advisory evidence (not proof).

    Returns None when no input block is present. Never raises; missing fields → caution notes.
    """
    if not isinstance(signals, dict):
        return None
    inp = signals.get("input")
    if not isinstance(inp, dict) or not inp:
        return None
    notes: list[str] = []
    flags: list[str] = []
    human = inp.get("human_permille")
    label = inp.get("human_label")
    if isinstance(human, int):
        if human < INPUT_HUMAN_MIN:
            flags.append("human_score_low")
            notes.append(f"human_permille={human} below advisory floor {INPUT_HUMAN_MIN}"
                         + (f" ({label})" if label else ""))
        else:
            notes.append(f"human_permille={human}" + (f" ({label})" if label else ""))
    else:
        flags.append("human_score_missing")
    match_p, chance_p = inp.get("match_permille"), inp.get("chance_permille")
    if isinstance(match_p, int) and isinstance(chance_p, int):
        if match_p < chance_p + INPUT_ALIGN_EDGE:
            flags.append("alignment_not_above_chance")
            notes.append(f"alignment match={match_p} chance={chance_p} (need +{INPUT_ALIGN_EDGE})")
        else:
            notes.append(f"alignment match={match_p} vs chance={chance_p} (ok)")
    else:
        flags.append("alignment_missing")
    he = inp.get("haptic_events")
    hc = inp.get("haptic_high_confidence")
    if isinstance(he, int):
        if he <= 0:
            flags.append("no_haptic_events")
            notes.append("no haptic events recorded")
        elif isinstance(hc, int) and hc < INPUT_HAPTIC_MIN:
            flags.append("haptic_low_confidence_only")
            notes.append(f"haptic_events={he} but high_confidence={hc}")
        else:
            notes.append(f"haptic_events={he} high_confidence={hc}")
    # Signed hashes present? advisory only
    if not inp.get("inputs_sha256"):
        flags.append("inputs_sha256_missing")
    ok = not any(f in flags for f in (
        "human_score_low", "alignment_not_above_chance", "no_haptic_events", "haptic_low_confidence_only"))
    return {
        "present": True,
        "ok": ok,
        "flags": flags,
        "notes": notes,
        "status": inp.get("status"),
        "human_permille": human,
        "match_permille": match_p,
        "chance_permille": chance_p,
        "haptic_events": he,
        "haptic_high_confidence": hc,
        "claims": "advisory evidence only — not cryptographic proof of a human player",
    }


def verify_receipt(r: dict[str, Any], *, referee_pub: str | None = None,
                   allowlist_path: str | None = None, revocations_path: str | None = None,
                   now_ms: int | None = None) -> dict[str, Any]:
    errors: list[str] = []
    partial: list[str] = []
    trusted_ref = False
    referee_sig_ok = False
    input_advisory = _input_proof_advisory(r.get("signals") if isinstance(r, dict) else None)
    referee_entry: dict[str, Any] | None = None
    review_mode = False
    delegate: dict[str, Any] | None = None
    try:
        if r.get("schema") != SCHEMA:
            return {"status": FAIL, "trust_tier": None, "errors": ["schema"], "partial": [],
                    "referee_trust": "unknown"}
        msg = signing_bytes(r)
        sigs = r.get("signatures") or {}
        ref = r["referee"]
        if referee_pub is None:
            partial.append("referee_key_not_pinned")
        elif referee_pub.lower() != ref["pubkey"].lower():
            errors.append("referee_key_mismatch")
        rs = sigs.get("referee")
        if not rs or rs.get("key_id") != kid(ref["pubkey"]) or not verify(ref["pubkey"], rs["sig"], msg):
            errors.append("bad_referee_signature")
        else:
            referee_sig_ok = True

        referee_entry = lookup(ref["pubkey"], path=allowlist_path)
        if referee_entry and referee_entry.get("status") == "active":
            trusted_ref = True
        elif referee_entry and referee_entry.get("status") == "revoked":
            partial.append(f"referee_revoked:{referee_entry.get('id')}")
        elif ref.get("delegation") is not None:
            # Online service key certified by an offline root key (qor_receipt.delegation).
            if ref.get("key_kind") != REVIEW_KEY_KIND:
                partial.append("delegation_scope")
            else:
                delegate, dprobs = D.check_delegation(
                    ref["delegation"], ref["pubkey"], (r.get("review") or {}).get("reviewed_ms"),
                    allowlist=load_allowlist(allowlist_path), revocations=D.load_revocations(revocations_path),
                    now_ms=now_ms)
                if delegate:
                    trusted_ref = True
                    referee_entry = delegate
                else:
                    partial += dprobs
        else:
            partial.append("referee_not_on_allowlist")

        players = r.get("players")
        if not isinstance(players, list) or not players:
            errors.append("no_players")
            players = []
        roles_seen: set[str] = set()
        for p in players:
            role = p.get("role")
            if role not in ROLES:
                errors.append(f"bad_player_role:{role!r}")
                continue
            if role in roles_seen:
                errors.append(f"duplicate_player_role:{role}")
            roles_seen.add(role)
            ps = (sigs.get("players") or {}).get(role)
            if not ps:
                errors.append(f"missing_player_signature:{role}")
            elif ps.get("key_id") != kid(p["pubkey"]) or not verify(p["pubkey"], ps["sig"], msg):
                errors.append(f"bad_player_signature:{role}")
            kind = p.get("key_kind")
            if kind in UNVERIFIED_KIND_CLAIMS:
                partial.append(f"{role}:key_kind_claim_unverified:{kind}")

        # Official post-hoc review receipt: the official key signs the receipt; checkpoint countersignatures are the
        # capture (laptop) referee's and are checked under review.capture_referee. Every review check must pass.
        chain_pub = ref["pubkey"]
        review_mode = ref.get("key_kind") == REVIEW_KEY_KIND
        if review_mode:
            cap, rerrs, rpart = _review_problems(r, ref)
            errors += rerrs
            partial += rpart
            if cap:
                chain_pub = cap

        m = r["match"]
        if m.get("mode") == "dual" and roles_seen != {"home", "away"}:
            errors.append("dual_missing_roles")

        ref_kind = str(ref.get("key_kind") or "")
        if any(ref_kind.startswith(p) for p in LOCAL_REFEREE_PREFIXES):
            partial.append("referee_local_self_attested")
            trusted_ref = False  # local referee is never the official allowlisted operator key in spirit
        if ref_kind in UNVERIFIED_KIND_CLAIMS:
            partial.append(f"referee_key_kind_claim_unverified:{ref_kind}")
        for p in players:
            if p.get("role") in ROLES and p.get("pubkey", "").lower() == ref.get("pubkey", "").lower():
                partial.append("referee_same_as_player")
                trusted_ref = False
                break

        # Pin must be an allowlisted active key for VERIFIED. A URL/?referee= pin of an
        # unknown key never upgrades trust (loud warning is UI-side).
        pin_is_delegate = bool(delegate and referee_pub is not None and referee_pub.lower() == ref["pubkey"].lower())
        if referee_pub is not None and not pin_is_delegate and not is_trusted_active(referee_pub, path=allowlist_path):
            trusted_ref = False
            if "referee_pin_not_allowlisted" not in partial:
                partial.append("referee_pin_not_allowlisted")

        for p in players:
            role = p.get("role")
            if role not in ROLES:
                continue
            errors += [f"{role}:{e}" for e in verify_chain(
                r["checkpoints"].get(role, []), match_id=m["match_id"], role=role, referee_pub=chain_pub,
                start_ms=m["started_ms"], end_ms=m["ended_ms"])]
            for ph in PHASES:
                cs = [c for c in r["challenges"] if c["role"] == role and c["phase"] == ph]
                if not cs:
                    partial.append(f"{role}:challenge_missing:{ph}")
                for c in cs:
                    pat = c.get("pattern") or []
                    if not isinstance(pat, list) or len(pat) < DEFAULT_LEN + 1:
                        errors.append(f"{role}:{ph}:pattern_too_short")
                    elif c["pattern"] != pattern_for(c["nonce"], len(c["pattern"]) - 1):
                        errors.append(f"{role}:{ph}:pattern_not_from_nonce")
                    res = c.get("result")
                    if res is None:
                        partial.append(f"{role}:challenge_unanswered:{ph}")
                    elif not res.get("ok"):
                        errors.append(f"{role}:{ph}:challenge_failed:{res.get('reason')}")
                    else:
                        # Do not trust bare result.ok: require committed evidence + live referee window.
                        ev_errs = _challenge_evidence_errors(c, r["checkpoints"].get(role, []))
                        if review_mode and not official_challenge_ok(
                                c, allowlist_path=allowlist_path, delegate_pub=ref["pubkey"] if delegate else None):
                            ev_errs.append("challenge_nonce_not_official")
                        if ev_errs:
                            for e in ev_errs:
                                partial.append(f"{role}:{ph}:{e}")
            partial.extend(challenge_consistency_partials(r.get("challenges") or [], role))
            mp = (r.get("signals") or {}).get("motion_permille", {}).get(role)
            if mp is None:
                partial.append(f"{role}:motion_signal_missing")
            elif mp < MOTION_MIN_PERMILLE:
                partial.append(f"{role}:motion_correlation_low")
        ag = r.get("agreement") or {}
        if m.get("mode") == "dual" and ag.get("timelines_agree") is not True:
            errors.append("dual_timelines_disagree")
        res = r["result"]
        errors.extend(result_errors(res))
        if res.get("method") not in RESULT_METHODS:
            errors.append("bad_result_method")
        tier = compute_tier(r)
        if r.get("trust_tier") != tier:
            errors.append("trust_tier_claim_wrong")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        errors.append(f"malformed:{type(e).__name__}")
        tier = None
        trusted_ref = False

    if errors:
        status = FAIL
    elif not trusted_ref:
        status = SELF_ATTESTED
    elif partial:
        status = PARTIAL
    else:
        status = VERIFIED
    if delegate and status in (VERIFIED, PARTIAL) and status not in delegate["allowed_statuses"]:
        partial.append(f"delegation_caps_status:{status}")
        status = PARTIAL if PARTIAL in delegate["allowed_statuses"] else SELF_ATTESTED

    out = {"status": status, "trust_tier": tier, "errors": errors, "partial": partial,
           "referee_trust": ("active" if trusted_ref else ("revoked" if referee_entry and referee_entry.get("status") == "revoked" else "untrusted")),
           "input_advisory": input_advisory}
    if referee_entry:
        out["referee_id"] = referee_entry.get("id")
    if delegate:
        out["delegation"] = {"serial": delegate["serial"], "delegated_by": delegate["delegated_by"],
                             "not_after_ms": delegate["not_after_ms"]}
    if review_mode and isinstance(r.get("review"), dict):
        rv = r["review"]
        vb = ((rv.get("checks") or {}).get("video_bytes") or {}).get("detail") or {}
        out["verification_basis"] = "official-post-hoc-review"
        out["review"] = {"scope": rv.get("scope"), "video_method": vb.get("method") if isinstance(vb, dict) else None,
                         "segments_verified": vb.get("segments_verified") if isinstance(vb, dict) else None,
                         "segments_total": vb.get("segments_total") if isinstance(vb, dict) else None,
                         "not_proven": rv.get("not_proven")}
    # Additive. A disagreed or single-sided pair never edits status, errors, or partial.
    # device_key is informational and does not edit them either.
    if referee_sig_ok and isinstance(r, dict):
        review = r.get("review") if isinstance(r.get("review"), dict) else None
        corr = corroboration_view(review)
        if corr:
            out["corroboration"] = corr
        device_key = device_key_view(review)
        if device_key:
            out["device_key"] = device_key
    return out
