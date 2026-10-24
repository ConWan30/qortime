"""onnxruntime CPU session with the pinned settings from ``ort_session.json`` (part of the reader identity)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SESSION_FILE = HERE / "ort_session.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def session_settings() -> dict:
    """The pinned settings and the sha256 of the committed file they come from."""
    s = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    return {"file": "eval/ocr_spike/ort_session.json", "sha256": sha256_file(SESSION_FILE),
            **{k: v for k, v in s.items() if k not in ("schema", "note")}}


def pinned_session(model_path: Path, threads: int | None = None):
    """Session built only from ``ort_session.json``. ``threads`` > 1 is allowed for bulk synthetic eval
    speed only; such a session is NOT the pinned reader setup (``sess._ocr_spike_pinned`` is False)."""
    import onnxruntime as ort

    s = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    so = ort.SessionOptions()
    so.intra_op_num_threads = int(threads or s["intra_op_num_threads"])
    so.inter_op_num_threads = int(s["inter_op_num_threads"])
    so.execution_mode = getattr(ort.ExecutionMode, s["execution_mode"])
    so.graph_optimization_level = getattr(ort.GraphOptimizationLevel, s["graph_optimization_level"])
    so.use_deterministic_compute = bool(s["use_deterministic_compute"])
    for k, v in s["session_config"].items():
        so.add_session_config_entry(k, v)
    so.log_severity_level = 3
    sess = ort.InferenceSession(str(model_path), sess_options=so, providers=list(s["providers"]))
    if sess.get_providers() != list(s["providers"]):
        raise RuntimeError(f"providers {sess.get_providers()} != pinned {s['providers']}")
    sess._ocr_spike_pinned = so.intra_op_num_threads == s["intra_op_num_threads"]
    return sess


def run(sess, x: np.ndarray, batch: int = 32) -> np.ndarray:
    name = sess.get_inputs()[0].name
    outs = [sess.run(None, {name: x[i:i + batch]})[0] for i in range(0, len(x), batch)]
    return np.concatenate(outs, 0) if outs else np.zeros((0,), np.float32)


def ort_identity() -> dict:
    """onnxruntime version + sha256 of the native module that ran (for a readers.json ``runtimes`` entry)."""
    import onnxruntime as ort
    from onnxruntime.capi import onnxruntime_pybind11_state as st

    return {"id": "onnxruntime", "version": ort.__version__, "sha256": sha256_file(Path(st.__file__))}
