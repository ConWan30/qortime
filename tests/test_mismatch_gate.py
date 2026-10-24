"""The video-vs-log mismatch claim gate has no allow path.

Every input, including every trick from QorProof's #68 review, returns
``allowed=False`` with ceiling ``PARTIAL``. The reason code only says which
condition a future (separately reviewed) allow path would have failed first.
"""
from __future__ import annotations

import ast
import enum
import itertools
from pathlib import Path

import pytest

from qor_receipt.vision import mismatch_gate as mg

ROOT = Path(__file__).resolve().parents[1]
CAL = mg.EncodeLatencyCalibration(source="test-rig", n_samples=200, p50_ms=38.0, p99_ms=71.0)
COVER = [(0, 50_000), (52_000, 200_000)]
GAPS = [(50_000, 52_000)]
INSIDE = 100_000


def gate(status="verified", coverage=COVER, align_gaps=GAPS, cal=CAL, video_ms=INSIDE, log_ms=INSIDE + 40):
    return mg.mismatch_claim_allowed(
        status, coverage=coverage, align_gaps=align_gaps, encode_latency=cal, video_ms=video_ms, log_ms=log_ms,
    )


def assert_no_claim(result, reason=None):
    assert type(result) is mg.MismatchGateResult
    assert result.allowed is False
    assert result.no_claim is True
    assert result.ceiling == "PARTIAL"
    assert not result
    assert result.to_dict()["allowed"] is False
    if reason is not None:
        assert result.reason == reason, result


# --- the result object ------------------------------------------------------

def test_result_is_frozen_and_cannot_be_flipped():
    result = gate()
    for name, value in (("allowed", True), ("ceiling", "VERIFIED"), ("reason", "x"), ("_reason", "x")):
        with pytest.raises(AttributeError):
            setattr(result, name, value)
    with pytest.raises(ValueError):
        mg.MismatchGateResult("allowed")
    assert_no_claim(result)


def test_patched_ceiling_global_does_not_change_the_result(monkeypatch):
    monkeypatch.setattr(mg, "CEILING", "VERIFIED")
    result = gate()
    assert result.ceiling == "PARTIAL"
    assert result.to_dict()["ceiling"] == "PARTIAL"
    assert "ceiling='PARTIAL'" in repr(result)
    assert_no_claim(result, mg.R_NO_BUNDLE_VERIFIER)


def test_object_setattr_cannot_forge_the_reason():
    result = mg.mismatch_claim_allowed()
    assert result.reason == mg.R_STATUS_NOT_VERIFIED
    for name in ("_reason", "reason", "allowed", "ceiling", "__dict__", "x"):
        with pytest.raises((AttributeError, TypeError)):
            object.__setattr__(result, name, mg.R_NO_BUNDLE_VERIFIER)
    assert not hasattr(result, "__dict__") and type(result).__slots__ == ()
    assert result.reason == mg.R_STATUS_NOT_VERIFIED
    assert_no_claim(result, mg.R_STATUS_NOT_VERIFIED)


def test_direct_construction_bypassing_validation_fails():
    with pytest.raises(TypeError):
        mg.MismatchGateResult.__new__(tuple, mg.R_NO_BUNDLE_VERIFIER)
    forged = tuple.__new__(mg.MismatchGateResult, ("allowed",))
    with pytest.raises(ValueError):
        forged.reason  # noqa: B018 - re-validated on read
    assert forged.allowed is False and forged.ceiling == "PARTIAL" and not forged


def test_result_cannot_be_subclassed():
    with pytest.raises(TypeError):
        class Evil(mg.MismatchGateResult):
            __slots__ = ()
            allowed = property(lambda self: True)
    with pytest.raises(TypeError):
        type("Evil", (mg.MismatchGateResult,), {"__slots__": (), "allowed": property(lambda self: True)})


