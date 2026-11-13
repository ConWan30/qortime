"""Video frame rate for the referee's capture clock.

The manifest ``frames`` field is QorCap's live encoder counter, so its fit against ``mono_ms``
(``clock.capture_clock``) is in frames of the recording's own rate. Before this module the referee
assumed 30/1 everywhere. A 60/1 recording then fails the fit's 5 % sanity check, the fit falls back
to ``(0.0, 33.3)``, and challenge-rule v2 and motion timing are wrong.

Measured sources (the media itself):

* the fMP4 the referee holds: video-track ``mdhd`` timescale x sample count / total stated sample
  duration (``trun`` per-sample, else ``tfhd`` default, else ``trex`` default). The base duration
  ``d`` is the most common sample duration. Samples within 1 tick of ``d`` set the rate (snapped to
  ``N/1`` or ``N*1000/1001`` within 0.002 %). Samples within 1 tick of ``k`` times the in-rate mean
  duration, for 2 <= k <= 8, are gaps, allowed up to ``max(1, total // 1000)`` of the samples; a
  shorter final sample per file is exempt. Anything else, or more gaps than that, is a RateError. The mean of the in-rate samples, not the mode, so 59.94
  at a 90 kHz timescale (durations alternating 1501/1502) is exactly 60000/1001. The unsnapped mean
  and the gap count are kept alongside the snapped rate;
* for match_index also ffprobe ``r_frame_rate`` of a supplied video and ``frame_clock.jsonl``
  (mean PTS step per file, snapped the same way).

Rules:

* No rate information at all (every bundle recorded before this, and fixtures without sample
  durations) is the legacy 30/1. Verdicts on those bundles do not change.
* Measured sources must agree exactly (no tolerance across files or sources).
* ``bundle.json`` ``frame_rate`` is declared, not measured, and is not in the manifest chain. It only
  cross-checks a measured rate (within 1 %). Declared with nothing measured is UNMEASURED (RateError),
  never the rate. QorCap does not write it today.
* Anything present but unusable, durations that are not in-rate or allowed gaps, more gaps than
  ``max(1, total // 1000)``, a mean that does not snap, or a disagreement raises ``RateError``. Every parse failure is a
  RateError. Callers fail closed: the dependent check is not computed and says why. A measured rate
  is never replaced by 30, and an unknown rate is never a pass.

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
from collections import Counter
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
MAX_TRUNS_PER_TRAF = 1024          # truns in one traf above this is a RateError
MAX_BOXES_PER_MOOF = 1024          # child boxes in one moof above this is a RateError
MAX_GAP_K = 8                      # a gap is a sample within 1 tick of k * in-rate mean, 2 <= k <= 8
MAX_NON_TRUN_TRAF_CHILDREN = 1024  # non-trun children in one traf above this is a RateError
MAX_BOXES_PER_FRAGMENT = 1024      # top-level boxes in one fragment above this is a RateError


class RateError(ValueError):
    """The recording's frame rate is present but unusable, or its sources disagree. Never guessed."""


@dataclass(frozen=True)
class Resolved:
    rate: Fraction
    source: str  # "legacy" when no rate information exists
    mean: Fraction | None = None  # unsnapped mean of the measured source; None when legacy
    gaps: int = 0  # in-rate-adjacent holes (k*d, 2 <= k <= 8) accepted in the measurement

    @property
    def legacy(self) -> bool:
        return self.source == "legacy"


@dataclass(frozen=True)
class Measured:
    """Unsnapped mean of in-rate samples, plus accepted gap count, for one fMP4 file."""

    mean: Fraction
    gaps: int = 0
    last_dur: int | None = None
    timescale: int | None = None
    base_dur: int | None = None


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


def _u32(p: bytes | memoryview, i: int) -> int | None:
    return int.from_bytes(p[i:i + 4], "big") if len(p) >= i + 4 else None


def _trex_default(init: bytes, track: int | None) -> int | None:
    moov = next((p for t, p in _boxes(init) if t == b"moov"), None)
    mvex = next((p for t, p in _boxes(moov or b"") if t == b"mvex"), None)
    for t, p in _boxes(mvex or b""):
        if t == b"trex" and len(p) >= 16 and (track is None or _u32(p, 4) == track):
            return _u32(p, 12)
    return None


class _Tally:
    """Duration histogram plus the last stated sample, used to classify in-rate vs gaps."""

    __slots__ = ("samples", "counts", "last_dur")

    def __init__(self) -> None:
        self.samples = 0
        self.counts: dict[int, int] = {}
        self.last_dur: int | None = None

    def add_const(self, n: int, dur: int) -> None:
        if n <= 0:
            return
        self.samples += n
        self.counts[dur] = self.counts.get(dur, 0) + n
        self.last_dur = dur

    def add_durs(self, durs: array.array) -> None:
        if not durs:
            return
        self.samples += len(durs)
        self.counts.update(Counter(durs))
        self.last_dur = int(durs[-1])


