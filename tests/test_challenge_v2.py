"""Challenge rule v2: honest Madden/PS5 latency, and the attacks the wider ceiling must still stop.

Rule v1 (the default, and any receipt with no result.rule) stays 0..600 ms on the segment clock.
Rule v2 is the integer opt-in. These tests do not decode video; the presentation-time clock is
covered by test_referee_service.py.
"""
from qor_receipt.challenge import (
    RULE2_MAX_LATENCY_MS,
    apply_consistency,
    challenge_rule_of,
    pattern_for,
    verify_response,
)
from qor_receipt.demo import perform
from qor_receipt.receipt import _challenge_evidence_errors, challenge_consistency_partials
from qor_receipt.review.clock import capture_clock, fragment_pts0_ms

NONCE = "00112233445566778899aabbccddeeff"
# match-20261017-110028, presentation time of the fully drawn pause menu after OPTIONS.
HONEST_110028 = (1556, 1535, 1715)
PHASES = ("kickoff", "halftime", "end")


def _v(events, seen, **kw):
    return verify_response(pattern_for(NONCE), events, issued_ms=0, deadline_ms=90_000,
                           pause_seen_ms=seen, max_latency_ms=RULE2_MAX_LATENCY_MS, **kw)


def _measured(lats, rule=2):
    return [{"phase": ph, "result": {"ok": True, "reason": "ok", "latency_ms": lat, "rule": rule}}
            for ph, lat in zip(PHASES, lats)]


def _chal(lat, rule=None, phase="kickoff", role="home"):
    res = {"ok": True, "reason": "ok", "pause_press_ms": 1_500, "done_ms": 2_000, "latency_ms": lat,
           "hid_sha256": "a" * 64}
    if rule is not None:
        res["rule"] = rule
    return {"role": role, "phase": phase, "issued_ms": 1_000, "deadline_ms": 20_000, "result": res}


def test_match_110028_onsets_fail_v1_and_pass_v2_consistency():
    ev = perform(pattern_for(NONCE), 10_000)
    for lat in HONEST_110028:
        v1 = verify_response(pattern_for(NONCE), ev, issued_ms=0, deadline_ms=90_000, pause_seen_ms=10_000 + lat)
        assert not v1.ok and v1.reason == "video_latency_too_high" and v1.latency_ms == lat
        v2 = _v(ev, 10_000 + lat)
        assert v2.ok and v2.latency_ms == lat
    measured = _measured(HONEST_110028)
    assert apply_consistency(measured) == []
    assert all(c["result"]["ok"] for c in measured)
    # median 1556; end is +159 ms, inside ±300.
    spread = max(HONEST_110028) - min(HONEST_110028)
    assert spread == 180 and spread <= 300


def test_prerecorded_video_fails_rule_v2():
    ev = perform(pattern_for(NONCE), 10_000)
    before = _v(ev, 10_000 - 400)
    assert not before.ok and before.reason == "video_before_input"
    missing = _v(ev, None)
    assert not missing.ok and missing.reason == "pause_not_seen_on_video"


def test_replayed_pattern_fails_rule_v2():
    p = pattern_for(NONCE)
    replay = [p[0]] + (list(reversed(p[1:])) if p[1:] != p[:0:-1] else ["CROSS"] * 4)
    assert _v(perform(replay, 10_000), 10_200).reason == "pattern_mismatch"
    assert _v(perform(p, -5_000), -4_800).reason == "no_pause_press"


def test_artificially_delayed_video_fails_rule_v2_past_the_ceiling():
    ev = perform(pattern_for(NONCE), 10_000)
    late = _v(ev, 10_000 + 4_000)
    assert not late.ok and late.reason == "video_latency_too_high" and late.latency_ms == 4_000
    # 1.7 s is an honest-looking onset: v1 rejects it, the v2 ceiling alone accepts it.
    v1 = verify_response(pattern_for(NONCE), ev, issued_ms=0, deadline_ms=90_000, pause_seen_ms=10_000 + 1_700)
    assert v1.reason == "video_latency_too_high"
    assert _v(ev, 10_000 + 1_700).ok


def test_jittered_delay_fails_consistency_and_a_flat_delay_does_not():
    """±300 ms is the anti-relay signal v2 adds on top of the ceiling. A constant delay is not jitter."""
    honest = _measured((1556, 1535, 2200))
    assert apply_consistency(honest) == [2]
    assert honest[2]["result"]["ok"] is False and honest[2]["result"]["reason"] == "latency_inconsistent"
    assert honest[2]["result"]["latency_ms"] == 2200
    assert honest[0]["result"]["ok"] and honest[1]["result"]["ok"]
    flat = _measured((2400, 2400, 2400))
    assert apply_consistency(flat) == [] and all(c["result"]["ok"] for c in flat)


def test_consistency_boundary_is_exclusive_past_the_window():
    assert apply_consistency(_measured((1600, 2200))) == []
    flipped = _measured((1600, 2201))
    assert apply_consistency(flipped) == [0, 1]
    assert all(c["result"]["reason"] == "latency_inconsistent" for c in flipped)


def test_one_challenge_has_no_consistency_peer():
    lone = _measured((2400,))
    assert apply_consistency(lone) == [] and lone[0]["result"]["ok"]


