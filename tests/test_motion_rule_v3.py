"""Motion rule v3: live-play seconds, referee-measured exclusions, same floor.

Adversarial series are scored with the function the referee calls. The ffmpeg case checks that a
real review uses that rule and that a client label cannot change the exclusions.
"""
from __future__ import annotations

import inspect
import json
import random
import sys
from pathlib import Path

import pytest

from qor_receipt.challenge import challenge_rule_of, motion_correlation_permille
from qor_receipt.review.clock import frame_mono_ms, motion_mono_ms
from qor_receipt.review.motion import (
    MOTION_BASIS_V3,
    MOTION_MIN_BINS,
    MOTION_RULE_V3,
    MOTION_SAMPLE_MAX_SEGMENTS_V3,
    correlate_keyed,
    exclusion_bins,
    motion_sample_seqs,
    pause_span_bins,
    score_motion,
    scorebug_absent_bins,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import review_fixture as F  # noqa: E402


def _series(seed: int, n: int = 200) -> list[int]:
    rng = random.Random(seed)
    return [rng.randrange(0, 100) for _ in range(n)]


def _dict(values: list[int], shift: int = 0) -> dict[int, int]:
    return {i + shift: v for i, v in enumerate(values)}


def test_motion_rule_v3_constants_remain_for_old_receipts():
    assert MOTION_RULE_V3 == 3
    assert challenge_rule_of({"challenge_rule": 3}) == 1
    assert challenge_rule_of({"challenge_rule": 2}) == 2
    receipt = json.loads((ROOT / "schema" / "receipt.v0.json").read_text(encoding="utf-8"))
    rule_enum = receipt["properties"]["challenges"]["items"]["properties"]["result"]["oneOf"][1]["properties"]["rule"]["enum"]
    assert rule_enum == [1, 2]
    assert "motion-rule/v3" in MOTION_BASIS_V3
    assert MOTION_SAMPLE_MAX_SEGMENTS_V3 == 24


def test_historical_list_correlation_is_unchanged():
    """The list helper is what older code paths and the public referee API still call."""
    x = [((i * 7) % 11) * 10 for i in range(40)]
    assert motion_correlation_permille(x, [0, 0] + x[:-2]) > 900
    assert motion_correlation_permille(x, [50] * 40) == -1000


def test_historical_receipt_keeps_the_pre_v3_basis():
    rec = json.loads((ROOT / "tests" / "vectors" / "review_receipt.json").read_text(encoding="utf-8"))["receipt"]
    basis = rec["signals"]["motion_basis"]
    assert basis.startswith("referee-computed: controller report rate")
    assert "motion-rule/v3" not in basis


def test_correlated_sample_passes_and_adversarial_samples_fail():
    base = _series(1)
    motion = _dict(base)
    assert correlate_keyed(_dict(base), motion) == 1000

    assert correlate_keyed(_dict(_series(2)), motion) < 300
    assert correlate_keyed(_dict(_series(99)), motion) < 300
    # Inputs moved by 5 s. The lag search only looks 0..3 s, and this series is not periodic.
    shifted = correlate_keyed(_dict(base, shift=5), motion)
    assert shifted is not None and shifted < 300
    constant = correlate_keyed({i: 40 for i in range(len(base))}, motion)
    assert constant == -1000


def test_idle_seconds_stay_in_the_series():
    base = _series(1)
    energy = _dict(base)
    energy[10] = 0
    energy[11] = 0
    motion = _dict(base)
    mp, detail = score_motion(energy, motion, sample_segments=[1])
    assert mp is not None and mp >= 300
    assert detail["bins"] == len(base)
    assert detail["excluded"]["total"] == 0


def test_fewer_than_20_bins_is_not_a_passing_score():
    motion = {i: i for i in range(MOTION_MIN_BINS - 1)}
    energy = {i: i for i in motion}
    assert correlate_keyed(energy, motion) is None
    mp, detail = score_motion(energy, motion, sample_segments=[1])
    assert mp is None and "motion_permille" not in detail
    assert detail["note"] == "too few decoded seconds for a motion correlation"


def test_scorebug_absence_needs_an_ok_read_and_ignores_geometry():
    unknown = [(i * 1000, "layout_unknown") for i in range(30)]
    assert scorebug_absent_bins(unknown) == set()
    mixed = unknown + [(0, "ok"), (0, "ok"), (1000, "layout_unknown")]
    # second 0 is ok,ok,layout_unknown -> kept. second 1 is two layout_unknown -> dropped.
    # seconds 2..29 are one layout_unknown each -> dropped.
    dropped = scorebug_absent_bins(mixed)
    assert 0 not in dropped and 1 in dropped and 29 in dropped
    tiny = [(i * 1000, "frame_too_small") for i in range(10)] + [(0, "ok")]
    assert scorebug_absent_bins(tiny) == set()


def test_pause_span_is_the_menu_not_the_issue_window():
    bins = pause_span_bins([(5_000, 6_800)])
    assert bins == {5, 6}
    assert not set(range(0, 90)) <= bins


def test_client_labels_cannot_widen_exclusions():
    """A failing match stays failing when the only extra signal is a client-shaped label."""
    base = _series(1)
    motion = _dict(base)
    other = _dict(_series(4))
    mp, detail = score_motion(other, motion, sample_segments=list(range(1, 25)))
    assert mp is not None and mp < 300
    client_reasons = [(i * 1000, "scorebug_absent") for i in range(len(base))]
    mp2, detail2 = score_motion(other, motion, scorebug_reasons=client_reasons, sample_segments=list(range(1, 25)))
    assert detail2["excluded"] == detail["excluded"]
    assert detail2["excluded"]["total"] == 0
    assert mp2 == mp and mp2 < 300
    for fn in (score_motion, exclusion_bins, motion_sample_seqs):
        names = set(inspect.signature(fn).parameters)
        assert not names & {"timeline", "client", "labels", "motion_exclude_ms"}
    parts = exclusion_bins(pause_spans_mono=[(5_000, 6_800)])
    assert parts["all"] == {5, 6}


def _long_manifest():
    segs = [{"seq": 0, "offset": 0, "mono_ms": 0, "motion_exclude": True}]
    segs += [{"seq": i, "offset": 100, "mono_ms": i * 2000, "motion_exclude": True, "skip_motion": True}
             for i in range(1, 61)]
    return segs


def test_sample_is_one_per_stratum_and_ignores_client_flags():
    from qor_receipt.review.motion import _media_segments, _time_strata

    segs = _long_manifest()
    chosen = motion_sample_seqs(segs, random.Random(0), max_segments=MOTION_SAMPLE_MAX_SEGMENTS_V3)
    assert len(chosen) == 24
    assert 0 not in chosen
    assert chosen == sorted(set(chosen))
    strata = _time_strata(_media_segments(segs), 24)
    for bucket in strata:
        hit = [int(m["seq"]) for m in bucket if int(m["seq"]) in set(chosen)]
        assert len(hit) == (1 if bucket else 0)
    short = motion_sample_seqs(segs[:10], random.Random(0), max_segments=MOTION_SAMPLE_MAX_SEGMENTS_V3)
    assert short == list(range(1, 10))


def test_v3_helper_sample_still_depends_on_explicit_rng():
    """The v3 stratified helper remains available for historical analysis; seed controls the draw."""
    segs = _long_manifest()
    a = motion_sample_seqs(segs, random.Random(1), max_segments=MOTION_SAMPLE_MAX_SEGMENTS_V3)
    b = motion_sample_seqs(segs, random.Random(2), max_segments=MOTION_SAMPLE_MAX_SEGMENTS_V3)
    again = motion_sample_seqs(segs, random.Random(1), max_segments=MOTION_SAMPLE_MAX_SEGMENTS_V3)
    assert a == again and a != b
    assert len(a) == 24 and len(b) == 24


def test_motion_clock_matches_frame_clock_at_30_fps_only():
    clock = (1_000.0, 1000.0 / 30.0)
    assert motion_mono_ms(clock, 5_000.0, 0, 10) == frame_mono_ms(clock, 5_000.0, 0, 30)
    assert motion_mono_ms(clock, 5_000.0, 3, 30) == frame_mono_ms(clock, 5_000.0, 3, 30) == 6_100
    # 3 frames at 10 Hz is 300 ms, which is 9 frames at 30 Hz.
    assert motion_mono_ms(clock, 5_000.0, 3, 10) == frame_mono_ms(clock, 5_000.0, 9, 30) == 6_300
    assert frame_mono_ms(clock, 5_000.0, 3, 10) != motion_mono_ms(clock, 5_000.0, 3, 10)


def test_engine_motion_source_does_not_read_client_labels():
    eng = (ROOT / "qor_receipt" / "review" / "engine.py").read_text(encoding="utf-8")
    body = eng.split("def _motion", 1)[1].split("\n    def ", 1)[0]
    assert "timeline" not in body
    assert "motion_exclude" not in body
    src = (ROOT / "qor_receipt" / "review" / "motion.py").read_text(encoding="utf-8")
    assert "timeline.jsonl" not in src
    for banned in ("attested", "attestation", "hardware-verified"):
        assert banned not in src
        assert banned not in MOTION_BASIS_V3


def test_pause_scan_records_the_held_span_only():
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from qor_receipt.review.media import scan_pause

    def frame(paused: bool):
        img = np.zeros((360, 640), np.uint8)
        if paused:
            img[40:58, 280:420] = 235
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    frames = [(i * 33, frame(False)) for i in range(8)]
    frames += [(300 + i * 33, frame(True)) for i in range(15)]
    frames += [(900 + i * 33, frame(False)) for i in range(6)]
    scan = scan_pause(frames)
    assert scan.error is None and len(scan.spans) == 1
    start, end = scan.spans[0]
    assert 300 <= start <= 400
    assert 300 < end < 900
    assert pause_span_bins([(start, end)]) <= {0, 1}
    assert 5 not in pause_span_bins([(start, end)])


def test_score_motion_helper_still_accepts_explicit_v3_rule():
    """Historical detail shape: callers can still ask score_motion for rule 3."""
    base = _series(1)
    motion = _dict(base)
    energy = _dict(base)
    mp, detail = score_motion(
        energy, motion, sample_segments=list(range(1, 25)),
        rule=MOTION_RULE_V3, basis=MOTION_BASIS_V3,
        sample_max_segments=MOTION_SAMPLE_MAX_SEGMENTS_V3,
    )
    assert mp is not None and mp >= 300
    assert detail["rule"] == 3
    assert detail["basis"] == MOTION_BASIS_V3
    assert detail["sample_max_segments"] == 24