def _sample_durations(payload: bytes | memoryview, start: int, count: int, per: int) -> array.array:
    """Big-endian u32 sample durations from a ``trun``. ``per`` is the bytes per sample row.

    The duration rows are copied into a compact ``bytes`` first. ``array('I')`` over a memoryview
    of the parent fragment is not safe when that view is not 4-byte aligned in the original buffer.
    """
    chunk = bytes(payload[start:start + per * count])
    if per == 4:
        arr = array.array("I", chunk)
        if sys.byteorder == "little":
            arr.byteswap()
        return arr
    arr = array.array("I")
    arr.extend(d for (d,) in struct.iter_unpack(">" + "I" + "x" * (per - 4), chunk))
    return arr


def _gap_budget(total: int) -> int:
    """A short file may have one gap; otherwise at most 0.1 % of samples (integer)."""
    return max(1, total // 1000)


def _is_gap(dur: int, mean: Fraction) -> bool:
    """True when ``dur`` is within 1 tick of ``k * mean`` for 2 <= k <= MAX_GAP_K.

    ``mean`` is the in-rate mean duration (the average of samples within 1 tick of the modal
    ``d``), not ``d`` itself, so a k-frame hole at 59.94 / 90 kHz (mean 1501.5) is accepted.
    """
    if mean <= 0:
        return False
    for k in range(2, MAX_GAP_K + 1):
        if abs(dur - k * mean) <= 1:
            return True
    return False


def _add_trun(tally: _Tally, p: bytes | memoryview, fallback: int | None, budget: int) -> None:
    if len(p) < 8:
        return
    flags = int.from_bytes(p[1:4], "big")
    count = _u32(p, 4) or 0
    i = 8 + (4 if flags & 0x01 else 0) + (4 if flags & 0x04 else 0)
    if count <= 0:
        return
    if count > MAX_TRUN_SAMPLES:
        raise RateError(f"trun states {count} samples (cap {MAX_TRUN_SAMPLES})")
    if flags & 0x100:
        per = 4 * sum(1 for f in (0x100, 0x200, 0x400, 0x800) if flags & f)
        if count > max(0, (len(p) - i) // per):
            raise RateError(f"trun states {count} samples but carries fields for {max(0, (len(p) - i) // per)}")
        tally.add_durs(_sample_durations(p, i, count, per))
    elif fallback is not None:
        if count > budget:
            raise RateError(f"trun states {count} samples in a {budget}-byte fragment")
        tally.add_const(count, fallback)
    # else: no duration source for this run; skipped (no information)


def _add_fragment(tally: _Tally, fragment: bytes, track: int | None, trex_dur: int | None) -> None:
    """Add the video ``traf`` sample durations of one fragment. Nothing is materialised.

    A ``trun`` with no duration source (no per-sample durations, no tfhd or trex default) is skipped.
    A sample count larger than the bytes present can carry (per-sample fields in the ``trun``, or one
    byte of media data per sample in the fragment) is a RateError, so a crafted u32 count costs nothing.
    A count above ``MAX_TRUN_SAMPLES`` is a RateError without walking the rows. More than
    ``MAX_TRUNS_PER_TRAF`` truns in a traf, more than ``MAX_NON_TRUN_TRAF_CHILDREN`` other children
    in a traf, more than ``MAX_BOXES_PER_MOOF`` child boxes in a moof, or more than
    ``MAX_BOXES_PER_FRAGMENT`` top-level boxes in a fragment, is a RateError. Per-sample durations
    are read in bulk (``array`` / ``struct``). Truns are parsed on a second walk of the traf so they
    are not collected into a list (tfhd is first by spec, but a crafted order still sees it).

    Parsing stops at the first ``mdat``, so a segment with several moof/mdat pairs contributes only
    its first moof. For a constant rate the value is unchanged; there are just fewer samples.
    """
    budget = len(fragment)
    n_top = 0
    for t, moof in _boxes(fragment):
        n_top += 1
        if n_top > MAX_BOXES_PER_FRAGMENT:
            raise RateError(f"fragment has more than {MAX_BOXES_PER_FRAGMENT} boxes")
        if t == b"mdat":
            break
        if t != b"moof":
            continue
        n_boxes = 0
        for t2, traf in _boxes(moof):
            n_boxes += 1
            if n_boxes > MAX_BOXES_PER_MOOF:
                raise RateError(f"moof has more than {MAX_BOXES_PER_MOOF} boxes")
            if t2 != b"traf":
                continue
            tid = default = None
            n_truns = n_other = 0
            for t3, p in _boxes(traf):
                if t3 == b"trun":
                    n_truns += 1
                    if n_truns > MAX_TRUNS_PER_TRAF:
                        raise RateError(f"traf has more than {MAX_TRUNS_PER_TRAF} truns")
                else:
                    n_other += 1
                    if n_other > MAX_NON_TRUN_TRAF_CHILDREN:
                        raise RateError(f"traf has more than {MAX_NON_TRUN_TRAF_CHILDREN} non-trun boxes")
                if t3 == b"tfhd" and len(p) >= 8:
                    flags = int.from_bytes(p[1:4], "big")
                    tid = _u32(p, 4)
                    i = 8 + (8 if flags & 0x01 else 0) + (4 if flags & 0x02 else 0)
                    if flags & 0x08:
                        default = _u32(p, i)
            if track is not None and tid != track:
                continue
            fallback = default if default is not None else trex_dur
            for t3, p in _boxes(traf):
                if t3 == b"trun":
                    _add_trun(tally, p, fallback, budget)


def _fmp4_measure(init: bytes, fragments: Iterable[bytes | None]) -> Measured | None:
    """Unsnapped in-rate mean and accepted gap count, or None when no duration is stated.

    RateError when the stated durations are unusable: fewer than MIN_SAMPLES in-rate samples, a zero
    total, a duration that is not within 1 tick of the base ``d`` or of ``k`` times the in-rate mean
    (2..8), more gaps than ``max(1, total // 1000)``, or any parse failure. A final sample shorter
    than ``d`` is exempt.
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
    if not tally.counts:
        raise RateError(f"fMP4 states {tally.samples} sample(s) with no durations: not a measurable rate")
    # most common duration; on a tie, the shorter one (so 1501 beats 1502 when counts are equal)
    d = max(tally.counts, key=lambda dur: (tally.counts[dur], -dur))
    counts = dict(tally.counts)
    if tally.last_dur is not None and tally.last_dur < d:
        ld = tally.last_dur
        left = counts.get(ld, 0) - 1
        if left > 0:
            counts[ld] = left
        else:
            counts.pop(ld, None)
    inrate_n = inrate_dur = 0
    leftover: list[tuple[int, int]] = []
    for dur, n in counts.items():
        if abs(dur - d) <= 1:
            inrate_n += n
            inrate_dur += n * dur
        else:
            leftover.append((dur, n))
    mean_dur = Fraction(inrate_dur, inrate_n) if inrate_n and inrate_dur > 0 else Fraction(d, 1)
    gaps = 0
    for dur, n in leftover:
        if inrate_n and _is_gap(dur, mean_dur):
            gaps += n
        else:
            raise RateError(
                f"fMP4 sample durations are not constant: duration {dur} ticks is not within 1 tick "
                f"of base {d} or k times the in-rate mean {float(mean_dur):.3f} (2..{MAX_GAP_K})"
            )
    if inrate_n < MIN_SAMPLES or inrate_dur <= 0:
        raise RateError(
            f"fMP4 states {tally.samples} sample(s) with {inrate_n} in-rate and total duration "
            f"{inrate_dur}: not a measurable rate"
        )
    total = tally.samples
    budget = _gap_budget(total)
    if gaps > budget:
        raise RateError(
            f"fMP4 has {gaps} gap sample(s) in {total} ({100.0 * gaps / total:.3f} %), "
            f"above the budget of {budget}"
        )
    return Measured(Fraction(timescale * inrate_n, inrate_dur), gaps, tally.last_dur, timescale, d)


def fmp4_rate(init: bytes, fragments: Iterable[bytes | None]) -> Fraction | None:
    """Rate of the video track over ``fragments`` (streamed, one at a time).

    None when the init has no video timescale or no fragment states a sample duration (no
    information; the legacy default applies upstream). RateError for a stated rate that is unusable:
    fewer than MIN_SAMPLES in-rate samples, a zero total, a duration that is not an allowed gap,
    more gaps than ``max(1, total // 1000)``, a mean that does not snap, or any parse failure.
    """
    measured = _fmp4_measure(init, fragments)
    return None if measured is None else snap(measured.mean, "fMP4 video sample durations")


def resolve(measured: list[tuple[str, Fraction]], declared: list[tuple[str, Fraction]] = (),
            mean: Fraction | None = None, gaps: int = 0) -> Resolved:
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
    return Resolved(r0, where0 if len(measured) == 1 else f"{where0} (+{len(measured) - 1} agreeing)", mean, gaps)


def declared_rate(meta: Any) -> list[tuple[str, Fraction]]:
    if isinstance(meta, dict) and "frame_rate" in meta:
        return [("bundle.json frame_rate", parse_rate(meta["frame_rate"], "bundle.json frame_rate"))]
    return []


def bundle_rate(meta: dict[str, Any], media: Iterable[tuple[str, bytes, Iterable[bytes | None]]]) -> Resolved:
    """The rate for a referee review. ``media`` is ``(file, init bytes, fragments)`` per file; the
    fragments may be a generator so only one is in memory at a time."""
    measured: list[tuple[str, Fraction]] = []
    unsnapped: Fraction | None = None
    gaps = 0
    for name, init, frags in media:
        got = _fmp4_measure(init, frags)
        if got is not None:
            measured.append((f"{name} fMP4 sample durations", snap(got.mean, "fMP4 video sample durations")))
            if unsnapped is None:
                unsnapped = got.mean
            gaps += got.gaps
    return resolve(measured, declared_rate(meta), mean=unsnapped, gaps=gaps)


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
