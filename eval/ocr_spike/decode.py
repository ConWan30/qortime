"""CTC greedy decoding for PP-OCR recognisers (unmasked argmax, collapse repeats, drop blank).

There is deliberately no alphabet mask: masking the argmax coerced an out-of-alphabet top pick into the best
in-alphabet character (QorProof review of #73, P1). ``read_fields`` instead marks such a read INVALID.
Model outputs are softmax probabilities (PP-OCR export ends in softmax).
"""

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


def greedy_detail(logits: np.ndarray, charset: list[str]) -> list[dict]:
    """[N,T,C] probs -> per sample: text, per-char top-1 prob, per-char margin (top1 - top2)."""
    idx = logits.argmax(-1)
    top2 = np.partition(logits, -2, axis=-1)[..., -2:]
    p1 = np.take_along_axis(logits, idx[..., None], -1)[..., 0]
    margin = p1 - top2.min(-1)
    res = []
    for n in range(idx.shape[0]):
        chars, probs, margins, prev = [], [], [], -1
        for t, k in enumerate(idx[n]):
            k = int(k)
            if k != prev and k != 0:
                chars.append(charset[k])
                probs.append(float(p1[n, t]))
                margins.append(float(margin[n, t]))
            prev = k
        res.append({"chars": chars, "probs": probs, "margins": margins})
    return res


def greedy(logits: np.ndarray, charset: list[str]) -> list[tuple[str, float]]:
    """[N,T,C] -> [(text, mean top-1 prob of emitted chars)]. Unmasked."""
    return [("".join(d["chars"]), float(np.mean(d["probs"])) if d["probs"] else 0.0) for d in greedy_detail(logits, charset)]


def read_field(detail: dict, alphabet: str, upper: bool = False) -> dict:
    """One field read (``upper`` only changes the reported text). ``valid`` is False (abstain-marked, NOT coerced) if any emitted top-1 char is outside
    ``alphabet``. Empty text is ``valid: False, reason: empty``. Confidence/margin are always recorded."""
    raw = list(detail["chars"])
    oob = sorted({c for c in raw if c not in alphabet})  # validated on the raw top-1 chars, before any upper()
    text = "".join(raw).upper() if upper else "".join(raw)
    p, m = detail["probs"], detail["margins"]
    r = {"text": text, "valid": bool(text) and not oob,
         "reason": "ok" if text and not oob else ("empty" if not text else "out_of_alphabet_top1"),
         "min_conf": round(min(p), 6) if p else None, "mean_conf": round(float(np.mean(p)), 6) if p else None,
         "min_margin": round(min(m), 6) if m else None}
    if oob:
        r["out_of_alphabet"] = oob
    return r
