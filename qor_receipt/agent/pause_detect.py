"""Madden pause-menu detector (per frame, cheap). NEEDS TUNING against real Madden 27 pause frames.

The challenge verifier wants the pause menu on video 0..600 ms after the OPTIONS press, so this
runs on every captured frame (not the 5/s scorebug cadence) on a 160x90 grayscale thumbnail.

Signals (all thresholds pluggable via ``PauseThresholds`` / ``--pause-config JSON``):
- ``dim``: mean luma OUTSIDE the menu region drops below ``dim_ratio`` x the running gameplay baseline (Madden dims the
  field behind the menu);
- ``panel``: fraction of bright (>= ``panel_luma``) pixels inside the menu region ``panel_roi``
  (x0, y0, x1, y1 as 0..1 fractions) is at least ``panel_min_frac``;
- optional ``template``: normalized cross-correlation vs a captured pause-menu thumbnail
  >= ``template_min`` (overrides the heuristic when it fires).
- ``dark_menu``: the real Madden 27 pause menu (tuned on a 640x360 capture, Oct 2026) is NOT a
  bright panel: the whole screen goes near-black (thumb mean ~10, lower half ~3) with small white
  text (menu list, top-centre scoreboard "14 - 0 / 3rd 5:00"). Matched by absolute luma caps plus a
  bright-text fraction window, so it needs no gameplay baseline and ignores pure-black fades.
  The default caps are 16/8. A cheap USB3.0 Video capture of the same menu measured about 16.6/6.7
  (end menu about 21/13); rule 3 uses 26/16 and does not change this default.
Paused = template match OR dark_menu OR (dim AND panel), held for ``confirm_frames`` consecutive frames;
the onset time is the FIRST frame of that run, so confirmation adds no latency to pause_seen_ms.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

NEEDS_TUNING = True
THUMB = (160, 90)
# Default dark-menu caps. Rule 1 and rule 2 reviews use these. Do not raise them: a rule-3 review
# opts into the capture-path caps below instead.
DARK_MENU_MAX_LUMA = 16.0
DARK_MENU_MAX_LOWER = 8.0
# Cheap "USB3.0 Video" capture of Madden 27 (Oct 2026). The pause menu measured about 16.6 full /
# 6.7 lower and the end menu about 21 / 13, so 16/8 returns pause_not_seen_on_video. Gameplay on
# the same path measures about 60-110. Text fraction, confirm_frames, and the other signals stay.
CALIBRATED_DARK_MENU_MAX_LUMA = 26.0
CALIBRATED_DARK_MENU_MAX_LOWER = 16.0


@dataclass
class PauseThresholds:
    dim_ratio: float = 0.70
    panel_roi: tuple[float, float, float, float] = (0.05, 0.15, 0.45, 0.85)
    panel_luma: int = 170
    panel_min_frac: float = 0.12
    confirm_frames: int = 2
    baseline_alpha: float = 0.05
    min_baseline: float = 20.0
    template_path: str | None = None
    template_min: float = 0.80
    dark_menu: bool = True
    dark_menu_max_luma: float = DARK_MENU_MAX_LUMA
    dark_menu_max_lower: float = DARK_MENU_MAX_LOWER
    dark_menu_bright_min: float = 0.005
    dark_menu_bright_max: float = 0.05
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None) -> "PauseThresholds":
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        names = {f.name for f in fields(cls)}
        kw = {k: (tuple(v) if k == "panel_roi" else v) for k, v in raw.items() if k in names}
        return cls(**kw)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calibrated_thresholds() -> PauseThresholds:
    """Rule-3 dark-menu caps. Every other threshold stays on the ``PauseThresholds`` default."""
    return PauseThresholds(dark_menu_max_luma=CALIBRATED_DARK_MENU_MAX_LUMA,
                           dark_menu_max_lower=CALIBRATED_DARK_MENU_MAX_LOWER)


class PauseDetector:
    def __init__(self, th: PauseThresholds | None = None) -> None:
        self.th = th or PauseThresholds()
        self.baseline: float | None = None
        self.paused = False
        self._run: list[int] = []
        self.onsets: list[int] = []     # t_ms of each not-paused -> paused transition
        self.last: dict[str, float] = {}
        self._tpl = None
        if self.th.template_path:
            import cv2

            t = cv2.imread(self.th.template_path, cv2.IMREAD_GRAYSCALE)
            if t is None:
                raise FileNotFoundError(self.th.template_path)
            self._tpl = cv2.resize(t, THUMB, interpolation=cv2.INTER_AREA).astype("float32")

    def reset(self) -> None:
        """Frame-size change: forget the baseline and any partial run."""
        self.baseline = None
        self._run = []

    def features(self, img: Any) -> dict[str, float]:
        import cv2
        import numpy as np

        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        g = cv2.resize(g, THUMB, interpolation=cv2.INTER_AREA)
        x0, y0, x1, y1 = self.th.panel_roi
        ys, xs = slice(int(y0 * THUMB[1]), int(y1 * THUMB[1])), slice(int(x0 * THUMB[0]), int(x1 * THUMB[0]))
        roi = g[ys, xs]
        outside = np.ones(g.shape, bool)
        outside[ys, xs] = False
        f = {"luma": float(g[outside].mean()) if outside.any() else float(g.mean()), "panel_frac": float((roi >= self.th.panel_luma).mean()) if roi.size else 0.0}
        f["bright_frac"] = float((g >= self.th.panel_luma).mean())
        f["full_luma"] = float(g.mean())
        f["lower_luma"] = float(g[THUMB[1] // 2:].mean())
        if self._tpl is not None:
            a = g.astype("float32") - g.mean()
            b = self._tpl - self._tpl.mean()
            den = float(np.sqrt((a * a).sum() * (b * b).sum())) or 1.0
            f["template"] = float((a * b).sum() / den)
        return f

    def classify(self, f: dict[str, float]) -> bool:
        th = self.th
        if "template" in f and f["template"] >= th.template_min:
            return True
        if th.dark_menu and "full_luma" in f and f["full_luma"] <= th.dark_menu_max_luma \
                and f["lower_luma"] <= th.dark_menu_max_lower \
                and th.dark_menu_bright_min <= f["bright_frac"] <= th.dark_menu_bright_max:
            return True
        base = self.baseline
        dim = base is not None and base >= th.min_baseline and f["luma"] <= th.dim_ratio * base
        return bool(dim and f["panel_frac"] >= th.panel_min_frac)

    def update(self, t_ms: int, img: Any, reset: bool = False) -> bool:
        if reset:
            self.reset()
        f = self.features(img)
        raw = self.classify(f)
        self.last = {**f, "raw": float(raw), "baseline": self.baseline or 0.0}
        if raw:
            self._run.append(t_ms)
            if not self.paused and len(self._run) >= self.th.confirm_frames:
                self.paused = True
                self.onsets.append(self._run[0])
        else:
            self._run = []
            self.paused = False
            a = self.th.baseline_alpha
            self.baseline = f["luma"] if self.baseline is None else (1 - a) * self.baseline + a * f["luma"]
        return self.paused

    def onset_after(self, t_ms: int, slack_ms: int = 300, horizon_ms: int = 5_000) -> int | None:
        """First pause onset in [t_ms - slack, t_ms + horizon]. Slack lets the referee SEE (and reject)
        video that paused before the press instead of the agent hiding it."""
        return next((o for o in self.onsets if t_ms - slack_ms <= o <= t_ms + horizon_ms), None)
