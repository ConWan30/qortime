"""Pinned PP-OCR recogniser preprocessing (matches PaddleOCR ``RecResizeImg`` / ``resize_norm_img``).

BGR uint8 crop -> resize to height 48 keeping aspect (width capped at 320, cv2 INTER_LINEAR) ->
right-pad with zeros to 48x320. Normalisation ``(x/255 - 0.5)/0.5`` is done separately in numpy so
a stored uint8 probe tensor fully determines the model input.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

REC_H, REC_W = 48, 320


def resize_pad_u8(bgr: np.ndarray) -> tuple[np.ndarray, int]:
    """Return (48x320x3 uint8 image, valid width). Padding is 0 here and 0.0 after normalize_batch."""
    h, w = bgr.shape[:2]
    if h == 0 or w == 0:
        raise ValueError("empty crop")
    rw = min(REC_W, int(math.ceil(REC_H * w / float(h))))
    out = np.zeros((REC_H, REC_W, 3), np.uint8)
    out[:, :rw] = cv2.resize(bgr, (rw, REC_H), interpolation=cv2.INTER_LINEAR)
    return out, rw


def normalize_batch(u8: np.ndarray, valid_w: np.ndarray) -> np.ndarray:
    """uint8 [N,48,320,3] + valid widths -> float32 NCHW. Padding is 0.0 after normalisation, as in PaddleOCR."""
    x = u8.astype(np.float32) / np.float32(255.0)
    x = (x - np.float32(0.5)) / np.float32(0.5)
    for i, vw in enumerate(valid_w):
        x[i, :, int(vw):] = 0.0
    return np.ascontiguousarray(x.transpose(0, 3, 1, 2))


def prepare(crops: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """BGR crops -> (uint8 [N,48,320,3], int32 valid widths)."""
    imgs, ws = zip(*(resize_pad_u8(c) for c in crops)) if crops else ((), ())
    return np.stack(imgs) if imgs else np.zeros((0, REC_H, REC_W, 3), np.uint8), np.asarray(ws, np.int32)
