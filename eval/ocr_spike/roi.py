"""Field ROIs for the standard Madden 27 bottom strip, at 1280x720.

The current reader (``qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py``,
``MADDEN27_STANDARD``) stores boxes in 640x360 reference pixels. Score, quarter and
clock boxes below are those boxes x2, padded a little so a recogniser sees whole
glyphs. Team, play clock and down & distance are not read today; their boxes were
measured on the three public 170123 highlight clips (1280x720).

Boxes are ``(x1, x2, y1, y2)`` in full-frame 1280x720 pixels.
"""

from __future__ import annotations

import numpy as np

FRAME_W, FRAME_H = 1280, 720
# The reader's bottom band starts at 330/360 of frame height (profile.band_y0).
BAND_Y0 = 660
BAND_H = FRAME_H - BAND_Y0  # 60 rows

# Reader boxes at 640x360 (copied from MADDEN27_STANDARD so this module has no import on the vendored code).
REF_LEFT_SCORE = (220, 246, 342, 357)
REF_RIGHT_SCORE = (257, 284, 342, 357)
REF_QUARTER = (430, 447, 344, 353)
REF_CLOCK_TEXT = (447, 480, 344, 353)


def _x2(box: tuple[int, int, int, int], pad_x: int = 0, pad_y: int = 0) -> tuple[int, int, int, int]:
    x1, x2, y1, y2 = box
    return (2 * x1 - pad_x, 2 * x2 + pad_x, 2 * y1 - pad_y, 2 * y2 + pad_y)


FIELDS: dict[str, tuple[int, int, int, int]] = {
    "team_l": (330, 422, 682, 712),
    "score_l": _x2(REF_LEFT_SCORE, 4, 2),
    "score_r": _x2(REF_RIGHT_SCORE, 4, 2),
    "team_r": (584, 674, 682, 712),
    "quarter": _x2(REF_QUARTER, 4, 4),
    "game_clock": _x2(REF_CLOCK_TEXT, 2, 4),
    "play_clock": (962, 1026, 684, 712),
    "down_distance": (1038, 1182, 682, 712),
}
FIELD_NAMES = tuple(FIELDS)


def crop_field(frame: np.ndarray, field: str, dx: int = 0, dy: int = 0) -> np.ndarray:
    """Crop one field from a 1280x720 BGR frame. ``dx``/``dy`` are frame-pixel offsets (e.g. reader anchor x2)."""
    if frame.shape[0] != FRAME_H or frame.shape[1] != FRAME_W:
        raise ValueError(f"expected {FRAME_W}x{FRAME_H}, got {frame.shape[1]}x{frame.shape[0]}")
    x1, x2, y1, y2 = FIELDS[field]
    x1, x2, y1, y2 = x1 + dx, x2 + dx, y1 + dy, y2 + dy
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(FRAME_W, x2), min(FRAME_H, y2)
    return np.ascontiguousarray(frame[y1:y2, x1:x2])


def crop_band_field(band: np.ndarray, field: str, dx: int = 0, dy: int = 0) -> np.ndarray:
    """Same as :func:`crop_field` on a 1280x60 band (frame rows 660..720)."""
    if band.shape[0] != BAND_H or band.shape[1] != FRAME_W:
        raise ValueError(f"expected band {FRAME_W}x{BAND_H}, got {band.shape[1]}x{band.shape[0]}")
    x1, x2, y1, y2 = FIELDS[field]
    y1, y2 = y1 - BAND_Y0 + dy, y2 - BAND_Y0 + dy
    x1, x2 = x1 + dx, x2 + dx
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(FRAME_W, x2), min(BAND_H, y2)
    return np.ascontiguousarray(band[y1:y2, x1:x2])