def test_mixin_cannot_bypass_the_final_class():
    class Mixin:
        def __init_subclass__(cls, **kwargs):
            return None

    with pytest.raises(TypeError):
        class Evil(Mixin, mg.MismatchGateResult):  # noqa: F841
            pass


def test_lookalike_result_never_compares_equal_to_a_real_one():
    class LookAlike:
        __slots__ = ("_reason",)
        allowed = True
        ceiling = "VERIFIED"

        def __init__(self, reason):
            self._reason = reason

    real = gate()
    fake = LookAlike(mg.R_NO_BUNDLE_VERIFIER)
    assert real != fake and fake != real
    assert real.__eq__(fake) is False and real.__ne__(fake) is True
    plain = (mg.R_NO_BUNDLE_VERIFIER,)
    assert real != plain and plain != real and not real.__eq__(plain)
    assert real == mg.MismatchGateResult(mg.R_NO_BUNDLE_VERIFIER)
    with pytest.raises(ValueError):
        mg.MismatchGateResult(VerifiedSub(mg.R_NO_BUNDLE_VERIFIER))


@pytest.mark.parametrize("how", ["copy", "deepcopy", "pickle"])
def test_copy_deepcopy_and_pickle_round_trip_stays_no_claim(how):
    import copy
    import pickle

    original = gate(video_ms=51_000)
    clone = {
        "copy": copy.copy,
        "deepcopy": copy.deepcopy,
        "pickle": lambda r: pickle.loads(pickle.dumps(r, protocol=pickle.HIGHEST_PROTOCOL)),
    }[how](original)
    assert clone == original and hash(clone) == hash(original)
    assert_no_claim(clone, mg.R_EVENT_IN_GAP)
    with pytest.raises(AttributeError):
        clone.allowed = True  # type: ignore[misc]


def test_forged_pickle_cannot_carry_an_unknown_reason():
    import pickle

    blob = pickle.dumps(mg.MismatchGateResult(mg.R_NO_BUNDLE_VERIFIER)).replace(b"no_bundle_verifier", b"verified_ok_allow!")
    with pytest.raises(ValueError):
        pickle.loads(blob)


def test_well_formed_inputs_still_no_claim_because_nothing_verifies_a_bundle():
    """Every caller-supplied input looks fine. There is still no allow path."""
    assert_no_claim(gate(), mg.R_NO_BUNDLE_VERIFIER)


def test_defaults_are_no_claim():
    assert_no_claim(mg.mismatch_claim_allowed(), mg.R_STATUS_NOT_VERIFIED)


# --- status tricks -----------------------------------------------------------

class NeEqStr(str):
    def __ne__(self, other):
        return False

    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class VerifiedSub(str):
    pass


class ForeignEnum(str, enum.Enum):
    V = "verified"


class LocalStatus(str, enum.Enum):
    VERIFIED = "verified"


class EqObject:
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False


@pytest.mark.parametrize("status", [
    NeEqStr("broken"),            # str subclass overriding __ne__/__eq__
    NeEqStr("verified"),
    VerifiedSub("verified"),      # plain subclass of 'verified'
    ForeignEnum.V,                # an enum equal to 'verified'
    LocalStatus.VERIFIED,
    EqObject(),
    True,                         # bool
    1,
    b"verified", bytearray(b"verified"), "verified\x00", "verifıed", "ｖerified", "VERIFIED", " verified",
    "missing", "stripped", "broken", "ok", "", None, ["verified"],
], ids=repr)
def test_status_tricks_are_no_claim(status):
    assert_no_claim(gate(status=status), mg.R_STATUS_NOT_VERIFIED)


class MadeUpFrameBind:
    """Hand-built stand-in for a frame_bind result, with a made-up digest."""

    status = "verified"
    digest = "ab" * 32
    chain_head = "cd" * 32
    coverage = ((0, 200_000),)


