"""QorProof's #68 attack tests (pr68-scratch/test_attack_pr68.py), ported.

Ported as written where possible. Changes:
- The gate moved to ``qor_receipt.vision.mismatch_gate`` and returns a
  ``MismatchGateResult``; ``allowed()`` returns ``result.allowed``.
- Cases that expected ``True`` (C1 outer gap edges, C2 ``log_ms=None``, C3
  unsorted/negative coverage) now expect ``False``: the gate has no allow path.
  Each also checks the reason code it now returns.
- B3-B5 and B7 use n_samples=200 instead of 5 (5 failed the 30-sample minimum
  before the trick ran) and assert ``encode_latency_calibration_invalid`` next to
  a control that passes calibration.
- A4: ``FrameBindStatus`` was removed (enums are no_claim), so A4 checks it is
  gone and that a module-level enum equal to "verified" is still no_claim.
- D1 (harness cache) and E1-E3 (estimator identity) run against this repo.
  The cache is per instance and keyed by ``estimate_cache_key``. An estimator
  sha256 is stored only when the class object is ``OpticalFlowPanEstimator`` and
  the digest is recomputed from source. An ``unhashed`` identity is not cached.
  D1 runs when that estimator is on the allowlist. Off the pin it skips.
  When ``CI=true``, an unknown hash fails the test instead of skipping.
  Their sys.path points at this repo's tests/ instead of QorProof's checkout.
"""
import enum
import os
import sys
from pathlib import Path

import pytest

from qor_receipt.vision import mismatch_gate as mg
from qor_receipt.vision import pan_estimator as pe

CAL = mg.EncodeLatencyCalibration(source="test-rig", n_samples=200, p50_ms=38.0, p99_ms=71.0)
COVER = [(0, 50_000), (52_000, 200_000)]
GAPS = [(50_000, 52_000)]


def result(status="verified", coverage=COVER, cal=CAL, video_ms=100_000, log_ms=None, align_gaps=GAPS):
    out = mg.mismatch_claim_allowed(
        status, coverage=coverage, align_gaps=align_gaps, encode_latency=cal, video_ms=video_ms, log_ms=log_ms,
    )
    assert out.ceiling == "PARTIAL"
    return out


def allowed(**kw):
    return result(**kw).allowed


# ---- rule 1: status must be exactly the str 'verified'
class EvilStr(str):
    def __ne__(self, o):
        return False

    def __eq__(self, o):
        return True

    __hash__ = str.__hash__


class PlainSub(str):
    pass


class OtherEnum(str, enum.Enum):
    V = "verified"


def test_A1_str_subclass_custom_ne_spoofs_verified():
    assert allowed(status=EvilStr("broken")) is False


def test_A2_plain_str_subclass_verified():
    assert allowed(status=PlainSub("verified")) is False


def test_A3_foreign_str_enum_equal_to_verified():
    assert allowed(status=OtherEnum.V) is False


def test_A4_own_enum_verified():
    assert not hasattr(pe, "FrameBindStatus") and not hasattr(mg, "FrameBindStatus")

    class FrameBindStatus(str, enum.Enum):
        VERIFIED = "verified"

    assert allowed(status=FrameBindStatus.VERIFIED) is False
    assert result(status=FrameBindStatus.VERIFIED, log_ms=100_001).reason == mg.R_STATUS_NOT_VERIFIED


@pytest.mark.parametrize("s", [b"verified", bytearray(b"verified"), "verified\x00", "verifıed", "ｖerified"])
def test_A5_bytes_and_lookalikes(s):
    assert allowed(status=s) is False


class EqObj:
    def __eq__(self, o):
        return True

    def __ne__(self, o):
        return False


def test_A6_obj_custom_eq():
    assert allowed(status=EqObj()) is False


# ---- rule 2: calibration
@pytest.mark.parametrize("kw", [
    dict(source="x", n_samples=1, p50_ms=0, p99_ms=0),
    dict(source="self-reported-by-recorder", n_samples=10**18, p50_ms=1e300, p99_ms=1e308),
    dict(source=" ", n_samples=1, p50_ms=0.0, p99_ms=0.0),
])
def test_B1_trivial_or_absurd_calibration_is_accepted(kw):
    assert allowed(cal=mg.EncodeLatencyCalibration(**kw)) is False
    assert result(cal=mg.EncodeLatencyCalibration(**kw), log_ms=100_001).reason == mg.R_CALIBRATION_INVALID


