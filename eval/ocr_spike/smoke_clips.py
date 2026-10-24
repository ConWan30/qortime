"""SMOKE TEST ONLY: current reader vs zero-shot PP-OCRv6 vs fine-tuned PP-OCRv6 on the 3 public 170123 clips.

Reads only ``site/matches/170123/clips/*.mp4`` (public, hash-locked in ``clips/manifest.json``; sha256 checked
before use). Samples 4 fps like QorCap. The current reader is QorTime's vendored ``ScorebugReader`` on the
640x360 ``INTER_AREA`` frame (QorCap's ``prep``), optionally with QorCap's ``standard_reader`` extra templates
(``--qorcap PATH``). OCR crops are the anchored 1280x720 ROIs (reader anchor offset x2).

This is a plumbing/agreement smoke test on 3 clips from ONE session. It is NOT accuracy evidence and NOT
evidence that any reader is better. Replacement decisions need Con's blind, game-split, frozen labelled set.

    python eval/ocr_spike/smoke_clips.py --zeroshot FP32.onnx --zeroshot-dict ppocrv6_dict.txt \
        --finetuned INT8.onnx [--qorcap ../qorcap] --out smoke.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

from ocr_spike import decode, ortrun, preprocess  # noqa: E402
from ocr_spike.eval_rec import FIELD_ALPHABET  # noqa: E402
from ocr_spike.roi import FIELD_NAMES, crop_field  # noqa: E402

CLIPS = ROOT / "site" / "matches" / "170123" / "clips"
READ_FIELDS = ("score_l", "score_r", "quarter", "game_clock")
QWORD = {1: "1ST", 2: "2ND", 3: "3RD", 4: "4TH", 5: "OT"}


def make_reader(qorcap: str | None):
    if qorcap:
        sys.path.insert(0, str(Path(qorcap) / "sidecar"))
        from qorcap_standard import standard_reader  # type: ignore

        return standard_reader(), "qorcap.standard_reader (QorTime ScorebugReader + 110028/084523 templates)"
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader

    return ScorebugReader(), "qortime ScorebugReader (madden27_standard_v1)"


def current_fields(fr) -> dict[str, str | None]:
    return {
        "score_l": None if fr.left_score is None else str(fr.left_score),
        "score_r": None if fr.right_score is None else str(fr.right_score),
        "quarter": QWORD.get(fr.quarter) if fr.quarter else None,
        "game_clock": fr.clock_text,
    }


def frames_4fps(path: Path):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = fps / 4.0
    i, nxt = 0, 0.0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i >= nxt - 1e-6:
            yield int(round(i * 1000 / fps)), fr
            nxt += step
        i += 1
    cap.release()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zeroshot", required=True)
    ap.add_argument("--zeroshot-dict", required=True)
    ap.add_argument("--finetuned")
    ap.add_argument("--finetuned-dict", default=str(Path(__file__).resolve().parent / "scorebug_dict.txt"))
    ap.add_argument("--qorcap")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    manifest = json.loads((CLIPS / "manifest.json").read_text(encoding="utf-8"))
    reader, reader_desc = make_reader(a.qorcap)
    models = {"zeroshot": (Path(a.zeroshot), decode.load_charset(Path(a.zeroshot_dict)))}
    if a.finetuned:
        models["finetuned"] = (Path(a.finetuned), decode.load_charset(Path(a.finetuned_dict)))
    sessions = {k: ortrun.pinned_session(p, threads=1) for k, (p, _) in models.items()}
    rows = []
    for clip in manifest["clips"]:
        path = CLIPS / clip["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != clip["sha256"]:
            print(f"{clip['id']}: sha256 does not match manifest.json", file=sys.stderr)
            return 1
        for t_ms, frame in frames_4fps(path):
            small = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
            fr = reader.read(small)
            dx, dy = fr.offset if fr.offset else (0, 0)
            cur = current_fields(fr)
            crops = [crop_field(frame, f, 2 * dx, 2 * dy) for f in FIELD_NAMES]
            u8, vw = preprocess.prepare(crops)
            x = preprocess.normalize_batch(u8, vw)
            row = {"clip": clip["id"], "t_ms": t_ms, "reader_ok": fr.ok, "reader_reason": fr.reason, "offset": [dx, dy],
                   "current": cur, "score_margin": fr.score_margin}
            for k, sess in sessions.items():
                logits = sess.run(None, {sess.get_inputs()[0].name: x})[0]
                cs = models[k][1]
                row[k] = {f: decode.greedy(logits[i:i + 1], cs, decode.allowed_mask(cs, FIELD_ALPHABET[f]))[0][0].upper()
                          for i, f in enumerate(FIELD_NAMES)}
            rows.append(row)
    table = {}
    for f in READ_FIELDS:
        t = {"frames": len(rows), "current_read": sum(r["current"][f] is not None for r in rows)}
        for k in models:
            both = [r for r in rows if r["current"][f] is not None]
            t[f"{k}_agrees_when_current_read"] = sum(r[k][f] == r["current"][f] for r in both)
            t[f"{k}_nonempty_when_current_blank"] = sum(bool(r[k][f]) for r in rows if r["current"][f] is None)
        if len(models) == 2:
            t["zeroshot_eq_finetuned_all_frames"] = sum(r["zeroshot"][f] == r["finetuned"][f] for r in rows)
        table[f] = t
    for f in ("team_l", "team_r", "play_clock", "down_distance"):
        if len(models) == 2:
            table[f] = {"frames": len(rows), "current_read": "not read by current reader",
                        "zeroshot_eq_finetuned_all_frames": sum(r["zeroshot"][f] == r["finetuned"][f] for r in rows)}
    out = {"label": "SMOKE TEST ONLY - 3 public clips, one session, not accuracy evidence, not evidence of superiority",
           "current_reader": reader_desc, "models": {k: {"path": str(p), "sha256": ortrun.sha256_file(p)} for k, (p, _) in models.items()},
           "clips": [{"id": c["id"], "sha256": c["sha256"]} for c in manifest["clips"]], "agreement": table, "rows": rows}
    txt = json.dumps(out, indent=2)
    if a.out:
        Path(a.out).write_text(txt + "\n")
    print(json.dumps({"label": out["label"], "agreement": table}, indent=2))
    _ = np
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
