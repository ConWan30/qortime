import pytest

from qor_receipt.challenge import ALPHABET, PAUSE, motion_correlation_permille, new_nonce, pattern_for, verify_response
from qor_receipt.demo import perform

NONCE = "00112233445566778899aabbccddeeff"


def test_pattern_deterministic_and_shape():
    p = pattern_for(NONCE)
    assert p == pattern_for(NONCE) and p[0] == PAUSE and len(p) == 5
    assert all(b in ALPHABET for b in p[1:]) and all(a != b for a, b in zip(p[1:], p[2:]))


def test_patterns_vary():
    assert len({tuple(pattern_for(new_nonce())) for _ in range(50)}) > 40


# This nonce's pattern uses CROSS before the last slot. Verifiers must keep recomputing it.
UNSAFE_NONCE = "b9343e477a1df8a8794fd4d579a421b3"
UNSAFE_PATTERN = ["OPTIONS", "CROSS", "TRIANGLE", "CROSS", "CIRCLE"]


def test_pattern_for_keeps_an_old_nonce_that_exits_the_menu_early():
    from qor_receipt.challenge import pattern_menu_safe
    assert pattern_for(UNSAFE_NONCE) == UNSAFE_PATTERN
    assert not pattern_menu_safe(UNSAFE_PATTERN)
    # The old pattern still answers a challenge. Issuance policy does not change verification.
    assert verify_response(UNSAFE_PATTERN, perform(UNSAFE_PATTERN, 1_000), issued_ms=0, deadline_ms=20_000,
                           pause_seen_ms=1_120).ok


def test_new_challenge_nonce_keeps_circle_and_cross_for_the_last_slot():
    from qor_receipt.challenge import MENU_EXIT_BUTTONS, new_challenge_nonce, pattern_menu_safe
    seen = set()
    for _ in range(40):
        nonce = new_challenge_nonce()
        pattern = pattern_for(nonce)
        assert pattern_menu_safe(pattern)
        assert pattern[0] == PAUSE
        assert all(b not in MENU_EXIT_BUTTONS for b in pattern[1:-1])
        seen.add(tuple(pattern))
    assert len(seen) > 15


def test_live_referee_issues_a_menu_safe_pattern():
    from fastapi.testclient import TestClient

    from qor_receipt.keys import SoftwareKey
    from qor_receipt.referee.app import create_app

    key = SoftwareKey()
    client = TestClient(create_app(key, lambda: 5_000))
    mid = client.post("/matches", json={"players": [{"role": "home", "player_id": "p", "pubkey": key.pubkey_hex}]}).json()["match_id"]
    for _ in range(12):
        ch = client.post(f"/matches/{mid}/challenges", json={"phase": "kickoff", "role": "home"}).json()
        assert ch["pattern"] == pattern_for(ch["nonce"])
        assert all(b not in ("CROSS", "CIRCLE") for b in ch["pattern"][1:-1])


def test_referee_redraws_fresh_nonces_and_keeps_an_explicit_one(tmp_path):
    from qor_receipt.keys import SoftwareKey
    from qor_receipt.review.engine import Referee
    ref = Referee(SoftwareKey(), workdir=tmp_path, now=lambda: 1_000, core_factory=None)
    for phase in ("kickoff", "halftime", "end"):
        tok = ref.issue_challenge(phase)
        assert tok["pattern"] == pattern_for(tok["nonce"])
        assert all(b not in ("CROSS", "CIRCLE") for b in tok["pattern"][1:-1])
    pinned = ref.issue_challenge("kickoff", nonce=UNSAFE_NONCE)
    assert pinned["nonce"] == UNSAFE_NONCE and pinned["pattern"] == UNSAFE_PATTERN


def _v(events, seen=1_120, **kw):
    return verify_response(pattern_for(NONCE), events, issued_ms=0, deadline_ms=20_000, pause_seen_ms=seen, **kw)


def test_good_response():
    r = _v(perform(pattern_for(NONCE), 1_000))
    assert r.ok and r.latency_ms == 120


def test_noise_before_pause_ok():
    ev = [{"t_ms": 500, "button": "CROSS", "down": True}] + perform(pattern_for(NONCE), 1_000)
    assert _v(ev).ok


@pytest.mark.parametrize("case,reason", [
    ("wrong", "pattern_mismatch"), ("nopause", "no_pause_press"), ("late", "no_pause_press"),
    ("before_issue", "no_pause_press"), ("extra", "pattern_mismatch"),
])
def test_bad_hid(case, reason):
    p = pattern_for(NONCE)
    if case == "wrong":
        ev = perform([PAUSE] + list(reversed(p[1:])) if p[1:] != p[:0:-1] else [PAUSE] + ["CROSS"] * 4, 1_000)
    elif case == "nopause":
        ev = perform(p[1:], 1_000)
    elif case == "late":
        ev = perform(p, 25_000)
    elif case == "before_issue":
        ev = perform(p, -5_000)
    else:
        ev = perform(p[:2] + ["TRIANGLE" if p[2] != "TRIANGLE" else "CROSS"] + p[2:], 1_000)
    assert _v(ev).reason == reason


@pytest.mark.parametrize("seen,reason", [(None, "pause_not_seen_on_video"), (900, "video_before_input"),
                                         (2_000, "video_latency_too_high")])