def test_B2_calibration_values_never_used_by_gate():
    c = mg.EncodeLatencyCalibration(source="r", n_samples=5, p50_ms=10.0, p99_ms=3_600_000.0)
    assert allowed(cal=c, video_ms=10, log_ms=199_999) is False


class EvilFloat(float):
    def __lt__(self, o):
        return False


# B3-B5, B7: n_samples was 5 in the original, which failed the 30-sample minimum
# before the trick ran. Now n_samples=200, a control calibration with the same
# fields gets past calibration (no_bundle_verifier), and the trick alone decides.
def _reason_with(cal):
    return result(cal=cal, log_ms=100_001).reason


def test_B3_float_subclass_negative_latency():
    assert _reason_with(mg.EncodeLatencyCalibration(source="r", n_samples=200, p50_ms=50.0, p99_ms=60.0)) == mg.R_NO_BUNDLE_VERIFIER
    c = mg.EncodeLatencyCalibration(source="r", n_samples=200, p50_ms=EvilFloat(-50.0), p99_ms=60.0)
    assert allowed(cal=c) is False
    assert _reason_with(c) == mg.R_CALIBRATION_INVALID
    positive = mg.EncodeLatencyCalibration(source="r", n_samples=200, p50_ms=EvilFloat(50.0), p99_ms=60.0)
    assert _reason_with(positive) == mg.R_CALIBRATION_INVALID


class EvilInt(int):
    def __le__(self, o):
        return False


def test_B4_int_subclass_negative_samples():
    assert _reason_with(mg.EncodeLatencyCalibration(source="r", n_samples=200, p50_ms=1.0, p99_ms=2.0)) == mg.R_NO_BUNDLE_VERIFIER
    c = mg.EncodeLatencyCalibration(source="r", n_samples=EvilInt(-5), p50_ms=1.0, p99_ms=2.0)
    assert allowed(cal=c) is False
    assert _reason_with(c) == mg.R_CALIBRATION_INVALID
    # The subclass itself is rejected, even with a value above the minimum.
    assert _reason_with(mg.EncodeLatencyCalibration(source="r", n_samples=EvilInt(200), p50_ms=1.0, p99_ms=2.0)) == mg.R_CALIBRATION_INVALID


def test_B5_post_hoc_mutation_of_frozen_cal():
    c = mg.EncodeLatencyCalibration(source="r", n_samples=200, p50_ms=1.0, p99_ms=2.0)
    assert _reason_with(c) == mg.R_NO_BUNDLE_VERIFIER
    object.__setattr__(c, "p50_ms", -1.0)
    assert allowed(cal=c) is False
    assert _reason_with(c) == mg.R_CALIBRATION_INVALID


def test_B6_duck_typed_and_dict_rejected():
    class Duck:
        source = "r"
        n_samples = 5
        p50_ms = 1.0
        p99_ms = 2.0

    assert allowed(cal=Duck()) is False
    assert allowed(cal={"source": "r", "n_samples": 5, "p50_ms": 1.0, "p99_ms": 2.0}) is False


@pytest.mark.parametrize("v", [float("inf"), float("-inf"), float("nan"), -0.0001, True, "1", None])
def test_B7_bad_values_rejected(v):
    assert _reason_with(mg.EncodeLatencyCalibration(source="r", n_samples=200, p50_ms=5.0, p99_ms=10.0)) == mg.R_NO_BUNDLE_VERIFIER
    c = mg.EncodeLatencyCalibration(source="r", n_samples=200, p50_ms=v, p99_ms=10.0)
    assert allowed(cal=c) is False
    assert _reason_with(c) == mg.R_CALIBRATION_INVALID


# ---- rule 5: coverage edges
def test_C1_gap_edges():
    assert allowed(video_ms=50_000) is False and allowed(video_ms=51_999) is False
    # Originally True: there is no allow path any more.
    assert allowed(video_ms=49_999) is False and allowed(video_ms=52_000) is False
    assert result(video_ms=50_000, log_ms=50_000).reason == mg.R_EVENT_IN_GAP
    assert result(video_ms=51_999, log_ms=51_999).reason == mg.R_EVENT_IN_GAP
    assert result(video_ms=49_999, log_ms=49_999).reason == mg.R_NO_BUNDLE_VERIFIER
    assert result(video_ms=52_000, log_ms=52_000).reason == mg.R_NO_BUNDLE_VERIFIER


