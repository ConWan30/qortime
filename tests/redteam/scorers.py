"""Pluggable scorers for the aimbot red-team harness.

A scorer is a small object with ``name``, ``version``, ``score``, and ``identity``.
``identity().sha256`` covers the code that actually ran, so a re-run of the same
tree prints the same hash and a later edit prints a different one.

Motion rule v3 is one scorer. A future camera-pan estimator plugs in behind
``CameraPanEstimator`` and is consumed by ``PanAgreementScorer``. The estimator's
output is one record per 100 ms:

    {"mono_ms", "yaw_rate", "pitch_rate", "confidence", "no_claim"}

Unreadable frames set ``no_claim`` true and leave the three measurements null.
``NoClaimPanEstimator`` is a stub of that shape. It does not look at pixels.
"""
from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from qor_receipt.receipt import MOTION_MIN_PERMILLE
from qor_receipt.review.media import input_energy_series
from qor_receipt.review.motion import correlate_keyed, score_motion
from qor_receipt.review.policy import PARTIAL_CHECK, PASS

BIN_MS = 100
LABELS = ("attack", "benign")


@dataclass(frozen=True)
class VariantSample:
    """One rewritten log plus the synthetic on-screen motion series it is scored against."""

    name: str
    label: str
    rows: list[dict[str, Any]]
    motion: dict[int, int]
    sample_segments: tuple[int, ...]
    duration_ms: int

    def __post_init__(self) -> None:
        if self.label not in LABELS:
            raise ValueError(f"label must be attack or benign, got {self.label!r}")


@dataclass(frozen=True)
class Score:
    """``flagged`` is true (catch), false (clear), or None (abstain)."""

    flagged: bool | None
    detail: dict[str, Any]

    @property
    def decision(self) -> str:
        if self.flagged is None:
            return "abstain"
        return "flag" if self.flagged else "clear"


@dataclass(frozen=True)
class PanBin:
    """One 100 ms camera-pan bin. ``no_claim`` marks an unreadable frame."""

    mono_ms: int
    yaw_rate: float | None
    pitch_rate: float | None
    confidence: float | None
    no_claim: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mono_ms": self.mono_ms,
            "yaw_rate": self.yaw_rate,
            "pitch_rate": self.pitch_rate,
            "confidence": self.confidence,
            "no_claim": self.no_claim,
        }


