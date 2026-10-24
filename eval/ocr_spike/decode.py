"""CTC greedy decoding for PP-OCR recognisers (argmax, collapse repeats, drop blank)."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def load_charset(dict_path: Path, use_space_char: bool = True) -> list[str]:
    """Index 0 is the CTC blank, then the dictionary lines, then ' ' when use_space_char (PaddleOCR CTCLabelDecode)."""
    chars = Path(dict_path).read_text(encoding="utf-8").splitlines()
    out = ["<blank>"] + chars
    if use_space_char:
        out.append(" ")
    return out


def greedy(logits: np.ndarray, charset: list[str], allowed: np.ndarray | None = None) -> list[tuple[str, float]]:
    """[N,T,C] scores -> [(text, mean max-prob)]. ``allowed`` (bool [C]) restricts argmax to a sub-alphabet."""
    x = logits
    if allowed is not None:
        x = np.where(allowed[None, None, :], x, -np.inf)
    idx = x.argmax(-1)
    prob = np.take_along_axis(logits, idx[..., None], -1)[..., 0]
    res = []
    for n in range(idx.shape[0]):
        chars, probs, prev = [], [], -1
        for t, k in enumerate(idx[n]):
            k = int(k)
            if k != prev and k != 0:
                chars.append(charset[k])
                probs.append(float(prob[n, t]))
            prev = k
        res.append(("".join(chars), float(np.mean(probs)) if probs else 0.0))
    return res


def allowed_mask(charset: list[str], alphabet: str) -> np.ndarray:
    m = np.zeros(len(charset), bool)
    m[0] = True
    for i, c in enumerate(charset):
        if c in alphabet:
            m[i] = True
    return m
