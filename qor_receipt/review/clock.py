"""Presentation-time clock for challenge rule v2.

``manifest.jsonl`` ``mono_ms`` / ``wall_ms`` are written when an fMP4 fragment is hashed, which is
when the next ``moof`` arrives. That stamp is the fragment's close. ``segment_ranges`` already
treats it as an end and uses the previous close as this fragment's start. On a QorCap capture that
start is still about one fragment (~1.6 s) after the first frame inside it, because the remuxer
lags the capture clock.

Rule v2 dates a decoded frame from the fragment's base media decode time (``tfdt``, the first
sample's PTS) through the manifest frame fit — the same fit ``align.rs`` uses for the input proof.
A manifest whose ``frames`` field does not advance (the synthetic review fixture) keeps PTS as the
mono clock. A fragment with no ``tfdt`` keeps the ``segment_ranges`` time, which fails closed:
the lagged stamp still sits past the 2500 ms ceiling.

Rule v1 does not use this module. Its frames stay on ``segment_ranges``.
"""
from __future__ import annotations

import math
from typing import Any


def _boxes(data: bytes | memoryview):
    """Iterate child boxes as ``(type, payload)`` without copying the payload.

    ``type`` is a 4-byte ``bytes`` key. ``payload`` is a memoryview over the original buffer
    (or a memoryview slice), so callers do not pay a copy per box. The iterator walks by
    offsets; it does not build a list.
    """
    view = data if isinstance(data, memoryview) else memoryview(data)
    i, n = 0, len(view)
    while i + 8 <= n:
        size = int.from_bytes(view[i:i + 4], "big")
        typ = bytes(view[i + 4:i + 8])
        hdr = 8
        if size == 1 and i + 16 <= n:
            size = int.from_bytes(view[i + 8:i + 16], "big")
            hdr = 16
        if size < hdr or i + size > n:
            return
        yield typ, view[i + hdr:i + size]
        i += size


def _video_track(init: bytes) -> tuple[int | None, int | None]:
    """(track id, media timescale) of the video track in an fMP4 init segment."""
    moov = next((p for t, p in _boxes(init) if t == b"moov"), None)
    if not moov:
        return None, None
    for typ, trak in _boxes(moov):
        if typ != b"trak":
            continue
        tid = ts = None
        vide = False
        for t, p in _boxes(trak):
            if t == b"tkhd" and p:
                off = 20 if p[0] == 1 else 12
                if len(p) >= off + 4:
                    tid = int.from_bytes(p[off:off + 4], "big")
            elif t == b"mdia":
                for t2, p2 in _boxes(p):
                    if t2 == b"hdlr" and len(p2) >= 12 and p2[8:12] == b"vide":
                        vide = True
                    elif t2 == b"mdhd" and p2:
                        off = 20 if p2[0] == 1 else 12
                        if len(p2) >= off + 4:
                            ts = int.from_bytes(p2[off:off + 4], "big")
        if vide and tid and ts:
            return tid, ts
    return None, None


def fragment_pts0_ms(init: bytes, fragment: bytes) -> float | None:
    """PTS of the first sample in an fMP4 fragment, in milliseconds. None when the boxes are absent.

    The video track is selected on purpose. An audio ``traf`` in the same ``moof`` has its own
    timescale and must not date the picture.
    """
    tid, timescale = _video_track(init)
    if not timescale:
        return None
    moof = next((p for t, p in _boxes(fragment) if t == b"moof"), None)
    if not moof:
        return None
    found: list[tuple[int | None, int]] = []
    for typ, traf in _boxes(moof):
        if typ != b"traf":
            continue
        track = base = None
        for t, p in _boxes(traf):
            if t == b"tfhd" and len(p) >= 8:
                track = int.from_bytes(p[4:8], "big")
            elif t == b"tfdt" and len(p) >= 8:
                base = int.from_bytes(p[4:12], "big") if p[0] == 1 and len(p) >= 12 else int.from_bytes(p[4:8], "big")
        if base is not None:
            found.append((track, base))
    if not found:
        return None
    base = next((b for track, b in found if tid is not None and track == tid), None)
    if base is None:
        base = found[0][1]
    return base * 1000.0 / timescale


