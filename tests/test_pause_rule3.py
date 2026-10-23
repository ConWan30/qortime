"""Rule 3 dark-menu caps, and why a missed pause is written into the review note.

The caps are the cheap USB3.0 Video measurement (Madden 27, Oct 2026): pause menu about 16.6 full /
6.7 lower, end menu about 21 / 13, gameplay about 60-110. The 16/8 defaults false-negative that
menu. Rule 1 and rule 2 keep those defaults. The text-fraction window and the two-frame hold stay.
"""
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from qor_receipt.agent.pause_detect import (  # noqa: E402
    CALIBRATED_DARK_MENU_MAX_LOWER,
    CALIBRATED_DARK_MENU_MAX_LUMA,
    DARK_MENU_MAX_LOWER,
    DARK_MENU_MAX_LUMA,
    PauseDetector,
    PauseThresholds,
    calibrated_thresholds,
)
from qor_receipt.review.engine import pause_miss_note  # noqa: E402
from qor_receipt.review.media import scan_pause  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "madden27_live"


def _gray(upper: int, lower: int, bright: int = 0) -> np.ndarray:
    """160x90 frame. Upper and lower halves are flat; the first ``bright`` pixels are white text."""
    g = np.zeros((90, 160), np.uint8)
    g[:45] = upper
    g[45:] = lower
    if bright:
        g.flat[:bright] = 235
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def _flat(luma: int) -> np.ndarray:
    return cv2.cvtColor(np.full((90, 160), luma, np.uint8), cv2.COLOR_GRAY2BGR)


def _features(img):
    return PauseDetector().features(img)


def test_calibrated_thresholds_change_only_the_luma_caps():
    base, cal = PauseThresholds(), calibrated_thresholds()
    assert (base.dark_menu_max_luma, base.dark_menu_max_lower) == (DARK_MENU_MAX_LUMA, DARK_MENU_MAX_LOWER) == (16.0, 8.0)
    assert (cal.dark_menu_max_luma, cal.dark_menu_max_lower) == (CALIBRATED_DARK_MENU_MAX_LUMA, CALIBRATED_DARK_MENU_MAX_LOWER)
    assert (cal.dark_menu_max_luma, cal.dark_menu_max_lower) == (26.0, 16.0)
    assert cal.dark_menu_bright_min == base.dark_menu_bright_min == 0.005
    assert cal.dark_menu_bright_max == base.dark_menu_bright_max == 0.05
    assert cal.confirm_frames == base.confirm_frames == 2
    assert cal.panel_luma == base.panel_luma and cal.dim_ratio == base.dim_ratio


def test_measured_pause_and_end_menu_pass_rule3_and_fail_the_defaults():
    # upper 24 / lower 7 / 80 white pixels -> full ~16.7, lower 7. Models the measured 16.6 / 6.7 menu.
    pause = _gray(24, 7, 80)
    # upper 27 / lower 13 / 80 white pixels -> full ~21.2, lower 13. Models the measured 21 / 13 end menu.
    end = _gray(27, 13, 80)
    default, rule3 = PauseDetector(), PauseDetector(calibrated_thresholds())
    for img, full_band, lower_band in ((pause, (16.0, 18.0), (6.0, 8.0)), (end, (20.0, 23.0), (12.0, 14.0))):
        f = _features(img)
        assert full_band[0] < f["full_luma"] <= full_band[1], f
        assert lower_band[0] <= f["lower_luma"] <= lower_band[1], f
        assert 0.005 <= f["bright_frac"] <= 0.05, f
        assert default.classify(f) is False
        assert rule3.classify(f) is True


def test_rule3_still_needs_two_frames():
    img = _gray(24, 7, 80)
    d = PauseDetector(calibrated_thresholds())
    assert d.update(100, img) is False
    assert d.update(133, img) is True and d.onsets == [100]


