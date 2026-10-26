"""``hands.json`` sidecar (qortime.hands.v0): build, sanitize, lower-only cap (hackathon work)."""
from __future__ import annotations

import copy
import re
from typing import Any

from qor_receipt.ai_ruling.signing import receipt_jcs_sha256
from qor_receipt.hands.inputs import verify_inputs
from qor_receipt.hands.press_to_pixel import score_match
from qor_receipt.hands.track import build_track

SCHEMA = "qortime.hands.v0"
PARTIAL = "PARTIAL"
CAP_CODE = "show_your_hands_flags"
MIN_FLAGS = 2
LABEL = ("ADVISORY, LOWER-ONLY. Show Your Hands overlays the verified controller inputs on the clip and checks that "
         "on-screen actions have a press behind them. It can only lower VERIFIED to PARTIAL, never raise a status "
         "or fail a match.")
BUILT = "Meta AI Developer Hackathon, Oct 26 - Nov 4 2026"
_BAD_KEYS = re.compile(r"path|instance|serial|(^|_)(vid|pid)($|_)|gamertag|psn|device_id|hid_id|usb_id", re.I)
_BAD_VALS = re.compile(r"HID\\|USB\\|VID_|PID_|\\\\\?\\|&MI_|\{[0-9a-f-]{36}\}", re.I)


