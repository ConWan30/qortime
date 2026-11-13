"""Video frame rate for the referee's capture clock.

The manifest ``frames`` field is QorCap's live encoder counter, so its fit against ``mono_ms``
(``clock.capture_clock``) is in frames of the recording's own rate. Before this module the referee
assumed 30/1 everywhere. A 60/1 recording then fails the fit's 5 % sanity check, the fit falls back
to ``(0.0, 33.3)``, and challenge-rule v2 and motion timing are wrong.

Measured sources (the media itself):

* the fMP4 the referee holds: video-track ``mdhd`` timescale x sample count / total stated sample
  duration (``trun`` per-sample, else ``tfhd`` default, else ``trex`` default). Sample durations must
  be constant to within 1 timescale tick (min and max). The mean is then snapped to ``N/1`` or
  ``N*1000/1001`` within 0.002 %. The mean, not the mode, so 59.94 at a 90 kHz timescale (durations
  alternating 1501/1502) is exactly 60000/1001. The unsnapped mean is kept alongside the snapped rate;
* for match_index also ffprobe ``r_frame_rate`` of a supplied video and ``frame_clock.jsonl``
  (mean PTS step per file, snapped the same way).

Rules:

* No rate information at all (every bundle recorded before this, and fixtures without sample
  durations) is the legacy 30/1. Verdicts on those bundles do not change.
* Measured sources must agree exactly (no tolerance across files or sources).
* ``bundle.json`` ``frame_rate`` is declared, not measured, and is not in the manifest chain. It only
  cross-checks a measured rate (within 1 %). Declared with nothing measured is UNMEASURED (RateError),
  never the rate. QorCap does not write it today.
* Anything present but unusable, durations that are not constant to within 1 tick, a mean that does
  not snap, or a disagreement raises ``RateError``. Every parse failure is a RateError. Callers fail
  closed: the dependent check is not computed and says why. A measured rate is never replaced by 30,
  and an unknown rate is never a pass.

The string parse is its own, stricter grammar: ``N/D``, ASCII digits, fullmatch after strip, at most 12
digits per group, 1 <= N/D <= 1000, compared as a Fraction. It does not mirror qortime #129's
``hands.sidecar.clip_fps``, which bounds the whole field (<= 32 chars) and so accepts some 26-32 char
forms this rejects. Both fail closed, and they read different fields. Deduping them is a follow-up.
"""
from __future__ import annotations

import array
import re
import struct
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from qor_receipt.review.clock import _boxes, _video_track

LEGACY = Fraction(30, 1)
_RATIONAL = re.compile(r"([0-9]+)/([0-9]+)", re.ASCII)
_MAX_DIGITS = 12
DECLARED_AGREE = Fraction(1, 100)  # declared (bundle.json) vs measured
SNAP_TOL = Fraction(1, 50_000)     # 2e-5: mean rate must be this close to N/1 or N*1000/1001
MIN_SAMPLES = 10                   # fewer stated samples than this is not a measurement
MAX_TRUN_SAMPLES = 65_536          # a single trun above this is refused (RateError), not walked


class RateError(ValueError):
    """The recording's frame rate is present but unusable, or its sources disagree. Never guessed."""


@dataclass(frozen=True)
class Resolved:
    rate: Fraction
    source: str  # "legacy" when no rate information exists
    mean: Fraction | None = None  # unsnapped mean of the measured source; None when legacy

    @property
    def legacy(self) -> bool:
        return self.source == "legacy"


def label(r: Fraction) -> str:
    return f"{r.numerator}/{r.denominator}"


def _valid(r: Fraction) -> bool:
    return 1 <= r <= 1000


def parse_rate(value: Any, where: str = "frame_rate") -> Fraction:
    """``"60/1"`` -> Fraction(60). Anything else -> RateError (fail closed)."""
    m = _RATIONAL.fullmatch(value.strip()) if isinstance(value, str) else None
    if m and len(m.group(1)) <= _MAX_DIGITS and len(m.group(2)) <= _MAX_DIGITS:
        num, den = int(m.group(1)), int(m.group(2))
        if num > 0 and den > 0 and _valid(Fraction(num, den)):
            return Fraction(num, den)
    shown = repr(value)
    shown = shown if len(shown) <= 40 else shown[:37] + "..."
    raise RateError(f"{where} {shown} is not a frame rate (want N/D with 1 <= N/D <= 1000)")


def snap(mean: Fraction, where: str) -> Fraction:
    """A measured mean rate -> exactly N/1 or N*1000/1001 when within SNAP_TOL, else RateError."""
    cands = [Fraction(round(mean)), Fraction(round(mean * Fraction(1001, 1000)) * 1000, 1001)]
    cands = [c for c in cands if c > 0]
    if cands:
        best = min(cands, key=lambda c: (abs(mean / c - 1), c.denominator))
        if abs(mean / best - 1) <= SNAP_TOL and _valid(best):
            return best
    raise RateError(f"{where}: mean rate {float(mean):.6f} fps is not N/1 or N*1000/1001 within 0.002 % "
                    "(variable or unusual rate; frame rate UNMEASURED)")


