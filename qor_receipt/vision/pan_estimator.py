"""Camera-pan estimator: sparse optical flow on CPU, one record per 100 ms bin.

ADVISORY ONLY. This module is not imported by the referee, the review engine, or
the verifier. Its output may feed an advisory right-stick-vs-camera check. That
check can never lift a verdict above PARTIAL and never overrides deterministic
math (hash chains, input proof, motion rule v3, scorebug locks).

Method
------
1. Each frame is converted to grayscale and downscaled to ``work_width`` px wide.
2. Overlay regions (scorebug strip, burned highlight caption, broadcaster bug) are
   masked out, both for feature detection and for where a tracked point lands.
3. Shi-Tomasi corners (``goodFeaturesToTrack``) on the previous frame are tracked
   into the current frame with pyramidal Lucas-Kanade, then tracked back; points
   whose forward-backward error exceeds ``fb_max_err_px`` are dropped.
4. Global motion is the per-axis median of the surviving flow vectors. No RANSAC,
   no random sampling: the estimate is a deterministic order statistic.
   Inliers are vectors within ``inlier_tol_px`` of that median; the residual is the
   median distance of inliers to it.
5. Confidence = inlier_ratio * feature_factor * residual_factor, each in [0, 1].

Units and signs
---------------
``yaw_rate`` and ``pitch_rate`` are **percent of frame width per second** (%W/s).
Both axes are normalised by frame *width* so a pixel means the same on each axis.
No field-of-view assumption is made, so these are not degrees.

* ``yaw_rate > 0``: camera pans right (scene content moves left on screen).
* ``pitch_rate > 0``: camera tilts up (scene content moves down on screen).

A pan of 0.3 frame widths per second is ``yaw_rate == 30.0``.

Fail-closed
-----------
A bin is ``no_claim`` (with null rates and confidence) when any frame pair inside
it is unreadable: decode failure (``None`` frame), a black or flat frame, a scene
cut (masked grayscale histogram jump), a timing gap, too few tracked features,
a low inlier ratio, a high residual, low confidence, or any exception. A bin with
no frame pair at all is also ``no_claim``. Values are never interpolated or filled.

Determinism
-----------
``cv2.setNumThreads(1)`` for the duration of a run (restored afterwards), fixed
parameters, median-based estimation, and rounding of every output float to
``ROUND_DIGITS``. ``estimator_sha256()`` covers this module's source text, the
version string, the parameters, ``cv2.__version__``, and a SHA-256 of
``cv2.getBuildInformation()``, so two OpenCV builds never share a hash. It is
what a harness should record. The tested build is pinned in
``tests/constraints-pan-estimator.txt`` (``PINNED_OPENCV_WHEEL``).

Clock binding gate (applies to any future video-vs-log mismatch detector)
-------------------------------------------------------------------------
A detector that compares an on-screen event with the USB HID log (skill move,
user catch, controlled-player motion with no matching logged press) must call
``mismatch_claim_allowed``. Without per-frame clock binding it returns False for
every event, whatever the spacing: the result is ``no_claim``. Spacing alone
never permits a claim.

Observation only, not a limit or threshold: on the match 170123 highlight
clips (one session, 3 clips), two independent clip-to-capture-clock alignments
(segment-manifest interpolation vs. the checkpoint lock time) disagreed by
120-719 ms. ``OBSERVED_MAX_CLIP_CLOCK_SKEW_MS`` (719) is the observed max skew
(3 clips). ``SKEW_MARGIN_FACTOR`` (1.5) is a guess, not a bound. Neither value
is used by the gate.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ESTIMATOR_NAME = "qorai-optical-flow-pan"
ESTIMATOR_VERSION = "qorai-optical-flow-pan/v1"
BIN_MS = 100
ROUND_DIGITS = 3
RATE_UNITS = "percent_of_frame_width_per_second"

# Tested OpenCV wheel (see tests/constraints-pan-estimator.txt). Not enforced at
# runtime: a different build still runs, but it reports a different sha256.
PINNED_OPENCV_WHEEL = "opencv-python-headless==5.0.0.93"
PINNED_OPENCV_VERSION = "5.0.0"
# sha256(cv2.getBuildInformation()) of that wheel's cp37-abi3-manylinux_2_28_x86_64 build (CI and the box).
PINNED_OPENCV_BUILD_INFO_SHA256 = "0ef08003c31127b3b5d34410ab13483c511e73b99e278b1a8abd6ddcb265699a"

# Observed clip/capture clock skew, match 170123, single session, 3 highlight clips:
# (min, max) in ms across the 3 clips. A measurement, not a limit or threshold.
OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS = (120, 719)
# Observed max skew (3 clips). Not a limit, not a threshold.
OBSERVED_MAX_CLIP_CLOCK_SKEW_MS = 719
# Margin someone might apply to the observed max. A guess, not a bound.
SKEW_MARGIN_FACTOR = 1.5
# Both observation constants get replaced once per-frame clock binding lands; the
# binding itself, not these numbers, must then define any timing tolerance. The
# gate below does not read them.


def mismatch_claim_allowed(per_frame_binding: bool, video_ms: int | None = None, log_ms: int | None = None) -> bool:
    """Gate for any video-vs-log mismatch claim. False means ``no_claim``.

    Without per-frame clock binding (``per_frame_binding is not True``) this is
    always False, whatever ``video_ms`` and ``log_ms`` are: event spacing never
    permits a claim. With binding it returns True and leaves timing tolerance to
    the bound clock; there is no path here that relies on the observed skew.
    ``video_ms`` and ``log_ms`` are accepted only so callers pass them and the
    tests can prove they do not matter.
    """
    del video_ms, log_ms
    return per_frame_binding is True

FracBox = tuple[float, float, float, float]  # x0, x1, y0, y1 as fractions of the full frame


def _scorebug_band() -> FracBox:
    """Bottom scorebug strip from the vendored local_scorebug profile (Madden NFL 27 standard)."""
    try:
        from qor_receipt._vendor.qoresence.vision.local_scorebug.profile import MADDEN27_STANDARD

        return (0.0, 1.0, float(MADDEN27_STANDARD.band_y0), float(MADDEN27_STANDARD.band_y1))
    except Exception:  # pragma: no cover - vendored profile always ships; fall back to the same band
        return (0.0, 1.0, 330 / 360, 1.0)


# Static masks, as full-frame fractions. Measured on 1280x720 QorCap highlight clips
# of match 170123 (Oct 2026): the qorcap-highlights burned caption box sits top-left
# and the EA broadcaster bug top-right. Generous margins on purpose.
SCOREBUG_BAND: FracBox = _scorebug_band()
HIGHLIGHT_CAPTION: FracBox = (0.0, 0.56, 0.0, 0.21)
BROADCAST_BUG: FracBox = (0.88, 1.0, 0.0, 0.10)
DEFAULT_MASKS: tuple[FracBox, ...] = (SCOREBUG_BAND, HIGHLIGHT_CAPTION, BROADCAST_BUG)


@dataclass(frozen=True)
class PanParams:
    work_width: int = 320
    border_frac: float = 0.03
    max_corners: int = 300
    quality_level: float = 0.01
    min_distance: int = 7
    block_size: int = 7
    lk_win: int = 21
    lk_levels: int = 3
    lk_iters: int = 30
    lk_eps: float = 0.01
    fb_max_err_px: float = 1.0
    min_features: int = 30
    full_features: int = 120
    inlier_tol_px: float = 1.5
    min_inlier_ratio: float = 0.5
    max_residual_px: float = 1.0
    min_confidence: float = 0.35
    black_mean: float = 18.0
    flat_std: float = 6.0
    hist_bins: int = 32
    cut_bhattacharyya: float = 0.45
    max_gap_ms: int = 250
    masks: tuple[FracBox, ...] = field(default=DEFAULT_MASKS)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["masks"] = [list(box) for box in self.masks]
        return out


DEFAULT_PARAMS = PanParams()


@dataclass(frozen=True)
class PanBin:
    """Same shape as the red-team contract: ``{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}``."""

    mono_ms: int
    yaw_rate: float | None
    pitch_rate: float | None
    confidence: float | None
    no_claim: bool = False
    reason: str | None = None  # why a bin is no_claim; not part of the contract dict

    def to_dict(self) -> dict[str, Any]:
        return {
            "mono_ms": self.mono_ms,
            "yaw_rate": self.yaw_rate,
            "pitch_rate": self.pitch_rate,
            "confidence": self.confidence,
            "no_claim": self.no_claim,
        }


@dataclass(frozen=True)
class PairResult:
    """Motion between two consecutive frames, attributed to the later frame's bin."""

    mono_ms: int
    ok: bool
    reason: str
    yaw_rate: float | None = None
    pitch_rate: float | None = None
    confidence: float | None = None
    features: int = 0
    inlier_ratio: float | None = None
    residual_px: float | None = None


