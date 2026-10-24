"""Harness adapter for ``qor_receipt.vision.pan_estimator.OpticalFlowPanEstimator``.

``VariantSample`` carries controller rows and a one-number-per-second motion
series, not pixels. The adapter therefore takes a ``frame_source`` that turns a
sample into ``(mono_ms, frame)`` pairs. In the harness that source renders
synthetic frames (``redteam.pan_frames``) whose camera follows the harness's
pan seconds. No recorded footage is read here.

The estimator is advisory. Whatever it reports, the pan-agreement scorer is an
example decision in a test-only harness; it cannot raise a referee verdict above
PARTIAL and it never overrides the deterministic checks.
"""
from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from typing import Any

from qor_receipt.vision.pan_estimator import OpticalFlowPanEstimator
from redteam.scorers import EstimateCache, PanBin, VariantSample, estimate_cache_key

FrameSource = Callable[[VariantSample], Iterable[tuple[int, Any]]]


class OpticalFlowHarnessEstimator:
    """``CameraPanEstimator`` over a frame source. Bins cover ``[0, sample.duration_ms)``."""

    def __init__(
        self,
        frame_source: FrameSource,
        core: OpticalFlowPanEstimator | None = None,
        cache_key: Callable[[VariantSample], Hashable] | None = None,
    ) -> None:
        self.core = core or OpticalFlowPanEstimator()
        self.frame_source = frame_source
        self.cache_key = cache_key
        self.name = self.core.name
        self.version = self.core.version
        self._cache = EstimateCache()

    @property
    def sha256(self) -> str:
        return self.core.sha256

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        """Reuse bins only for this instance, and only when the frames and the window match.

        ``cache_key`` turns the cache on. The store key is ``estimate_cache_key``
        (identity, including the core class and instance attributes, plus the
        frame-source path and a hash of the frame bytes) together with
        ``sample.duration_ms`` and the value ``cache_key`` returned. Bins cover
        ``[0, duration_ms)``, so a later call with the same frames and a longer
        window must not reuse the shorter result. When the identity is
        ``unhashed``, nothing is stored.
        """
        frames = self._frames(sample)
        key = None
        if self.cache_key is not None:
            caller_key = self.cache_key(sample)
            base = estimate_cache_key(self, self, self.frame_source, frames)
            # An unhashed identity is not stored. Two unresolvable cores must not share bins.
            if base is not None:
                duration_ms = getattr(sample, "duration_ms", None)
                key = (*base, duration_ms, caller_key)
                hit = self._cache.get(key)
                if hit is not None:
                    return list(hit)
        bins = self._bins(frames, sample)
        if key is not None:
            self._cache.put(key, bins)
        return bins

    def _estimate(self, sample: VariantSample) -> list[PanBin]:
        return self._bins(self._frames(sample), sample)

    def _frames(self, sample: VariantSample) -> list[Any]:
        try:
            return list(self.frame_source(sample))
        except Exception:
            return []

    def _bins(self, frames: list[Any], sample: VariantSample) -> list[PanBin]:
        bins = self.core.estimate_frames(frames, 0, sample.duration_ms)
        return [
            PanBin(mono_ms=b.mono_ms, yaw_rate=b.yaw_rate, pitch_rate=b.pitch_rate, confidence=b.confidence, no_claim=b.no_claim)
            for b in bins
        ]