def test_bad_video(seen, reason):
    r = _v(perform(pattern_for(NONCE), 1_000), seen=seen)
    assert not r.ok and r.reason == reason


def test_motion_correlation():
    x = [((i * 7) % 11) * 10 for i in range(40)]
    assert motion_correlation_permille(x, [0, 0] + x[:-2]) > 900
    assert motion_correlation_permille(x, [50] * 40) == -1000
    assert motion_correlation_permille(x, [((i * 5) % 3) * 9 for i in range(40)]) < 500


# ---------------------------------------------------------------- v0.3.11: pause-capable windows
def test_v0311_window_is_90s_and_capped():
    from qor_receipt import challenge as CH
    assert CH.DEFAULT_DEADLINE_MS == 90_000 and CH.LEGACY_DEADLINE_MS == 20_000 and CH.MAX_WINDOW_MS == 120_000


def test_dead_options_press_before_the_real_one_does_not_void_the_challenge():
    """Online Madden: OPTIONS during a live play / kickoff return does nothing. The answering press is the first
    OPTIONS followed by exactly the pattern, and the pause frame is judged against THAT press."""
    p = pattern_for(NONCE)
    ev = perform([PAUSE, "CROSS"], 2_000) + perform(p, 30_000)
    r = verify_response(p, ev, issued_ms=0, deadline_ms=90_000, pause_seen_ms=30_150)
    assert r.ok and r.pause_press_ms == 30_000 and r.latency_ms == 150
    # the pause seen after the dead press is not accepted for the real one
    r2 = verify_response(p, ev, issued_ms=0, deadline_ms=90_000, pause_seen_ms=2_150)
    assert not r2.ok and r2.reason == "video_before_input"


def test_response_press_ms_and_pattern_still_must_be_exact():
    from qor_receipt.challenge import response_press_ms
    p = pattern_for(NONCE)
    ev = perform([PAUSE], 1_000) + perform(p, 5_000)
    assert response_press_ms(p, ev, issued_ms=0, deadline_ms=90_000) == 5_000
    assert response_press_ms(p, perform([PAUSE, "CROSS"], 1_000), issued_ms=0, deadline_ms=90_000) == 1_000
    assert response_press_ms(p, perform(p, 95_000), issued_ms=0, deadline_ms=90_000) is None
    # an extra press inside the pattern is still a mismatch, presses before issue still do not count
    bad = perform(p[:2] + ["TRIANGLE" if p[2] != "TRIANGLE" else "CROSS"] + p[2:], 1_000)
    assert verify_response(p, bad, issued_ms=0, deadline_ms=90_000, pause_seen_ms=1_100).reason == "pattern_mismatch"
    assert verify_response(p, perform(p, -1_000), issued_ms=0, deadline_ms=90_000,
                           pause_seen_ms=-900).reason == "no_pause_press"


def test_recorder_rerun_fills_ok_result_only():
    """QorCap finalize (qor_receipt.review.local): ok -> result in the receipt; failure -> null + local reason."""
    from qor_receipt.review.local import rerun
    p = pattern_for(NONCE)
    c = {"id": NONCE[:8], "phase": "kickoff", "pattern": p, "issued_ms": 10_000, "deadline_ms": 100_000}
    presses = [dict(e, mono_ms=e["t_ms"]) for e in perform(p, 40_000)]
    out = rerun(c, presses, lambda lo, hi: [40_200])
    assert out["result"]["ok"] and out["result"]["latency_ms"] == 200 and out["result"]["reverified_by"] == "recorder"
    assert len(out["result"]["hid_sha256"]) == 64
    miss = rerun(c, presses, lambda lo, hi: [])
    assert miss["result"] is None and miss["local"]["reason"] == "pause_not_seen_on_video"

    def boom(lo, hi):
        raise RuntimeError("ffmpeg not available")
    err = rerun(c, presses, boom)
    assert err["result"] is None and "ffmpeg" in err["local"]["note"]
    none = rerun(c, [], lambda lo, hi: [])
    assert none["result"] is None and none["local"]["reason"] == "no_pause_press"


def test_recorder_challenge_results_on_bundle_dir(tmp_path):
    """Presses come from the committed inputs.jsonl (wall = header anchor + mono), like the referee."""
    import json as _j

    from qor_receipt.review.local import challenge_results
    p = pattern_for(NONCE)
    bits = {"CROSS": 0, "CIRCLE": 1, "SQUARE": 2, "TRIANGLE": 3, "OPTIONS": 9, "UP": 12, "DOWN": 13, "LEFT": 14, "RIGHT": 15}
    rows = [{"type": "header", "schema": "qorcap-inputs/v0", "wall_anchor_ms": 1_000_000}]
    t = 50_000
    for b in p:
        rows += [{"type": "in", "t_us": t * 1000, "b": 1 << bits[b]}, {"type": "in", "t_us": (t + 100) * 1000, "b": 0}]
        t += 600
    (tmp_path / "inputs.jsonl").write_text("\n".join(_j.dumps(r) for r in rows) + "\n")
    c = {"id": "x", "phase": "halftime", "pattern": p, "issued_ms": 1_040_000, "deadline_ms": 1_130_000}
    out = challenge_results(tmp_path, [c], decode=False)
    assert out[0]["result"] is None and out[0]["local"]["pause_press_ms"] == 1_050_000
    assert out[0]["local"]["presses_in_window"] == 5 and "ffmpeg" in out[0]["local"]["note"]
