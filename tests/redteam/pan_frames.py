"""Synthetic frames with a known camera pan, for the pan-estimator tests and the harness.

Nothing here is footage. A deterministic textured "world" image (seeded noise,
blurred, plus a sine grid) is cropped through a moving window. The crop moves by
a known number of pixels per frame, and a static fake scorebug strip is drawn on
top of every frame, so an estimator that leaks overlay motion or ignores the mask
shows up in the tests.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

import cv2
import numpy as np

FRAME_W = 320
FRAME_H = 180
WORLD_W = 1600
WORLD_H = 900
WORLD_SEED = 20261024


def world_texture(seed: int = WORLD_SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 256, size=(WORLD_H, WORLD_W), dtype=np.uint8)
    blurred = cv2.GaussianBlur(noise, (0, 0), 2.0)
    yy, xx = np.mgrid[0:WORLD_H, 0:WORLD_W]
    grid = (40 * np.sin(xx / 13.0) * np.cos(yy / 17.0)).astype(np.int16)
    tex = np.clip(blurred.astype(np.int16) * 2 - 128 + grid, 0, 255).astype(np.uint8)
    return cv2.cvtColor(tex, cv2.COLOR_GRAY2BGR)


def draw_scorebug(frame: np.ndarray, text: str = "CLE 0 - TEN 0  Q3 0:59") -> np.ndarray:
    """Static fake scorebug strip in the bottom band the estimator masks (y >= 330/360 of height)."""
    h, w = frame.shape[:2]
    y0 = int(round(h * 0.93))
    frame[y0:h, :] = (30, 30, 30)
    cv2.rectangle(frame, (int(w * 0.30), y0 + 1), (int(w * 0.48), h - 1), (240, 240, 240), -1)
    cv2.putText(frame, text, (4, h - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1, cv2.LINE_8)
    return frame


def crop(world: np.ndarray, x: float, y: float) -> np.ndarray:
    """Sub-pixel crop at world position (x, y) via an affine warp (deterministic, bilinear)."""
    m = np.float32([[1, 0, -x], [0, 1, -y]])
    return cv2.warpAffine(world, m, (FRAME_W, FRAME_H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def pan_frames(
    n_frames: int,
    dx_per_frame: float,
    dy_per_frame: float = 0.0,
    frame_ms: int = 100,
    start: tuple[float, float] = (600.0, 300.0),
    scorebug: bool = True,
) -> list[tuple[int, np.ndarray]]:
    """Camera moves (+dx right, +dy down in world px) each frame, so content moves the opposite way."""
    world = world_texture()
    out = []
    for i in range(n_frames):
        frame = crop(world, start[0] + dx_per_frame * i, start[1] + dy_per_frame * i)
        if scorebug:
            frame = draw_scorebug(frame)
        out.append((i * frame_ms, frame))
    return out


def camera_path_frames(
    duration_ms: int,
    velocity_px_s: Callable[[int], tuple[float, float]],
    frame_ms: int = 100,
    start: tuple[float, float] = (600.0, 300.0),
) -> Iterator[tuple[int, np.ndarray]]:
    """Integrate a per-frame camera velocity (world px/s, +x right, +y down) and render with a scorebug."""
    world = world_texture()
    x, y = start
    for t in range(0, duration_ms, frame_ms):
        if t > 0:
            vx, vy = velocity_px_s(t)
            x += vx * frame_ms / 1000.0
            y += vy * frame_ms / 1000.0
        yield t, draw_scorebug(crop(world, x, y))
