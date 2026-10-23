"""Capture-path pause caps (challenge rule v2) and menu-safe challenge nonces.

Rule v1 keeps full <= 16 / lower <= 8. Rule v2 uses the measured USB/HDMI caps
full <= 26 / lower <= 16. The bright-text fraction, the 300 ms / 5 s onset window,
the 2500 ms ceiling, and the ±300 ms spread do not move. ``pattern_for`` does not move.
"""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from qor_receipt.agent.pause_detect import (  # noqa: E402
    ONSET_HORIZON_MS,
    ONSET_SLACK_MS,
    PauseDetector,
    detector_for_challenge_rule,
)
from qor_receipt.challenge import (  # noqa: E402
    MAX_LATENCY_MS,
    MENU_LEAVE,
    RULE2_CONSISTENCY_MS,
    RULE2_MAX_LATENCY_MS,
    menu_safe_nonce,
    pattern_for,
    pattern_from_digest,
    pattern_menu_safe,
)
from qor_receipt.keys import SoftwareKey  # noqa: E402
from qor_receipt.review.engine import Referee  # noqa: E402
from qor_receipt.review.media import PauseScan, install_rule_pause, pause_not_seen_note, scan_pause  # noqa: E402

NONCE = "00112233445566778899aabbccddeeff"
# Recorder measurements this calibration has to see, and the gameplay band it must reject.
MENU_ONSET = (16.6, 6.7)   # full, lower — kickoff menu on the USB path
MENU_END = (21.0, 13.0)    # full, lower — brighter end-phase menu
GAMEPLAY = (60, 85, 110)