def opencv_identity() -> dict[str, str]:
    """``cv2.__version__`` plus a SHA-256 of ``cv2.getBuildInformation()`` (compile-time, stable per wheel)."""
    try:
        build = cv2.getBuildInformation()
    except Exception:
        build = "unavailable"
    return {
        "cv2_version": str(cv2.__version__),
        "build_info_sha256": hashlib.sha256(str(build).encode("utf-8")).hexdigest(),
    }


def opencv_matches_pin() -> bool:
    """True on the pinned wheel build. Hash-locked docs only reproduce on that build."""
    ident = opencv_identity()
    return ident["cv2_version"] == PINNED_OPENCV_VERSION and ident["build_info_sha256"] == PINNED_OPENCV_BUILD_INFO_SHA256


def estimator_sha256(params: PanParams = DEFAULT_PARAMS) -> str:
    """SHA-256 over the version, the parameters, this module's source, and the OpenCV build identity."""
    src = Path(__file__).read_bytes()
    blob = b"\n".join([
        ESTIMATOR_VERSION.encode("utf-8"),
        json.dumps(params.to_json(), sort_keys=True, separators=(",", ":")).encode("utf-8"),
        json.dumps(opencv_identity(), sort_keys=True, separators=(",", ":")).encode("utf-8"),
        src,
    ])
    return hashlib.sha256(blob).hexdigest()


