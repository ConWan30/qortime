"""Re-run check helpers: pinned single-thread inference and byte-level digests of the outputs."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

import numpy as np

from . import decode, ortrun, preprocess


def cpu_info() -> dict:
    """CPU identity + SIMD features (numpy's runtime dispatch table works on Linux, macOS and Windows)."""
    feats: dict[str, bool] = {}
    try:
        from numpy._core._multiarray_umath import __cpu_features__ as cf  # numpy >= 2
    except ImportError:  # pragma: no cover
        from numpy.core._multiarray_umath import __cpu_features__ as cf
    feats = {k: bool(v) for k, v in cf.items()}
    info = {"machine": platform.machine(), "system": platform.system(), "processor": platform.processor(),
            "numpy_cpu_features": sorted(k for k, v in feats.items() if v)}
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        txt = cpuinfo.read_text(errors="replace")
        for line in txt.splitlines():
            if line.startswith("model name") or line.startswith("Model") or line.startswith("CPU part"):
                info.setdefault("model_name", line.split(":", 1)[1].strip())
            if line.startswith("flags") or line.startswith("Features"):
                fl = line.split(":", 1)[1].split()
                info["isa_flags_of_interest"] = sorted(f for f in fl if any(
                    k in f for k in ("avx", "vnni", "amx", "fma", "f16c", "asimd", "i8mm", "dotprod", "sve", "bf16")))
                break
    return info


def infer(model: Path, u8: np.ndarray, vw: np.ndarray, threads: int = 1, batch: int = 1) -> np.ndarray:
    """Pinned session; batch=1 by default so the result does not depend on how samples are grouped."""
    sess = ortrun.pinned_session(model, threads=threads)
    return ortrun.run(sess, preprocess.normalize_batch(u8, vw), batch=batch)


def digest(logits: np.ndarray, charset: list[str]) -> dict:
    texts = [t for t, _ in decode.greedy(logits, charset)]
    per = [hashlib.sha256(np.ascontiguousarray(logits[i]).tobytes()).hexdigest() for i in range(logits.shape[0])]
    return {
        "n": int(logits.shape[0]), "shape": list(logits.shape), "dtype": str(logits.dtype),
        "logits_sha256": hashlib.sha256(np.ascontiguousarray(logits).tobytes()).hexdigest(),
        "texts_sha256": hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest(),
        "per_sample_sha256": per, "texts": texts,
    }


def input_sha256(u8: np.ndarray, vw: np.ndarray) -> str:
    h = hashlib.sha256(np.ascontiguousarray(u8).tobytes())
    h.update(np.ascontiguousarray(vw.astype("<i4")).tobytes())
    return h.hexdigest()