def _u32(p: bytes, i: int) -> int | None:
    return int.from_bytes(p[i:i + 4], "big") if len(p) >= i + 4 else None


def _trex_default(init: bytes, track: int | None) -> int | None:
    moov = next((p for t, p in _boxes(init) if t == b"moov"), None)
    mvex = next((p for t, p in _boxes(moov or b"") if t == b"mvex"), None)
    for t, p in _boxes(mvex or b""):
        if t == b"trex" and len(p) >= 16 and (track is None or _u32(p, 4) == track):
            return _u32(p, 12)
    return None


class _Tally:
    __slots__ = ("samples", "duration", "min_dur", "max_dur")

    def __init__(self) -> None:
        self.samples = 0
        self.duration = 0
        self.min_dur: int | None = None
        self.max_dur: int | None = None

    def add(self, n: int, total: int, lo: int, hi: int) -> None:
        if n <= 0:
            return
        self.samples += n
        self.duration += total
        self.min_dur = lo if self.min_dur is None else min(self.min_dur, lo)
        self.max_dur = hi if self.max_dur is None else max(self.max_dur, hi)


def _sample_durations(payload: bytes, start: int, count: int, per: int) -> array.array:
    """Big-endian u32 sample durations from a ``trun``. ``per`` is the bytes per sample row."""
    if per == 4:
        arr = array.array("I", payload[start:start + 4 * count])
        if sys.byteorder == "little":
            arr.byteswap()
        return arr
    arr = array.array("I")
    arr.extend(d for (d,) in struct.iter_unpack(">" + "I" + "x" * (per - 4), payload[start:start + per * count]))
    return arr


