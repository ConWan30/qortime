"""``hands.json`` sidecar (qortime.hands.v0): build, sanitize, lower-only cap (hackathon work)."""
from __future__ import annotations

import copy
import math
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


_RATIONAL = re.compile(r"([0-9]+)/([0-9]+)", re.ASCII)
# "60000/1001" is 11 characters. A 400-digit numerator used to OverflowError on num/den
# (QorProof INFO Q1). Bound the field before int() or division so that becomes a clean refusal.
_MAX_FRAME_RATE_CHARS = 32
# Highlight clips are ~12 s. 24 h is far above a match and keeps duration_s away from overflow.
_DEFAULT_DURATION_S = 12.0
_MAX_DURATION_S = 86_400.0
_CONTROLS = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


class ClipInputError(ValueError):
    """A highlight clip's source, start_s, duration_s, file or id is missing or not usable. Never guessed."""


class ClipRateError(ClipInputError):
    """A highlight clip's ``source.frame_rate`` is present but not a usable rate. Never guessed."""


def _safe_label(value: object) -> str:
    """Single-line label for error text. Never interpolates raw control characters."""
    if isinstance(value, str):
        value = _CONTROLS.sub("\ufffd", value)
    return ascii(value)


def _usable_clip_name(value: object, *, kind: str, clip_id: object = "?") -> str:
    """A single path component: relative, not '.' / '..', no separators or control characters.

    qorcap writes highlights ``file`` as Path.name (a basename) and ``id`` as the stem,
    e.g. ``hl00-q3-a3-h0.mp4``. Published manifests use the same shape. Subdirectories
    are not part of the format, so ``/`` and ``\\`` are refused rather than accepted.
    """
    from pathlib import Path

    owner = clip_id if kind == "file" else value
    if not isinstance(value, str) or not value or value.strip() != value or value in (".", ".."):
        raise ClipInputError(
            f"clip {_safe_label(owner)}: {kind} is missing or not a usable name; "
            "refusing to place it on the capture clock (fail closed)"
        )
    if (
        _CONTROLS.search(value)
        or "/" in value
        or "\\" in value
        or ":" in value
        or Path(value).is_absolute()
        or Path(value).name != value
    ):
        raise ClipInputError(
            f"clip {_safe_label(owner)}: {kind} is not a safe name; "
            "refusing to place it on the capture clock (fail closed)"
        )
    return value


def clip_id_and_file(clip: Any) -> tuple[str, str]:
    """Return ``(id, file)``. Both must be present safe names. Never guessed."""
    if not isinstance(clip, dict):
        raise ClipInputError(
            "highlight clip is not an object; refusing to place it on the capture clock (fail closed)"
        )
    cid = _usable_clip_name(clip.get("id"), kind="id")
    fname = _usable_clip_name(clip.get("file"), kind="file", clip_id=cid)
    return cid, fname


def clip_file_in_dir(clips_dir: Any, fname: str, *, clip_id: str = "?") -> Any:
    """Resolve ``fname`` inside ``clips_dir``. Refuse if the resolved path would leave it."""
    from pathlib import Path

    try:
        base = Path(clips_dir).resolve()
    except OSError:
        raise ClipInputError(
            f"clip {_safe_label(clip_id)}: clips directory is not usable; "
            "refusing to place it on the capture clock (fail closed)"
        ) from None
    dest = (base / fname).resolve()
    if dest == base or base not in dest.parents:
        raise ClipInputError(
            f"clip {_safe_label(clip_id)}: file is not inside the clips directory; "
            "refusing to place it on the capture clock (fail closed)"
        )
    return dest


def clip_start_s(clip: dict[str, Any]) -> float:
    """The clip's ``source.start_s`` as a finite number of seconds ≥ 0. Never guessed.

    ``source`` must be a dict. ``start_s`` must be an ``int`` or ``float`` (not ``bool``,
    not a string) that is finite and ≥ 0. Anything else raises ClipInputError.
    """
    cid = clip.get("id", "?") if isinstance(clip, dict) else "?"
    src = clip.get("source") if isinstance(clip, dict) else None
    if not isinstance(src, dict):
        raise ClipInputError(
            f"clip {_safe_label(cid)}: source is missing or not an object; "
            "refusing to place it on the capture clock (fail closed)"
        )
    if "start_s" not in src:
        raise ClipInputError(
            f"clip {_safe_label(cid)}: source.start_s is missing; "
            "refusing to place it on the capture clock (fail closed)"
        )
    raw = src["start_s"]
    # bool is a subclass of int, so True/False would otherwise pass as 1 or 0.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ClipInputError(
            f"clip {_safe_label(cid)}: source.start_s {_safe_label(raw)} is not a finite start time ≥ 0; "
            "refusing to place it on the capture clock (fail closed)"
        )
    try:
        start = float(raw)
    except OverflowError:
        start = float("inf")
    if raw < 0 or not math.isfinite(start):
        raise ClipInputError(
            f"clip {_safe_label(cid)}: source.start_s {_safe_label(raw)} is not a finite start time ≥ 0; "
            "refusing to place it on the capture clock (fail closed)"
        )
    return start


