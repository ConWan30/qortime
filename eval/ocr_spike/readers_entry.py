"""Emit a readers.json-shaped document (``qorcap-readers/v0``, as defined by open qortime PR #70) for the spike reader.

Shape copied from PR #70 ``schema/contracts/qorcap-readers.v0.json`` at c04d319 (closed objects):
``{schema, qorcap_version, readers:[{id, version, files:[{kind: code|pack, path, sha256}]}],
runtimes:[{id, version, sha256}]}``. The ONNX weight file is a ``pack``; onnxruntime is a ``runtime`` whose
sha256 is the native module that ran. The pinned ORT session settings (``ort_session.json``: CPUExecutionProvider,
ORT_ENABLE_BASIC, 1/1 threads, sequential, deterministic compute) are listed as a ``code`` file so they are part
of the reader identity; #70's closed schema has no other place for them. SPIKE ONLY, not for production bundles:
nothing writes it into a bundle.

    python eval/ocr_spike/readers_entry.py --model scorebug_rec_int8.onnx [--qorcap-version 0.3.32]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_spike import ortrun  # noqa: E402

HERE = Path(__file__).resolve().parent
CODE_FILES = ("roi.py", "preprocess.py", "decode.py", "ortrun.py", "ort_session.json")
_ID = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")


def build(model: Path, qorcap_version: str, reader_version: str = "0.1-spike", model_path_in_bundle: str | None = None) -> dict:
    files = [{"kind": "code", "path": f"eval/ocr_spike/{n}", "sha256": ortrun.sha256_file(HERE / n)} for n in CODE_FILES]
    files.append({"kind": "code", "path": "eval/ocr_spike/scorebug_dict.txt", "sha256": ortrun.sha256_file(HERE / "scorebug_dict.txt")})
    files.append({"kind": "pack", "path": model_path_in_bundle or f"models/{model.name}", "sha256": ortrun.sha256_file(model)})
    return {
        "schema": "qorcap-readers/v0",
        "qorcap_version": qorcap_version,
        "readers": [{"id": "ppocrv6_small_rec_scorebug_spike", "version": reader_version, "files": files}],
        "runtimes": [ortrun.ort_identity()],
    }


def check_shape(doc: dict) -> list[str]:
    """Mirror of PR #70's closed-schema rules (no import of #70 code; schema/ is untouched)."""
    errs = []
    if set(doc) != {"schema", "qorcap_version", "readers", "runtimes"}:
        errs.append("top-level keys")
    if doc.get("schema") != "qorcap-readers/v0":
        errs.append("schema const")
    if not (isinstance(doc.get("qorcap_version"), str) and 1 <= len(doc["qorcap_version"]) <= 80):
        errs.append("qorcap_version")
    for r in doc.get("readers") or [None]:
        if not isinstance(r, dict) or set(r) != {"id", "version", "files"} or not r["files"]:
            errs.append("reader shape")
            continue
        for f in r["files"]:
            if set(f) != {"kind", "path", "sha256"} or f["kind"] not in ("code", "pack") or not _HEX.match(f["sha256"]):
                errs.append(f"file {f}")
            if len(f.get("path", "")) > 240 or re.search(r"\.(key|pem|p12|pfx)$", f.get("path", ""), re.I):
                errs.append(f"path {f.get('path')}")
    for rt in doc.get("runtimes") or [None]:
        if not isinstance(rt, dict) or set(rt) != {"id", "version", "sha256"} or not _ID.match(rt["id"]) or not _HEX.match(rt["sha256"]):
            errs.append(f"runtime {rt}")
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--qorcap-version", default="0.3.32")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    doc = build(Path(a.model), a.qorcap_version)
    errs = check_shape(doc)
    txt = json.dumps(doc, indent=2)
    if a.out:
        Path(a.out).write_text(txt + "\n")
    print(txt)
    if errs:
        print("shape errors:", errs, file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