def test_hand_built_verified_object_with_made_up_digest_is_no_claim():
    fake = MadeUpFrameBind()
    assert_no_claim(gate(status=fake), mg.R_STATUS_NOT_VERIFIED)
    assert_no_claim(gate(status=fake.status, coverage=list(fake.coverage), align_gaps=None), mg.R_NO_BUNDLE_VERIFIER)


def test_real_frame_bind_result_waits_on_the_schemas_pr():
    """WAITS ON QorArch's schemas PR (verify_frame_bind(bundle), not on main yet).

    When it lands, this test must change to: build a bundle whose frame_bind chain
    verifies, pass the bundle bytes/path to the gate, and assert the gate itself
    called verify_frame_bind and recomputed the digest. A caller-supplied
    FrameBindResult must stay no_claim: it is only a return value, not proof. Until then the gate cannot see a bundle, so even a
    status taken from a real verifier is caller-supplied and is no_claim. This is
    deliberately an active assertion, not a skip.
    """
    assert not hasattr(mg, "FrameBindResult") and not hasattr(mg, "verify_frame_bind")
    assert "bundle" not in mg.mismatch_claim_allowed.__code__.co_varnames
    assert_no_claim(gate(), mg.R_NO_BUNDLE_VERIFIER)


# --- calibration tricks ------------------------------------------------------

class EvilFloat(float):
    def __lt__(self, other):
        return False

    def __le__(self, other):
        return True


class EvilInt(int):
    def __le__(self, other):
        return False

    def __lt__(self, other):
        return False


def C(**kw):
    base = dict(source="test-rig", n_samples=200, p50_ms=38.0, p99_ms=71.0)
    base.update(kw)
    return mg.EncodeLatencyCalibration(**base)


@pytest.mark.parametrize("cal", [
    C(n_samples=1, p50_ms=0, p99_ms=0),                       # p50 = p99 = 0, n = 1
    C(source="x", n_samples=1, p50_ms=0.0, p99_ms=0.0),
    C(p99_ms=1e308),                                          # absurd p99
    C(source="self-reported-by-recorder", n_samples=10**18, p50_ms=1e300, p99_ms=1e308),
    C(source=" "),
    C(source=""),
    C(source=VerifiedSub("rig")),
    C(p50_ms=EvilFloat(-50.0)),                               # float subclass, negative
    C(p50_ms=-0.0001),
    C(n_samples=EvilInt(-5)),                                 # int subclass, negative
    C(n_samples=-5),
    C(n_samples=True),                                        # bool
    C(p50_ms=True),
    C(p50_ms=float("nan")), C(p50_ms=float("inf")), C(p99_ms=float("-inf")),
    C(p50_ms="1"), C(p50_ms=None),
    C(p50_ms=90.0, p99_ms=60.0),
    C(p99_ms=3_600_000.0),
], ids=repr)
def test_calibration_tricks_are_no_claim(cal):
    assert_no_claim(gate(cal=cal), mg.R_CALIBRATION_INVALID)


def test_missing_duck_dict_and_mutated_calibrations_are_no_claim():
    assert_no_claim(gate(cal=None), mg.R_CALIBRATION_MISSING)

    class Duck:
        source = "r"
        n_samples = 200
        p50_ms = 1.0
        p99_ms = 2.0

    assert_no_claim(gate(cal=Duck()), mg.R_CALIBRATION_INVALID)
    assert_no_claim(gate(cal={"source": "r", "n_samples": 200, "p50_ms": 1.0, "p99_ms": 2.0}), mg.R_CALIBRATION_INVALID)

    class SubCal(mg.EncodeLatencyCalibration):
        pass

    assert_no_claim(gate(cal=SubCal("r", 200, 1.0, 2.0)), mg.R_CALIBRATION_INVALID)
    mutated = C()
    object.__setattr__(mutated, "p50_ms", -1.0)
    assert_no_claim(gate(cal=mutated), mg.R_CALIBRATION_INVALID)


# --- time and coverage tricks ------------------------------------------------

class AlwaysInside(int):
    def __ge__(self, other):
        return True

    def __lt__(self, other):
        return True

    def __le__(self, other):
        return True


