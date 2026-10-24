"""Red team for motion rule v3: row-count correlation misses stick-channel aimbots.

The assertions match today's rule. ``full-idle`` is partial. ``idle-right-stick``,
``blended-assist``, and ``snap-flicks`` stay pass. That miss is the known gap.
The marker keeps the gap visible in ``pytest -m known_gap`` without failing CI.

The harness calls ``score_motion`` and the input-proof checks directly. It does
not submit a bundle.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redteam.aimbot_harness import (  # noqa: E402
    FLICK_MS,
    SECONDS,
    VARIANTS,
    control_rows,
    evaluate,
    input_proof_errors,
    render_report,
    rewrite_rows,
    seal_input_log,
)
from redteam.scorers import (  # noqa: E402
    BIN_MS,
    MotionRuleV3Scorer,
    NoClaimPanEstimator,
    PanAgreementScorer,
    PanBin,
    Score,
    VariantSample,
    rates_for,
    validate_pan_bin,
)

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "redteam" / "motion-rule-v3-aimbot.md"
REDTEAM = Path(__file__).resolve().parent / "redteam"
MISSED = ("idle-right-stick", "blended-assist", "snap-flicks")

pytestmark = pytest.mark.known_gap(
    reason="motion rule v3 counts controller rows, so stick-channel aimbots stay pass; full-idle is partial",
)


@pytest.fixture(scope="module")
def report():
    return evaluate()


def test_fixture_is_synthetic_and_long_enough_to_score():
    rows = control_rows()
    ins = [row for row in rows if row.get("type") == "in"]
    assert len(ins) > 100
    assert {row["rx"] for row in ins} != {128}
    assert max(int(row["t_us"]) // 1000 for row in ins) // 1000 == SECONDS - 1


def test_variants_keep_left_stick_buttons_and_row_times():
    base = [row for row in control_rows() if row.get("type") == "in"]
    for name in ("idle-right-stick", "blended-assist", "snap-flicks"):
        rewritten = [row for row in rewrite_rows(control_rows(), name) if row.get("type") == "in"]
        assert len(rewritten) == len(base)
        for original, row in zip(base, rewritten):
            assert row["t_us"] == original["t_us"]
            assert row["b"] == original["b"]
            assert row["lx"] == original["lx"] and row["ly"] == original["ly"]
            assert (row["l2"], row["r2"]) == (original["l2"], original["r2"])
    idle = [row for row in rewrite_rows(control_rows(), "idle-right-stick") if row.get("type") == "in"]
    assert {(row["rx"], row["ry"]) for row in idle} == {(128, 128)}
    blend = [row for row in rewrite_rows(control_rows(), "blended-assist") if row.get("type") == "in"]
    assert [row["rx"] for row in blend] != [row["rx"] for row in base]
    snaps = [row for row in rewrite_rows(control_rows(), "snap-flicks") if row.get("type") == "in"]
    assert any((row["rx"], row["ry"]) == (240, 40) for row in snaps)
    assert FLICK_MS < 300


def test_full_idle_drops_controller_rows():
    rewritten = rewrite_rows(control_rows(), "full-idle")
    assert not any(row.get("type") == "in" for row in rewritten)
    assert any(row.get("type") == "header" for row in rewritten)


def test_motion_rule_v3_misses_stick_channel_aimbots(report):
    """Honest current result: only a fully idle log is partial."""
    motion = report.scores["motion-rule-v3"]
    control = motion["control"]
    assert control.detail["motion_check"] == "pass"
    assert control.detail["motion_permille"] >= 300
    assert control.flagged is False
    assert control.detail["rule"] == 3
    assert "motion-rule/v3" in control.detail["basis"]
    assert control.detail["bins"] >= 20
    assert control.detail["excluded_total"] == 0

    idle_log = motion["full-idle"]
    assert idle_log.detail["motion_check"] == "partial"
    assert idle_log.detail["motion_permille"] == -1000
    assert idle_log.flagged is True

    for name in MISSED:
        score = motion[name]
        assert score.detail["motion_check"] == "pass"
        assert score.flagged is False
        assert score.detail["motion_permille"] == control.detail["motion_permille"]

    caught = [name for name in VARIANTS if motion[name].flagged]
    assert caught == ["full-idle"]


def test_right_stick_runs_show_the_rewrite_was_applied(report):
    runs = {variant.sample.name: variant.right_stick_run_ms for variant in report.variants}
    assert runs["control"] >= 300
    assert runs["idle-right-stick"] == 0
    assert runs["full-idle"] == 0
    assert runs["snap-flicks"] < 300
    assert runs["snap-flicks"] > 0


def test_rewritten_log_fails_input_proof_before_a_live_precheck_would_score_motion(report):
    by_name = {variant.sample.name: variant for variant in report.variants}
    assert by_name["control"].input_proof == "pass"
    assert by_name["control"].input_proof_errors == ()
    sealed = seal_input_log(control_rows())
    assert input_proof_errors(sealed) == ()
    for name in VARIANTS:
        if name == "control":
            continue
        errors = by_name[name].input_proof_errors
        assert by_name[name].input_proof == "fail"
        text = "\n".join(errors)
        assert "SHA-256 mismatch" in text
        assert "input_hash does not match" in text
        assert "inputs_sha256 does not match" in text
        assert "inputs_chain_head does not match" in text


def test_catch_and_false_alarm_rates_for_each_scorer(report):
    motion = report.rates["motion-rule-v3"]
    assert motion.catch_rate == 0.25
    assert motion.false_alarm_rate == 0.0
    assert motion.true_positives == 1
    assert motion.false_negatives == 3
    assert motion.false_positives == 0
    assert motion.true_negatives == 1
    assert motion.decided_attacks == 4
    assert motion.abstained == 0
    assert motion.version == "motion-rule/v3"
    assert len(motion.sha256) == 64

    pan = report.rates["pan-agreement"]
    assert pan.catch_rate is None
    assert pan.false_alarm_rate is None
    assert pan.true_positives == 0
    assert pan.false_positives == 0
    assert pan.decided_attacks == 0
    assert pan.decided_benign == 0
    assert pan.abstained == 5
    assert pan.version == "pan-agreement/v0"
    assert report.identities["pan-agreement"].estimator_name == "example-no-claim"
    assert report.identities["pan-agreement"].estimator_version == "example-no-claim/v0"
    assert pan.sha256 != motion.sha256

    again = evaluate()
    assert again.rates["motion-rule-v3"].sha256 == motion.sha256
    assert again.rates["pan-agreement"].sha256 == pan.sha256
    assert again.harness_sha256 == report.harness_sha256


def test_no_claim_stub_marks_every_100ms_frame():
    sample = report_sample()
    bins = NoClaimPanEstimator().estimate(sample)
    assert len(bins) == sample.duration_ms // BIN_MS
    assert bins[0].mono_ms == 0
    assert bins[1].mono_ms == 100
    for pan_bin in bins:
        validate_pan_bin(pan_bin)
        assert pan_bin.no_claim is True
        assert pan_bin.to_dict() == {
            "mono_ms": pan_bin.mono_ms,
            "yaw_rate": None,
            "pitch_rate": None,
            "confidence": None,
            "no_claim": True,
        }
    score = PanAgreementScorer(NoClaimPanEstimator()).score(sample)
    assert score.flagged is None
    assert score.decision == "abstain"
    assert score.detail["no_claim"] == len(bins)
    assert score.detail["claimed"] == 0


def report_sample() -> VariantSample:
    rows = control_rows()
    return VariantSample(
        name="control",
        label="benign",
        rows=rows,
        motion={sec: 1 for sec in range(SECONDS)},
        sample_segments=(1,),
        duration_ms=SECONDS * 1000,
    )


def _idle_rows(duration_ms: int) -> list[dict]:
    rows = []
    for t_ms in range(0, duration_ms, 25):
        rows.append({"type": "in", "t_us": t_ms * 1000, "rx": 128, "ry": 128, "lx": 160, "ly": 128, "b": 0})
    return rows


def _moving_rows(duration_ms: int) -> list[dict]:
    rows = []
    for t_ms in range(0, duration_ms, 25):
        rows.append({"type": "in", "t_us": t_ms * 1000, "rx": 200, "ry": 128, "lx": 160, "ly": 128, "b": 1})
    return rows


def _claimed_bins(duration_ms: int, yaw: float) -> list[PanBin]:
    return [
        PanBin(mono_ms=t, yaw_rate=yaw, pitch_rate=0.0, confidence=1.0, no_claim=False)
        for t in range(0, duration_ms, BIN_MS)
    ]


class _FixedEstimator:
    """Test double. Not a camera estimator."""

    name = "fixed-bins"
    version = "test-double/v0"

    def __init__(self, bins: list[PanBin]) -> None:
        self._bins = bins

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        return list(self._bins)


def test_pan_estimator_plugs_in_and_the_example_scorer_flags_idle_stick_against_yaw():
    duration = 2000
    bins = _claimed_bins(duration, yaw=40.0)
    idle = VariantSample("idle-right-stick", "attack", _idle_rows(duration), {0: 1, 1: 1}, (1,), duration)
    moving = VariantSample("control", "benign", _moving_rows(duration), {0: 1, 1: 1}, (1,), duration)
    idle_score = PanAgreementScorer(_FixedEstimator(bins)).score(idle)
    moving_score = PanAgreementScorer(_FixedEstimator(bins)).score(moving)
    assert idle_score.flagged is True
    assert moving_score.flagged is False
    silent = [PanBin(mono_ms=t, yaw_rate=None, pitch_rate=None, confidence=None, no_claim=True) for t in range(0, duration, BIN_MS)]
    assert PanAgreementScorer(_FixedEstimator(silent)).score(idle).flagged is None
    swapped = PanAgreementScorer(_FixedEstimator(bins)).identity().sha256
    stubbed = PanAgreementScorer(NoClaimPanEstimator()).identity().sha256
    assert swapped != stubbed


def test_no_claim_rejects_a_bin_that_still_carries_a_rate():
    with pytest.raises(ValueError):
        validate_pan_bin(PanBin(mono_ms=0, yaw_rate=1.0, pitch_rate=None, confidence=None, no_claim=True))
    with pytest.raises(ValueError):
        validate_pan_bin(PanBin(mono_ms=50, yaw_rate=1.0, pitch_rate=0.0, confidence=1.0, no_claim=False))
    with pytest.raises(ValueError):
        validate_pan_bin(PanBin(mono_ms=0, yaw_rate=1.0, pitch_rate=0.0, confidence=None, no_claim=False))


def test_rate_math_counts_abstentions_as_undecided():
    samples = [
        VariantSample("control", "benign", [], {}, (), 0),
        VariantSample("full-idle", "attack", [], {}, (), 0),
        VariantSample("idle-right-stick", "attack", [], {}, (), 0),
    ]
    identity = MotionRuleV3Scorer().identity()
    only_idle = rates_for(samples, {
        "control": Score(False, {}),
        "full-idle": Score(True, {}),
        "idle-right-stick": Score(False, {}),
    }, identity)
    assert only_idle.catch_rate == 0.5
    assert only_idle.false_alarm_rate == 0.0
    none_decided = rates_for(samples, {sample.name: Score(None, {}) for sample in samples}, identity)
    assert none_decided.catch_rate is None
    assert none_decided.false_alarm_rate is None
    assert none_decided.abstained == 3
    alarmed = rates_for(samples, {
        "control": Score(True, {}),
        "full-idle": Score(True, {}),
        "idle-right-stick": Score(None, {}),
    }, identity)
    assert alarmed.false_alarm_rate == 1.0
    assert alarmed.catch_rate == 1.0
    assert alarmed.decided_attacks == 1


def test_extra_scorer_is_accepted_and_motion_rule_stays(report):
    duration = 2000
    bins = _claimed_bins(duration, yaw=40.0)

    class AlwaysClear:
        name = "always-clear"
        version = "always-clear/v0"

        def score(self, sample: VariantSample) -> Score:
            return Score(False, {})

        def identity(self):
            return MotionRuleV3Scorer().identity().__class__(
                name=self.name, version=self.version, sha256="ab" * 32,
            )

    custom = evaluate(scorers=[AlwaysClear(), PanAgreementScorer(_FixedEstimator(bins))])
    assert list(custom.scores) == ["motion-rule-v3", "always-clear", "pan-agreement"]
    assert custom.rates["always-clear"].catch_rate == 0.0
    assert custom.rates["always-clear"].false_alarm_rate == 0.0
    assert custom.rates["motion-rule-v3"].sha256 == report.rates["motion-rule-v3"].sha256
    # The fixed estimator claims yaw on every bin. The synthetic attacks with an idle
    # right stick are decided by the example policy; this only checks the plug-in ran.
    assert custom.scores["pan-agreement"]["control"].flagged is not None


def test_harness_imports_stay_inside_the_test_tree():
    forbidden = {
        "socket", "http", "urllib", "httpx", "requests",
        "qor_receipt.referee", "qor_receipt.referee.app",
        "qor_receipt.referee_service", "qor_receipt.referee_service.app",
        "qor_receipt.trusted_referees",
    }
    for path in sorted(REDTEAM.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        assert modules.isdisjoint(forbidden)
        text = path.read_text(encoding="utf-8")
        assert "site/" not in text
        assert "SoftwareKey" not in text
        assert "trusted_referees" not in text


def test_report_prints_and_matches_the_committed_doc(capsys):
    text = render_report()
    with capsys.disabled():
        print(text)
    assert DOC.is_file()
    assert DOC.read_text(encoding="utf-8") == text
    assert "idle-right-stick" in text
    assert "catch rate" in text
    assert "false-alarm rate" in text
    assert "example-no-claim/v0" in text
