"""Recorder-side challenge re-run (QorCap finalize, v0.3.11).

QorCap <= 0.3.10 copied every challenge into the receipt with ``result: null``, so a correctly answered challenge
still showed ``challenge_unanswered`` (match 2026-10-17 #4, challenge 44b8d01f). This runs the referee's own rules
on the laptop's bundle before the receipt is signed:

* presses: rising edges in the committed ``inputs.jsonl`` (wall time = header ``wall_anchor_ms`` + mono);
* answering press: the first OPTIONS press inside [issued_ms, deadline_ms] followed by exactly the pattern;
* pause seen: the pause detector for the bundle's challenge rule on the bundle's own video, decoded at
  30 Hz around that press, from segment bytes whose SHA-256 matches the manifest. Rule v2 uses the
  measured capture-path luma caps; rule v1 keeps the historical caps. The text fraction is the same;
* result: ``verify_response`` + ``hid_sha256`` of the window's presses + ``reverified_by: "recorder"``.

Only an ``ok`` result is written into the receipt. A failed or undecidable check leaves ``result: null`` (the
verifiers say ``challenge_unanswered``: never VERIFIED) and the reason goes to ``challenge_results.jsonl``, so a
missed challenge never turns the score receipt into FAIL on the player's own laptop. The official referee re-runs
everything from raw bytes either way; nothing here is trusted by it.

The latency ceiling here stays 0..600 ms and the result carries no ``rule`` field. A menu at 1.6 s is still
``video_latency_too_high`` on this path. The official referee is what stamps rule v2 and its 2500 ms ceiling.
"""
from __future__ import annotations

import hashlib
import statistics
from pathlib import Path
from typing import Any

from qor_receipt.agent.pause_detect import PauseThresholds, capture_path_thresholds, detector_for_challenge_rule
from qor_receipt.challenge import RULE_V2, challenge_rule_of, response_press_ms, verify_response
from qor_receipt.jcs import sha256_hex
from qor_receipt.review import bundle as B
from qor_receipt.review import media as M

SMALL = B.META_FILES


def load_dir(d: Path) -> B.Bundle:
    files = {}
    for n in SMALL:
        p = d / n
        if p.is_file():
            files[n] = p.read_bytes()
    b = B.load(files)
    B.check_manifest(b)
    return b


def segment_reader(d: Path, b: B.Bundle):
    by_seq = {int(m["seq"]): m for m in b.segments}

    def read(seq: int) -> bytes | None:
        m = by_seq.get(seq)
        if m is None:
            return None
        p = d / str(m["file"])
        try:
            with open(p, "rb") as f:
                f.seek(int(m["offset"]))
                data = f.read(int(m["bytes"]))
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == m["sha256"] else None
    return read


def wall_minus_mono(b: B.Bundle) -> int:
    diffs = [int(m["wall_ms"]) - int(m["mono_ms"]) for m in b.segments if "wall_ms" in m and "mono_ms" in m]
    return int(statistics.median(diffs)) if diffs else (B.wall_anchor(b) or 0)


def rerun(c: dict[str, Any], presses: list[dict[str, Any]], onsets_for) -> dict[str, Any]:
    """One challenge -> {"result": dict|None, "local": dict}. ``onsets_for(lo_wall, hi_wall)`` -> pause onsets (wall)."""
    from qor_receipt.review.engine import pause_seen_for

    try:
        issued, deadline = int(c["issued_ms"]), int(c["deadline_ms"])
    except (KeyError, TypeError, ValueError):
        return {"result": None, "local": {"ok": False, "reason": "challenge_timing_malformed"}}
    pat = c.get("pattern") or []
    win = [p for p in presses if p.get("down", True) and issued <= p["t_ms"] <= deadline]
    pp = response_press_ms(pat, win, issued_ms=issued, deadline_ms=deadline)
    pause_seen, note = None, None
    if pp is not None:
        try:
            pause_seen = pause_seen_for(pp, onsets_for(pp - 1_000, pp + 5_000))
        except Exception as e:  # never silent
            note = f"pause decode failed: {type(e).__name__}: {e}"
    res = verify_response(pat, [{"t_ms": p["t_ms"], "button": p["button"], "down": True} for p in win],
                          issued_ms=issued, deadline_ms=deadline, pause_seen_ms=pause_seen).to_dict()
    res["hid_sha256"] = sha256_hex(win)
    res["reverified_by"] = "recorder"
    local = dict(res, phase=c.get("phase"), id=c.get("id"), presses_in_window=len(win))
    if note:
        local["note"] = note
    return {"result": res if res["ok"] else None, "local": local}


def challenge_results(d: str | Path, challenges: list[dict[str, Any]], *, decode: bool = True) -> list[dict[str, Any]]:
    """Fill ``result`` for each challenge from the bundle in ``d``; returns [{"result", "local"}] in order."""
    d = Path(d)
    b = load_dir(d)
    presses = B.press_events(b)
    wmm = wall_minus_mono(b)
    rd = segment_reader(d, b)
    rng = B.segment_ranges(b)
    rule = challenge_rule_of(b.meta)
    thresholds = capture_path_thresholds() if rule == RULE_V2 else PauseThresholds()
    holder: dict[str, Any] = {}

    def onsets_for(lo_wall: int, hi_wall: int) -> list[int]:
        holder.clear()
        if not decode or not M.ffmpeg_bin():
            raise RuntimeError("ffmpeg not available")
        lo, hi = lo_wall - wmm, hi_wall - wmm
        seqs = [s for s, (a, z) in rng.items() if z >= lo and a <= hi]

        def frames():
            for run in M.runs(seqs, b):
                for t, img in M.decode_run(b, run, rd, hz=30.0, size=(640, 360)):
                    yield t + wmm, img
        scan = M.scan_pause(frames(), detector_for_challenge_rule(rule))
        holder["scan"] = scan
        if scan.error:
            raise RuntimeError(scan.error)
        return list(scan.onsets)

    out = []
    for c in challenges:
        holder.clear()
        row = rerun(c, presses, onsets_for)
        local = row["local"]
        if local.get("reason") == "pause_not_seen_on_video":
            local["note"] = M.pause_not_seen_note(str(local.get("phase") or "challenge"), holder.get("scan"),
                                                  local.get("pause_press_ms"), prior=local.get("note"),
                                                  thresholds=thresholds, rule=rule)
        out.append(row)
    return out
