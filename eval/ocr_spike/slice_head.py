"""Shrink PP-OCRv6 rec heads to the scorebug alphabet, keeping the pretrained rows (run in a Paddle env).

The official checkpoint's CTC head scores 18,710 classes (blank + 18,708 dictionary chars + space) and the
NRTR head 18,714 (blank, <unk>, <s>, </s>, dictionary, space). We keep only the rows for the characters in
``scorebug_dict.txt`` so fine-tuning starts from the pretrained classifier for those characters instead of a
random head, and the exported model has a 69-class CTC output (blank + 67 chars + space).

    python eval/ocr_spike/slice_head.py --src PP-OCRv6_small_rec_pretrained.pdparams --full-dict ppocrv6_dict.txt \
        --small-dict eval/ocr_spike/scorebug_dict.txt --out scorebug_init.pdparams
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def index_map(full: list[str], small: list[str], n_special: int, n_tail: int = 1) -> list[int]:
    """Rows to keep: specials, each small-dict char's row in the full layout, then the ``n_tail`` trailing rows.

    CTC tail = [space]. NRTR tail = [space, spare] (ppocr's Transformer adds one output row: out_channels + 1).
    """
    pos = {c: i for i, c in enumerate(full)}
    missing = [c for c in small if c not in pos]
    if missing:
        raise ValueError(f"chars not in full dict: {missing}")
    total = n_special + len(full) + n_tail
    return list(range(n_special)) + [n_special + pos[c] for c in small] + list(range(total - n_tail, total))


def main(argv=None) -> int:
    import numpy as np
    import paddle

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--full-dict", required=True)
    ap.add_argument("--small-dict", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    full = Path(a.full_dict).read_text(encoding="utf-8").splitlines()
    small = Path(a.small_dict).read_text(encoding="utf-8").splitlines()
    sd = paddle.load(a.src)
    ctc = index_map(full, small, 1)
    gtc = index_map(full, small, 4, 2)

    def take(k, idx, axis):
        v = np.asarray(sd[k].numpy())
        assert v.shape[axis] in (len(full) + 2, len(full) + 6), (k, v.shape)
        sd[k] = paddle.to_tensor(np.take(v, idx, axis=axis))

    take("head.ctc_head.fc.weight", ctc, 1)
    take("head.ctc_head.fc.bias", ctc, 0)
    take("head.gtc_head.embedding.embedding.weight", gtc, 0)
    take("head.gtc_head.tgt_word_prj.weight", gtc, 1)
    paddle.save(sd, a.out)
    info = {"src_sha256": hashlib.sha256(Path(a.src).read_bytes()).hexdigest(),
            "out_sha256": hashlib.sha256(Path(a.out).read_bytes()).hexdigest(),
            "ctc_classes": len(ctc), "nrtr_classes": len(gtc),
            "small_dict_sha256": hashlib.sha256(Path(a.small_dict).read_bytes()).hexdigest()}
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