def capture_clock(segments: list[dict[str, Any]], fps: float = 30.0) -> tuple[float, float]:
    """(mono_ms of video PTS 0, ms per frame) from the manifest, for a capture counter running at ``fps``.

    Media segments whose ``frames`` count advances are the live capture counter at each close.
    Fitting that against ``mono_ms`` is the clock in ``align.rs`` ``file_clocks``: frame 0 of the
    file, extrapolated back from the closes. A manifest that stores a constant ``frames`` (the
    synthetic review fixture) did not use that counter; its PTS is already the mono clock.

    ``fps`` is the video's frame rate (30 by default, which is every caller before qorcap #164's
    ``source.frame_rate``). It sets the nominal ms per frame for the 5 % sanity check and the fallback,
    and the 10 s minimum span (``10 * fps`` frames). At 30 the result is unchanged.

    The means and sums use ``math.fsum`` (correctly rounded). Built-in ``sum`` of floats is
    compensated from Python 3.12 and plain before it, so the same manifest could fit to different
    last bits on 3.11 and 3.13.
    """
    if not (isinstance(fps, (int, float)) and 1.0 <= fps <= 1000.0):
        raise ValueError(f"capture_clock: fps {fps!r} is not a frame rate")
    nominal = 1000.0 / fps
    media = []
    for s in segments:
        if s.get("kind") == "init":
            continue
        fr, mono = s.get("frames"), s.get("mono_ms")
        if isinstance(fr, bool) or isinstance(mono, bool):
            continue
        if isinstance(fr, (int, float)) and isinstance(mono, (int, float)) and fr > 0:
            media.append((float(fr), float(mono)))
    if len(media) >= 5 and media[-1][0] - media[0][0] >= 10 * fps:
        n = float(len(media))
        mx = math.fsum(p[0] for p in media) / n
        my = math.fsum(p[1] for p in media) / n
        sxx = math.fsum((p[0] - mx) ** 2 for p in media)
        sxy = math.fsum((p[0] - mx) * (p[1] - my) for p in media)
        if sxx > 0:
            slope = sxy / sxx
            if abs(slope / nominal - 1.0) <= 0.05:
                return my - slope * mx, slope
    return 0.0, nominal


def frame_mono_ms(clock: tuple[float, float], pts0_ms: float, n: int, hz: float, fps: float | None = None) -> int:
    """Capture mono time of decoded frame ``n`` in a run whose first sample PTS is ``pts0_ms``.

    Media time is ``pts0_ms + n * 1000/hz`` (``hz`` is the decode rate). It is mapped to a frame of
    the recording at ``fps`` (the video's own rate, see ``review.rate``) and through the manifest
    fit. ``fps=None`` keeps the old meaning, one decoded frame per media frame (``fps == hz``),
    which is the challenge-rule v2 clock at 30 fps. Passing ``fps=30.0`` with ``hz=30.0`` is the
    same arithmetic.
    """
    intercept, slope = clock
    rate = hz if fps is None else fps
    frame = (pts0_ms + n * 1000.0 / hz) * (rate / 1000.0)
    return int(round(intercept + slope * frame))


def motion_mono_ms(clock: tuple[float, float], pts0_ms: float, n: int, hz: float, fps: float = 30.0) -> int:
    """Capture mono time of motion-sample frame ``n``.

    Media time is ``pts0_ms + n * 1000/hz``. That time is mapped through the recording's frame at
    ``fps`` (30 by default), the frame the manifest fit uses, so a 10 Hz sample lands on the same
    clock as a 30 Hz frame with the same PTS. At 30 fps this matches ``frame_mono_ms``.
    """
    intercept, slope = clock
    media_ms = pts0_ms + n * 1000.0 / hz
    frame = media_ms / (1000.0 / fps)
    return int(round(intercept + slope * frame))