def sanitize(obj: Any) -> Any:
    """Drop any key that could identify the pad or player; refuse strings that look like device paths."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if not _BAD_KEYS.search(str(k))}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, str) and _BAD_VALS.search(obj):
        return "[removed]"
    return obj


def decide_cap(clips: list[dict[str, Any]]) -> str | None:
    n = sum(len(c["flags"]) for c in clips if c.get("scorable"))
    return PARTIAL if n >= MIN_FLAGS else None


def build(receipt: dict[str, Any], records: list[dict[str, Any]], chunks: list[dict[str, Any]],
          clips: list[dict[str, Any]], *, role: str = "away", with_track: bool = True,
          context: dict[str, Any] | None = None) -> dict[str, Any]:
    """clips: [{id, start_mono_ms, duration_s, actions_ms:[...], stimuli_ms:[...]}] on the capture clock.

    ``context`` (optional) is the Muse Spark action-filter report (calls, cost, per-event labels); recorded as is.
    """
    chk = verify_inputs(records, chunks, receipt, role)
    doc: dict[str, Any] = {"schema": SCHEMA, "label": LABEL, "built_during": BUILT, "affects_verdict": "lower_only",
                           "max_cap": PARTIAL, "receipt_jcs_sha256": receipt_jcs_sha256(receipt), "role": role,
                           "inputs_verified": chk.ok, "clips": [], "cap": None}
    if not chk.ok:
        doc["problems"] = chk.problems[:10]
        doc["summary"] = "Inputs don't match the receipt: no overlay, no flags."
        return sanitize(doc)
    doc["inputs"] = {"reports": len(chk.records), "chunks": sum(1 for c in chunks if c.get("n", 1)),
                     "all_chunks_committed": True}
    rows = []
    for c in clips:
        t0, dur = int(c["start_mono_ms"]), float(c.get("duration_s", 12.0))
        rs = [r for r in chk.records if t0 * 1000 - 2_000_000 <= r["t_us"] <= (t0 + dur * 1000) * 1000]
        rows.append({"id": c.get("id", "clip"), "records": rs, "actions_ms": list(c.get("actions_ms") or []),
                     "stimuli_ms": list(c.get("stimuli_ms") or []), "t0": t0, "dur": dur, "fps": int(c.get("fps", 30))})
    scored = score_match(rows)
    for row, res in zip(rows, scored):
        if with_track:
            res["track"] = build_track(row["records"], row["t0"], row["dur"], row["fps"])
        doc["clips"].append(res)
    if context is not None:
        doc["action_filter"] = context
    doc["cap"] = decide_cap(doc["clips"])
    flagged = sum(len(c["flags"]) for c in doc["clips"] if c["scorable"])
    scorable = sum(1 for c in doc["clips"] if c["scorable"])
    doc["summary"] = (f"{flagged} flag(s) in {scorable} scorable clip(s) of {len(doc['clips'])}."
                      if scorable else "No clip could be aligned to the inputs: nothing scored, no penalty.")
    return sanitize(doc)


def check_hands(doc: Any, receipt: dict[str, Any], *, keys: dict[str, Any] | None = None) -> list[str]:
    """Problems with a signed hands.json for this receipt. Empty list = it may be shown / applied (lower-only)."""
    from qor_receipt.ai_ruling.signing import check_signed

    p = check_signed(doc, receipt, schema=SCHEMA, keys=keys)
    if p == ["not_an_object"]:
        return p
    if doc.get("affects_verdict") != "lower_only" or doc.get("max_cap") != PARTIAL:
        p.append("not_lower_only")
    if doc.get("cap") not in (None, PARTIAL):
        p.append("bad_cap")
    clips = doc.get("clips") if isinstance(doc.get("clips"), list) else []
    if doc.get("cap") == PARTIAL and (not doc.get("inputs_verified") or decide_cap(clips) != PARTIAL):
        p.append("cap_inconsistent")
    if sanitize(doc) != doc:
        p.append("identifying_data")
    return p


def apply_cap(status: str, cap: str | None) -> str:
    if cap is None:
        return status
    if cap != PARTIAL:
        raise ValueError("hands cap must be PARTIAL or None")
    return PARTIAL if status == "VERIFIED" else status


def apply_to_result(v: dict[str, Any], doc: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(v)
    if not doc or not doc.get("inputs_verified") or doc.get("affects_verdict") != "lower_only":
        return out
    new = apply_cap(out["status"], doc.get("cap"))
    if new != out["status"]:
        out["status"] = new
        out.setdefault("partial", []).append(CAP_CODE)
    return out


def main(argv: list[str] | None = None) -> int:
    """python -m qor_receipt.hands.sidecar RECEIPT INPUTS_JSONL CLIPS_MANIFEST SEGMENTS_JSONL --clips DIR
    [--muse] [--max-calls N] [--no-track] [--sign-key PATH] [--out FILE]

    Inputs stay local. ``--no-track`` leaves the per-frame input track out (use it for anything published).
    """
    import argparse
    import json
    from pathlib import Path

    from qor_receipt.hands.muse_context import MuseContext, filter_actions
    from qor_receipt.hands.video_events import detect, energy_curve
    from qor_receipt.review.clock import capture_clock

    ap = argparse.ArgumentParser(prog="qor_receipt.hands.sidecar")
    ap.add_argument("receipt"); ap.add_argument("inputs"); ap.add_argument("clips_manifest"); ap.add_argument("segments")  # noqa: E702
    ap.add_argument("--clips", required=True); ap.add_argument("--role", default="away")  # noqa: E702
    ap.add_argument("--muse", action="store_true"); ap.add_argument("--max-calls", type=int, default=12)  # noqa: E702
    ap.add_argument("--no-track", action="store_true"); ap.add_argument("--sign-key"); ap.add_argument("--out")  # noqa: E702
    a = ap.parse_args(argv)
    receipt = json.loads(Path(a.receipt).read_text(encoding="utf-8"))
    recs = [json.loads(x) for x in Path(a.inputs).read_text(encoding="utf-8").splitlines() if x.strip()]
    data, chunks = [r for r in recs if r.get("type") != "chunk"], [r for r in recs if r.get("type") == "chunk"]
    segs = [json.loads(x) for x in Path(a.segments).read_text(encoding="utf-8").splitlines() if x.strip()]
    clock = capture_clock([s for s in segs if s.get("type") == "segment"])
    man = json.loads(Path(a.clips_manifest).read_text(encoding="utf-8"))
    clips, cands = [], []
    for c in man.get("clips", []):
        f = Path(a.clips) / c["file"]
        if not f.exists():
            continue
        t0 = clock[0] + clock[1] * float(c["source"]["start_s"]) * 30.0
        e, cc, fps = energy_curve(str(f))
        evs = [dict(ev, clip=c["id"], t0=t0, path=str(f)) for ev in detect(e, cc, fps)]
        cands += evs
        clips.append({"id": c["id"], "start_mono_ms": round(t0), "duration_s": float(c.get("duration_s", 12.0)), "events": evs})
    report = None
    if a.muse:
        kept, report = filter_actions(cands, _frames_for, MuseContext(), max_calls=a.max_calls)
    else:
        kept = []          # no Muse => no confirmed actions => nothing scored (fail closed)
    for c in clips:
        c["actions_ms"] = [round(ev["t0"] + ev["t_s"] * 1000) for ev in kept if ev["clip"] == c["id"]]
        c.pop("events")
    doc = build(receipt, data, chunks, clips, role=a.role, with_track=not a.no_track, context=report)
    if a.sign_key:
        from qor_receipt.ai_ruling.signing import sign_ruling
        from qor_receipt.keys import FileKey

        doc = sign_ruling(doc, FileKey(Path(a.sign_key)))
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def _frames_for(ev: dict[str, Any]) -> tuple[bytes, bytes]:
    import cv2

    cap = cv2.VideoCapture(ev["path"])
    out = []
    for dt in (-0.4, 0.6):
        cap.set(cv2.CAP_PROP_POS_MSEC, (float(ev["t_s"]) + dt) * 1000)
        ok, f = cap.read()
        if not ok:
            raise ValueError("frame")
        out.append(cv2.imencode(".jpg", cv2.resize(f, (640, 360)), [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes())
    cap.release()
    return out[0], out[1]


if __name__ == "__main__":
    raise SystemExit(main())