def validate_pan_bin(bin: PanBin) -> None:
    """Reject a bin that does not match the estimator contract."""
    if bin.mono_ms < 0 or bin.mono_ms % BIN_MS != 0:
        raise ValueError("mono_ms must be a non-negative multiple of 100")
    if bin.no_claim:
        if bin.yaw_rate is not None or bin.pitch_rate is not None or bin.confidence is not None:
            raise ValueError("no_claim bins carry no yaw_rate, pitch_rate, or confidence")
        return
    for name, value in (("yaw_rate", bin.yaw_rate), ("pitch_rate", bin.pitch_rate), ("confidence", bin.confidence)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number when the frame is readable")
    if not 0.0 <= float(bin.confidence) <= 1.0:
        raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ScorerIdentity:
    name: str
    version: str
    sha256: str
    estimator_name: str | None = None
    estimator_version: str | None = None


@dataclass(frozen=True)
class RateResult:
    """Catch rate and false-alarm rate over the variants a scorer actually decided.

    An abstention is neither a catch nor a false alarm. Both rates are None when
    that class has no decision (for example the no_claim stub).
    """

    scorer: str
    version: str
    sha256: str
    attacks: int
    benign: int
    true_positives: int
    false_negatives: int
    false_positives: int
    true_negatives: int
    abstained: int
    decided_attacks: int
    decided_benign: int
    catch_rate: float | None
    false_alarm_rate: float | None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def motion_check_state(permille: int | None) -> str:
    """Same comparison ``Referee.review`` uses for the motion check.

    ``pass`` when the permille is at or above ``MOTION_MIN_PERMILLE``, otherwise
    ``partial``. This harness does not call the referee. A partial motion check
    is not a FAIL.
    """
    if permille is not None and permille >= MOTION_MIN_PERMILLE:
        return PASS
    return PARTIAL_CHECK


class _InputLog:
    """``input_energy_series`` only reads ``.inputs``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.inputs = rows


@runtime_checkable
class Scorer(Protocol):
    name: str
    version: str

    def score(self, sample: VariantSample) -> Score:
        """Return a flag, a clear, or an abstention for one variant."""

    def identity(self) -> ScorerIdentity:
        """Version plus a hash of the implementation that produced the score."""


@runtime_checkable
class CameraPanEstimator(Protocol):
    """Plug-in point for a future camera-pan estimator.

    ``estimate`` returns one bin per 100 ms of ``sample.duration_ms``. A frame
    the estimator cannot read is ``no_claim=True`` with null rates. This
    protocol does not define how yaw or pitch are computed.
    """

    name: str
    version: str

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        """Per-100 ms bins covering the sample."""


class MotionRuleV3Scorer:
    """Shipped motion rule: controller-row rate versus frame-difference energy.

    The score comes from ``input_energy_series`` and ``score_motion``. Row
    counts are the controller signal. Stick axes are not an input.
    """

    name = "motion-rule-v3"
    version = "motion-rule/v3"

    def score(self, sample: VariantSample) -> Score:
        energy = input_energy_series(_InputLog(sample.rows))
        permille, detail = score_motion(energy, sample.motion, sample_segments=list(sample.sample_segments))
        state = motion_check_state(permille)
        return Score(flagged=state == PARTIAL_CHECK, detail={
            "motion_permille": permille,
            "motion_check": state,
            "bins": detail.get("bins"),
            "rule": detail.get("rule"),
            "basis": detail.get("basis"),
            "excluded_total": (detail.get("excluded") or {}).get("total"),
        })

    def identity(self) -> ScorerIdentity:
        motion_src = Path(inspect.getfile(score_motion)).read_text(encoding="utf-8")
        blob = "\n".join([
            self.version,
            f"MOTION_MIN_PERMILLE={MOTION_MIN_PERMILLE}",
            inspect.getsource(type(self)),
            inspect.getsource(input_energy_series),
            inspect.getsource(correlate_keyed),
            motion_src,
        ])
        return ScorerIdentity(name=self.name, version=self.version, sha256=_sha256_text(blob))


class NoClaimPanEstimator:
    """Stub estimator. Every frame is unreadable.

    This exists so a camera-pan scorer has a plug-in to call. It does not
    estimate yaw or pitch, and it does not read a picture.
    """

    name = "example-no-claim"
    version = "example-no-claim/v0"

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        return [
            PanBin(mono_ms=t, yaw_rate=None, pitch_rate=None, confidence=None, no_claim=True)
            for t in range(0, sample.duration_ms, BIN_MS)
        ]


def right_stick_idle(rows: Sequence[Mapping[str, Any]], start_ms: int, end_ms: int, deadzone: int) -> bool:
    """True when every right-stick sample in ``[start_ms, end_ms)`` is inside the deadzone.

    A window with no rows is idle. ``deadzone`` matches the agent's stick decoder
    (centered at 128). This is example-scorer policy, not a verification threshold.
    """
    for row in rows:
        if row.get("type") != "in" or not isinstance(row.get("t_us"), int):
            continue
        t_ms = int(row["t_us"]) // 1000
        if t_ms < start_ms or t_ms >= end_ms:
            continue
        if abs(int(row.get("rx", 128)) - 128) > deadzone or abs(int(row.get("ry", 128)) - 128) > deadzone:
            return False
    return True


class PanAgreementScorer:
    """Example scorer over a camera-pan estimator's 100 ms bins.

    A claimed bin disagrees when the estimator reports a camera rate with
    enough confidence and the logged right stick in that same 100 ms is idle.
    The scorer flags the variant when those disagreements are at least
    ``min_disagree_bins`` and at least half of the claimed bins. Fewer than
    ``min_claimed_bins`` readable frames is an abstention, which is how the
    ``no_claim`` stub scores every variant.

    The numbers below belong to this example. They are not ``MOTION_MIN_PERMILLE``
    and the verifier does not read them.
    """

    name = "pan-agreement"
    version = "pan-agreement/v0"
    min_claimed_bins = 20
    min_confidence = 0.5
    rate_floor = 10.0
    min_disagree_bins = 10
    stick_deadzone = 8

    def __init__(self, estimator: CameraPanEstimator) -> None:
        self.estimator = estimator

    def score(self, sample: VariantSample) -> Score:
        bins = self.estimator.estimate(sample)
        for pan_bin in bins:
            validate_pan_bin(pan_bin)
        claimed = [pan_bin for pan_bin in bins if not pan_bin.no_claim]
        no_claim = len(bins) - len(claimed)
        detail: dict[str, Any] = {
            "estimator": self.estimator.name,
            "estimator_version": self.estimator.version,
            "bins": len(bins),
            "claimed": len(claimed),
            "no_claim": no_claim,
        }
        if len(claimed) < self.min_claimed_bins:
            detail["reason"] = "too few claimed bins"
            return Score(flagged=None, detail=detail)
        disagree = 0
        for pan_bin in claimed:
            yaw, pitch, confidence = pan_bin.yaw_rate, pan_bin.pitch_rate, pan_bin.confidence
            if yaw is None or pitch is None or confidence is None:
                raise ValueError("claimed bin missing a measurement")
            moving = abs(yaw) >= self.rate_floor or abs(pitch) >= self.rate_floor
            if confidence < self.min_confidence or not moving:
                continue
            if right_stick_idle(sample.rows, pan_bin.mono_ms, pan_bin.mono_ms + BIN_MS, self.stick_deadzone):
                disagree += 1
        detail["disagree_bins"] = disagree
        flagged = disagree >= self.min_disagree_bins and disagree * 2 >= len(claimed)
        return Score(flagged=flagged, detail=detail)

    def identity(self) -> ScorerIdentity:
        blob = "\n".join([
            self.version,
            inspect.getsource(type(self)),
            inspect.getsource(validate_pan_bin),
            inspect.getsource(right_stick_idle),
            self.estimator.version,
            inspect.getsource(type(self.estimator)),
        ])
        return ScorerIdentity(
            name=self.name,
            version=self.version,
            sha256=_sha256_text(blob),
            estimator_name=self.estimator.name,
            estimator_version=self.estimator.version,
        )


def rates_for(samples: Sequence[VariantSample], scores: Mapping[str, Score], identity: ScorerIdentity) -> RateResult:
    """Catch rate = true positives / decided attacks. False-alarm rate = false positives / decided benign."""
    attacks = [sample for sample in samples if sample.label == "attack"]
    benign = [sample for sample in samples if sample.label == "benign"]
    if len(attacks) + len(benign) != len(samples):
        raise ValueError("every sample needs an attack or benign label")

    def flagged(sample: VariantSample) -> bool | None:
        return scores[sample.name].flagged

    true_positives = sum(flagged(sample) is True for sample in attacks)
    false_negatives = sum(flagged(sample) is False for sample in attacks)
    false_positives = sum(flagged(sample) is True for sample in benign)
    true_negatives = sum(flagged(sample) is False for sample in benign)
    abstained = sum(flagged(sample) is None for sample in samples)
    decided_attacks = true_positives + false_negatives
    decided_benign = false_positives + true_negatives
    catch = None if decided_attacks == 0 else true_positives / decided_attacks
    false_alarm = None if decided_benign == 0 else false_positives / decided_benign
    return RateResult(
        scorer=identity.name,
        version=identity.version,
        sha256=identity.sha256,
        attacks=len(attacks),
        benign=len(benign),
        true_positives=true_positives,
        false_negatives=false_negatives,
        false_positives=false_positives,
        true_negatives=true_negatives,
        abstained=abstained,
        decided_attacks=decided_attacks,
        decided_benign=decided_benign,
        catch_rate=catch,
        false_alarm_rate=false_alarm,
    )


def default_scorers() -> list[Scorer]:
    """Motion rule v3, plus the example pan scorer with the no_claim stub plugged in."""
    return [MotionRuleV3Scorer(), PanAgreementScorer(NoClaimPanEstimator())]
