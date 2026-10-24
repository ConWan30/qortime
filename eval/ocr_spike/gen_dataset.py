"""Write a synthetic scorebug dataset: band PNGs, per-field crop PNGs, labels JSONL, manifest.

    python eval/ocr_spike/gen_dataset.py --out DIR --fonts FONT_DIR --train 20000 --val 2000 --seed 20261024

Also writes PaddleOCR rec label lists (``rec_train.txt``/``rec_val.txt``: "relpath<TAB>label").
SYNTHETIC ONLY: never part of any set used to decide whether to replace the current reader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from multiprocessing import Pool
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "ocr_spike"

from ocr_spike import synth  # noqa: E402
from ocr_spike.roi import FIELD_NAMES  # noqa: E402

HERE = Path(__file__).resolve().parent
_FB = None


def _init(font_dir: str, specs) -> None:
    global _FB
    _FB = synth.FontBank(Path(font_dir), specs)


def _one(args):
    seed, split, i = args
    s = synth.make_sample(seed, split, i, _FB)
    band = synth.png_bytes(s["band"])
    crops = {f: synth.png_bytes(s["crops"][f]) for f in FIELD_NAMES}
    return i, s["labels"], s["params"], band, crops


def code_sha256() -> str:
    h = hashlib.sha256()
    for name in ("synth.py", "roi.py", "gen_dataset.py", "fonts.json"):
        h.update(name.encode() + b"\0" + (HERE / name).read_bytes())
    return h.hexdigest()


def write_split(out: Path, split: str, n: int, seed: int, pool: Pool) -> dict:
    sd = out / split
    (sd / "bands").mkdir(parents=True, exist_ok=True)
    for f in FIELD_NAMES:
        (sd / "crops" / f).mkdir(parents=True, exist_ok=True)
    lab_path = out / f"labels_{split}.jsonl"
    rec_path = out / f"rec_{split}.txt"
    roll = hashlib.sha256()
    with open(lab_path, "w", encoding="utf-8", newline="\n") as lf, open(rec_path, "w", encoding="utf-8", newline="\n") as rf:
        for i, labels, params, band, crops in pool.imap(_one, [(seed, split, i) for i in range(n)], chunksize=32):
            sid = f"{split}-{i:06d}"
            bp = f"{split}/bands/{sid}.png"
            (out / bp).write_bytes(band)
            rec = {"id": sid, "split": split, "seed": seed, "index": i, "labels": labels, "params": params,
                   "band": {"path": bp, "sha256": hashlib.sha256(band).hexdigest()}, "crops": {}}
            for f in FIELD_NAMES:
                cp = f"{split}/crops/{f}/{sid}.png"
                (out / cp).write_bytes(crops[f])
                rec["crops"][f] = {"path": cp, "sha256": hashlib.sha256(crops[f]).hexdigest()}
                rf.write(f"{cp}\t{labels[f]}\n")
            line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            lf.write(line + "\n")
            roll.update(line.encode() + b"\n")
    return {"n": n, "labels_jsonl": lab_path.name, "labels_sha256": hashlib.sha256(lab_path.read_bytes()).hexdigest(),
            "rec_list": rec_path.name, "rec_list_sha256": hashlib.sha256(rec_path.read_bytes()).hexdigest()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--fonts", required=True)
    ap.add_argument("--train", type=int, default=20000)
    ap.add_argument("--val", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    specs = synth.verify_fonts(Path(a.fonts), synth.load_font_specs(HERE / "fonts.json"))
    if not specs:
        print("no font matched its pinned sha256", file=sys.stderr)
        return 2
    import cv2
    import numpy
    import PIL
    from PIL import features

    manifest = {
        "schema": "qortime-ocr-spike-synth/v0",
        "synthetic": True,
        "counts_toward_replacement_decision": False,
        "generator": synth.GENERATOR_VERSION,
        "code_sha256": code_sha256(),
        "seed": a.seed,
        "fonts": [{"file": s.file, "sha256": s.sha256, "wght": s.wght} for s in specs],
        "versions": {"python": platform.python_version(), "numpy": numpy.__version__, "opencv": cv2.__version__,
                     "pillow": PIL.__version__, "freetype": features.version("freetype2")},
        "fields": list(FIELD_NAMES),
        "splits": {},
    }
    with Pool(a.workers, initializer=_init, initargs=(a.fonts, specs)) as pool:
        for split, n in (("train", a.train), ("val", a.val)):
            if n > 0:
                manifest["splits"][split] = write_split(out, split, n, a.seed, pool)
                print(split, manifest["splits"][split], flush=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("manifest sha256", hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