def clip_duration_s(clip: dict[str, Any]) -> float:
    """The clip's ``duration_s`` as a finite number of seconds > 0 and ≤ 24 h. Never guessed.

    Absent means 12, the historical default. Present, it must be an ``int`` or ``float``
    (not ``bool``, not a string) that is finite, > 0, and at most ``_MAX_DURATION_S``.
    Anything else raises ClipInputError.
    """
    cid = clip.get("id", "?") if isinstance(clip, dict) else "?"
    if not isinstance(clip, dict) or "duration_s" not in clip:
        return _DEFAULT_DURATION_S
    raw = clip["duration_s"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ClipInputError(
            f"clip {_safe_label(cid)}: duration_s {_safe_label(raw)} is not a finite duration > 0 and ≤ 24 h; "
            "refusing to place it on the capture clock (fail closed)"
        )
    try:
        dur = float(raw)
    except OverflowError:
        dur = float("inf")
    if dur <= 0 or dur > _MAX_DURATION_S or not math.isfinite(dur):
        raise ClipInputError(
            f"clip {_safe_label(cid)}: duration_s {_safe_label(raw)} is not a finite duration > 0 and ≤ 24 h; "
            "refusing to place it on the capture clock (fail closed)"
        )
    return dur


def clip_fps(clip: dict[str, Any]) -> float:
    """Frames per second the highlight clip's ``source.start_s`` was cut at.

    qorcap (from #164) writes ``source.frame_rate`` as a rational string such as ``"60/1"`` or
    ``"60000/1001"`` on clips that are not 30/1. Absent (every older manifest, and every 30/1 clip) means
    30/1. Present but not ``N/D`` with 1 <= N/D <= 1000, or a field longer than
    ``_MAX_FRAME_RATE_CHARS``, raises ClipRateError (fail closed).
    """
    src = clip.get("source") if isinstance(clip.get("source"), dict) else {}
    if "frame_rate" not in src:
        return 30.0
    fr = src["frame_rate"]
    field = fr.strip() if isinstance(fr, str) else None
    m = _RATIONAL.fullmatch(field) if field is not None and len(field) <= _MAX_FRAME_RATE_CHARS else None
    num, den = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    if not m or num <= 0 or den <= 0 or not 1.0 <= num / den <= 1000.0:
        raise ClipRateError(f"clip {clip.get('id', '?')}: source.frame_rate {fr!r} is not a positive frame rate; "
                            "refusing to place it on the capture clock (fail closed)")
    return num / den


def clip_start_mono_ms(clip: dict[str, Any], segments: list[dict[str, Any]]) -> float:
    """Capture mono ms of the clip's first frame: the manifest frame fit at the clip's own rate.

    ``source`` must be a dict and ``start_s`` a finite number ≥ 0 (not a bool). A bad
    value raises ClipInputError (fail closed). The returned time is finite.
    """
    from qor_receipt.review.clock import capture_clock

    start_s = clip_start_s(clip)
    fps = clip_fps(clip)
    intercept, ms_per_frame = capture_clock(segments, fps)
    t0 = intercept + ms_per_frame * start_s * fps
    if not math.isfinite(t0):
        cid = clip.get("id", "?") if isinstance(clip, dict) else "?"
        raise ClipInputError(
            f"clip {_safe_label(cid)}: start on the capture clock is not finite for source.start_s {_safe_label(start_s)}; "
            "refusing to place it on the capture clock (fail closed)"
        )
    return t0


def main(argv: list[str] | None = None) -> int:
    """python -m qor_receipt.hands.sidecar RECEIPT INPUTS_JSONL CLIPS_MANIFEST SEGMENTS_JSONL --clips DIR
    [--muse] [--max-calls N] [--no-track] [--sign-key PATH] [--out FILE]

    Inputs stay local. ``--no-track`` leaves the per-frame input track out (use it for anything published).
    """
    import argparse
    import json
    import sys
    from pathlib import Path

    from qor_receipt.hands.muse_context import MuseContext, filter_actions
    from qor_receipt.hands.video_events import detect, energy_curve

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
    media = [s for s in segs if s.get("type") == "segment"]
    man = json.loads(Path(a.clips_manifest).read_text(encoding="utf-8"))
    raw_clips = man.get("clips", [])
    if not isinstance(raw_clips, list):
        print(
            "error: highlights clips is not a list; refusing to place it on the capture clock (fail closed)",
            file=sys.stderr,
        )
        return 2
    # Validate every clip (id, file, start, duration, rate) before any decode, and refuse
    # a duplicate id, so a later refusal never follows partial work.
    prepared: list[tuple[str, Path, float, float]] = []
    seen_ids: set[str] = set()
    try:
        for c in raw_clips:
            cid, fname = clip_id_and_file(c)
            if cid in seen_ids:
                raise ClipInputError(
                    f"clip {_safe_label(cid)}: duplicate id; refusing to place it on the capture clock (fail closed)"
                )
            seen_ids.add(cid)
            f = clip_file_in_dir(a.clips, fname, clip_id=cid)
            t0 = clip_start_mono_ms(c, media)
            dur = clip_duration_s(c)
            prepared.append((cid, f, t0, dur))
    except ClipInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    clips, cands = [], []
    for cid, f, t0, dur in prepared:
        if not f.exists() or not f.is_file():
            continue
        e, cc, fps = energy_curve(str(f))
        evs = [dict(ev, clip=cid, t0=t0, path=str(f)) for ev in detect(e, cc, fps)]
        cands += evs
        clips.append({"id": cid, "start_mono_ms": round(t0), "duration_s": dur, "events": evs})
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
