"""Fine-tune PP-OCRv6_small_rec on the synthetic scorebug crops (CPU), export ONNX, int8-quantise, record hashes.

Steps (each is a sub-command so a long CPU run can be resumed):

    # 1. lists + PaddleOCR config (Paddle env)
    python eval/ocr_spike/finetune.py prepare --data DATA --paddleocr PADDLEOCR --init scorebug_init.pdparams --work WORK
    # 2. train (Paddle env; long). Prints the exact command; --run executes it.
    python eval/ocr_spike/finetune.py train --work WORK --paddleocr PADDLEOCR [--run] [--resume]
    # 3. export best checkpoint -> Paddle PIR inference model -> ONNX (paddle2onnx 2.x)
    python eval/ocr_spike/finetune.py export --work WORK --paddleocr PADDLEOCR --paddle2onnx /path/to/py312env/bin/paddle2onnx
    # 4. int8 static quantisation with onnxruntime (ORT env), calibration on synthetic TRAIN crops only
    python eval/ocr_spike/finetune.py quantize --work WORK --data DATA

``WORK/weights.json`` collects sha256 of every input/output weight file. Nothing here is committed weights.
SYNTHETIC TRAINING DATA ONLY; scores on synthetic data never count toward replacing the current reader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261024


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record(work: Path, key: str, value) -> None:
    p = work / "weights.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    d[key] = value
    p.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")


def cmd_prepare(a) -> int:
    sys.path.insert(0, str(HERE.parent))
    from ocr_spike.paddle_config import build_config, write_config

    data, work = Path(a.data).resolve(), Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    lines = (data / "rec_train.txt").read_text(encoding="utf-8").splitlines()
    random.Random(SEED).shuffle(lines)
    sub = lines[: a.train_crops]
    (data / "rec_train_sub.txt").write_text("\n".join(sub) + "\n", encoding="utf-8")
    val = (data / "rec_val.txt").read_text(encoding="utf-8").splitlines()[: a.val_crops]
    (data / "rec_val_small.txt").write_text("\n".join(val) + "\n", encoding="utf-8")
    base = Path(a.paddleocr) / "configs/rec/PP-OCRv6/PP-OCRv6_small_rec.yml"
    cfg = build_config(base, data, Path(a.init).resolve(), work / "out", epochs=a.epochs, batch=a.batch, lr=a.lr,
                       workers=a.workers, eval_every=a.eval_every, dict_path=str(HERE / "scorebug_dict.txt"))
    cfg["Train"]["dataset"]["label_file_list"] = [str(data / "rec_train_sub.txt")]
    write_config(cfg, work / "train.yml")
    record(work, "init_pdparams", {"path": str(Path(a.init).resolve()), "sha256": sha256(Path(a.init))})
    record(work, "base_config_sha256", sha256(base))
    record(work, "train_list", {"crops": len(sub), "sha256": sha256(data / "rec_train_sub.txt")})
    record(work, "val_small_list", {"crops": len(val), "sha256": sha256(data / "rec_val_small.txt")})
    print(work / "train.yml")
    return 0


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("OMP_NUM_THREADS", str(os.cpu_count() or 8))
    env.setdefault("FLAGS_use_mkldnn", "1")  # oneDNN kernels: ~2x faster conv fwd+bwd on this CPU
    return env


def cmd_train(a) -> int:
    work = Path(a.work).resolve()
    c = [sys.executable, "tools/train.py", "-c", str(work / "train.yml")]
    if a.resume:
        c += ["-o", f"Global.checkpoints={work / 'out' / 'latest'}"]
    print("cd", a.paddleocr, "&&", " ".join(c))
    if a.run:
        return subprocess.call(c, cwd=a.paddleocr, env=_env())
    return 0


def cmd_export(a) -> int:
    work = Path(a.work).resolve()
    ck = work / "out" / a.checkpoint
    infer = work / "infer"
    c = [sys.executable, "tools/export_model.py", "-c", str(work / "train.yml"), "-o",
         f"Global.pretrained_model={ck}", f"Global.save_inference_dir={infer}"]
    print(" ".join(c))
    rc = subprocess.call(c, cwd=a.paddleocr, env=_env())
    if rc:
        return rc
    record(work, "trained_pdparams", {"path": str(ck) + ".pdparams", "sha256": sha256(Path(str(ck) + ".pdparams"))})
    onnx = work / "scorebug_rec_fp32.onnx"
    model_file = next((infer / n for n in ("inference.json", "inference.pdmodel") if (infer / n).exists()))
    c = [a.paddle2onnx, "--model_dir", str(infer), "--model_filename", model_file.name,
         "--params_filename", "inference.pdiparams", "--save_file", str(onnx), "--opset_version", "14"]
    print(" ".join(c))
    rc = subprocess.call(c)
    if rc == 0:
        record(work, "fp32_onnx", {"path": str(onnx), "sha256": sha256(onnx)})
    return rc


def cmd_quantize(a) -> int:
    import cv2
    import numpy as np
    from onnxruntime.quantization import CalibrationDataReader, QuantFormat, QuantType, quantize_static
    from onnxruntime.quantization.shape_inference import quant_pre_process

    sys.path.insert(0, str(HERE.parent))
    from ocr_spike import preprocess

    work, data = Path(a.work).resolve(), Path(a.data).resolve()
    fp32 = work / "scorebug_rec_fp32.onnx"
    pre = work / "scorebug_rec_fp32_pre.onnx"
    quant_pre_process(str(fp32), str(pre), skip_symbolic_shape=True)
    lines = (data / "rec_train.txt").read_text(encoding="utf-8").splitlines()
    random.Random(SEED + 1).shuffle(lines)
    calib = [line.split("\t")[0] for line in lines[: a.calib]]
    name = "x"

    class Reader(CalibrationDataReader):
        def __init__(self):
            self.i = 0

        def get_next(self):
            if self.i >= len(calib):
                return None
            batch = calib[self.i:self.i + 16]
            self.i += 16
            u8, vw = preprocess.prepare([cv2.imread(str(data / p), cv2.IMREAD_COLOR) for p in batch])
            return {name: preprocess.normalize_batch(u8, vw)}

    out = work / "scorebug_rec_int8.onnx"
    # U8U8 avoids the x64 U8S8 VPMADDUBSW saturation path on non-VNNI CPUs (ORT quantization docs).
    quantize_static(str(pre), str(out), Reader(), quant_format=QuantFormat.QDQ, activation_type=QuantType.QUInt8,
                    weight_type=QuantType.QUInt8, per_channel=True, op_types_to_quantize=["Conv", "MatMul"])
    import onnxruntime

    record(work, "int8_onnx", {"path": str(out), "sha256": sha256(out), "calib_crops": len(calib),
                               "calib_source": "synthetic train split", "ort_version": onnxruntime.__version__,
                               "format": "QDQ U8U8 per-channel, Conv+MatMul"})
    print(json.dumps(json.loads((work / "weights.json").read_text()), indent=2))
    _ = np
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("prepare")
    p.add_argument("--data", required=True)
    p.add_argument("--paddleocr", required=True)
    p.add_argument("--init", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--train-crops", type=int, default=24000)
    p.add_argument("--val-crops", type=int, default=480)
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--eval-every", type=int, default=100)
    p = sp.add_parser("train")
    p.add_argument("--work", required=True)
    p.add_argument("--paddleocr", required=True)
    p.add_argument("--run", action="store_true")
    p.add_argument("--resume", action="store_true")
    p = sp.add_parser("export")
    p.add_argument("--work", required=True)
    p.add_argument("--paddleocr", required=True)
    p.add_argument("--checkpoint", default="best_accuracy")
    p.add_argument("--paddle2onnx", default="paddle2onnx", help="paddle2onnx>=2.0 executable (no cp313 wheel; use a 3.12 env)")
    p = sp.add_parser("quantize")
    p.add_argument("--work", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--calib", type=int, default=512)
    a = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "train": cmd_train, "export": cmd_export, "quantize": cmd_quantize}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
