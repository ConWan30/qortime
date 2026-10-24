"""Pluggable scorers for the aimbot red-team harness.

A scorer is a small object with ``name``, ``version``, ``score``, and ``identity``.
``identity().sha256`` covers the package version, the scorer module, and every
in-repo module that module reaches by import. A re-run of the same tree prints
the same hash. An edit to any of those files prints a different one.

Two estimator plug-ins feed a scorer. Neither one is implemented here:

* ``CameraPanEstimator`` returns one bin per 100 ms:
  ``{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}``.
* ``VideoEventEstimator`` returns events:
  ``{mono_ms, event_kind, confidence, no_claim}``.

Unreadable frames and unread events set ``no_claim`` and leave the measurements
null. The stubs ``NoClaimPanEstimator`` and ``NoClaimEventEstimator`` do that
and do not look at a picture. A scorer may compare either output with the
logged rows. Motion rule v3 uses neither; it counts controller rows.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import qor_receipt
from qor_receipt.review.media import input_energy_series
from qor_receipt.review.motion import score_motion
from qor_receipt.review.policy import PARTIAL_CHECK, PASS

PACKAGE_VERSION = qor_receipt.__version__
BIN_MS = 100
EVENT_MATCH_WINDOW_MS = 150
STICK_DEADZONE = 8
STICK_ACTION = "stick_action"
BUTTON_ACTION = "button_action"
EVENT_KINDS = frozenset({STICK_ACTION, BUTTON_ACTION})
LABELS = ("attack", "benign")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _motion_min_permille() -> int:
    """The shipped floor, read from ``qor_receipt/receipt.py`` without importing it.

    Importing that module also imports ``qor_receipt.trusted_referees``. This harness
    scores ``score_motion`` directly and does not load the trust root.
    """
    path = REPO_ROOT / "qor_receipt" / "receipt.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != "MOTION_MIN_PERMILLE":
                continue
            value = node.value.value if isinstance(node.value, ast.Constant) else None
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    raise RuntimeError("MOTION_MIN_PERMILLE is not an integer assignment in qor_receipt/receipt.py")


MOTION_MIN_PERMILLE = _motion_min_permille()


@dataclass(frozen=True)
class VariantSample:
    """One controller log plus the synthetic on-screen series it is scored against."""

    name: str
    label: str
    rows: list[dict[str, Any]]
    motion: dict[int, int]
    sample_segments: tuple[int, ...]
    duration_ms: int
    cohort: str = "honest-seal"
    events: tuple[VideoEvent, ...] = ()

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


@dataclass(frozen=True)
class VideoEvent:
    """One video-derived event. ``no_claim`` marks an event the estimator could not read.

    ``event_kind`` is game-agnostic. ``stick_action`` is a right-stick gesture
    (in Madden, a skill move or hit stick). ``button_action`` is a button press
    (in Madden, a user catch or any other button macro).
    """

    mono_ms: int
    event_kind: str | None
    confidence: float | None
    no_claim: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mono_ms": self.mono_ms,
            "event_kind": self.event_kind,
            "confidence": self.confidence,
            "no_claim": self.no_claim,
        }


def _finite_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _confidence(value: Any) -> float:
    number = _finite_number("confidence", value)
    if not 0.0 <= number <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return number


def validate_pan_bin(pan_bin: PanBin) -> None:
    """Reject a bin that does not match the estimator contract."""
    if isinstance(pan_bin.mono_ms, bool) or not isinstance(pan_bin.mono_ms, int):
        raise ValueError("mono_ms must be an integer")
    if pan_bin.mono_ms < 0 or pan_bin.mono_ms % BIN_MS != 0:
        raise ValueError("mono_ms must be a non-negative multiple of 100")
    if pan_bin.no_claim:
        if pan_bin.yaw_rate is not None or pan_bin.pitch_rate is not None or pan_bin.confidence is not None:
            raise ValueError("no_claim bins carry no yaw_rate, pitch_rate, or confidence")
        return
    _finite_number("yaw_rate", pan_bin.yaw_rate)
    _finite_number("pitch_rate", pan_bin.pitch_rate)
    _confidence(pan_bin.confidence)


def validate_video_event(event: VideoEvent) -> None:
    """Reject an event that does not match the estimator contract."""
    if isinstance(event.mono_ms, bool) or not isinstance(event.mono_ms, int) or event.mono_ms < 0:
        raise ValueError("mono_ms must be a non-negative integer")
    if event.no_claim:
        if event.event_kind is not None or event.confidence is not None:
            raise ValueError("no_claim events carry no event_kind or confidence")
        return
    if not isinstance(event.event_kind, str) or not event.event_kind:
        raise ValueError("event_kind must be a non-empty string when the event is claimed")
    _confidence(event.confidence)


@dataclass(frozen=True)
class ScorerIdentity:
    name: str
    version: str
    sha256: str
    package_version: str
    estimator_name: str | None = None
    estimator_version: str | None = None
    files: tuple[str, ...] = ()
    estimator_sha256: str | None = None


@dataclass(frozen=True)
class RateResult:
    """Catch rate and false-alarm rate over the variants a scorer actually decided.

    An abstention is neither a catch nor a false alarm. Both rates are None when
    that class has no decision (for example a no_claim stub).
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


