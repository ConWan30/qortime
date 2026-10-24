"""Claim gate for any future video-vs-log mismatch detector. It has NO allow path.

ADVISORY ONLY, NO ALLOW PATH. ``mismatch_claim_allowed`` always returns a
``MismatchGateResult`` whose ``allowed`` is False and whose ``ceiling`` is
``"PARTIAL"``. It never trusts a caller-built status, coverage, or calibration
object: anyone can construct those, so they cannot prove anything about a bundle.
The inputs are still validated, with exact-type checks (subclasses of ``str``,
``int``, ``float`` and enums are rejected), so the ``reason`` code says which
condition a future allow path would have failed first.

A detector that compares an on-screen event with the USB HID log (skill move,
user catch, controlled-player motion with no matching logged press) must call
this gate and treat ``allowed=False`` as ``no_claim``.

What a real allow path will need (each one is missing today):

1. The bundle itself, not a status string. The gate must take the bundle bytes
   or path, run the frame_bind verifier (qorcap PR 75 design: ``frames.jsonl``
   plus a running hash in checkpoints) and recompute the digest.
   Frame binding proves that frames were not altered or reordered after
   recording. It does NOT prove press-to-frame timing or alignment.
2. A verifiable encode-latency calibration bound to that bundle and to its
   measurement. Frame ``mono_ms`` is stamped after encoding and pipe buffering,
   while HID rows are stamped at the USB read, so every press-to-frame gap
   includes encode latency that has not been measured. A hand-built
   ``EncodeLatencyCalibration`` is not verifiable and never allows.
3. Coverage and align_gaps taken from the verified frame_bind result. An event
   inside an align_gap is no_claim even when a caller's coverage overlaps it.

TODO(schemas PR, QorArch): that PR exposes ``verify_frame_bind(bundle)`` as the
entry point; ``FrameBindResult`` is only its return value and proves nothing on
its own (anyone can build one). When the PR is on main, change the gate to take
the bundle bytes or path and call ``verify_frame_bind`` on the bundle itself, so
the digest is recomputed inside the gate. Never accept a ``FrameBindResult`` (or
any status) from the caller. Do not build against either before it is on main.
An allow path may return only in a separate reviewed PR that also adds a
bundle-bound, verifiable encode-latency calibration. Even then, the most a
mismatch signal may do is hold a verdict at PARTIAL; it never raises one.

Observation only, not a limit or threshold: on the match 170123 highlight clips
(one session, 3 clips) two independent clip-to-capture-clock alignments disagreed
by 120-719 ms. Those numbers and the 1.5x margin are not read by the gate and
get replaced once bundles carry a verified frame_bind result and a verified
calibration.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from qor_receipt.review.frame_bind import FRAME_BIND_STATUSES

# Observed clip/capture clock skew, match 170123, single session, 3 highlight clips:
# (min, max) in ms. A measurement, not a limit or threshold.
OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS = (120, 719)
# Observed max skew (3 clips). Not a limit, not a threshold.
OBSERVED_MAX_CLIP_CLOCK_SKEW_MS = 719
# Margin someone might apply to the observed max. A guess, not a bound.
SKEW_MARGIN_FACTOR = 1.5
# Both observation constants get replaced once bundles carry a verified frame_bind
# result and a verified encode-latency calibration. The gate does not read them.

# Status vocabulary is FRAME_BIND_STATUSES from qor_receipt.review.frame_bind.
# Only the first value would ever pass condition 1; enums and str subclasses do not.

# Sanity bounds used only to pick a reason code. Guesses, not bounds anyone verified.
MIN_CALIBRATION_SAMPLES = 30
MAX_PLAUSIBLE_ENCODE_LATENCY_MS = 2_000.0

CEILING = "PARTIAL"

# Reason codes, in the order the gate checks them. The last one is returned when
# every caller-supplied input looks well formed: they are still caller-supplied.
R_STATUS_NOT_VERIFIED = "frame_bind_not_verified"
R_CALIBRATION_MISSING = "encode_latency_calibration_missing"
R_CALIBRATION_INVALID = "encode_latency_calibration_invalid"
R_VIDEO_TIME_MISSING = "video_time_missing"
R_LOG_TIME_MISSING = "log_time_missing"
R_TIME_INVALID = "event_time_invalid"
R_COVERAGE_MISSING = "coverage_missing"
R_COVERAGE_INVALID = "coverage_invalid"
R_ALIGN_GAPS_INVALID = "align_gaps_invalid"
R_COVERAGE_OVERLAPS_GAP = "coverage_overlaps_align_gap"
R_EVENT_IN_GAP = "event_in_align_gap"
R_EVENT_OUTSIDE_COVERAGE = "event_outside_coverage"
R_NO_BUNDLE_VERIFIER = "no_bundle_verifier"
REASONS = (
    R_STATUS_NOT_VERIFIED, R_CALIBRATION_MISSING, R_CALIBRATION_INVALID, R_VIDEO_TIME_MISSING,
    R_LOG_TIME_MISSING, R_TIME_INVALID, R_COVERAGE_MISSING, R_COVERAGE_INVALID, R_ALIGN_GAPS_INVALID,
    R_COVERAGE_OVERLAPS_GAP, R_EVENT_IN_GAP, R_EVENT_OUTSIDE_COVERAGE, R_NO_BUNDLE_VERIFIER,
)


@dataclass(frozen=True)
class EncodeLatencyCalibration:
    """Caller-described encode latency. Not verifiable, so it can never allow a claim.

    Kept only so callers can say what they have and get an informative reason.
    """

    source: str
    n_samples: int
    p50_ms: float
    p99_ms: float


class MismatchGateResult:
    """Frozen gate result. ``allowed`` is always False and ``ceiling`` is ``"PARTIAL"``.

    ``allowed`` is a read-only property, not a stored field, and the object is
    falsy, so ``if gate(...):`` never takes the claim branch.
    """

    __slots__ = ("_reason",)

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        object.__setattr__(self, "_reason", reason)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("MismatchGateResult is frozen")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("MismatchGateResult is frozen")

    @property
    def allowed(self) -> bool:
        return False

    @property
    def no_claim(self) -> bool:
        return not self.allowed

    @property
    def ceiling(self) -> str:
        return CEILING

    @property
    def reason(self) -> str:
        return self._reason

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        return type(other) is MismatchGateResult and other._reason == self._reason

    def __hash__(self) -> int:
        return hash(("MismatchGateResult", self._reason))

    def __repr__(self) -> str:
        return f"MismatchGateResult(allowed=False, reason={self._reason!r}, ceiling={CEILING!r})"

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": False, "no_claim": self.no_claim, "reason": self._reason, "ceiling": CEILING}


def _exact_int(value: Any) -> bool:
    return type(value) is int


def _exact_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _calibration_reason(cal: Any) -> str | None:
    if cal is None:
        return R_CALIBRATION_MISSING
    if type(cal) is not EncodeLatencyCalibration:
        return R_CALIBRATION_INVALID
    source, n, p50, p99 = cal.source, cal.n_samples, cal.p50_ms, cal.p99_ms
    if type(source) is not str or not source.strip():
        return R_CALIBRATION_INVALID
    if not _exact_int(n) or n < MIN_CALIBRATION_SAMPLES:
        return R_CALIBRATION_INVALID
    if not (_exact_number(p50) and _exact_number(p99)):
        return R_CALIBRATION_INVALID
    if not (0 < p50 <= p99 <= MAX_PLAUSIBLE_ENCODE_LATENCY_MS):
        return R_CALIBRATION_INVALID
    return None


def _stretches(value: Any) -> tuple[tuple[int, int], ...] | None:
    """Exact list/tuple of exact (int, int) pairs, non-negative, start < end, sorted, non-overlapping."""
    if type(value) not in (list, tuple):
        return None
    out: list[tuple[int, int]] = []
    for item in value:
        if type(item) not in (list, tuple) or len(item) != 2:
            return None
        start, end = item
        if not (_exact_int(start) and _exact_int(end)) or start < 0 or start >= end:
            return None
        if out and start < out[-1][1]:
            return None
        out.append((start, end))
    return tuple(out)


def _inside(t: int, stretches: Sequence[tuple[int, int]]) -> bool:
    return any(start <= t < end for start, end in stretches)


def _overlaps(a: Sequence[tuple[int, int]], b: Sequence[tuple[int, int]]) -> bool:
    return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in b)


def mismatch_claim_allowed(
    frame_bind_status: Any = None,
    *,
    coverage: Any = None,
    align_gaps: Any = None,
    encode_latency: Any = None,
    video_ms: Any = None,
    log_ms: Any = None,
) -> MismatchGateResult:
    """Always ``allowed=False``, ``ceiling="PARTIAL"``. ``reason`` names the first unmet condition.

    Inputs are caller-supplied and therefore untrusted. When all of them look
    well formed the reason is ``no_bundle_verifier``: nothing in qortime verifies
    a bundle's frame_bind chain or a bundle-bound calibration yet.
    """
    if type(frame_bind_status) is not str or frame_bind_status != FRAME_BIND_STATUSES[0]:
        return MismatchGateResult(R_STATUS_NOT_VERIFIED)
    cal_reason = _calibration_reason(encode_latency)
    if cal_reason is not None:
        return MismatchGateResult(cal_reason)
    if video_ms is None:
        return MismatchGateResult(R_VIDEO_TIME_MISSING)
    if log_ms is None:
        return MismatchGateResult(R_LOG_TIME_MISSING)
    if not (_exact_int(video_ms) and _exact_int(log_ms)) or video_ms < 0 or log_ms < 0:
        return MismatchGateResult(R_TIME_INVALID)
    if coverage is None:
        return MismatchGateResult(R_COVERAGE_MISSING)
    cover = _stretches(coverage)
    if cover is None:
        return MismatchGateResult(R_COVERAGE_INVALID)
    if not cover:
        return MismatchGateResult(R_COVERAGE_MISSING)
    gaps = () if align_gaps is None else _stretches(align_gaps)
    if gaps is None:
        return MismatchGateResult(R_ALIGN_GAPS_INVALID)
    if _inside(video_ms, gaps) or _inside(log_ms, gaps):
        return MismatchGateResult(R_EVENT_IN_GAP)
    if _overlaps(cover, gaps):
        return MismatchGateResult(R_COVERAGE_OVERLAPS_GAP)
    if not (_inside(video_ms, cover) and _inside(log_ms, cover)):
        return MismatchGateResult(R_EVENT_OUTSIDE_COVERAGE)
    return MismatchGateResult(R_NO_BUNDLE_VERIFIER)