def _r(value: float) -> float:
    out = round(float(value), ROUND_DIGITS)
    return 0.0 if out == 0 else out  # no "-0.0"


class _Prepared:
    __slots__ = ("gray", "mask", "hist", "width", "reason")

    def __init__(self, gray, mask, hist, width: int, reason: str | None) -> None:
        self.gray = gray
        self.mask = mask
        self.hist = hist
        self.width = width
        self.reason = reason


class OpticalFlowPanEstimator:
    """Frames in, 100 ms ``PanBin`` records out. See the module docstring for method and units."""

    name = ESTIMATOR_NAME
    version = ESTIMATOR_VERSION

    def __init__(self, params: PanParams = DEFAULT_PARAMS) -> None:
        self.params = params
        self._mask_cache: dict[tuple[int, int], Any] = {}

    @property
    def sha256(self) -> str:
        return estimator_sha256(self.params)

    def identity(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
            "units": RATE_UNITS,
            "bin_ms": BIN_MS,
            "params": self.params.to_json(),
            "opencv": opencv_identity(),
        }

    # -- frame preparation -------------------------------------------------
    def _mask(self, h: int, w: int):
        key = (h, w)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached
        p = self.params
        mask = np.zeros((h, w), dtype=np.uint8)
        bx, by = int(round(w * p.border_frac)), int(round(h * p.border_frac))
        mask[by:h - by, bx:w - bx] = 255
        for x0, x1, y0, y1 in p.masks:
            xa, xb = int(math.floor(x0 * w)), int(math.ceil(x1 * w))
            ya, yb = int(math.floor(y0 * h)), int(math.ceil(y1 * h))
            mask[max(ya, 0):min(yb, h), max(xa, 0):min(xb, w)] = 0
        self._mask_cache[key] = mask
        return mask

    def _prepare(self, frame) -> _Prepared:
        p = self.params
        if frame is None:
            return _Prepared(None, None, None, 0, "decode_failure")
        img = np.asarray(frame)
        if img.ndim == 3 and img.shape[2] == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        elif img.ndim == 3 and img.shape[2] == 4:
            gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        elif img.ndim == 2:
            gray = img
        else:
            return _Prepared(None, None, None, 0, "bad_frame_shape")
        if gray.dtype != np.uint8 or gray.shape[0] < 16 or gray.shape[1] < 16:
            return _Prepared(None, None, None, 0, "bad_frame_shape")
        h, w = gray.shape
        if w != p.work_width:
            nh = max(16, int(round(h * p.work_width / w)))
            gray = cv2.resize(gray, (p.work_width, nh), interpolation=cv2.INTER_AREA)
        h, w = gray.shape
        mask = self._mask(h, w)
        mean, std = cv2.meanStdDev(gray, mask=mask)
        if float(mean[0][0]) < p.black_mean:
            return _Prepared(gray, mask, None, w, "black_frame")
        if float(std[0][0]) < p.flat_std:
            return _Prepared(gray, mask, None, w, "flat_frame")
        hist = cv2.calcHist([gray], [0], mask, [p.hist_bins], [0, 256])
        cv2.normalize(hist, hist, alpha=1.0, norm_type=cv2.NORM_L1)
        return _Prepared(gray, mask, hist, w, None)

    # -- one pair ------------------------------------------------------------
    def _pair(self, a: _Prepared, b: _Prepared, dt_ms: int, mono_ms: int) -> PairResult:
        p = self.params
        if a.reason or b.reason:
            return PairResult(mono_ms, False, b.reason or a.reason or "unreadable")
        if dt_ms <= 0 or dt_ms > p.max_gap_ms:
            return PairResult(mono_ms, False, "timing_gap")
        if a.gray.shape != b.gray.shape:
            return PairResult(mono_ms, False, "frame_size_change")
        dist = float(cv2.compareHist(a.hist, b.hist, cv2.HISTCMP_BHATTACHARYYA))
        if dist > p.cut_bhattacharyya:
            return PairResult(mono_ms, False, "scene_cut")
        pts0 = cv2.goodFeaturesToTrack(
            a.gray, maxCorners=p.max_corners, qualityLevel=p.quality_level,
            minDistance=p.min_distance, mask=a.mask, blockSize=p.block_size,
        )
        if pts0 is None or len(pts0) < p.min_features:
            return PairResult(mono_ms, False, "few_features", features=0 if pts0 is None else len(pts0))
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, p.lk_iters, p.lk_eps)
        win = (p.lk_win, p.lk_win)
        pts1, st1, _ = cv2.calcOpticalFlowPyrLK(a.gray, b.gray, pts0, None, winSize=win, maxLevel=p.lk_levels, criteria=crit)
        if pts1 is None:
            return PairResult(mono_ms, False, "flow_failed")
        back, st2, _ = cv2.calcOpticalFlowPyrLK(b.gray, a.gray, pts1, None, winSize=win, maxLevel=p.lk_levels, criteria=crit)
        if back is None:
            return PairResult(mono_ms, False, "flow_failed")
        p0, p1, pb = pts0.reshape(-1, 2), pts1.reshape(-1, 2), back.reshape(-1, 2)
        good = (st1.reshape(-1) == 1) & (st2.reshape(-1) == 1)
        good &= np.linalg.norm(p0 - pb, axis=1) <= p.fb_max_err_px
        h, w = b.gray.shape
        xi = np.clip(np.round(p1[:, 0]).astype(np.int64), 0, w - 1)
        yi = np.clip(np.round(p1[:, 1]).astype(np.int64), 0, h - 1)
        inside = (p1[:, 0] >= 0) & (p1[:, 0] <= w - 1) & (p1[:, 1] >= 0) & (p1[:, 1] <= h - 1)
        good &= inside & (b.mask[yi, xi] > 0)
        n = int(good.sum())
        if n < p.min_features:
            return PairResult(mono_ms, False, "few_tracked", features=n)
        flow = (p1 - p0)[good].astype(np.float64)
        med = np.median(flow, axis=0)
        dev = np.linalg.norm(flow - med, axis=1)
        inl = dev <= p.inlier_tol_px
        inlier_ratio = float(inl.sum()) / float(n)
        residual = float(np.median(dev[inl])) if inl.any() else float("inf")
        feat_factor = min(1.0, n / float(p.full_features))
        res_factor = max(0.0, 1.0 - residual / p.max_residual_px) if math.isfinite(residual) else 0.0
        confidence = inlier_ratio * feat_factor * res_factor
        base = PairResult(mono_ms, False, "", features=n, inlier_ratio=_r(inlier_ratio),
                          residual_px=_r(residual) if math.isfinite(residual) else None)
        if inlier_ratio < p.min_inlier_ratio:
            return _replace(base, reason="low_inlier_ratio")
        if not math.isfinite(residual) or residual > p.max_residual_px:
            return _replace(base, reason="high_residual")
        if confidence < p.min_confidence:
            return _replace(base, reason="low_confidence")
        # Content moving left (dx < 0) means the camera panned right: yaw = -dx.
        # Content moving down (dy > 0) means the camera tilted up: pitch = +dy.
        scale = 100.0 * 1000.0 / (float(b.width) * float(dt_ms))
        yaw = -float(med[0]) * scale
        pitch = float(med[1]) * scale
        return _replace(base, ok=True, reason="ok", yaw_rate=_r(yaw), pitch_rate=_r(pitch), confidence=_r(confidence))

    # -- public --------------------------------------------------------------
    def pairs(self, frames: Iterable[tuple[int, Any]]) -> list[PairResult]:
        """Per-pair motion. ``frames`` yields ``(mono_ms, BGR-or-gray ndarray or None)`` in time order."""
        prev_threads = cv2.getNumThreads()
        cv2.setNumThreads(1)
        out: list[PairResult] = []
        try:
            prev: _Prepared | None = None
            prev_ms: int | None = None
            for mono_ms, frame in frames:
                mono_ms = int(mono_ms)
                try:
                    cur = self._prepare(frame)
                except Exception:
                    cur = _Prepared(None, None, None, 0, "exception")
                if prev is not None and prev_ms is not None:
                    try:
                        res = self._pair(prev, cur, mono_ms - prev_ms, mono_ms)
                    except Exception:
                        res = PairResult(mono_ms, False, "exception")
                    out.append(res)
                prev, prev_ms = cur, mono_ms
        finally:
            cv2.setNumThreads(prev_threads)
        return out

    def estimate_frames(self, frames: Iterable[tuple[int, Any]], start_ms: int, end_ms: int) -> list[PanBin]:
        """One ``PanBin`` per 100 ms in ``[start_ms, end_ms)``; ``start_ms`` must be a multiple of 100."""
        if start_ms % BIN_MS != 0 or end_ms < start_ms:
            raise ValueError("start_ms must be a multiple of 100 and end_ms >= start_ms")
        try:
            pair_results = self.pairs(frames)
        except Exception:
            pair_results = None
        return bin_pairs(pair_results, start_ms, end_ms)


