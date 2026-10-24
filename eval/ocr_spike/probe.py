"""Committed synthetic probe set (tiny) + input loading shared by the re-run and golden checks.

    python eval/ocr_spike/probe.py build --fonts FONT_DIR --out eval/ocr_spike/probe --samples 4

The probe is SYNTHETIC (split "probe", disjoint seed stream from train/val). It exists only to pin model
outputs across machines; it is never an accuracy set and never counts toward replacing the current reader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_spike import preprocess  # noqa: E402
from ocr_spike.roi import FIELD_NAMES  # noqa: E402

PROBE_DIR = Path(__file__).resolve().parent / "probe"


def load_probe(probe_dir: Path = PROBE_DIR) -> tuple[np.ndarray, np.ndarray, list[str], dict]:
    meta = json.loads((Path(probe_dir) / "probe.json").read_text(encoding="utf-8"))
    crops, ids = [], []
    for item in meta["items"]:
        raw = (Path(probe_dir) / item["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError(f"probe file changed: {item['file']}")
        crops.append(cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR))
        ids.append(item["id"])
    u8, vw = preprocess.prepare(crops)
    return u8, vw, ids, meta


def load_inputs(a) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """From ``a.probe`` (committed probe dir) or ``a.data``/``a.split``/``a.limit`` (generated synthetic split)."""
    if getattr(a, "probe", None):
        u8, vw, ids, _ = load_probe(Path(a.probe))
        return u8, vw, ids
    data = Path(a.data)
    rows = [json.loads(x) for x in (data / f"labels_{a.split}.jsonl").read_text(encoding="utf-8").splitlines()]
    if a.limit:
        rows = rows[: a.limit]
    crops, ids = [], []
    for r in rows:
        for f in FIELD_NAMES:
            crops.append(cv2.imread(str(data / r["crops"][f]["path"]), cv2.IMREAD_COLOR))
            ids.append(f"{r['id']}/{f}")
    u8, vw = preprocess.prepare(crops)
    return u8, vw, ids


def build(fonts: Path, out: Path, samples: int, seed: int) -> dict:
    from ocr_spike import synth
    from ocr_spike.rerun import input_sha256

    specs = synth.verify_fonts(fonts, synth.load_font_specs(Path(__file__).resolve().parent / "fonts.json"))
    fb = synth.FontBank(fonts, specs)
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for i in range(samples):
        s = synth.make_sample(seed, "probe", i, fb)
        for f in FIELD_NAMES:
            b = synth.png_bytes(s["crops"][f])
            name = f"p{i:02d}_{f}.png"
            (out / name).write_bytes(b)
            items.append({"id": f"probe-{i:02d}/{f}", "file": name, "sha256": hashlib.sha256(b).hexdigest(),
                          "label": s["labels"][f]})
    meta = {"schema": "qortime-ocr-spike-probe/v0", "synthetic": True, "counts_toward_replacement_decision": False,
            "seed": seed, "split": "probe", "items": items}
    (out / "probe.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    u8, vw, _, _ = load_probe(out)
    meta["preprocessed_input_sha256"] = input_sha256(u8, vw)
    (out / "probe.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("--fonts", required=True)
    b.add_argument("--out", default=str(PROBE_DIR))
    b.add_argument("--samples", type=int, default=4)
    b.add_argument("--seed", type=int, default=20261024)
    a = ap.parse_args(argv)
    meta = build(Path(a.fonts), Path(a.out), a.samples, a.seed)
    print(len(meta["items"]), meta["preprocessed_input_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