def _menu(full_mean: float, lower_mean: float, bright_n: int) -> np.ndarray:
    """160x90 gray thumb whose means match, with ``bright_n`` white pixels in the upper half."""
    h, w = 90, 160
    lower_n = (h // 2) * w
    upper_n = h * w - lower_n
    lower_sum = int(round(lower_mean * lower_n))
    full_sum = int(round(full_mean * h * w))
    low_base, low_extra = divmod(lower_sum, lower_n)
    low = np.full(lower_n, low_base, np.uint8)
    low[:low_extra] += 1
    rest_n = upper_n - bright_n
    rest_sum = full_sum - int(low.sum()) - 255 * bright_n
    assert rest_n > 0 and rest_sum >= 0
    rest_base, rest_extra = divmod(rest_sum, rest_n)
    assert rest_base < 170
    up = np.empty(upper_n, np.uint8)
    up[:bright_n] = 255
    up[bright_n:] = rest_base
    up[bright_n:bright_n + rest_extra] += 1
    g = np.zeros((h, w), np.uint8)
    g[:h // 2] = up.reshape((h // 2, w))
    g[h // 2:] = low.reshape((h // 2, w))
    return cv2.resize(g, (160, 90), interpolation=cv2.INTER_AREA)


def _feats(rule: int, img: np.ndarray) -> dict:
    d = detector_for_challenge_rule(rule)
    return d.features(img)


def test_limits_that_must_not_move():
    assert MAX_LATENCY_MS == 600
    assert RULE2_MAX_LATENCY_MS == 2500 and RULE2_CONSISTENCY_MS == 300
    assert ONSET_SLACK_MS == 300 and ONSET_HORIZON_MS == 5_000
    old, new = detector_for_challenge_rule(1), detector_for_challenge_rule(2)
    assert old.th.dark_menu_max_luma == 16 and old.th.dark_menu_max_lower == 8
    assert new.th.dark_menu_max_luma == 26 and new.th.dark_menu_max_lower == 16
    assert old.th.dark_menu_bright_min == new.th.dark_menu_bright_min == 0.005
    assert old.th.dark_menu_bright_max == new.th.dark_menu_bright_max == 0.05
    assert old.th.confirm_frames == new.th.confirm_frames == 2
    # Anything that is not the integer 2 keeps the historical caps.
    assert detector_for_challenge_rule(None).th.dark_menu_max_luma == 16
    assert detector_for_challenge_rule(3).th.dark_menu_max_luma == 16


def test_measured_menus_fail_rule_v1_and_pass_rule_v2():
    """Before: 16/8 misses both measured menus. After: 26/16 sees them. Gameplay and blank dark do not."""
    onset = _menu(*MENU_ONSET, 144)
    end = _menu(*MENU_END, 144)
    f_on, f_end = _feats(2, onset), _feats(2, end)
    assert f_on["full_luma"] == pytest.approx(16.6, abs=1e-6)
    assert f_on["lower_luma"] == pytest.approx(6.7, abs=1e-6)
    assert f_end["full_luma"] == pytest.approx(21.0, abs=1e-6)
    assert f_end["lower_luma"] == pytest.approx(13.0, abs=1e-6)
    assert 0.005 <= f_on["bright_frac"] <= 0.05
    v1, v2 = detector_for_challenge_rule(1), detector_for_challenge_rule(2)
    assert v1.classify(v1.features(onset)) is False
    assert v1.classify(v1.features(end)) is False
    assert v2.classify(f_on) is True and v2.classify(f_end) is True
    # Confirmation is still two frames. The onset is the first of that run.
    assert v2.update(1_000, onset) is False and v2.onsets == []
    assert v2.update(1_033, onset) is True and v2.onsets == [1_000]
    # Historical dark menu (full 10 / lower 3, the 640x360 tune) still counts for both rules.
    classic = _menu(10, 3, 144)
    assert v1.classify(v1.features(classic)) is True
    assert detector_for_challenge_rule(2).classify(detector_for_challenge_rule(2).features(classic)) is True


def test_gameplay_and_dark_without_text_are_not_menus():
    v2 = detector_for_challenge_rule(2)
    for luma in GAMEPLAY:
        plain = np.full((90, 160), luma, np.uint8)
        assert v2.classify(v2.features(plain)) is False
        with_text = _menu(float(luma), float(luma), 144)
        got = v2.features(with_text)
        assert got["full_luma"] == pytest.approx(luma, abs=1e-6)
        assert v2.classify(got) is False
    blank = np.full((90, 160), 3, np.uint8)
    assert v2.features(blank)["bright_frac"] == 0
    assert v2.classify(v2.features(blank)) is False
    run = detector_for_challenge_rule(2)
    for i, luma in enumerate((60, 85, 110, 70, 100, 60, 90)):
        assert run.update(i * 33, np.full((90, 160), luma, np.uint8)) is False
    for i in range(8):
        assert run.update(1_000 + i * 33, np.full((90, 160), 2, np.uint8)) is False
    assert run.onsets == []


def test_text_fraction_and_luma_edges_stay_closed():
    v2 = detector_for_challenge_rule(2)
    assert v2.features(_menu(16.6, 6.7, 72))["bright_frac"] == pytest.approx(0.005, abs=1e-9)
    assert v2.classify(v2.features(_menu(16.6, 6.7, 72))) is True          # exactly 0.5%
    assert v2.classify(v2.features(_menu(16.6, 6.7, 57))) is False         # just under 0.5%
    assert v2.features(_menu(16.6, 6.7, 720))["bright_frac"] == pytest.approx(0.05, abs=1e-9)
    assert v2.classify(v2.features(_menu(16.6, 6.7, 720))) is True         # exactly 5%
    assert v2.classify(v2.features(_menu(16.6, 6.7, 735))) is False        # just over 5%
    assert v2.classify(v2.features(_menu(26, 16, 144))) is True
    assert v2.classify(v2.features(_menu(26.1, 16, 144))) is False
    assert v2.classify(v2.features(_menu(26, 16.1, 144))) is False
    v1 = detector_for_challenge_rule(1)
    assert v1.classify(v1.features(_menu(16, 8, 144))) is True
    assert v1.classify(v1.features(_menu(16.1, 8, 144))) is False


def test_pause_not_seen_notes_name_the_case():
    play = np.full((90, 160), 80, np.uint8)
    det = detector_for_challenge_rule(2)

    def frames():
        yield 1_600, play
        yield 1_633, play

    scan = scan_pause(frames(), det)
    note = pause_not_seen_note("kickoff", scan, 1_500, rule=2)
    assert note.startswith("kickoff: pause_not_seen_on_video: frames decoded but no menu found")
    assert "frames_decoded=2" in note and "frames_near_press=2" in note
    assert "full_luma=80.00" in note and "lower_luma=80.00" in note and "bright_frac=0.0000" in note
    assert "caps full<=26 lower<=16" in note and "rule=2" in note
    missing = pause_not_seen_note("end", None, 5_000, missing=[4, 5], thresholds=detector_for_challenge_rule(1).th, rule=1)
    assert "required segments missing seq=4,5" in missing
    assert "frames_decoded=0" in missing and "darkest_near_press=none" in missing and "caps full<=16" in missing
    ff = pause_not_seen_note("halftime", None, 5_000, prior="pause decode failed: RuntimeError: ffmpeg not available",
                             thresholds=det.th, rule=2)
    assert "ffmpeg not available" in ff and "frames_decoded=0" in ff
    empty = scan_pause(iter(()), det)
    assert "no frames decoded" in pause_not_seen_note("kickoff", empty, 1_000, rule=2)
    far = PauseScan(onsets=[], frames_decoded=1, samples=[{"t_ms": 0, "full_luma": 10.0, "lower_luma": 3.0, "bright_frac": 0.01}],
                    max_luma=26, max_lower=16, bright_min=0.005, bright_max=0.05)
    away = pause_not_seen_note("kickoff", far, 10_000, rule=2)
    assert "none in the onset window" in away and "frames_near_press=0" in away and "darkest_near_press=none" in away

    def boom():
        yield 1_000, play
        raise RuntimeError("ffmpeg decoded no frames from segments 3..3")

    bad = scan_pause(boom(), det)
    failed = pause_not_seen_note("kickoff", bad, 1_000, rule=2)
    assert "decode failed" in failed and "RuntimeError" in failed and "frames_decoded=1" in failed
    assert bad.onsets == []


def test_reader_core_pause_follows_the_rule():
    class Core:
        def __init__(self):
            self.pause = PauseDetector()

    v2 = install_rule_pause(Core(), 2)
    assert v2.pause.th.dark_menu_max_luma == 26 and v2.pause.th.dark_menu_max_lower == 16
    v1 = install_rule_pause(Core(), 1)
    assert v1.pause.th.dark_menu_max_luma == 16
    bare = object()
    assert install_rule_pause(bare, 2) is bare


def test_issued_patterns_are_menu_safe_and_derivation_is_unchanged():
    """CROSS/CIRCLE only in the last slot. Guessing the mode goes from 1/512 to 1/204."""
    unsafe = pattern_for(NONCE)
    assert unsafe == ["OPTIONS", "CROSS", "SQUARE", "DOWN", "SQUARE"]
    assert not pattern_menu_safe(unsafe)
    counts: dict[tuple[str, ...], int] = {}
    safe_counts: dict[tuple[str, ...], int] = {}
    for a in range(8):
        for b in range(8):
            for c in range(8):
                for d in range(8):
                    body = tuple(pattern_from_digest(bytes([a, b, c, d]))[1:])
                    counts[body] = counts.get(body, 0) + 1
                    if pattern_menu_safe(["OPTIONS", *body]):
                        safe_counts[body] = safe_counts.get(body, 0) + 1
    safe_draws = sum(safe_counts.values())
    assert sum(counts.values()) == 4096
    assert safe_draws == 1632 and safe_draws / 4096 == 0.3984375
    assert max(counts.values()) == 8 and max(safe_counts.values()) == 8
    assert max(counts.values()) / 4096 == 1 / 512
    assert max(safe_counts.values()) / safe_draws == 1 / 204
    for _ in range(30):
        nonce = menu_safe_nonce()
        pat = pattern_for(nonce)
        assert pattern_menu_safe(pat) and pat == pattern_for(nonce)
        assert all(b not in MENU_LEAVE for b in pat[1:-1])
    with pytest.raises(RuntimeError):
        menu_safe_nonce(draws=0)


def test_referee_keeps_a_supplied_nonce_and_draws_a_safe_one(tmp_path):
    ref = Referee(SoftwareKey(), workdir=tmp_path, core_factory=None)
    kept = ref.issue_challenge("kickoff", nonce=NONCE)
    assert kept["nonce"] == NONCE and kept["pattern"] == pattern_for(NONCE)
    assert not pattern_menu_safe(kept["pattern"])
    for phase in ("kickoff", "halftime", "end"):
        tok = ref.issue_challenge(phase)
        assert pattern_menu_safe(tok["pattern"])
        assert tok["pattern"] == pattern_for(tok["nonce"])


def test_dev_referee_issues_menu_safe(tmp_path):
    from fastapi.testclient import TestClient

    from qor_receipt.referee.app import create_app

    client = TestClient(create_app(SoftwareKey(), lambda: 0))
    mid = client.post("/matches", json={"players": [{"role": "home", "player_id": "p", "pubkey": "ab" * 32}]}).json()["match_id"]
    body = client.post(f"/matches/{mid}/challenges", json={"phase": "kickoff", "role": "home"}).json()
    assert pattern_menu_safe(body["pattern"]) and body["pattern"] == pattern_for(body["nonce"])
