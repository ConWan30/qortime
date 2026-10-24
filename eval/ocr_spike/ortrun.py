"""onnxruntime CPU session with the pinned "same setup" options used for re-run checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pinned_session(model_path: Path, threads: int = 1):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    so.use_deterministic_compute = True
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    so.log_severity_level = 3
    return ort.InferenceSession(str(model_path), sess_options=so, providers=["CPUExecutionProvider"])


def run(sess, x: np.ndarray, batch: int = 32) -> np.ndarray:
    name = sess.get_inputs()[0].name
    outs = [sess.run(None, {name: x[i:i + batch]})[0] for i in range(0, len(x), batch)]
    return np.concatenate(outs, 0) if outs else np.zeros((0,), np.float32)


def ort_identity() -> dict:
    """onnxruntime version + sha256 of the native module that ran (for a readers.json ``runtimes`` entry)."""
    import onnxruntime as ort
    from onnxruntime.capi import onnxruntime_pybind11_state as st

    return {"id": "onnxruntime", "version": ort.__version__, "sha256": sha256_file(Path(st.__file__))}
