"""Re-run check: same model, same inputs, pinned single-thread CPU setup -> byte-identical outputs?

Runs the model twice in this process and once in each of two fresh subprocesses, then compares the raw
logits bytes and decoded strings of all four runs. Exits 1 on ANY difference. A pass means the outputs are
re-runnable with the same pinned setup on THIS machine; it says nothing about other CPUs (see golden_check.py).

    python eval/ocr_spike/rerun_check.py --model m.onnx --dict d.txt --data SYNTH_DIR --split val [--limit N] --out r.json
    python eval/ocr_spike/rerun_check.py --model m.onnx --dict d.txt --probe probe/   # committed probe crops
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_spike import decode, ortrun, rerun  # noqa: E402
from ocr_spike.probe import load_inputs  # noqa: E402


def _one_run(a, out: Path) -> None:
    u8, vw, ids = load_inputs(a)
    logits = rerun.infer(Path(a.model), u8, vw, threads=1)
    d = rerun.digest(logits, decode.load_charset(Path(a.dict)))
    d["input_sha256"] = rerun.input_sha256(u8, vw)
    d["ids"] = ids
    out.write_text(json.dumps(d))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--dict", required=True)
    ap.add_argument("--data")
    ap.add_argument("--split", default="val")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--probe")
    ap.add_argument("--out")
    ap.add_argument("--_child", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a._child:
        _one_run(a, Path(a._child))
        return 0
    import onnxruntime

    t0 = time.time()
    runs = []
    with tempfile.TemporaryDirectory() as td:
        for k in range(2):  # in-process, twice
            p = Path(td) / f"inproc{k}.json"
            _one_run(a, p)
            runs.append(("in_process_%d" % k, json.loads(p.read_text())))
        base = [sys.executable, str(Path(__file__).resolve())] + [x for x in (argv or sys.argv[1:]) if not x.startswith("--out")]
        if a.out:
            base = [x for x in base if x != a.out]
        for k in range(2):  # two separate processes
            p = Path(td) / f"proc{k}.json"
            subprocess.run(base + ["--_child", str(p)], check=True)
            runs.append(("subprocess_%d" % k, json.loads(p.read_text())))
    ref = runs[0][1]
    diffs = []
    for name, r in runs[1:]:
        for key in ("input_sha256", "logits_sha256", "texts_sha256"):
            if r[key] != ref[key]:
                diffs.append({"run": name, "field": key})
        bad = [i for i, (x, y) in enumerate(zip(ref["per_sample_sha256"], r["per_sample_sha256"])) if x != y]
        if bad:
            diffs.append({"run": name, "field": "per_sample", "n_differ": len(bad), "first": bad[:10]})
    report = {
        "verdict": "IDENTICAL" if not diffs else "DIFFERENT",
        "claim": "re-runnable with the same pinned setup (this machine only)" if not diffs else "NOT re-runnable",
        "model_sha256": ortrun.sha256_file(Path(a.model)),
        "onnxruntime": onnxruntime.__version__, "ort_runtime": ortrun.ort_identity(),
        "ort_session": ortrun.session_settings(),
        "cpu": rerun.cpu_info(), "n_inputs": ref["n"], "input_sha256": ref["input_sha256"],
        "logits_sha256": ref["logits_sha256"], "texts_sha256": ref["texts_sha256"],
        "runs": [n for n, _ in runs], "diffs": diffs, "seconds": round(time.time() - t0, 1),
    }
    txt = json.dumps(report, indent=2)
    if a.out:
        Path(a.out).write_text(txt + "\n")
    print(txt)
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