@pytest.mark.parametrize("t", [float("nan"), float("inf"), -float("inf"), 100_000.0, True, None, "100000"])
def test_C2_nan_inf_times(t):
    assert allowed(video_ms=t) is False
    # Originally `is (t is None)`: log_ms=None is now no_claim too.
    assert allowed(log_ms=t) is False
    expected = mg.R_LOG_TIME_MISSING if t is None else mg.R_TIME_INVALID
    assert result(log_ms=t).reason == expected


def test_C3_overlap_unsorted_negative_cov():
    # Originally True for unsorted and negative coverage: both are now rejected as invalid.
    assert allowed(coverage=[(52_000, 200_000), (0, 50_000)], video_ms=100) is False
    assert result(coverage=[(52_000, 200_000), (0, 50_000)], video_ms=100, log_ms=100).reason == mg.R_COVERAGE_INVALID
    assert allowed(coverage=[(-10, 10)], video_ms=-5) is False
    assert result(coverage=[(-10, 10)], video_ms=5, log_ms=5).reason == mg.R_COVERAGE_INVALID
    assert allowed(coverage=[], video_ms=0) is False
    assert allowed(coverage=COVER + [(49_000, 53_000)], video_ms=51_000) is False
    assert result(coverage=COVER + [(49_000, 53_000)], video_ms=51_000, log_ms=51_000).reason == mg.R_COVERAGE_INVALID
    assert result(coverage=COVER + [(49_000, 53_000)], log_ms=100_001).reason == mg.R_COVERAGE_INVALID


def test_C4_generator_coverage_consumed():
    g = (s for s in COVER)
    assert mg.mismatch_claim_allowed("verified", coverage=g, encode_latency=CAL, video_ms=100_000, log_ms=100_001).allowed is False


class EvilInt2(int):
    def __lt__(self, o):
        return True

    def __le__(self, o):
        return True

    def __ge__(self, o):
        return False


def test_C5_int_subclass_time_outside_coverage():
    assert allowed(video_ms=EvilInt2(51_000)) is False
    assert result(video_ms=EvilInt2(51_000), log_ms=100_001).reason == mg.R_TIME_INVALID


def test_C6_no_log_ms_means_log_unchecked():
    assert allowed(log_ms=None) is False
    assert result(log_ms=None).reason == mg.R_LOG_TIME_MISSING


class EvilInt3(int):
    def __ge__(self, o):
        return True

    def __lt__(self, o):
        return True


def test_C7_int_subclass_time_in_gap_spoofs_coverage():
    assert allowed(video_ms=EvilInt3(51_000), log_ms=EvilInt3(-10**12)) is False
    assert result(video_ms=EvilInt3(51_000), log_ms=EvilInt3(-10**12)).reason == mg.R_TIME_INVALID


# ---- harness cache and estimator identity (tests/redteam/scorers.py and
# tests/redteam/optical_flow.py).


def _harness_path():
    import sys
    from pathlib import Path

    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)


def test_cache_includes_duration_when_frames_are_reused():
    """Same frames and the same short cache key must not reuse bins for a longer window."""
    _harness_path()
    from redteam.optical_flow import OpticalFlowHarnessEstimator
    from redteam.pan_frames import pan_frames

    frames = pan_frames(10, 9.6)

    class S:
        def __init__(self, duration_ms: int) -> None:
            self.duration_ms = duration_ms

    est = OpticalFlowHarnessEstimator(lambda s: frames, cache_key=lambda s: "k")
    short = est.estimate(S(1000))
    long = est.estimate(S(3000))
    assert len(short) == 10
    assert len(long) == 30


def test_mutated_core_attribute_is_not_served_from_the_cache():
    _harness_path()
    from redteam.optical_flow import OpticalFlowHarnessEstimator

    class YawCore(pe.OpticalFlowPanEstimator):
        def __init__(self) -> None:
            super().__init__()
            self.yaw = 1.0

        def estimate_frames(self, frames, start, end):
            return [pe.PanBin(t, self.yaw, 0.0, 1.0) for t in range(start, end, 100)]

    class S:
        duration_ms = 1000

    est = OpticalFlowHarnessEstimator(lambda s: [], cache_key=lambda s: "k", core=YawCore())
    assert est.estimate(S())[0].yaw_rate == 1.0
    est.core.yaw = 9.0
    assert est.estimate(S())[0].yaw_rate == 9.0