def test_int_subclass_event_time_inside_a_gap_is_no_claim():
    assert_no_claim(gate(video_ms=AlwaysInside(51_000), log_ms=AlwaysInside(-10**12)), mg.R_TIME_INVALID)
    assert_no_claim(gate(video_ms=AlwaysInside(51_000)), mg.R_TIME_INVALID)


@pytest.mark.parametrize("t", [float("nan"), float("inf"), -float("inf"), 100_000.0, True, False, "100000", -5])
def test_bad_event_times_are_no_claim(t):
    assert_no_claim(gate(video_ms=t), mg.R_TIME_INVALID)
    assert_no_claim(gate(log_ms=t), mg.R_TIME_INVALID)


def test_missing_times_are_no_claim():
    assert_no_claim(gate(video_ms=None), mg.R_VIDEO_TIME_MISSING)
    assert_no_claim(gate(log_ms=None), mg.R_LOG_TIME_MISSING)


def test_align_gap_is_no_claim_even_when_coverage_overlaps_it():
    assert_no_claim(gate(video_ms=50_000), mg.R_EVENT_IN_GAP)
    assert_no_claim(gate(video_ms=51_999, log_ms=51_999), mg.R_EVENT_IN_GAP)
    assert_no_claim(gate(video_ms=INSIDE, log_ms=51_000), mg.R_EVENT_IN_GAP)
    papered = [(0, 49_000), (49_000, 53_000), (53_000, 200_000)]
    assert_no_claim(gate(coverage=papered, video_ms=51_000, log_ms=51_000), mg.R_EVENT_IN_GAP)
    assert_no_claim(gate(coverage=papered), mg.R_COVERAGE_OVERLAPS_GAP)
    assert_no_claim(gate(video_ms=49_999, log_ms=49_999), mg.R_NO_BUNDLE_VERIFIER)
    assert_no_claim(gate(video_ms=52_000, log_ms=52_000), mg.R_NO_BUNDLE_VERIFIER)
    assert_no_claim(gate(video_ms=200_000, log_ms=200_000), mg.R_EVENT_OUTSIDE_COVERAGE)


@pytest.mark.parametrize("coverage", [
    COVER + [(49_000, 53_000)],          # overlapping stretch
    [(52_000, 200_000), (0, 50_000)],    # unsorted
    [(-10, 10)],                          # negative
    [(10, 10)], [(20, 10)],
    [(0.0, 200_000.0)], [(True, 200_000)], [(AlwaysInside(0), 200_000)],
    [5], "0-200000", (s for s in COVER), {(0, 200_000)},
], ids=repr)
def test_bad_coverage_is_no_claim(coverage):
    assert_no_claim(gate(coverage=coverage), mg.R_COVERAGE_INVALID)


def test_empty_or_missing_coverage_and_bad_gaps_are_no_claim():
    assert_no_claim(gate(coverage=None), mg.R_COVERAGE_MISSING)
    assert_no_claim(gate(coverage=[]), mg.R_COVERAGE_MISSING)
    assert_no_claim(gate(align_gaps=[(52, 50)]), mg.R_ALIGN_GAPS_INVALID)
    assert_no_claim(gate(align_gaps="none"), mg.R_ALIGN_GAPS_INVALID)


# --- sweep: no combination ever allows ----------------------------------------

STATUSES = ["verified", "broken", NeEqStr("broken"), VerifiedSub("verified"), ForeignEnum.V, True, None, MadeUpFrameBind()]
CALS = [CAL, None, C(n_samples=1, p50_ms=0, p99_ms=0), C(p99_ms=1e308), C(p50_ms=EvilFloat(-50.0)), C(n_samples=EvilInt(-5))]
COVERAGES = [COVER, [(0, 10**12)], None, [], COVER + [(49_000, 53_000)]]
GAPSETS = [GAPS, None, [], [(0, 1)]]
TIMES = [0, 1, 49_999, 50_000, 51_000, 52_000, INSIDE, 199_999, 200_000, 10**11, -1, None, AlwaysInside(51_000), 100_000.0, True]