@pytest.mark.parametrize("luma", [60, 85, 110])
def test_gameplay_luma_fails_both_detectors(luma):
    f = _features(_flat(luma))
    assert 60 <= f["full_luma"] <= 110
    assert PauseDetector().classify(f) is False
    assert PauseDetector(calibrated_thresholds()).classify(f) is False


def test_dim_scenes_fail_rule3():
    rule3 = PauseDetector(calibrated_thresholds())
    # Dim, but still brighter than the calibrated cap.
    dim = _features(_flat(40))
    assert 26 < dim["full_luma"] < 60
    assert rule3.classify(dim) is False
    # Muddy and under the cap, with no white text. The text-fraction floor rejects it.
    muddy = _features(_gray(20, 10, 0))
    assert muddy["full_luma"] <= 26 and muddy["lower_luma"] <= 16 and muddy["bright_frac"] < 0.005
    assert rule3.classify(muddy) is False
    # Under the cap, but the bright HUD is past the text-fraction ceiling.
    hud = _features(_gray(10, 8, 800))
    assert hud["full_luma"] <= 26 and hud["lower_luma"] <= 16 and hud["bright_frac"] > 0.05
    assert rule3.classify(hud) is False
    # A pure-black fade has no text either.
    assert rule3.classify(_features(_flat(0))) is False


def test_real_fixture_frames_keep_their_verdict_under_both_caps():
    """The 2026-10-13 thumbs are darker than 16/8. Rule 3 must still accept the menus and reject gameplay."""
    default, rule3 = PauseDetector(), PauseDetector(calibrated_thresholds())
    for name, want in (("pause_menu_3rd_thumb.png", True), ("pause_menu_1st_thumb.png", True), ("gameplay_thumb.png", False)):
        f = _features(cv2.imread(str(FX / name)))
        assert default.classify(f) is want and rule3.classify(f) is want, (name, f)


def test_scan_pause_reports_min_luma_near_the_press_and_a_decode_error():
    menu = _gray(24, 7, 80)
    play = _flat(80)
    frames = [(1_000, play), (1_600, menu), (1_633, menu), (4_000, play)]
    scan = scan_pause(frames, detector=PauseDetector(calibrated_thresholds()), press_ms=1_000)
    assert scan.error is None and scan.frames == 4 and scan.onsets == [1_600]
    assert scan.min_full_luma is not None and scan.min_full_luma < 20
    far = scan_pause([(10_000, menu), (10_033, menu)], detector=PauseDetector(calibrated_thresholds()), press_ms=0)
    assert far.onsets == [10_000] and far.near_frames == 0 and far.min_full_luma is None

    def boom():
        yield 0, play
        raise RuntimeError("broken pipe")

    err = scan_pause(boom())
    assert err.frames == 1 and err.error and "RuntimeError" in err.error and "broken pipe" in err.error


def test_pause_miss_note_names_the_three_causes_and_adds_no_result_field():
    missing = pause_miss_note("kickoff", segments_missing=True, missing=[4, 5])
    assert missing == "kickoff: pause_not_seen_on_video: required segments missing (4,5)"
    empty = pause_miss_note("halftime", segments_missing=True, missing=[])
    assert "required segments missing (none)" in empty
    decode = pause_miss_note("end", decode_error="RuntimeError: broken pipe", frames_decoded=3)
    assert decode == "end: pause_not_seen_on_video: decode failed (RuntimeError: broken pipe); frames_decoded=3"
    none = pause_miss_note("end", decode_error=None, frames_decoded=0)
    assert "decode failed (no frames); frames_decoded=0" in none
    quiet = pause_miss_note("kickoff", frames_decoded=48, min_full_luma=16.64, min_lower_luma=6.7,
                            near_frames=40, caps=(16.0, 8.0))
    assert quiet.startswith("kickoff: pause_not_seen_on_video: no menu found; frames_decoded=48;")
    assert "min_luma_near_press=16.6 (lower 6.7, near_frames=40); dark_menu_caps=16/8" in quiet