def _replace(res: PairResult, **kw: Any) -> PairResult:
    data = asdict(res)
    data.update(kw)
    return PairResult(**data)


def bin_pairs(pair_results: Sequence[PairResult] | None, start_ms: int, end_ms: int) -> list[PanBin]:
    """Group pair results into 100 ms bins. Any unreadable pair in a bin makes the whole bin no_claim."""
    starts = list(range(start_ms, end_ms, BIN_MS))
    if pair_results is None:
        return [PanBin(t, None, None, None, True, "exception") for t in starts]
    grouped: dict[int, list[PairResult]] = {}
    for res in pair_results:
        key = (res.mono_ms // BIN_MS) * BIN_MS
        grouped.setdefault(key, []).append(res)
    bins: list[PanBin] = []
    for t in starts:
        group = grouped.get(t, [])
        if not group:
            bins.append(PanBin(t, None, None, None, True, "no_frame_pair"))
            continue
        bad = [res for res in group if not res.ok]
        if bad:
            bins.append(PanBin(t, None, None, None, True, bad[0].reason))
            continue
        yaw = float(np.median([res.yaw_rate for res in group]))
        pitch = float(np.median([res.pitch_rate for res in group]))
        conf = min(float(res.confidence) for res in group)
        bins.append(PanBin(t, _r(yaw), _r(pitch), _r(conf), False, None))
    return bins


def bins_jsonl(bins: Sequence[PanBin]) -> bytes:
    """Canonical JSONL of the contract fields (used by the determinism test and the CLI)."""
    return "".join(json.dumps(b.to_dict(), sort_keys=True, separators=(",", ":")) + "\n" for b in bins).encode("utf-8")


def iter_video_frames(path: str | Path, mono_offset_ms: int = 0):
    """Yield ``(mono_ms, frame)`` from a video file. A failed read mid-stream yields ``None`` frames, never a guess."""
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0:
            return
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                if total and idx < total:
                    yield mono_offset_ms + int(round(idx * 1000.0 / fps)), None
                    idx += 1
                    continue
                break
            yield mono_offset_ms + int(round(idx * 1000.0 / fps)), frame
            idx += 1
    finally:
        cap.release()


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m qor_receipt.vision.pan_estimator VIDEO [--mono-offset-ms N]`` -> JSONL bins on stdout."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Advisory camera-pan estimate per 100 ms (no verdict effect).")
    ap.add_argument("video")
    ap.add_argument("--mono-offset-ms", type=int, default=0)
    args = ap.parse_args(argv)
    frames = list(iter_video_frames(args.video, args.mono_offset_ms))
    if not frames:
        print(json.dumps({"error": "could not decode video"}), file=sys.stderr)
        return 2
    start = (frames[0][0] // BIN_MS) * BIN_MS
    end = frames[-1][0] + 1
    est = OpticalFlowPanEstimator()
    bins = est.estimate_frames(frames, start, end)
    sys.stdout.write(json.dumps({"estimator": est.identity()}, sort_keys=True) + "\n")
    sys.stdout.buffer.write(bins_jsonl(bins))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
