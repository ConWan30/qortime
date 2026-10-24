"""CameraPanEstimator (optical flow): synthetic frames only. No recorded footage is used.

Sign convention: yaw_rate > 0 is the camera panning right (content moves left);
pitch_rate > 0 is the camera tilting up (content moves down). Units are percent
of frame width per second. The synthetic frames are 320 px wide and 100 ms apart,
so a camera move of d px per frame is d / 320 * 10 * 100 %W/s.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redteam.pan_frames import FRAME_W, crop, draw_scorebug, pan_frames, world_texture  # noqa: E402
from redteam.scorers import PanBin as ContractBin  # noqa: E402
from redteam.scorers import validate_pan_bin  # noqa: E402

from qor_receipt.vision import pan_estimator as pe  # noqa: E402

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


def test_hash_covers_the_opencv_build(monkeypatch):
    base = pe.estimator_sha256()
    ident = pe.opencv_identity()
    assert ident["cv2_version"] == cv2.__version__
    assert len(ident["build_info_sha256"]) == 64
    assert pe.OpticalFlowPanEstimator().identity()["opencv"] == ident

    monkeypatch.setattr(pe.cv2, "__version__", "0.0.0-other")
    other_version = pe.estimator_sha256()
    monkeypatch.undo()
    monkeypatch.setattr(pe.cv2, "getBuildInformation", lambda: "a different OpenCV build")
    other_build = pe.estimator_sha256()
    monkeypatch.undo()

    assert len({base, other_version, other_build}) == 3
    assert pe.estimator_sha256() == base


def test_pinned_wheel_matches_the_constraints_file():
    text = (ROOT / "tests" / "constraints-pan-estimator.txt").read_text(encoding="utf-8")
    pins = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert pe.PINNED_OPENCV_WHEEL in pins
    assert pe.PINNED_OPENCV_WHEEL.startswith("opencv-python-headless==" + pe.PINNED_OPENCV_VERSION + ".")
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert pe.PINNED_OPENCV_WHEEL in project["optional-dependencies"]["dev"]
    # The referee image installs .[referee,vision]; neither extra carries the exact pin.
    for extra in ("referee", "vision"):
        assert pe.PINNED_OPENCV_WHEEL not in project["optional-dependencies"][extra]


def test_skew_constants_are_observations_not_limits():
    assert pe.OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS == (120, 719)
    assert pe.OBSERVED_MAX_CLIP_CLOCK_SKEW_MS == 719
    assert pe.SKEW_MARGIN_FACTOR == 1.5
    # The old permissive spacing helper and "limit" names must not come back.
    for gone in ("within_clock_skew", "CLOCK_SKEW_NO_CLAIM_MS", "MEASURED_CLIP_CAPTURE_SKEW_MS"):
        assert not hasattr(pe, gone)


SPACINGS_MS = (0, 1, 119, 120, 500, 719, 720, 1078, 1079, 1500, 10_000, 3_600_000, -719, -10_000)


# Hand-built for the tests. qortime has no frame_bind check and no encode-latency
# calibration yet (qorcap PR 75 is unmerged), so no production code produces these.
CAL = pe.EncodeLatencyCalibration(source="test-rig", n_samples=200, p50_ms=38.0, p99_ms=71.0)
COVER = [(0, 50_000), (52_000, 200_000)]  # align_gap at [50_000, 52_000)
INSIDE = 100_000


def allowed(status=pe.FrameBindStatus.VERIFIED, coverage=COVER, cal=CAL, video_ms=INSIDE, log_ms=None):
    return pe.mismatch_claim_allowed(status, coverage=coverage, encode_latency=cal, video_ms=video_ms, log_ms=log_ms)


def test_all_three_conditions_allow_and_spacing_inside_coverage_does_not_matter():
    assert allowed() is True
    assert allowed(status="verified") is True
    for d in (0, 1, 150, 719, 1079, 5_000):
        assert allowed(video_ms=INSIDE + d, log_ms=INSIDE) is True


def test_defaults_are_no_claim():
    assert pe.mismatch_claim_allowed() is False
    assert pe.mismatch_claim_allowed(video_ms=INSIDE, log_ms=INSIDE) is False
    assert pe.mismatch_claim_allowed(pe.FrameBindStatus.VERIFIED, video_ms=INSIDE) is False  # no coverage, no calibration
    assert {s.value for s in pe.FrameBindStatus} == {"verified", "missing", "stripped", "broken"}


@pytest.mark.parametrize("status", ["missing", "stripped", "broken",
                                    pe.FrameBindStatus.MISSING, pe.FrameBindStatus.STRIPPED, pe.FrameBindStatus.BROKEN])
def test_each_non_verified_status_is_no_claim(status):
    assert allowed(status=status) is False


@pytest.mark.parametrize("status", ["VERIFIED", "Verified", " verified", "ok", "unknown", "", None, True, 1, ["verified"]])
def test_unknown_or_non_string_status_is_no_claim(status):
    assert allowed(status=status) is False


@pytest.mark.parametrize("cal", [
    None,
    {"source": "rig", "n_samples": 10, "p50_ms": 30.0, "p99_ms": 60.0},
    pe.EncodeLatencyCalibration(source="", n_samples=10, p50_ms=30.0, p99_ms=60.0),
    pe.EncodeLatencyCalibration(source="rig", n_samples=0, p50_ms=30.0, p99_ms=60.0),
    pe.EncodeLatencyCalibration(source="rig", n_samples=10, p50_ms=float("nan"), p99_ms=60.0),
    pe.EncodeLatencyCalibration(source="rig", n_samples=10, p50_ms=-1.0, p99_ms=60.0),
    pe.EncodeLatencyCalibration(source="rig", n_samples=10, p50_ms=90.0, p99_ms=60.0),
])
def test_verified_chain_without_measured_encode_latency_is_no_claim(cal):
    assert allowed(cal=cal) is False


def test_event_in_an_align_gap_or_outside_coverage_is_no_claim():
    assert allowed(video_ms=50_000) is False  # first ms of the gap
    assert allowed(video_ms=51_999) is False  # last ms of the gap
    assert allowed(video_ms=52_000) is True   # coverage resumes
    assert allowed(video_ms=49_999) is True
    assert allowed(video_ms=200_000) is False  # past the end (half-open)
    assert allowed(video_ms=INSIDE, log_ms=51_000) is False  # log event in the gap
    assert allowed(video_ms=None) is False
    for bad in (None, [], "0-200000", [(10, 10)], [(20, 10)], [(0.0, 200_000.0)], [5]):
        assert allowed(coverage=bad) is False


def test_without_every_condition_spacing_never_permits_a_claim():
    """Fails if a spacing-only allow path comes back."""
    partial = [
        dict(status="missing"), dict(status="broken"), dict(status="stripped"), dict(status="nope"),
        dict(cal=None), dict(coverage=None), dict(coverage=[(0, 1)]),
    ]
    for kwargs in partial:
        for spacing in SPACINGS_MS:
            assert allowed(video_ms=INSIDE + spacing, log_ms=INSIDE, **kwargs) is False
    gate_src = inspect.getsource(pe.mismatch_claim_allowed)
    body = gate_src.split('"""')[-1]
    for name in ("OBSERVED_MAX_CLIP_CLOCK_SKEW_MS", "OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS", "SKEW_MARGIN_FACTOR", "719",
                 "abs(", "os.environ", "getenv"):
        assert name not in body