def test_no_input_combination_ever_allows():
    seen = set()
    count = 0
    for status, cal, cover, gaps, v, lg in itertools.product(STATUSES, CALS, COVERAGES, GAPSETS, TIMES, TIMES):
        result = mg.mismatch_claim_allowed(status, coverage=cover, align_gaps=gaps, encode_latency=cal, video_ms=v, log_ms=lg)
        assert result.allowed is False and result.ceiling == "PARTIAL" and not result
        seen.add(result.reason)
        count += 1
    assert count == len(STATUSES) * len(CALS) * len(COVERAGES) * len(GAPSETS) * len(TIMES) ** 2
    assert mg.R_NO_BUNDLE_VERIFIER in seen and mg.R_EVENT_IN_GAP in seen


def test_gate_source_has_no_true_return_and_reads_no_skew_or_env():
    tree = ast.parse(Path(mg.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is True:
            pytest.fail(f"return True in mismatch_gate.py line {node.lineno}")
        if isinstance(node, ast.keyword) and node.arg == "frozen":
            assert node.value.value is True
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "mismatch_claim_allowed")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    for forbidden in ("OBSERVED_MAX_CLIP_CLOCK_SKEW_MS", "OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS", "SKEW_MARGIN_FACTOR", "environ", "getenv"):
        assert forbidden not in names


def test_skew_constants_are_observations_not_limits():
    assert mg.OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS == (120, 719)
    assert mg.OBSERVED_MAX_CLIP_CLOCK_SKEW_MS == 719
    assert mg.SKEW_MARGIN_FACTOR == 1.5


# --- nobody else calls it -------------------------------------------------------

ALLOWED_USERS = {
    "qor_receipt/vision/mismatch_gate.py", "tests/test_mismatch_gate.py", "tests/test_mismatch_gate_qorproof.py",
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", "build", "dist"}


def _repo_python_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if SKIP_DIRS.intersection(rel.parts) or any(part.endswith(".egg-info") for part in rel.parts):
            continue
        yield rel.as_posix(), path


def test_no_other_python_in_the_repo_references_the_gate():
    """AST scan of every Python file except the gate and this test.

    Flags imports of ``qor_receipt.vision.mismatch_gate``, any name or attribute
    ``mismatch_claim_allowed`` / ``MismatchGateResult``, ``importlib`` or
    ``__import__`` with a ``qor_receipt.vision`` string, and string constants
    containing ``mismatch_claim`` or ``mismatch_gate`` (catches getattr by name).
    """
    hits = []
    for rel, path in _repo_python_files():
        docstrings: set[int] = set()
        if rel in ALLOWED_USERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if any("mismatch_gate" in m for m in mods):
                    hits.append(f"{rel}:{node.lineno} import")
            elif isinstance(node, ast.Import):
                if any("mismatch_gate" in a.name for a in node.names):
                    hits.append(f"{rel}:{node.lineno} import")
            elif isinstance(node, ast.Name) and node.id in {"mismatch_claim_allowed", "MismatchGateResult"}:
                hits.append(f"{rel}:{node.lineno} name")
            elif isinstance(node, ast.Attribute) and node.attr in {"mismatch_claim_allowed", "MismatchGateResult"}:
                hits.append(f"{rel}:{node.lineno} attribute")
            elif isinstance(node, ast.Constant) and id(node) not in docstrings and isinstance(node.value, str) and (
                "mismatch_claim" in node.value or "mismatch_gate" in node.value
            ):
                hits.append(f"{rel}:{node.lineno} string")
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                if name in {"import_module", "__import__"} and any(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("qor_receipt.vision")
                    for a in node.args
                ):
                    hits.append(f"{rel}:{node.lineno} dynamic import")
    assert hits == []
