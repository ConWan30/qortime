"""Video frame rate for the referee's capture clock.

The manifest ``frames`` field is QorCap's live encoder counter, so its fit against ``mono_ms``
(``clock.capture_clock``) is in frames of the recording's own rate. Before this module the referee
assumed 30/1 everywhere. A 60/1 recording then fails the fit's 5 % sanity check, the fit falls back
to ``(0.0, 33.3)``, and challenge-rule v2 and motion timing are wrong.

The rate comes from the bundle's own media, the way ``align.rs`` ``file_rate`` gets it from the
stream: the video track's sample durations in the fMP4 the referee already holds (``mdhd``
timescale over the modal ``trun`` / ``tfhd`` / ``trex`` sample duration; ffprobe derives
``r_frame_rate`` for an MP4 from the same fields). An optional ``bundle.json`` ``frame_rate``
string (``"60/1"``, ``"60000/1001"``) is also read when present; QorCap does not write it today.

* No rate information at all (every bundle recorded before this, and every fixture without
  sample durations) is the legacy 30/1. Verdicts on those bundles do not change.
* Rate information that is present but unusable, or two sources that disagree by more than 1 %,
  raise ``RateError``. Callers fail closed: the dependent check is not computed and says why.
  A measured rate is never replaced by 30, and an unknown rate is never a pass.

The string parse mirrors qortime #129 ``hands.sidecar.clip_fps`` (``N/D``, ASCII digits, fullmatch
after strip, 1 <= N/D <= 1000). It compares as a ``Fraction`` so a huge numerator is a RateError,
not an OverflowError. Follow-up once #129 is on main: have ``clip_fps`` call ``parse_rate``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from fractions import Fraction
from typing import Any

from qor_receipt.review.clock import _boxes, _video_track

LEGACY = Fraction(30, 1)
_RATIONAL = re.compile(r"([0-9]+)/([0-9]+)", re.ASCII)
_MAX_DIGITS = 12
AGREE = Fraction(1, 100)  # align.rs file_rate: probe and frame clock must agree within 1 %


class RateError(ValueError):
    """The recording's frame rate is present but unusable, or its sources disagree. Never guessed."""


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
    raise RateError(f"{where} {value!r} is not a frame rate (want N/D with 1 <= N/D <= 1000)")


def _u32(p: bytes, i: int) -> int | None:
    return int.from_bytes(p[i:i + 4], "big") if len(p) >= i + 4 else None


def _trex_default(init: bytes, track: int | None) -> int | None:
    moov = next((p for t, p in _boxes(init) if t == b"moov"), None)
    mvex = next((p for t, p in _boxes(moov or b"") if t == b"mvex"), None)
    for t, p in _boxes(mvex or b""):
        if t == b"trex" and len(p) >= 16 and (track is None or _u32(p, 4) == track):
            return _u32(p, 12)
    return None


def _durations(fragment: bytes, track: int | None, trex_dur: int | None) -> list[int]:
    """Sample durations of the video ``traf`` in one ``moof``. Empty when the fragment states none."""
    out: list[int] = []
    for t, moof in _boxes(fragment):
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
                per = 4 * sum(1 for f in (0x100, 0x200, 0x400, 0x800) if flags & f)
                if count <= 0 or per == 0 and fallback is None:
                    continue
                if not flags & 0x100:
                    out.extend([fallback] * count)
                    continue
                for k in range(count):
                    d = _u32(p, i + k * per)
                    if d is None:
                        break
                    out.append(d)
    return out


def fmp4_rate(init: bytes, fragments: Iterable[bytes]) -> Fraction | None:
    """Rate of the video track: timescale / the modal sample duration over ``fragments``.

    None when the init has no video timescale or no fragment states a sample duration (legacy).
    RateError when a duration is stated but gives no usable rate (0, or outside 1..1000 fps).
    Ties between equally common durations take the shorter one, as ``align.rs``
    ``frame_clock_rate`` does.
    """
    track, timescale = _video_track(init)
    if not timescale:
        return None
    trex_dur = _trex_default(init, track)
    counts: dict[int, int] = {}
    for frag in fragments:
        for d in _durations(frag, track, trex_dur):
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    dur = max(counts, key=lambda k: (counts[k], -k))
    if dur <= 0 or not _valid(Fraction(timescale, dur)):
        raise RateError(f"fMP4 video sample duration {dur} at timescale {timescale} is not a frame rate")
    return Fraction(timescale, dur)


def resolve(sources: list[tuple[str, Fraction]]) -> Fraction:
    """One rate from (where, rate) pairs, first source first. None -> LEGACY. >1 % apart -> RateError."""
    if not sources:
        return LEGACY
    where0, r0 = sources[0]
    for where, r in sources[1:]:
        if abs(r / r0 - 1) > AGREE:
            raise RateError(f"mixed frame rates: {where0} says {label(r0)} but {where} says {label(r)}")
    return r0


def bundle_rate(meta: dict[str, Any], media: Iterable[tuple[str, bytes, list[bytes]]]) -> Fraction:
    """The rate for a referee review: fMP4 sample durations per file, then ``bundle.json`` ``frame_rate``.

    ``media`` is ``(file name, init bytes, [fragment bytes])`` for the fragments the referee holds.
    """
    sources: list[tuple[str, Fraction]] = []
    for name, init, frags in media:
        r = fmp4_rate(init, frags)
        if r is not None:
            sources.append((f"{name} fMP4 sample duration", r))
    if isinstance(meta, dict) and "frame_rate" in meta:
        sources.append(("bundle.json frame_rate", parse_rate(meta["frame_rate"], "bundle.json frame_rate")))
    return resolve(sources)


def frame_clock_rates(rows: Iterable[Any]) -> list[tuple[str, Fraction]]:
    """Per file, 90000 / the modal PTS step between consecutive frames of ``frame_clock.jsonl``.

    Mirrors ``align.rs`` ``frame_clock_rate``: only steps where ``frame`` advances by one and the PTS
    grows count, fewer than 10 steps is no rate for that file, ties take the shorter step. A step that
    gives a rate outside 1..1000 fps is a RateError.
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
        counts: dict[int, int] = {}
        for (f0, p0), (f1, p1) in zip(rows_f, rows_f[1:]):
            if f1 == f0 + 1 and p1 > p0:
                counts[p1 - p0] = counts.get(p1 - p0, 0) + 1
        if sum(counts.values()) < 10:
            continue
        step = max(counts, key=lambda k: (counts[k], -k))
        r = Fraction(90_000, step)
        if not _valid(r):
            raise RateError(f"frame_clock.jsonl {f}: PTS step {step} is not a frame rate")
        out.append((f"frame_clock.jsonl {f}", r))
    return out