def _resolve_module(name: str) -> Path | None:
    """Map an in-repo module name to a source file. Stdlib and third-party names return None."""
    parts = name.split(".")
    if not parts or parts[0] not in {"qor_receipt", "redteam"}:
        return None
    if parts[0] == "redteam":
        base = REPO_ROOT / "tests" / "redteam"
        rest = parts[1:]
    else:
        base = REPO_ROOT / "qor_receipt"
        rest = parts[1:]
    if not rest:
        init = base / "__init__.py"
        return init if init.is_file() else None
    file_py = base.joinpath(*rest).with_suffix(".py")
    if file_py.is_file():
        return file_py.resolve()
    init = base.joinpath(*rest, "__init__.py")
    if init.is_file():
        return init.resolve()
    return None


def _import_from_module(path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    rel = path.resolve().relative_to(REPO_ROOT)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        package = parts[:-1]
    else:
        package = parts[:-1]
    drop = node.level - 1
    if drop:
        package = package[:-drop]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package) if package else None


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_module(path, node)
            if not base:
                continue
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return names


def transitive_files(start: Sequence[Path]) -> tuple[Path, ...]:
    """Source files reachable from ``start`` by in-repo imports, sorted by repo-relative path."""
    seen: set[Path] = set()
    queue = [path.resolve() for path in start]
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        try:
            path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        seen.add(path)
        for name in _imported_names(path):
            resolved = _resolve_module(name)
            if resolved is not None:
                queue.append(resolved)
    return tuple(sorted(seen, key=lambda item: item.relative_to(REPO_ROOT).as_posix()))


