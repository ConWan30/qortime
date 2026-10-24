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
from typing import Any, ClassVar

from qor_receipt.vision.pan_estimator import OpticalFlowPanEstimator
from redteam.scorers import PanBin, VariantSample

FrameSource = Callable[[VariantSample], Iterable[tuple[int, Any]]]


class OpticalFlowHarnessEstimator:
    """``CameraPanEstimator`` over a frame source. Bins cover ``[0, sample.duration_ms)``."""

    _cache: ClassVar[dict[tuple[str, Hashable], tuple[PanBin, ...]]] = {}

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

    @property
    def sha256(self) -> str:
        return self.core.sha256

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        """``cache_key`` lets the harness reuse bins when the frames do not depend on the variant."""
        key = None if self.cache_key is None else (self.core.sha256, self.cache_key(sample))
        if key is not None and key in self._cache:
            return list(self._cache[key])
        bins = self._estimate(sample)
        if key is not None:
            self._cache[key] = tuple(bins)
        return bins

    def _estimate(self, sample: VariantSample) -> list[PanBin]:
        try:
            frames = self.frame_source(sample)
        except Exception:
            frames = []
        bins = self.core.estimate_frames(frames, 0, sample.duration_ms)
        return [
            PanBin(mono_ms=b.mono_ms, yaw_rate=b.yaw_rate, pitch_rate=b.pitch_rate, confidence=b.confidence, no_claim=b.no_claim)
            for b in bins
        ]