def test_unhashed_cores_do_not_share_cached_bins(tmp_path: Path):
    """Two cores whose source files are gone must not serve each other's bins."""
    import importlib.util

    _harness_path()
    from redteam.optical_flow import OpticalFlowHarnessEstimator

    def load(yaw: int):
        path = tmp_path / f"core_{yaw}.py"
        path.write_text(
            "class Core:\n"
            "    name = 'core'\n"
            "    version = 'v0'\n"
            f"    yaw = {yaw}\n"
            "    def estimate_frames(self, frames, start, end):\n"
            "        out = []\n"
            "        for t in range(start, end, 100):\n"
            "            row = type('B', (), {})()\n"
            "            row.mono_ms = t\n"
            "            row.yaw_rate = self.yaw\n"
            "            row.pitch_rate = 0.0\n"
            "            row.confidence = 1.0\n"
            "            row.no_claim = False\n"
            "            out.append(row)\n"
            "        return out\n",
            encoding="utf-8",
        )
        name = f"unhashed_core_{yaw}"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        path.unlink()
        module.__file__ = str(path)
        return module.Core()

    class S:
        duration_ms = 1000

    low = OpticalFlowHarnessEstimator(lambda s: [], cache_key=lambda s: "k", core=load(1))
    high = OpticalFlowHarnessEstimator(lambda s: [], cache_key=lambda s: "k", core=load(9))
    assert low.estimate(S())[0].yaw_rate == 1
    assert high.estimate(S())[0].yaw_rate == 9


def test_D1_harness_cache_shared_across_frame_sources():
    """Opposite pans must not share cached bins. Requires an allowlisted estimator."""
    if not pe.estimator_known():
        message = "estimator hash is not on the allowlist"
        if os.environ.get("CI") == "true":
            pytest.fail(message)
        pytest.skip(message)
    _harness_path()
    from redteam.optical_flow import OpticalFlowHarnessEstimator
    from redteam.pan_frames import pan_frames

    class S:
        duration_ms = 1000

    a = OpticalFlowHarnessEstimator(lambda s: pan_frames(10, 9.6), cache_key=lambda s: "k")
    b = OpticalFlowHarnessEstimator(lambda s: pan_frames(10, -9.6), cache_key=lambda s: "k")
    ra = a.estimate(S())
    rb = b.estimate(S())
    assert [x.yaw_rate for x in ra] != [x.yaw_rate for x in rb]


def test_E1_estimator_can_claim_any_sha():
    _harness_path()
    from redteam.scorers import NoClaimPanEstimator, PanAgreementScorer, source_identity

    class Spoof(NoClaimPanEstimator):
        name = pe.ESTIMATOR_NAME
        version = pe.ESTIMATOR_VERSION
        sha256 = pe.estimator_sha256()

    ident = source_identity(PanAgreementScorer(Spoof()), Spoof())
    assert ident.estimator_sha256 != pe.estimator_sha256()


def test_E2_sha_is_not_validated_as_hex():
    _harness_path()
    from redteam.scorers import NoClaimPanEstimator, PanAgreementScorer, source_identity

    class Weird(NoClaimPanEstimator):
        sha256 = "x\nestimator=forged"

    ident = source_identity(PanAgreementScorer(Weird()), Weird())
    assert ident.estimator_sha256 is None


def test_E3_core_subclass_outside_file_set_keeps_identical_identity():
    _harness_path()
    from redteam.optical_flow import OpticalFlowHarnessEstimator
    from redteam.scorers import PanAgreementScorer, source_identity

    class LyingCore(pe.OpticalFlowPanEstimator):
        @property
        def sha256(self):
            return pe.estimator_sha256()

        def estimate_frames(self, f, a, b):
            return [pe.PanBin(t, 99.0, 0.0, 1.0) for t in range(a, b, 100)]

    real = OpticalFlowHarnessEstimator(lambda s: [])
    fake = OpticalFlowHarnessEstimator(lambda s: [], core=LyingCore())
    r = source_identity(PanAgreementScorer(real), real)
    f = source_identity(PanAgreementScorer(fake), fake)
    assert (r.sha256, r.estimator_sha256, r.files) != (f.sha256, f.estimator_sha256, f.files)