def test_bundle_opt_in_fails_closed():
    assert challenge_rule_of({}) == 1
    assert challenge_rule_of({"challenge_rule": None}) == 1
    assert challenge_rule_of({"challenge_rule": 2}) == 2
    assert challenge_rule_of({"challenge_rule": "2"}) == 1
    assert challenge_rule_of({"challenge_rule": 3}) == 3
    assert challenge_rule_of({"challenge_rule": "3"}) == 1
    assert challenge_rule_of({"challenge_rule": 4}) == 1
    assert challenge_rule_of({"challenge_rule": True}) == 1
    # A version string alone does not widen anything. Rule 2 from before the calibrated recorder stays rule 2.
    assert challenge_rule_of({"qorcap_version": "0.3.31"}) == 1
    assert challenge_rule_of({"challenge_rule": 1, "qorcap_version": "0.3.31"}) == 1
    assert challenge_rule_of({"challenge_rule": 2, "qorcap_version": "0.3.30"}) == 2
    assert challenge_rule_of({"challenge_rule": 2, "qorcap_version": "test"}) == 2
    assert challenge_rule_of({"challenge_rule": 2, "qorcap_version": "v0.3.31"}) == 2
    # QorCap 0.3.31 wrote challenge_rule 2 and measured at the rule-3 caps. Stamp 3.
    assert challenge_rule_of({"challenge_rule": 2, "qorcap_version": "0.3.31"}) == 3
    assert challenge_rule_of({"challenge_rule": 2, "qorcap_version": "0.3.32"}) == 3
    assert challenge_rule_of({"challenge_rule": 2, "qorcap_version": "0.4.0"}) == 3
    assert challenge_rule_of({"challenge_rule": 3, "qorcap_version": "0.3.11"}) == 3


def test_old_receipt_keeps_the_600_ms_meaning():
    cps = [{"referee_ts_ms": 1_600}]
    old = _challenge_evidence_errors(_chal(1_700), cps)
    assert "challenge_latency_out_of_range" in old and "challenge_rule_unknown" not in old
    wide = _challenge_evidence_errors(_chal(1_700, 2), cps)
    assert wide == []
    rule3 = _challenge_evidence_errors(_chal(1_700, 3), cps)
    assert rule3 == []
    past_rule3 = _challenge_evidence_errors(_chal(2_501, 3), cps)
    assert "challenge_latency_out_of_range" in past_rule3 and "challenge_rule_unknown" not in past_rule3
    still = _challenge_evidence_errors(_chal(80), cps)
    assert still == []
    unknown = _challenge_evidence_errors(_chal(1_700, 9), cps)
    assert "challenge_rule_unknown" in unknown and "challenge_latency_out_of_range" in unknown
    fast_unknown = _challenge_evidence_errors(_chal(80, 9), cps)
    assert "challenge_rule_unknown" in fast_unknown and "challenge_latency_out_of_range" not in fast_unknown


def test_receipt_backstop_flags_inconsistent_and_mixed_rule_claims():
    consistent = [_chal(lat, 2, ph) for lat, ph in zip(HONEST_110028, PHASES)]
    assert challenge_consistency_partials(consistent, "home") == []
    inconsistent = [_chal(lat, 2, ph) for lat, ph in zip((1556, 1535, 2200), PHASES)]
    assert challenge_consistency_partials(inconsistent, "home") == ["home:challenge_latency_inconsistent"]
    mixed = [_chal(80, 1, "kickoff"), _chal(1556, 2, "halftime")]
    assert challenge_consistency_partials(mixed, "home") == ["home:challenge_rule_mixed"]
    mixed_23 = [_chal(1556, 2, "kickoff"), _chal(1605, 3, "halftime")]
    assert challenge_consistency_partials(mixed_23, "home") == ["home:challenge_rule_mixed"]
    rule3 = [_chal(lat, 3, ph) for lat, ph in zip(HONEST_110028, PHASES)]
    assert challenge_consistency_partials(rule3, "home") == []
    rule3_jitter = [_chal(lat, 3, ph) for lat, ph in zip((1556, 1535, 2200), PHASES)]
    assert challenge_consistency_partials(rule3_jitter, "home") == ["home:challenge_latency_inconsistent"]
    # A failed outlier is already not ok, so the backstop does not also fire.
    failed = _chal(2200, 2, "end")
    failed["result"]["ok"] = False
    assert challenge_consistency_partials([_chal(1556, 2, "kickoff"), _chal(1535, 2, "halftime"), failed], "home") == []


def test_presentation_clock_extrapolates_frame_zero_and_ignores_a_constant_counter():
    segs = [{"kind": "init", "frames": 129, "mono_ms": 6768}]
    for fr in range(129, 129 + 61 * 20, 61):
        segs.append({"kind": "media", "frames": fr, "mono_ms": int(round(2646.3 + 33.3346 * fr))})
    intercept, slope = capture_clock(segs)
    assert abs(intercept - 2646.3) < 5 and abs(slope - 33.3346) < 0.01
    assert fragment_pts0_ms(b"", b"") is None
    flat = [{"kind": "media", "frames": 60, "mono_ms": i * 2000} for i in range(1, 10)]
    assert capture_clock(flat) == (0.0, 1000.0 / 30.0)