def _add_fragment(tally: _Tally, fragment: bytes, track: int | None, trex_dur: int | None) -> None:
    """Add the video ``traf`` sample count and stated duration of one fragment. Nothing is materialised.

    A ``trun`` with no duration source (no per-sample durations, no tfhd or trex default) is skipped.
    A sample count larger than the bytes present can carry (per-sample fields in the ``trun``, or one
    byte of media data per sample in the fragment) is a RateError, so a crafted u32 count costs nothing.
    A count above ``MAX_TRUN_SAMPLES`` is a RateError without walking the rows. Per-sample durations
    are read in bulk (``array`` / ``struct``), not one Python iteration per sample.

    Parsing stops at the first ``mdat``, so a segment with several moof/mdat pairs contributes only
    its first moof. For a constant rate the value is unchanged; there are just fewer samples.
    """
    budget = len(fragment)
    for t, moof in _boxes(fragment):
        if t == b"mdat":
            break
        if t != b"moof":
            continue
        for t2, traf in _boxes(moof):
            if t2 != b"traf":
                continue
            tid = default = None
            truns: list[bytes] = []
            for t3, p in _boxes(traf):
                if t3 == b"tfhd" and len(p) >= 8:
                    flags = int.from_bytes(p[1:4], "big")
                    tid = _u32(p, 4)
                    i = 8 + (8 if flags & 0x01 else 0) + (4 if flags & 0x02 else 0)
                    if flags & 0x08:
                        default = _u32(p, i)
                elif t3 == b"trun":
                    truns.append(p)
            if track is not None and tid != track:
                continue
            fallback = default if default is not None else trex_dur
            for p in truns:
                if len(p) < 8:
                    continue
                flags = int.from_bytes(p[1:4], "big")
                count = _u32(p, 4) or 0
                i = 8 + (4 if flags & 0x01 else 0) + (4 if flags & 0x04 else 0)
                if count <= 0:
                    continue
                if count > MAX_TRUN_SAMPLES:
                    raise RateError(f"trun states {count} samples (cap {MAX_TRUN_SAMPLES})")
                if flags & 0x100:
                    per = 4 * sum(1 for f in (0x100, 0x200, 0x400, 0x800) if flags & f)
                    if count > max(0, (len(p) - i) // per):
                        raise RateError(f"trun states {count} samples but carries fields for {max(0, (len(p) - i) // per)}")
                    durs = _sample_durations(p, i, count, per)
                    tally.add(count, int(sum(durs)), int(min(durs)), int(max(durs)))
                elif fallback is not None:
                    if count > budget:
                        raise RateError(f"trun states {count} samples in a {budget}-byte fragment")
                    tally.add(count, count * fallback, fallback, fallback)
                # else: no duration source for this run; skipped (no information)


def _fmp4_mean(init: bytes, fragments: Iterable[bytes | None]) -> Fraction | None:
    """Unsnapped mean rate of the video track, or None when no duration is stated.

    RateError when the stated durations are unusable: fewer than MIN_SAMPLES samples, a zero total,
    durations not constant to within 1 timescale tick, or any parse failure.
    """
    try:
        track, timescale = _video_track(init or b"")
        if not timescale:
            return None
        trex_dur = _trex_default(init, track)
        tally = _Tally()
        for frag in fragments:
            if frag:
                _add_fragment(tally, frag, track, trex_dur)
    except RateError:
        raise
    except Exception as e:  # crafted boxes: fail closed, never a crash
        raise RateError(f"fMP4 rate parse error: {type(e).__name__}") from e
    if tally.samples == 0:
        return None
    if tally.samples < MIN_SAMPLES or tally.duration <= 0:
        raise RateError(f"fMP4 states {tally.samples} sample(s) with total duration {tally.duration}: not a measurable rate")
    if tally.min_dur is None or tally.max_dur is None or tally.max_dur - tally.min_dur > 1:
        raise RateError(
            f"fMP4 sample durations are not constant: min {tally.min_dur} max {tally.max_dur} ticks "
            "(more than 1 timescale tick apart)"
        )
    return Fraction(timescale * tally.samples, tally.duration)


def fmp4_rate(init: bytes, fragments: Iterable[bytes | None]) -> Fraction | None:
    """Rate of the video track over ``fragments`` (streamed, one at a time).

    None when the init has no video timescale or no fragment states a sample duration (no
    information; the legacy default applies upstream). RateError for a stated rate that is unusable:
    fewer than MIN_SAMPLES samples, a zero total, durations not constant to within 1 tick, a mean
    that does not snap, or any parse failure.
    """
    mean = _fmp4_mean(init, fragments)
    return None if mean is None else snap(mean, "fMP4 video sample durations")


def resolve(measured: list[tuple[str, Fraction]], declared: list[tuple[str, Fraction]] = (),
            mean: Fraction | None = None) -> Resolved:
    """One rate. Measured sources must be equal; declared ones only cross-check (1 %); none -> legacy."""
    if not measured:
        if declared:
            where, r = declared[0]
            raise RateError(f"{where} {label(r)} is declared only; no measured rate to check it against")
        return Resolved(LEGACY, "legacy")
    where0, r0 = measured[0]
    for where, r in measured[1:]:
        if r != r0:
            raise RateError(f"mixed frame rates: {where0} says {label(r0)} but {where} says {label(r)}")
    for where, r in declared:
        if abs(r / r0 - 1) > DECLARED_AGREE:
            raise RateError(f"mixed frame rates: {where0} says {label(r0)} but {where} says {label(r)}")
    return Resolved(r0, where0 if len(measured) == 1 else f"{where0} (+{len(measured) - 1} agreeing)", mean)


def declared_rate(meta: Any) -> list[tuple[str, Fraction]]:
    if isinstance(meta, dict) and "frame_rate" in meta:
        return [("bundle.json frame_rate", parse_rate(meta["frame_rate"], "bundle.json frame_rate"))]
    return []


def bundle_rate(meta: dict[str, Any], media: Iterable[tuple[str, bytes, Iterable[bytes | None]]]) -> Resolved:
    """The rate for a referee review. ``media`` is ``(file, init bytes, fragments)`` per file; the
    fragments may be a generator so only one is in memory at a time."""
    measured: list[tuple[str, Fraction]] = []
    unsnapped: Fraction | None = None
    for name, init, frags in media:
        mean = _fmp4_mean(init, frags)
        if mean is not None:
            measured.append((f"{name} fMP4 sample durations", snap(mean, "fMP4 video sample durations")))
            if unsnapped is None:
                unsnapped = mean
    return resolve(measured, declared_rate(meta), mean=unsnapped)


def frame_clock_rates(rows: Iterable[Any]) -> list[tuple[str, Fraction]]:
    """Per file, 90000 x steps / total PTS step over consecutive frames of ``frame_clock.jsonl``, snapped.

    Like ``align.rs`` ``frame_clock_rate`` only steps where ``frame`` advances by one and the PTS grows
    count, and fewer than 10 steps is no rate for that file. Unlike it, the mean step is used (then
    snapped), not the mode, so 59.94 at 90 kHz is exact; a mean that does not snap is a RateError.
    """
    by_file: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("type") not in (None, "frame"):
            continue
        f, fr, pts = r.get("file"), r.get("frame"), r.get("pts_90k")
        if isinstance(f, str) and type(fr) is int and type(pts) is int and pts >= 0:
            by_file.setdefault(f, []).append((fr, pts))
    out: list[tuple[str, Fraction]] = []
    for f, rows_f in by_file.items():
        steps = total = 0
        for (f0, p0), (f1, p1) in zip(rows_f, rows_f[1:]):
            if f1 == f0 + 1 and p1 > p0:
                steps += 1
                total += p1 - p0
        if steps < 10:
            continue
        out.append((f"frame_clock.jsonl {f}", snap(Fraction(90_000 * steps, total), f"frame_clock.jsonl {f}")))
    return out
