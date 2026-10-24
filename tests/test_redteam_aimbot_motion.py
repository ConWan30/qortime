"""Red team for motion rule v3 against passthrough adapters and log tamper.

The assertions match today's rule. An honestly sealed USB log whose screen shows
injected aim, a skill-move burst, or a button macro stays ``pass``. An honestly
sealed empty log against a moving screen is ``partial``. The marker keeps that
gap visible in ``pytest -m known_gap`` without failing CI.

The harness calls ``score_motion`` and the input-proof checks directly. It does
not submit a bundle.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redteam.aimbot_harness import (  # noqa: E402
    EVENT_AT_MS,
    FLICK_MS,
    HONEST_STICK_AIMBOTS,
    SECONDS,
    SKILL_HOLD_MS,
    control_rows,
    evaluate,
    input_proof_errors,
    render_report,
    rewrite_rows,
    seal_input_log,
    stick_aimbot_caught,
)
from redteam.imports_guard import audit_source  # noqa: E402
from redteam.scorers import (  # noqa: E402
    BIN_MS,
    BUTTON_ACTION,
    EVENT_MATCH_WINDOW_MS,
    STICK_ACTION,
    STICK_DEADZONE,
    EventPressAgreementScorer,
    MotionRuleV3Scorer,
    NoClaimEventEstimator,
    NoClaimPanEstimator,
    PanAgreementScorer,
    PanBin,
    Score,
    ScorerIdentity,
    VariantSample,
    VideoEvent,
    logged_press_matches,
    rates_for,
    validate_pan_bin,
    validate_video_event,
)

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "redteam" / "motion-rule-v3-aimbot.md"
REDTEAM = Path(__file__).resolve().parent / "redteam"
PROOF_NEEDLES = (
    "SHA-256 mismatch",
    "input_hash does not match",
    "inputs_sha256 does not match",
    "inputs_chain_head does not match",
)

pytestmark = pytest.mark.known_gap(
    reason=(
        "motion rule v3 counts controller rows, so an honest log with an assisted "
        "screen stays pass; an empty log against a moving screen is partial"
    ),
)


@pytest.fixture(scope="module")
def report():
    return evaluate()


def _by_name(report):
    return {variant.sample.name: variant for variant in report.variants}


def test_fixture_is_synthetic_and_long_enough_to_score():
    rows = control_rows()
    ins = [row for row in rows if row.get("type") == "in"]
    assert len(ins) > 100
    assert {row["rx"] for row in ins} != {128}
    assert {row["b"] for row in ins} != {0}
    assert max(int(row["t_us"]) // 1000 for row in ins) // 1000 == SECONDS - 1


def test_log_tamper_keeps_left_stick_buttons_and_row_times():
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


def test_full_idle_tamper_drops_controller_rows():
    rewritten = rewrite_rows(control_rows(), "full-idle")
    assert not any(row.get("type") == "in" for row in rewritten)
    assert any(row.get("type") == "header" for row in rewritten)


def test_motion_rule_v3_misses_honest_stick_aimbots(report):
    """Headline: three honest-seal stick aimbots stay pass, and input proof passes."""
    motion = report.scores["motion-rule-v3"]
    by_name = _by_name(report)
    control = motion["control"]
    assert control.detail["motion_check"] == "pass"
    assert 300 <= control.detail["motion_permille"] < 1000
    assert control.flagged is False
    assert control.detail["rule"] == 3
    assert "motion-rule/v3" in control.detail["basis"]
    assert control.detail["bins"] >= 20
    assert control.detail["excluded_total"] == 0
    assert by_name["control"].input_proof == "pass"
    assert by_name["control"].sample.label == "benign"

    for name in HONEST_STICK_AIMBOTS:
        score = motion[name]
        variant = by_name[name]
        assert score.detail["motion_check"] == "pass"
        assert score.flagged is False
        assert 300 <= score.detail["motion_permille"] < 1000
        assert variant.input_proof == "pass"
        assert variant.input_proof_errors == ()
        assert variant.sample.cohort == "honest-seal"
        assert variant.sample.label == "attack"
        assert variant.sample.rows == by_name["control"].sample.rows
    assert stick_aimbot_caught(report) == 0


def test_honest_macros_have_on_screen_events_and_no_logged_press(report):
    by_name = _by_name(report)
    motion = report.scores["motion-rule-v3"]
    expected = {
        "honest-stick-action": STICK_ACTION,
        "honest-button-macro": BUTTON_ACTION,
    }
    for name, kind in expected.items():
        variant = by_name[name]
        score = motion[name]
        assert variant.sample.cohort == "honest-seal"
        assert variant.input_proof == "pass"
        assert variant.screen_events == 4
        assert score.detail["motion_check"] == "pass"
        assert score.flagged is False
        assert 300 <= score.detail["motion_permille"] < 1000
        assert variant.sample.rows == by_name["control"].sample.rows
        assert len(variant.sample.events) == 4
        for event in variant.sample.events:
            assert event.event_kind == kind
            assert event.no_claim is False
            assert event.confidence == 0.9
            assert event.mono_ms % 1000 == EVENT_AT_MS
            assert logged_press_matches(
                variant.sample.rows, event.mono_ms, kind, EVENT_MATCH_WINDOW_MS, STICK_DEADZONE,
            ) is False


def test_honest_full_idle_is_partial_and_still_passes_input_proof(report):
    variant = _by_name(report)["honest-full-idle"]
    score = report.scores["motion-rule-v3"]["honest-full-idle"]
    assert variant.sample.cohort == "honest-seal"
    assert variant.input_rows == 0
    assert variant.input_proof == "pass"
    assert variant.input_proof_errors == ()
    assert score.detail["motion_check"] == "partial"
    assert score.detail["motion_permille"] == -1000
    assert score.flagged is True


def test_honest_skill_gesture_is_shorter_than_a_camera_hold(report):
    runs = {variant.sample.name: variant.right_stick_run_ms for variant in report.variants}
    assert 0 < runs["control"] <= SKILL_HOLD_MS
    assert runs["control"] < 300
    assert runs["honest-passthrough-aim"] == runs["control"]
    assert runs["tamper-idle-right-stick"] == 0
    assert runs["tamper-full-idle"] == 0
    assert runs["honest-full-idle"] == 0
    assert 0 < runs["tamper-snap-flicks"] < 300


def test_log_tamper_fails_input_proof_and_is_labeled(report):
    by_name = _by_name(report)
    sealed = seal_input_log(control_rows())
    assert input_proof_errors(sealed) == ()
    control_permille = report.scores["motion-rule-v3"]["control"].detail["motion_permille"]
    for name in ("tamper-idle-right-stick", "tamper-blended-assist", "tamper-snap-flicks", "tamper-full-idle"):
        variant = by_name[name]
        assert variant.sample.cohort == "log-tamper"
        assert variant.sample.label == "attack"
        assert variant.input_proof == "fail"
        text = "\n".join(variant.input_proof_errors)
        for needle in PROOF_NEEDLES:
            assert needle in text
    for name in ("tamper-idle-right-stick", "tamper-blended-assist", "tamper-snap-flicks"):
        score = report.scores["motion-rule-v3"][name]
        assert score.flagged is False
        assert score.detail["motion_permille"] == control_permille
    deleted = report.scores["motion-rule-v3"]["tamper-full-idle"]
    assert deleted.flagged is True
    assert deleted.detail["motion_permille"] == -1000
    assert "record count mismatch" in "\n".join(by_name["tamper-full-idle"].input_proof_errors)


def test_headline_rates_separate_the_honest_seal_cohort_from_log_tamper(report):
    motion = report.rates_headline["motion-rule-v3"]
    assert motion.catch_rate == pytest.approx(1 / 6)
    assert motion.false_alarm_rate == 0.0
    assert motion.true_positives == 1
    assert motion.false_negatives == 5
    assert motion.false_positives == 0
    assert motion.true_negatives == 1
    assert motion.attacks == 6
    assert motion.benign == 1
    assert motion.decided_attacks == 6
    assert motion.abstained == 0
    assert motion.version == "motion-rule/v3"
    assert len(motion.sha256) == 64

    tamper = report.rates_tamper["motion-rule-v3"]
    assert tamper.attacks == 4
    assert tamper.benign == 0
    assert tamper.true_positives == 1
    assert tamper.false_negatives == 3
    assert tamper.catch_rate == 0.25
    assert tamper.false_alarm_rate is None
    assert tamper.decided_benign == 0

    for name in ("pan-agreement", "event-press-agreement"):
        headline = report.rates_headline[name]
        tampered = report.rates_tamper[name]
        assert headline.catch_rate is None
        assert headline.false_alarm_rate is None
        assert headline.decided_attacks == 0
        assert headline.abstained == 7
        assert tampered.catch_rate is None
        assert tampered.abstained == 4
        assert headline.sha256 != motion.sha256

    again = evaluate()
    assert again.rates_headline["motion-rule-v3"].sha256 == motion.sha256
    assert again.rates_headline["pan-agreement"].sha256 == report.rates_headline["pan-agreement"].sha256
    assert again.rates_headline["event-press-agreement"].sha256 == report.rates_headline["event-press-agreement"].sha256
    assert again.harness_sha256 == report.harness_sha256


def test_scorer_hash_covers_package_version_and_transitive_files(report):
    motion = report.identities["motion-rule-v3"]
    pan = report.identities["pan-agreement"]
    events = report.identities["event-press-agreement"]
    assert motion.package_version == "0.1.0"
    assert report.package_version == "0.1.0"
    assert "qor_receipt/review/motion.py" in motion.files
    assert "qor_receipt/review/media.py" in motion.files
    assert "qor_receipt/review/policy.py" in motion.files
    assert "qor_receipt/__init__.py" in motion.files
    assert "tests/redteam/scorers.py" in motion.files
    assert "qor_receipt/receipt.py" not in motion.files
    assert "qor_receipt/trusted_referees.py" not in motion.files
    assert "qor_receipt/receipt.py" not in report.harness_files
    assert "qor_receipt/trusted_referees.py" not in report.harness_files
    assert "tests/redteam/aimbot_harness.py" in report.harness_files
    assert "tests/redteam/scorers.py" in report.harness_files
    assert pan.estimator_name == "example-no-claim"
    assert pan.estimator_version == "example-no-claim/v0"
    assert events.estimator_name == "example-no-claim-events"
    assert events.estimator_version == "example-no-claim-events/v0"
    assert len({motion.sha256, pan.sha256, events.sha256, report.harness_sha256}) == 4


def test_no_claim_stubs_mark_unreadable_frames_and_events():
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
    pan_score = PanAgreementScorer(NoClaimPanEstimator()).score(sample)
    assert pan_score.flagged is None
    assert pan_score.decision == "abstain"
    assert pan_score.detail["claimed"] == 0

    events = NoClaimEventEstimator().estimate(sample)
    assert len(events) == sample.duration_ms // 1000
    for event in events:
        validate_video_event(event)
        assert event.no_claim is True
        assert event.to_dict() == {
            "mono_ms": event.mono_ms,
            "event_kind": None,
            "confidence": None,
            "no_claim": True,
        }
    event_score = EventPressAgreementScorer(NoClaimEventEstimator()).score(sample)
    assert event_score.flagged is None
    assert event_score.detail["reason"] == "too few claimed events"


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


class _FixedPan:
    """Test double. Not a camera estimator."""

    name = "fixed-bins"
    version = "test-double/v0"

    def __init__(self, bins: list[PanBin]) -> None:
        self._bins = bins

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        return list(self._bins)


class _FixedEvents:
    """Test double. Not a video-event estimator."""

    name = "fixed-events"
    version = "test-double/v0"

    def __init__(self, events: list[VideoEvent]) -> None:
        self._events = events

    def estimate(self, sample: VariantSample) -> list[VideoEvent]:
        return list(self._events)


def _stick_events(duration_ms: int) -> list[VideoEvent]:
    return [
        VideoEvent(mono_ms=t, event_kind=STICK_ACTION, confidence=0.9, no_claim=False)
        for t in range(500, duration_ms, 1000)
    ]


def test_pan_estimator_plugs_in_and_the_example_scorer_flags_idle_stick_against_yaw():
    duration = 2000
    bins = _claimed_bins(duration, yaw=40.0)
    idle = VariantSample("idle-right-stick", "attack", _idle_rows(duration), {0: 1, 1: 1}, (1,), duration)
    moving = VariantSample("control", "benign", _moving_rows(duration), {0: 1, 1: 1}, (1,), duration)
    idle_score = PanAgreementScorer(_FixedPan(bins)).score(idle)
    moving_score = PanAgreementScorer(_FixedPan(bins)).score(moving)
    assert idle_score.flagged is True
    assert moving_score.flagged is False
    silent = [
        PanBin(mono_ms=t, yaw_rate=None, pitch_rate=None, confidence=None, no_claim=True)
        for t in range(0, duration, BIN_MS)
    ]
    assert PanAgreementScorer(_FixedPan(silent)).score(idle).flagged is None
    swapped = PanAgreementScorer(_FixedPan(bins)).identity().sha256
    stubbed = PanAgreementScorer(NoClaimPanEstimator()).identity().sha256
    assert swapped != stubbed


def test_event_estimator_plugs_in_and_flags_unmatched_presses():
    duration = 4000
    events = _stick_events(duration)
    idle = VariantSample("honest-stick-action", "attack", _idle_rows(duration), {0: 1}, (1,), duration, events=tuple(events))
    pressed = VariantSample("control", "benign", _moving_rows(duration), {0: 1}, (1,), duration, events=tuple(events))
    idle_score = EventPressAgreementScorer(_FixedEvents(events)).score(idle)
    pressed_score = EventPressAgreementScorer(_FixedEvents(events)).score(pressed)
    assert idle_score.flagged is True
    assert idle_score.detail["unmatched"] == 4
    assert pressed_score.flagged is False
    assert pressed_score.detail["unmatched"] == 0
    buttons = [
        VideoEvent(mono_ms=t, event_kind=BUTTON_ACTION, confidence=1.0, no_claim=False)
        for t in (500, 1500, 2500, 3500)
    ]
    assert EventPressAgreementScorer(_FixedEvents(buttons)).score(idle).flagged is True
    assert EventPressAgreementScorer(_FixedEvents(buttons)).score(pressed).flagged is False
    unread = [VideoEvent(mono_ms=t, event_kind=None, confidence=None, no_claim=True) for t in range(0, duration, 1000)]
    assert EventPressAgreementScorer(_FixedEvents(unread)).score(idle).flagged is None

    class _FromSample:
        name = "from-sample"
        version = "test-double/v0"

        def estimate(self, sample: VariantSample) -> list[VideoEvent]:
            return list(sample.events)

    custom = evaluate(scorers=[EventPressAgreementScorer(_FromSample())])
    assert custom.scores["event-press-agreement"]["honest-stick-action"].flagged is True
    assert custom.scores["event-press-agreement"]["honest-button-macro"].flagged is True
    assert custom.scores["event-press-agreement"]["control"].flagged is None
    assert custom.scores["motion-rule-v3"]["honest-stick-action"].flagged is False
    assert custom.scores["motion-rule-v3"]["honest-button-macro"].flagged is False


def test_pan_and_event_validation_rejects_non_finite_and_out_of_range_confidence():
    with pytest.raises(ValueError):
        validate_pan_bin(PanBin(mono_ms=0, yaw_rate=1.0, pitch_rate=None, confidence=None, no_claim=True))
    with pytest.raises(ValueError):
        validate_pan_bin(PanBin(mono_ms=50, yaw_rate=1.0, pitch_rate=0.0, confidence=1.0, no_claim=False))
    for yaw, pitch, confidence in (
        (float("nan"), 0.0, 1.0),
        (0.0, float("inf"), 1.0),
        (0.0, 0.0, float("nan")),
        (0.0, 0.0, float("-inf")),
        (0.0, 0.0, 1.1),
        (0.0, 0.0, -0.01),
        (True, 0.0, 1.0),
    ):
        with pytest.raises(ValueError):
            validate_pan_bin(PanBin(mono_ms=0, yaw_rate=yaw, pitch_rate=pitch, confidence=confidence, no_claim=False))
    validate_pan_bin(PanBin(mono_ms=0, yaw_rate=0.0, pitch_rate=0, confidence=0.0, no_claim=False))
    validate_pan_bin(PanBin(mono_ms=100, yaw_rate=0.0, pitch_rate=0.0, confidence=1, no_claim=False))

    with pytest.raises(ValueError):
        validate_video_event(VideoEvent(mono_ms=150, event_kind=STICK_ACTION, confidence=1.0, no_claim=True))
    with pytest.raises(ValueError):
        validate_video_event(VideoEvent(mono_ms=-1, event_kind=STICK_ACTION, confidence=1.0, no_claim=False))
    with pytest.raises(ValueError):
        validate_video_event(VideoEvent(mono_ms=150, event_kind="", confidence=1.0, no_claim=False))
    for confidence in (float("nan"), float("inf"), 1.1, -0.01):
        with pytest.raises(ValueError):
            validate_video_event(VideoEvent(mono_ms=150, event_kind=BUTTON_ACTION, confidence=confidence, no_claim=False))
    validate_video_event(VideoEvent(mono_ms=150, event_kind=STICK_ACTION, confidence=0.0, no_claim=False))
    validate_video_event(VideoEvent(mono_ms=150, event_kind=BUTTON_ACTION, confidence=1, no_claim=False))
    claimed = VideoEvent(mono_ms=150, event_kind=STICK_ACTION, confidence=0.5, no_claim=False)
    assert claimed.to_dict() == {
        "mono_ms": 150,
        "event_kind": STICK_ACTION,
        "confidence": 0.5,
        "no_claim": False,
    }


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
            return ScorerIdentity(name=self.name, version=self.version, sha256="ab" * 32, package_version="0")

    custom = evaluate(scorers=[AlwaysClear(), PanAgreementScorer(_FixedPan(bins))])
    assert list(custom.scores) == ["motion-rule-v3", "always-clear", "pan-agreement"]
    assert custom.rates_headline["always-clear"].catch_rate == 0.0
    assert custom.rates_headline["always-clear"].false_alarm_rate == 0.0
    assert custom.rates_headline["motion-rule-v3"].sha256 == report.rates_headline["motion-rule-v3"].sha256
    assert custom.scores["pan-agreement"]["control"].flagged is not None


def test_import_guard_blocks_urllib_request_and_network_tools():
    urllib_request = audit_source("from urllib.request import urlopen\n")
    assert urllib_request and "urllib.request" in urllib_request[0]
    http_client = audit_source("import http.client\n")
    assert http_client and "http.client" in http_client[0]
    curl = audit_source('import subprocess\nsubprocess.run(["curl", "https://example.invalid"])\n')
    assert any("curl" in item and "subprocess.run" in item for item in curl)
    ffmpeg = audit_source('import subprocess\nsubprocess.run(["ffmpeg", "-i", "clip.mp4"])\n')
    assert ffmpeg == []
    for path in sorted(REDTEAM.glob("*.py")):
        assert audit_source(path.read_text(encoding="utf-8"), filename=path.name) == []
        text = path.read_text(encoding="utf-8")
        assert "site/" not in text
        assert "SoftwareKey" not in text
    assert audit_source(Path(__file__).read_text(encoding="utf-8"), filename="test") == []


SYNTHETIC_RATE_CAVEAT = (
    "**SYNTHETIC-DATA CAVEAT:** these rates come from synthetic data with only one benign sample "
    "(n=1 control) and are NOT real-world or measured detection/false-alarm rates; "
    "'0/1 false alarms' is not a false-alarm rate."
)
CLOCK_WINDOW_CAVEAT = (
    "The 150 ms event window is valid only on synthetic data with a shared clock. "
    "On real footage, a video-vs-log mismatch claim is allowed only for a bundle that carries a "
    "frame_bind chain (qorcap #75, --frame-bind) that verifies; if that bundle's chain is missing, "
    "stripped or broken, every mismatch claim is no-claim. This is checked per bundle, not switched "
    "on by merging code, so older footage stays no-claim."
)
# trusted_referees stays here. The harness must not import qor_receipt.receipt, which is what loads it.
_FORBIDDEN_MODULE_TOKENS = ("qor_receipt.referee", "referee_service", "trusted_referees")


def _module_is_forbidden(name: str) -> bool:
    """True when ``name`` is one of the isolated modules, a parent alias, or a submodule."""
    parts = name.split(".")
    for token in _FORBIDDEN_MODULE_TOKENS:
        token_parts = token.split(".")
        width = len(token_parts)
        if any(parts[i:i + width] == token_parts for i in range(len(parts) - width + 1)):
            return True
    return False


def test_synthetic_rate_caveat_stays_beside_the_headline_table():
    text = DOC.read_text(encoding="utf-8")
    assert SYNTHETIC_RATE_CAVEAT in text
    assert CLOCK_WINDOW_CAVEAT in text
    section = text.split("## Pluggable scorers", 1)[1].split("## Reproducing", 1)[0]
    assert "Catch rate is flagged attacks" in section
    assert "event-press-agreement" in section
    assert SYNTHETIC_RATE_CAVEAT in section
    assert CLOCK_WINDOW_CAVEAT in section
    lines = section.splitlines()
    caveat_at = next(i for i, line in enumerate(lines) if "SYNTHETIC-DATA CAVEAT" in line)
    clock_at = next(i for i, line in enumerate(lines) if "frame_bind chain" in line)
    table_at = next(i for i, line in enumerate(lines) if line.startswith("| scorer |"))
    assert caveat_at < clock_at < table_at
    assert table_at - caveat_at <= 6
    assert "headline false-alarm rate" in lines[table_at]
    assert "every mismatch claim is no-claim" in lines[clock_at]
    assert "older footage stays no-claim" in lines[clock_at]
    assert "closer than 719" not in section


def test_harness_process_does_not_import_referee_modules():
    """A fresh interpreter scores every variant. Referee modules must stay unloaded.

    ``trusted_referees`` is still required to be absent. The harness reads the motion
    floor from ``receipt.py`` as text and does not import ``qor_receipt.receipt``, so
    the trust root never loads. Dropping it from this list would hide that import.
    """
    assert "trusted_referees" in _FORBIDDEN_MODULE_TOKENS
    code = (
        "import json, sys\n"
        "sys.path.insert(0, 'tests')\n"
        "from redteam.aimbot_harness import evaluate\n"
        "report = evaluate()\n"
        "assert len(report.variants) == 11\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout)
    leaked = [name for name in loaded if _module_is_forbidden(name)]
    assert leaked == []
    for name in (
        "qor_receipt.referee",
        "qor_receipt.referee_service",
        "qor_receipt.trusted_referees",
        "qor_receipt.receipt",
    ):
        assert name not in loaded
    assert "qor_receipt.review.motion" in loaded
    assert "qor_receipt.review.media" in loaded


def test_report_prints_and_matches_the_committed_doc(capsys):
    text = render_report()
    with capsys.disabled():
        print(text)
    assert DOC.is_file()
    assert DOC.read_text(encoding="utf-8") == text
    assert "0/3" in text
    assert "honest-stick-action" in text
    assert "honest-button-macro" in text
    assert "stick_action" in text
    assert "button_action" in text
    assert "log-tamper" in text
    assert "example-no-claim-events/v0" in text
