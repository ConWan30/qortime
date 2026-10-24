"""CameraPanEstimator (optical flow): synthetic frames only. No recorded footage is used.

Sign convention: yaw_rate > 0 is the camera panning right (content moves left);
pitch_rate > 0 is the camera tilting up (content moves down). Units are percent
of frame width per second. The synthetic frames are 320 px wide and 100 ms apart,
so a camera move of d px per frame is d / 320 * 10 * 100 %W/s.
"""
from __future__ import annotations

import ast
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


def test_clock_skew_window_is_no_claim():
    assert pe.MEASURED_CLIP_CAPTURE_SKEW_MS == (120, 719)
    assert pe.CLOCK_SKEW_NO_CLAIM_MS == 719
    assert pe.within_clock_skew(10_000, 10_719)
    assert pe.within_clock_skew(10_500, 10_000)
    assert not pe.within_clock_skew(10_000, 10_720)


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
