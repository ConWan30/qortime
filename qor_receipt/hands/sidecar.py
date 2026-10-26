"""``hands.json`` sidecar (qortime.hands.v0): build, sanitize, lower-only cap (hackathon work)."""
from __future__ import annotations

import copy
import re
from typing import Any

from qor_receipt.ai_ruling.signing import receipt_jcs_sha256
from qor_receipt.hands.inputs import verify_inputs
from qor_receipt.hands.press_to_pixel import score_clip
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
          clips: list[dict[str, Any]], *, role: str = "away", with_track: bool = True) -> dict[str, Any]:
    """clips: [{id, start_mono_ms, duration_s, actions_ms:[...], stimuli_ms:[...]}] on the capture clock."""
    chk = verify_inputs(records, chunks, receipt, role)
    doc: dict[str, Any] = {"schema": SCHEMA, "label": LABEL, "built_during": BUILT, "affects_verdict": "lower_only",
                           "max_cap": PARTIAL, "receipt_jcs_sha256": receipt_jcs_sha256(receipt), "role": role,
                           "inputs_verified": chk.ok, "clips": [], "cap": None}
    if not chk.ok:
        doc["problems"] = chk.problems[:10]
        doc["summary"] = "Inputs don't match the receipt: no overlay, no flags."
        return sanitize(doc)
    for c in clips:
        t0, dur = int(c["start_mono_ms"]), float(c.get("duration_s", 12.0))
        rs = [r for r in chk.records if t0 * 1000 - 2_000_000 <= r["t_us"] <= (t0 + dur * 1000) * 1000]
        res = score_clip(rs, list(c.get("actions_ms") or []), list(c.get("stimuli_ms") or []), str(c.get("id", "clip")))
        if with_track:
            res["track"] = build_track(rs, t0, dur, int(c.get("fps", 30)))
        doc["clips"].append(res)
    doc["cap"] = decide_cap(doc["clips"])
    flagged = sum(len(c["flags"]) for c in doc["clips"] if c["scorable"])
    scorable = sum(1 for c in doc["clips"] if c["scorable"])
    doc["summary"] = (f"{flagged} flag(s) in {scorable} scorable clip(s) of {len(doc['clips'])}."
                      if scorable else "No clip could be aligned: nothing scored, no penalty.")
    return sanitize(doc)



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