def test_nothing_in_qortime_produces_a_verified_status_or_a_calibration_yet():
    """qorcap PR 75 is not merged; no non-test qortime code builds a calibration or a verified status."""
    users = []
    for path in sorted((ROOT / "qor_receipt").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == "pan_estimator.py":
            assert "EncodeLatencyCalibration(" not in text
            assert "FrameBindStatus.VERIFIED" in text  # only the gate's comparison
            assert text.count("FrameBindStatus.VERIFIED") == 1
            continue
        if "EncodeLatencyCalibration" in text or "FrameBindStatus" in text or "mismatch_claim_allowed" in text:
            users.append(str(path))
    assert users == []


def test_scorebug_mask_comes_from_the_local_scorebug_profile():
    from qor_receipt._vendor.qoresence.vision.local_scorebug.profile import MADDEN27_STANDARD

    assert pe.SCOREBUG_BAND == (0.0, 1.0, MADDEN27_STANDARD.band_y0, MADDEN27_STANDARD.band_y1)
    assert pe.SCOREBUG_BAND in pe.DEFAULT_PARAMS.masks


def test_estimator_is_not_imported_by_referee_or_verifier_code():
    """Advisory only: nothing on the verdict path may import the pan estimator."""
    offenders = []
    for path in sorted((ROOT / "qor_receipt").rglob("*.py")):
        if "vision" in path.relative_to(ROOT / "qor_receipt").parts[:1]:
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            if any(m.startswith("qor_receipt.vision") for m in mods):
                offenders.append(str(path))
    assert offenders == []


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

