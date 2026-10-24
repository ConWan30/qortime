"""Exact-match accuracy per field of a PP-OCR rec ONNX model on a synthetic split.

    python eval/ocr_spike/eval_rec.py --data DIR --split val --model inference.onnx --dict char_dict.txt --out res.json

Reports two decodings: ``open`` (full dictionary) and ``field_alphabet`` (argmax restricted to the
characters a field can contain). SYNTHETIC-ONLY numbers: never evidence for replacing the current reader.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_spike import decode, ortrun, preprocess  # noqa: E402
from ocr_spike.roi import FIELD_NAMES  # noqa: E402
from ocr_spike.synth import normalize  # noqa: E402

DIG = "0123456789"
UP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
FIELD_ALPHABET = {
    "team_l": UP, "team_r": UP, "score_l": DIG, "score_r": DIG,
    "quarter": DIG + "stndrhSTNDRHO", "game_clock": DIG + ":", "play_clock": DIG + ":",
    "down_distance": DIG + UP + "&",
}


def load_split(data: Path, split: str, limit: int | None = None) -> list[dict]:
    rows = [json.loads(line) for line in (data / f"labels_{split}.jsonl").read_text(encoding="utf-8").splitlines()]
    return rows[:limit] if limit else rows


def evaluate(model: Path, charset: list[str], data: Path, rows: list[dict], threads: int = 8) -> dict:
    sess = ortrun.pinned_session(model, threads=threads)
    res = {"model_sha256": ortrun.sha256_file(model), "n_samples": len(rows), "fields": {}}
    preds_all = {}
    t0 = time.time()
    name = sess.get_inputs()[0].name
    for f in FIELD_NAMES:
        gold = [normalize(f, r["labels"][f]) for r in rows]
        masks = {"open": None, "field_alphabet": decode.allowed_mask(charset, FIELD_ALPHABET[f])}
        preds: dict[str, list[str]] = {m: [] for m in masks}
        for i in range(0, len(rows), 64):  # decode per batch: full-dictionary logits are ~3 MB per crop
            crops = [cv2.imread(str(data / r["crops"][f]["path"]), cv2.IMREAD_COLOR) for r in rows[i:i + 64]]
            u8, vw = preprocess.prepare(crops)
            logits = sess.run(None, {name: preprocess.normalize_batch(u8, vw)})[0]
            for m, mask in masks.items():
                preds[m] += [normalize(f, t) for t, _ in decode.greedy(logits, charset, mask)]
        out = {}
        for m in masks:
            ok = sum(p == g for p, g in zip(preds[m], gold))
            out[m] = {"correct": ok, "n": len(gold), "acc": round(ok / max(1, len(gold)), 4)}
        preds_all[f] = preds["open"]
        res["fields"][f] = out
    for mode in ("open", "field_alphabet"):
        c = sum(res["fields"][f][mode]["correct"] for f in FIELD_NAMES)
        n = sum(res["fields"][f][mode]["n"] for f in FIELD_NAMES)
        res[f"all_fields_{mode}"] = {"correct": c, "n": n, "acc": round(c / max(1, n), 4)}
    res["seconds"] = round(time.time() - t0, 1)
    res["examples"] = [{"id": rows[i]["id"], **{f: [rows[i]["labels"][f], preds_all[f][i]] for f in FIELD_NAMES}}
                       for i in range(min(5, len(rows)))]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--split", default="val")
    ap.add_argument("--model", required=True)
    ap.add_argument("--dict", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    data = Path(a.data)
    res = evaluate(Path(a.model), decode.load_charset(Path(a.dict)), data, load_split(data, a.split, a.limit), a.threads)
    res["synthetic_only"] = True
    res["counts_toward_replacement_decision"] = False
    txt = json.dumps(res, indent=2)
    if a.out:
        Path(a.out).write_text(txt + "\n", encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
