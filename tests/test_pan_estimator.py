"""CameraPanEstimator (optical flow): synthetic frames only. No recorded footage is used.

Sign convention: yaw_rate > 0 is the camera panning right (content moves left);
pitch_rate > 0 is the camera tilting up (content moves down). Units are percent
of frame width per second. The synthetic frames are 320 px wide and 100 ms apart,
so a camera move of d px per frame is d / 320 * 10 * 100 %W/s.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redteam.pan_frames import FRAME_W, crop, draw_scorebug, pan_frames, world_texture  # noqa: E402
from redteam.scorers import PanBin as ContractBin  # noqa: E402
from redteam.scorers import validate_pan_bin  # noqa: E402

from qor_receipt.vision import estimator_allowlist as al  # noqa: E402
from qor_receipt.vision import pan_estimator as pe  # noqa: E402

_PINNED_NUMPY = al.PINNED_NUMPY_REQUIREMENT.split("==", 1)[1]
if np.__version__ != _PINNED_NUMPY:
    _pin_message = (
        f"numpy {np.__version__} is not {al.PINNED_NUMPY_REQUIREMENT}. "
        "Off this pin the estimator hash is unknown and estimate_frames returns no_claim. "
        "Install the dev extra to run these tests."
    )
    # Dropping the pin must not keep CI green. Local runs may skip.
    if os.environ.get("CI") == "true":
        pytest.fail(_pin_message, pytrace=False)
    pytest.skip(_pin_message, allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
FRAME_MS = 100
DURATION = 2000


def rate(px_per_frame: float) -> float:
    return px_per_frame / FRAME_W * (1000 / FRAME_MS) * 100


def run(frames, duration=DURATION):
    return pe.OpticalFlowPanEstimator().estimate_frames(frames, 0, duration)


def claimed(bins):
    return [b for b in bins if not b.no_claim]


def check_contract(bins, duration=DURATION):
    assert [b.mono_ms for b in bins] == list(range(0, duration, 100))
    for b in bins:
        validate_pan_bin(ContractBin(b.mono_ms, b.yaw_rate, b.pitch_rate, b.confidence, b.no_claim))


@pytest.mark.parametrize("dx,dy", [(9.6, 0.0), (-9.6, 0.0), (0.0, 5.0), (0.0, -5.0), (3.0, -2.0), (16.0, 6.0)])
def test_known_pan_sign_and_magnitude(dx, dy):
    bins = run(pan_frames(DURATION // FRAME_MS, dx, dy))
    check_contract(bins)
    got = claimed(bins)
    assert len(got) >= 18  # bin 0 has no previous frame
    assert bins[0].no_claim and bins[0].reason == "no_frame_pair"
    for b in got:
        # camera +x (right) -> yaw +; camera +y (down in the world image) -> tilt down -> pitch -
        assert b.yaw_rate == pytest.approx(rate(dx), abs=max(0.5, 0.05 * abs(rate(dx))))
        assert b.pitch_rate == pytest.approx(-rate(dy), abs=max(0.5, 0.05 * abs(rate(dy))))
        assert b.confidence >= 0.5


def test_static_scene_is_zero():
    bins = run(pan_frames(DURATION // FRAME_MS, 0.0, 0.0))
    got = claimed(bins)
    assert len(got) >= 18
    assert all(abs(b.yaw_rate) < 0.2 and abs(b.pitch_rate) < 0.2 for b in got)


def test_overlay_only_motion_is_zero():
    """Static scene; only the scorebug strip and a top-left caption box change every frame."""
    world = world_texture()
    base = crop(world, 600.0, 300.0)
    frames = []
    for i in range(DURATION // FRAME_MS):
        frame = draw_scorebug(base.copy(), text=f"CLE {i} - TEN {i * 7 % 10}  Q4 0:{59 - i:02d}")
        h, w = frame.shape[:2]
        # A scrolling caption inside the masked highlight-caption box (top-left).
        cv2.rectangle(frame, (0, 0), (int(w * 0.5), int(h * 0.18)), (20, 20, 20), -1)
        cv2.putText(frame, "LOCK #%03d" % (i * 13), (4 + 3 * i, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        frames.append((i * FRAME_MS, frame))
    got = claimed(run(frames))
    assert len(got) >= 18
    assert all(abs(b.yaw_rate) < 0.2 and abs(b.pitch_rate) < 0.2 for b in got)


def test_unmasked_overlay_would_have_moved_the_estimate():
    """Control for the test above: the same moving bar outside every mask does register."""
    world = world_texture()
    base = crop(world, 600.0, 300.0)
    frames = []
    for i in range(DURATION // FRAME_MS):
        frame = base.copy()
        frame[60:140, :] = np.roll(base[60:140, :], -8 * i, axis=1)
        frames.append((i * FRAME_MS, frame))
    got = claimed(run(frames))
    assert len(got) >= 10
    assert all(b.yaw_rate > 5 for b in got)


def test_scene_cut_is_no_claim():
    a = pan_frames(10, 4.0)
    dark_world = (255 - world_texture()) // 3
    b = [(t + 1000, draw_scorebug(crop(dark_world, 100.0 + i * 4, 100.0))) for i, (t, _) in enumerate(pan_frames(10, 4.0))]
    bins = run(a + b)
    check_contract(bins)
    assert bins[10].no_claim and bins[10].reason == "scene_cut"
    assert len(claimed(bins[1:10])) == 9 and len(claimed(bins[11:])) == 9


def test_black_and_flat_frames_are_no_claim():
    frames = pan_frames(20, 4.0)
    frames[5] = (frames[5][0], np.zeros_like(frames[5][1]))
    frames[12] = (frames[12][0], np.full_like(frames[12][1], 128))
    bins = run(frames)
    check_contract(bins)
    assert bins[5].reason == "black_frame" and bins[6].no_claim  # pair into and out of the black frame
    assert bins[12].reason == "flat_frame" and bins[13].no_claim
    assert not bins[8].no_claim


def test_decode_failure_and_gaps_are_no_claim_never_filled():
    frames = pan_frames(20, 4.0)
    frames[7] = (frames[7][0], None)
    del frames[14:16]  # 300 ms gap
    bins = run(frames)
    check_contract(bins)
    assert bins[7].reason == "decode_failure" and bins[8].no_claim
    assert bins[14].no_claim and bins[15].no_claim and bins[16].reason == "timing_gap"
    for b in bins:
        if b.no_claim:
            assert b.yaw_rate is None and b.pitch_rate is None and b.confidence is None


def test_exception_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(pe.cv2, "calcOpticalFlowPyrLK", boom)
    bins = run(pan_frames(20, 4.0))
    check_contract(bins)
    assert all(b.no_claim for b in bins)
    assert {b.reason for b in bins[1:]} == {"exception"}

    def broken_frames():
        yield 0, pan_frames(1, 0.0)[0][1]
        raise OSError("decoder died")

    bins = pe.OpticalFlowPanEstimator().estimate_frames(broken_frames(), 0, 500)
    assert all(b.no_claim for b in bins)


def test_textureless_frames_are_no_claim():
    frames = []
    for i in range(10):
        img = np.full((180, 320, 3), 90, np.uint8)
        cv2.circle(img, (100 + 3 * i, 80), 30, (200, 200, 200), -1)
        frames.append((i * 100, img))
    bins = run(frames, 1000)
    assert all(b.no_claim for b in bins)


def test_two_runs_are_byte_identical_and_thread_count_restored():
    frames = pan_frames(30, 7.3, -2.1)
    before = cv2.getNumThreads()
    one = pe.bins_jsonl(run(frames, 3000))
    two = pe.bins_jsonl(pe.OpticalFlowPanEstimator().estimate_frames(frames, 0, 3000))
    assert one == two
    assert cv2.getNumThreads() == before
    assert b'"no_claim":false' in one


def test_version_and_hash_cover_source_and_params():
    est = pe.OpticalFlowPanEstimator()
    ident = est.identity()
    assert ident["version"] == pe.ESTIMATOR_VERSION == "qorai-optical-flow-pan/v1"
    assert ident["units"] == "percent_of_frame_width_per_second"
    assert len(ident["sha256"]) == 64 and ident["sha256"] == est.sha256 == pe.estimator_sha256()
    other = pe.estimator_sha256(pe.PanParams(min_features=31))
    assert other != est.sha256


def test_hash_covers_the_opencv_build_and_numpy(monkeypatch):
    base = pe.estimator_sha256()
    ident = pe.opencv_identity()
    assert ident["cv2_version"] == cv2.__version__
    assert ident["numpy_version"] == np.__version__
    assert ident["cv2_use_optimized"] in ("True", "False")
    assert len(ident["build_info_sha256"]) == 64
    assert pe.OpticalFlowPanEstimator().identity()["opencv"] == ident

    variants = []
    for target, attr, value in (
        (pe.cv2, "__version__", "0.0.0-other"),
        (pe.cv2, "getBuildInformation", lambda: "a different OpenCV build"),
        (pe.cv2, "useOptimized", lambda: not cv2.useOptimized()),
        (pe.np, "__version__", "0.0.0-other"),
    ):
        monkeypatch.setattr(target, attr, value)
        variants.append(pe.estimator_sha256())
        monkeypatch.undo()
    assert len({base, *variants}) == 5
    assert pe.estimator_sha256() == base


def test_estimator_known_rejects_a_to_json_lookalike():
    class Duck:
        def to_json(self):
            return pe.DEFAULT_PARAMS.to_json()

    class Sub(pe.PanParams):
        pass

    assert pe.estimator_known(Duck()) is False
    assert pe.estimator_known(Sub()) is False
    assert pe.estimator_known(pe.DEFAULT_PARAMS) is True


def test_estimator_known_rejects_int_and_float_subclasses():
    class LyingInt(int):
        pass

    class EvilFloat(float):
        pass

    assert pe.estimator_known(pe.PanParams(work_width=LyingInt(320))) is False
    assert pe.estimator_known(pe.PanParams(min_confidence=EvilFloat(0.35))) is False
    assert pe.estimator_known(pe.PanParams(work_width=True)) is False  # bool is not an exact int
    assert pe.estimator_known(pe.DEFAULT_PARAMS) is True


def test_bin_pairs_does_not_claim_forged_pair_results():
    class Forged(pe.OpticalFlowPanEstimator):
        def pairs(self, frames):
            return [
                pe.PairResult(t, True, "ok", yaw_rate=99.0, pitch_rate=0.0, confidence=1.0)
                for t in range(100, 1000, 100)
            ]

    frames = [(t, None) for t in range(0, 1000, 100)]
    bins = pe.bin_pairs(Forged().pairs(frames), 0, 1000)
    assert len(bins) == 10
    assert all(b.no_claim and b.yaw_rate is None for b in bins)


def test_replacing_mask_cache_is_not_known():
    est = pe.OpticalFlowPanEstimator()
    est._mask_cache = {}
    with pytest.warns(RuntimeWarning, match="_mask_cache"):
        assert est.identity()["estimator_known"] is False
    bins = est.estimate_frames([(0, None), (100, None)], 0, 200)
    assert bins and all(b.no_claim and b.reason == "unknown_estimator_hash" and b.yaw_rate is None for b in bins)
    assert pe.estimator_known(pe.DEFAULT_PARAMS) is True


def test_replacing_pair_on_the_instance_is_not_known():
    est = pe.OpticalFlowPanEstimator()
    est._pair = lambda *_args, **_kwargs: pe.PairResult(100, True, "ok", yaw_rate=99.0, pitch_rate=0.0, confidence=1.0)
    assert est.identity()["estimator_known"] is False
    bins = est.estimate_frames([(0, None), (100, None)], 0, 200)
    assert bins and all(b.no_claim and b.reason == "unknown_estimator_hash" and b.yaw_rate is None for b in bins)
    assert pe.estimator_known(pe.DEFAULT_PARAMS) is True


def test_subclass_that_overrides_pairs_is_not_known():
    class Evil(pe.OpticalFlowPanEstimator):
        def pairs(self, frames):
            return [pe.PairResult(100, True, "ok", yaw_rate=99.0, pitch_rate=0.0, confidence=1.0)]

    est = Evil()
    assert est.identity()["estimator_known"] is False
    bins = est.estimate_frames([(0, None), (100, None)], 0, 200)
    assert bins and all(b.no_claim and b.reason == "unknown_estimator_hash" and b.yaw_rate is None for b in bins)
    assert pe.estimator_known(pe.DEFAULT_PARAMS) is True


def test_pairs_and_bin_pairs_no_claim_when_the_hash_is_unknown(monkeypatch):
    monkeypatch.setattr(pe, "estimator_known", lambda params=pe.DEFAULT_PARAMS: False)
    frames = pan_frames(4, 1.0)
    pairs = pe.OpticalFlowPanEstimator().pairs(frames)
    assert pairs
    assert all(not pair.ok and pair.reason == "unknown_estimator_hash" for pair in pairs)
    planted = [pe.PairResult(100, True, "ok", yaw_rate=30.0, pitch_rate=0.0, confidence=1.0)]
    bins = pe.bin_pairs(planted, 0, 200)
    assert len(bins) == 2
    assert all(b.no_claim and b.reason == "unknown_estimator_hash" and b.yaw_rate is None for b in bins)


def test_cli_and_clip_script_exit_nonzero_on_an_unknown_hash(monkeypatch, capsys):
    import importlib.util

    monkeypatch.setattr(pe, "estimator_known", lambda params=pe.DEFAULT_PARAMS: False)
    assert pe.main(["/no/such.mp4"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == pe.UNKNOWN_ESTIMATOR_WARNING
    assert "WARNING" in err

    spec = importlib.util.spec_from_file_location("pan_estimate_clips", ROOT / "scripts" / "pan_estimate_clips.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OpticalFlowPanEstimator", pe.OpticalFlowPanEstimator)
    assert module.main() == 1
    assert capsys.readouterr().err.strip() == pe.UNKNOWN_ESTIMATOR_WARNING


@pytest.mark.parametrize("target,attr,value", [
    ("np", "__version__", "9.9.9"),
    ("cv2", "__version__", "9.9.9"),
    ("cv2", "getBuildInformation", lambda: "evil build"),
    ("cv2", "useOptimized", lambda: not cv2.useOptimized()),
], ids=["numpy_version", "opencv_version", "opencv_build", "use_optimized"])
def test_unknown_environment_fails_closed(monkeypatch, target, attr, value):
    frames = pan_frames(10, 9.6)
    assert claimed(run(frames, 1000))
    monkeypatch.setattr(getattr(pe, target), attr, value)
    assert pe.estimator_known() is False
    est = pe.OpticalFlowPanEstimator()
    ident = est.identity()
    assert ident["estimator_known"] is False
    bins = est.estimate_frames(frames, 0, 1000)
    assert len(bins) == 10
    assert all(b.no_claim and b.reason == "unknown_estimator_hash" and b.yaw_rate is None for b in bins)


def test_other_params_tampered_allowlist_and_exceptions_fail_closed(monkeypatch):
    from qor_receipt.vision import estimator_allowlist as al

    frames = pan_frames(10, 9.6)
    other = pe.OpticalFlowPanEstimator(pe.PanParams(min_features=31))
    assert other.identity()["estimator_known"] is False
    assert all(b.reason == "unknown_estimator_hash" for b in other.estimate_frames(frames, 0, 1000))

    sha = pe.estimator_sha256()
    for tampered in ({sha}, [sha], frozenset({sha.upper()}), frozenset({sha, "not-hex"}), frozenset()):
        monkeypatch.setattr(al, "KNOWN_ESTIMATOR_SHA256", tampered)
        assert pe.estimator_known() is False
    monkeypatch.undo()
    assert pe.estimator_known() is True

    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(pe, "estimator_sha256", boom)
    assert pe.estimator_known() is False
    monkeypatch.undo()
    monkeypatch.setattr(pe, "opencv_matches_pin", lambda: False)
    assert pe.estimator_known() is False
    assert pe.OpticalFlowPanEstimator().identity()["opencv_pinned"] is False


# sha256 of bins_jsonl on fixed synthetic inputs, produced on the pinned wheel.
# A change here means the estimator's output changed: bump the version or explain why.
GOLDEN_SYNTHETIC = {
    "pan_x9.6": "fa0296a060b775f6aeca09078d0c3875308dca0de20e6ec554b3db439ad70ccb",
    "pan_x3_y-2": "d379e97639479bc9616738c4f1acc2242a3963decfe6eb780e79c0eaff86e18f",
    "static": "cff5ed9fa5ba295125fea2b92d5fd185c0e5b24931af120b3a3dd4d7f5096ba7",
}


def _golden_inputs():
    return {
        "pan_x9.6": pan_frames(30, 9.6),
        "pan_x3_y-2": pan_frames(30, 3.0, -2.0),
        "static": pan_frames(30, 0.0),
    }


def test_golden_output_on_synthetic_frames():
    import hashlib

    for name, frames in _golden_inputs().items():
        digest = hashlib.sha256(pe.bins_jsonl(run(frames, 3000))).hexdigest()
        assert digest == GOLDEN_SYNTHETIC[name], name


def test_scorebug_mask_comes_from_the_local_scorebug_profile():
    from qor_receipt._vendor.qoresence.vision.local_scorebug.profile import MADDEN27_STANDARD

    assert pe.SCOREBUG_BAND == (0.0, 1.0, MADDEN27_STANDARD.band_y0, MADDEN27_STANDARD.band_y1)
    assert pe.SCOREBUG_BAND in pe.DEFAULT_PARAMS.masks


def test_plugs_into_the_redteam_harness_with_its_own_hash():
    """Plumbing only. The harness has no pixels, so a synthetic frame source stands in.

    This asserts the contract, the recorded identity, and determinism. It does not
    assert a catch rate: on Madden, camera pan versus right stick is not a valid
    aimbot signal (see docs/redteam/camera-pan-estimator.md).
    """
    from redteam.aimbot_harness import evaluate
    from redteam.optical_flow import OpticalFlowHarnessEstimator
    from redteam.scorers import PanAgreementScorer

    def frames(sample):
        return pan_frames(sample.duration_ms // FRAME_MS, 9.6)

    estimator = OpticalFlowHarnessEstimator(frames, cache_key=lambda sample: ("pan-9.6px", sample.duration_ms))
    report = evaluate(scorers=[PanAgreementScorer(estimator)])
    assert list(report.scores) == ["motion-rule-v3", "pan-agreement"]
    ident = report.identities["pan-agreement"]
    assert ident.estimator_version == pe.ESTIMATOR_VERSION
    assert ident.estimator_sha256 == pe.estimator_sha256()
    assert "qor_receipt/vision/pan_estimator.py" in ident.files
    assert "tests/redteam/optical_flow.py" in ident.files
    for name, score in report.scores["pan-agreement"].items():
        assert score.detail["estimator_version"] == pe.ESTIMATOR_VERSION
        assert score.detail["claimed"] >= score.detail["bins"] - 1
    sample = report.variants[0].sample
    one = [b.to_dict() for b in estimator._estimate(sample)]
    two = [b.to_dict() for b in estimator._estimate(sample)]
    assert one == two
    assert all(abs(b["yaw_rate"] - 30.0) < 1.5 for b in one if not b["no_claim"])

    from redteam.scorers import NoClaimPanEstimator, source_identity

    stub = source_identity(PanAgreementScorer(NoClaimPanEstimator()), NoClaimPanEstimator())
    assert stub.estimator_sha256 is None

