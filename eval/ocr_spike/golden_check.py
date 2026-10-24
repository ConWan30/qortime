"""Cross-machine golden check: pinned weights x committed synthetic probe -> output hashes vs committed golden.

    python eval/ocr_spike/golden_check.py --cache .cache/ocr_spike --report report.json      # check (CI)
    python eval/ocr_spike/golden_check.py --cache DIR --write-golden --model-file scorebug_ft_int8=PATH  # box

For every model in ``pinned_models.json`` it downloads (or derives) the file, verifies the sha256 BEFORE running,
runs it single-threaded with the pinned session on the probe, and compares the logits-bytes hash and the decoded
strings hash with ``golden.json``. Exit 1 on any mismatch. Unavailable models (``url: null``) are reported as
skipped, never as passed. CPU features are recorded so an int8-kernel difference (e.g. VNNI vs not) is visible.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_spike import decode, ortrun, rerun  # noqa: E402
from ocr_spike.probe import PROBE_DIR, load_probe  # noqa: E402

HERE = Path(__file__).resolve().parent
PINNED = HERE / "pinned_models.json"
GOLDEN = HERE / "golden.json"


def _fetch(url: str, dest: Path, sha: str) -> Path:
    if not dest.exists() or ortrun.sha256_file(dest) != sha:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.replace(dest)
    got = ortrun.sha256_file(dest)
    if got != sha:
        raise SystemExit(f"sha256 mismatch for {url}: got {got}, pinned {sha}")
    return dest


def derive_quantize_dynamic(src: Path, dest: Path) -> Path:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    quantize_dynamic(str(src), str(dest), weight_type=QuantType.QUInt8, per_channel=True,
                     op_types_to_quantize=["MatMul", "Conv"])
    return dest


def resolve_models(cache: Path, overrides: dict[str, str]) -> dict[str, dict]:
    pin = json.loads(PINNED.read_text())
    full = pin["dict"]["full"]
    dicts = {"full": _fetch(full["url"], cache / full["file"], full["sha256"]), "scorebug": HERE / pin["dict"]["scorebug"]["path"]}
    if ortrun.sha256_file(dicts["scorebug"]) != pin["dict"]["scorebug"]["sha256"]:
        raise SystemExit("scorebug_dict.txt changed")
    out = {}
    for name, m in pin["models"].items():
        rec = {"desc": m["desc"], "dict": dicts[m["dict"]], "pinned_sha256": m.get("sha256")}
        try:
            if name in overrides:
                rec["path"] = Path(overrides[name])
            elif m.get("url"):
                rec["path"] = _fetch(m["url"], cache / m["file"], m["sha256"])
            elif m.get("derive"):
                src = out[m["derive"]["from"]].get("path")
                if src is None:
                    raise FileNotFoundError("source model unavailable")
                rec["path"] = derive_quantize_dynamic(src, cache / m["file"])
            else:
                rec["skip"] = "not hosted (url is null)"
        except (OSError, FileNotFoundError) as e:
            rec["skip"] = f"unavailable: {type(e).__name__}: {e}"
        if "path" in rec:
            rec["sha256"] = ortrun.sha256_file(rec["path"])
            if m.get("sha256") and rec["sha256"] != m["sha256"] and name not in overrides:
                rec["error"] = f"sha256 {rec['sha256']} != pinned {m['sha256']}"
        out[name] = rec
    return out


def main(argv=None) -> int:
    import onnxruntime

    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache/ocr_spike")
    ap.add_argument("--report")
    ap.add_argument("--write-golden", action="store_true")
    ap.add_argument("--model-file", action="append", default=[], help="NAME=PATH override (box only)")
    a = ap.parse_args(argv)
    overrides = dict(x.split("=", 1) for x in a.model_file)
    u8, vw, ids, meta = load_probe(PROBE_DIR)
    in_sha = rerun.input_sha256(u8, vw)
    models = resolve_models(Path(a.cache), overrides)
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {"models": {}}
    sess_pin = ortrun.session_settings()
    report = {"onnxruntime": onnxruntime.__version__, "ort_runtime": ortrun.ort_identity(), "cpu": rerun.cpu_info(),
              "ort_session": sess_pin,
              "probe_input_sha256": in_sha, "probe_input_matches": in_sha == meta.get("preprocessed_input_sha256"),
              "models": {}}
    failed = not report["probe_input_matches"]
    if a.write_golden:
        golden["ort_session"] = sess_pin
    elif golden.get("ort_session", {}).get("sha256") != sess_pin["sha256"]:
        report["ort_session_matches_golden"] = False  # golden hashes only hold under the pinned session settings
        failed = True
    else:
        report["ort_session_matches_golden"] = True
    for name, m in models.items():
        r = {"desc": m["desc"]}
        if "skip" in m or "error" in m:
            r.update({k: m[k] for k in ("skip", "error") if k in m})
            failed |= "error" in m
            report["models"][name] = r
            continue
        logits = rerun.infer(m["path"], u8, vw, threads=1)
        d = rerun.digest(logits, decode.load_charset(m["dict"]))
        r.update({"model_sha256": m["sha256"], "logits_sha256": d["logits_sha256"], "texts_sha256": d["texts_sha256"]})
        if a.write_golden:
            golden["models"][name] = {"model_sha256": m["sha256"], "logits_sha256": d["logits_sha256"],
                                      "texts_sha256": d["texts_sha256"], "per_sample_sha256": d["per_sample_sha256"],
                                      "texts": d["texts"], "made_on": {"cpu": report["cpu"], "onnxruntime": onnxruntime.__version__}}
            r["status"] = "golden_written"
        else:
            g = golden["models"].get(name)
            if g is None:
                r["status"] = "no_golden"
            elif g["model_sha256"] != m["sha256"]:
                r["status"] = "golden_for_other_weights"
                failed = True
            else:
                same_bytes = g["logits_sha256"] == d["logits_sha256"]
                same_text = g["texts_sha256"] == d["texts_sha256"]
                r["status"] = "match" if same_bytes and same_text else "MISMATCH"
                r["logits_bytes_identical"] = same_bytes
                r["texts_identical"] = same_text
                r["samples_bytes_differ"] = sum(x != y for x, y in zip(g["per_sample_sha256"], d["per_sample_sha256"]))
                r["samples_text_differ"] = [ids[i] for i, (x, y) in enumerate(zip(g["texts"], d["texts"])) if x != y]
                failed |= r["status"] != "match"
        report["models"][name] = r
    if a.write_golden:
        golden["probe_input_sha256"] = in_sha
        golden["note"] = ("Made on the box listed per model. Equality elsewhere is what the CI matrix tests; "
                          "a mismatch is a finding, not something to re-bless silently.")
        GOLDEN.write_text(json.dumps(golden, indent=2) + "\n")
    txt = json.dumps(report, indent=2)
    if a.report:
        Path(a.report).write_text(txt + "\n")
    print(txt)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