def hash_file_set(start: Sequence[Path], prefix: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """SHA-256 of ``prefix`` plus each reachable file's repo-relative path and bytes."""
    files = transitive_files(start)
    lines = list(prefix)
    rels: list[str] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        rels.append(rel)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{rel} {digest}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(), tuple(rels)


def _class_file(obj: Any) -> Path:
    return Path(inspect.getfile(type(obj))).resolve()


def source_identity(scorer: Any, estimator: Any | None = None) -> ScorerIdentity:
    """Identity of one scorer: package version, version strings, and the resolved file set."""
    start = [_class_file(scorer)]
    if estimator is not None:
        start.append(_class_file(estimator))
    prefix = [
        f"qor-receipt=={PACKAGE_VERSION}",
        f"scorer={scorer.name}",
        f"version={scorer.version}",
        f"estimator={getattr(estimator, 'name', '')}",
        f"estimator_version={getattr(estimator, 'version', '')}",
    ]
    # An estimator may carry its own sha256 (for example one that folds in the
    # OpenCV build). It joins the prefix only when present, so stubs hash as before.
    estimator_sha = getattr(estimator, "sha256", None)
    if estimator_sha:
        prefix.append(f"estimator_sha256={estimator_sha}")
    digest, rels = hash_file_set(start, prefix)
    return ScorerIdentity(
        name=scorer.name,
        version=scorer.version,
        sha256=digest,
        package_version=PACKAGE_VERSION,
        estimator_name=getattr(estimator, "name", None),
        estimator_version=getattr(estimator, "version", None),
        files=rels,
        estimator_sha256=estimator_sha or None,
    )


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
        """Package version plus a hash of the resolved implementation."""


@runtime_checkable
class CameraPanEstimator(Protocol):
    """Plug-in for a future camera-pan estimator. This protocol does not compute yaw or pitch."""

    name: str
    version: str

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        """One bin per 100 ms. Unreadable frames are ``no_claim`` with null rates."""


@runtime_checkable
class VideoEventEstimator(Protocol):
    """Plug-in for a future video-event estimator. This protocol does not detect events."""

    name: str
    version: str

    def estimate(self, sample: VariantSample) -> list[VideoEvent]:
        """Events the picture suggests. Unread events are ``no_claim``."""


class MotionRuleV3Scorer:
    """Shipped motion rule: controller-row rate versus frame-difference energy.

    The score comes from ``input_energy_series`` and ``score_motion``. Row counts
    are the controller signal. Stick axes and button bits are not an input.
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
        return source_identity(self)


class NoClaimPanEstimator:
    """Stub pan estimator. Every frame is unreadable. It does not estimate yaw or pitch."""

    name = "example-no-claim"
    version = "example-no-claim/v0"

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        return [
            PanBin(mono_ms=t, yaw_rate=None, pitch_rate=None, confidence=None, no_claim=True)
            for t in range(0, sample.duration_ms, BIN_MS)
        ]


class NoClaimEventEstimator:
    """Stub event estimator. Every second is unreadable. It does not detect events."""

    name = "example-no-claim-events"
    version = "example-no-claim-events/v0"

    def estimate(self, sample: VariantSample) -> list[VideoEvent]:
        return [
            VideoEvent(mono_ms=t, event_kind=None, confidence=None, no_claim=True)
            for t in range(0, sample.duration_ms, 1000)
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


def logged_press_matches(rows: Sequence[Mapping[str, Any]], mono_ms: int, event_kind: str,
                         window_ms: int, deadzone: int) -> bool:
    """True when the log has the press a claimed video event would require.

    ``stick_action`` matches an off-center right stick. ``button_action`` matches
    a non-zero button mask. The window is inclusive. Other kinds do not match.
    """
    lo, hi = mono_ms - window_ms, mono_ms + window_ms
    for row in rows:
        if row.get("type") != "in" or not isinstance(row.get("t_us"), int):
            continue
        t_ms = int(row["t_us"]) // 1000
        if t_ms < lo or t_ms > hi:
            continue
        if event_kind == STICK_ACTION:
            rx = abs(int(row.get("rx", 128)) - 128)
            ry = abs(int(row.get("ry", 128)) - 128)
            if rx > deadzone or ry > deadzone:
                return True
        elif event_kind == BUTTON_ACTION and int(row.get("b") or 0) != 0:
            return True
    return False


class PanAgreementScorer:
    """Example scorer over a camera-pan estimator's 100 ms bins.

    A claimed bin disagrees when the estimator reports a camera rate with enough
    confidence and the logged right stick in that same 100 ms is idle. The scorer
    flags the variant when those disagreements are at least ``min_disagree_bins``
    and at least half of the claimed bins. Fewer than ``min_claimed_bins``
    readable frames is an abstention, which is how the no_claim stub scores.

    The numbers below belong to this example. They are not ``MOTION_MIN_PERMILLE``
    and the verifier does not read them.
    """

    name = "pan-agreement"
    version = "pan-agreement/v0"
    min_claimed_bins = 20
    min_confidence = 0.5
    rate_floor = 10.0
    min_disagree_bins = 10
    stick_deadzone = STICK_DEADZONE

    def __init__(self, estimator: CameraPanEstimator) -> None:
        self.estimator = estimator

    def score(self, sample: VariantSample) -> Score:
        bins = self.estimator.estimate(sample)
        for pan_bin in bins:
            validate_pan_bin(pan_bin)
        claimed = [pan_bin for pan_bin in bins if not pan_bin.no_claim]
        detail: dict[str, Any] = {
            "estimator": self.estimator.name,
            "estimator_version": self.estimator.version,
            "bins": len(bins),
            "claimed": len(claimed),
            "no_claim": len(bins) - len(claimed),
        }
        if len(claimed) < self.min_claimed_bins:
            detail["reason"] = "too few claimed bins"
            return Score(flagged=None, detail=detail)
        disagree = 0
        for pan_bin in claimed:
            yaw = _finite_number("yaw_rate", pan_bin.yaw_rate)
            pitch = _finite_number("pitch_rate", pan_bin.pitch_rate)
            confidence = _confidence(pan_bin.confidence)
            moving = abs(yaw) >= self.rate_floor or abs(pitch) >= self.rate_floor
            if confidence < self.min_confidence or not moving:
                continue
            if right_stick_idle(sample.rows, pan_bin.mono_ms, pan_bin.mono_ms + BIN_MS, self.stick_deadzone):
                disagree += 1
        detail["disagree_bins"] = disagree
        flagged = disagree >= self.min_disagree_bins and disagree * 2 >= len(claimed)
        return Score(flagged=flagged, detail=detail)

    def identity(self) -> ScorerIdentity:
        return source_identity(self, self.estimator)


class EventPressAgreementScorer:
    """Example scorer over video events and the logged presses.

    A claimed ``stick_action`` with no off-center right stick in the match window,
    or a claimed ``button_action`` with no logged button in that window, is unmatched.
    The scorer flags the variant when unmatched events are at least ``min_unmatched``
    and at least half of the claimed events. Fewer than ``min_claimed`` claimed
    events is an abstention, which is how the no_claim stub scores.

    These numbers are example policy. The verifier does not read them.
    """

    name = "event-press-agreement"
    version = "event-press-agreement/v0"
    min_claimed = 3
    min_confidence = 0.5
    min_unmatched = 3
    window_ms = EVENT_MATCH_WINDOW_MS
    stick_deadzone = STICK_DEADZONE

    def __init__(self, estimator: VideoEventEstimator) -> None:
        self.estimator = estimator

    def score(self, sample: VariantSample) -> Score:
        events = self.estimator.estimate(sample)
        for event in events:
            validate_video_event(event)
        claimed = [event for event in events if not event.no_claim]
        detail: dict[str, Any] = {
            "estimator": self.estimator.name,
            "estimator_version": self.estimator.version,
            "events": len(events),
            "claimed": len(claimed),
            "no_claim": len(events) - len(claimed),
        }
        if len(claimed) < self.min_claimed:
            detail["reason"] = "too few claimed events"
            return Score(flagged=None, detail=detail)
        unmatched = 0
        for event in claimed:
            confidence = _confidence(event.confidence)
            if confidence < self.min_confidence or event.event_kind not in EVENT_KINDS:
                continue
            if not logged_press_matches(sample.rows, event.mono_ms, str(event.event_kind), self.window_ms, self.stick_deadzone):
                unmatched += 1
        detail["unmatched"] = unmatched
        flagged = unmatched >= self.min_unmatched and unmatched * 2 >= len(claimed)
        return Score(flagged=flagged, detail=detail)

    def identity(self) -> ScorerIdentity:
        return source_identity(self, self.estimator)


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
    """Motion rule v3, plus the example pan and event scorers with no_claim stubs."""
    return [
        MotionRuleV3Scorer(),
        PanAgreementScorer(NoClaimPanEstimator()),
        EventPressAgreementScorer(NoClaimEventEstimator()),
    ]
