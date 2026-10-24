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

An ``ok`` result is written into the receipt. From recorder 0.3.34 a ``pattern_mismatch`` is stored as
``ok: false`` with ``reason: "pattern_mismatch"`` and ``reverified_by: "recorder"`` only when the controller
log can support it. Every non-empty line of ``inputs.jsonl`` must parse as a JSON object. A JSON value that
is not an object, such as ``42`` or ``[]``, is malformed, and so is a second ``header`` row. Chunk rows must
cover ``[issued_ms, deadline_ms]`` with no gap larger than 3000 ms (the 2 s fMP4 fragment plus 1 s slack).
Coverage uses the latest chunk at or before ``issued_ms``. That chunk must exist and ``issued_ms`` minus its
wall time must be at most 3000. Chunks earlier than it are not gap-checked, and a chunk after ``issued_ms``
does not close the open. From that chunk each next chunk is at most 3000 ms later until a chunk at or after
``deadline_ms``. A last chunk before the deadline does not cover it, even when the deadline is within 3000 ms.
A single chunk after the deadline does not cover the window, and neither does a
crash gap followed by rows after the deadline. Wall time is header ``wall_anchor_ms`` plus ``t_us // 1000``.
The pad's status must be ``connected`` at ``issued_ms`` and remain ``connected`` through ``deadline_ms + 2500``
with no ``stalled``, ``unplugged``, or ``missing`` row in that span. A malformed status row withholds it.
OPTIONS alone, and OPTIONS inside the window with the rest of the pattern after the deadline, are that stored
mismatch when the log passes this gate. Every other non-ok result stays ``result: null`` (the verifiers say
``challenge_unanswered``) and the reason goes to ``challenge_results.jsonl``, with
``withheld: controller_log`` when a mismatch was withheld. That file is bundle metadata, not ``receipt.v0``.
The official referee re-runs everything from raw bytes either way; nothing here is trusted by it.

The latency ceiling here stays 0..600 ms and the result carries no ``rule`` field. A menu at 1.6 s is still
``video_latency_too_high`` on this path. The official referee is what stamps rule v2 and its 2500 ms ceiling.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

from qor_receipt.agent.pause_detect import PauseThresholds, capture_path_thresholds, detector_for_challenge_rule
from qor_receipt.challenge import RULE_V2, challenge_rule_of, response_press_ms, verify_response
from qor_receipt.jcs import sha256_hex
from qor_receipt.review import bundle as B
from qor_receipt.review import media as M

SMALL = B.META_FILES
# A stalled row is written only after 2 s with no HID reports, on the next read timeout. The check
# therefore runs 2.5 s past the deadline. Same constant as the recorder.
STALL_LAG_MS = 2_500
# One inputs chunk per fMP4 segment. min_frag_duration is 2 s, and the encoder GOP is one second,
# so a chunk is about 2 s apart and at most 3 s.
CHUNK_INTERVAL_MS = 2_000
CHUNK_GAP_SLACK_MS = 1_000
CHUNK_GAP_MAX_MS = CHUNK_INTERVAL_MS + CHUNK_GAP_SLACK_MS


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


def inputs_log_intact(path: str | Path) -> bool:
    """False when ``inputs.jsonl`` is missing or any line is not one JSON object.

    The recheck's JSONL reader skips a corrupt line and drops a value that is not an object, so the
    presses that remain can look like a short pattern. A torn line, a bare ``42`` or ``[]``, or a second
    ``header`` row is not a player failure.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return False
    if not raw:
        return True
    headers = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            val = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(val, dict):
            return False
        if val.get("type") == "header":
            headers += 1
            if headers > 1:
                return False
    return True


def pad_connected_through(records: list, *, issued_ms: int, deadline_ms: int, anchor_ms: int) -> bool:
    """True when status records show ``connected`` from issue through the stall lag.

    ``stalled``, ``unplugged``, and ``missing`` anywhere in ``[issued_ms, deadline_ms + STALL_LAG_MS]``
    fail the check, including an unplug that reconnects before the deadline. A status row whose ``t_us``
    is not an integer, or whose ``state`` is not a string, fails closed. Times are the same wall clock
    as ``press_events`` (header ``wall_anchor_ms`` plus ``t_us // 1000``).
    """
    events: list[tuple[int, str]] = []
    for r in records:
        if not isinstance(r, dict) or r.get("type") != "status":
            continue
        t_us, state = r.get("t_us"), r.get("state")
        if isinstance(t_us, bool) or not isinstance(t_us, int) or not isinstance(state, str):
            return False
        events.append((anchor_ms + t_us // 1000, state))
    events.sort()
    state: str | None = None
    i = 0
    while i < len(events) and events[i][0] <= issued_ms:
        state = events[i][1]
        i += 1
    if state != "connected":
        return False
    end = deadline_ms + STALL_LAG_MS
    while i < len(events) and events[i][0] <= end:
        if events[i][1] != "connected":
            return False
        i += 1
    return True


def chunks_cover(records: list, *, issued_ms: int, deadline_ms: int, anchor_ms: int) -> bool:
    """Chunk rows cover ``[issued_ms, deadline_ms]`` with no gap over ``CHUNK_GAP_MAX_MS``.

    The open uses the latest chunk at or before ``issued_ms``. That chunk must exist, and
    ``issued_ms`` minus its wall time must be at most ``CHUNK_GAP_MAX_MS``. Chunks earlier than
    that one are not gap-checked. A chunk after ``issued_ms`` does not close the open. From that
    chunk, each next chunk is at most ``CHUNK_GAP_MAX_MS`` later, until a chunk at or after
    ``deadline_ms``. A last chunk before the deadline does not cover it, even when the deadline
    is within the bound. A gap after the chunk that reaches the deadline is ignored. A single
    chunk after the deadline does not cover the window. A crash that drops a stretch, then writes
    rows after the deadline, does not either.
    Times are header ``wall_anchor_ms`` plus ``t_us // 1000``. A chunk whose ``t_us`` is not an
    integer is skipped.
    """
    times = sorted({
        anchor_ms + r["t_us"] // 1000
        for r in records
        if isinstance(r, dict) and r.get("type") == "chunk"
        and isinstance(r.get("t_us"), int) and not isinstance(r.get("t_us"), bool)
    })
    if not times:
        return False
    prev: int | None = None
    i = 0
    while i < len(times) and times[i] <= issued_ms:
        prev = times[i]
        i += 1
    if prev is None or issued_ms - prev > CHUNK_GAP_MAX_MS:
        return False
    if prev >= deadline_ms:
        return True
    while i < len(times):
        if times[i] - prev > CHUNK_GAP_MAX_MS:
            return False
        prev = times[i]
        i += 1
        if prev >= deadline_ms:
            return True
    return False


def mismatch_log_trustworthy(bundle: str | Path, records: list, *, issued_ms: int, deadline_ms: int,
                             anchor_ms: int) -> bool:
    """A pattern mismatch may be stored only when the controller log can support it."""
    return (inputs_log_intact(Path(bundle) / "inputs.jsonl")
            and pad_connected_through(records, issued_ms=issued_ms, deadline_ms=deadline_ms, anchor_ms=anchor_ms)
            and chunks_cover(records, issued_ms=issued_ms, deadline_ms=deadline_ms, anchor_ms=anchor_ms))


def rerun(c: dict[str, Any], presses: list[dict[str, Any]], onsets_for, *,
          log_trustworthy: bool = False) -> dict[str, Any]:
    """One challenge -> {"result": dict|None, "local": dict}. ``onsets_for(lo_wall, hi_wall)`` -> pause onsets (wall).

    ``log_trustworthy`` defaults closed. A ``pattern_mismatch`` is stored only when the caller has
    checked every line, chunk coverage of the window, and the pad through the stall lag. Anything else
    with that reason is withheld.
    """
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
    stored = res if res.get("ok") else None
    if not res.get("ok") and res.get("reason") == "pattern_mismatch":
        if log_trustworthy:
            stored = res
        else:
            stored = None
            local["withheld"] = "controller_log"
    return {"result": stored, "local": local}


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

    anchor = B.wall_anchor(b) or 0
    out = []
    for c in challenges:
        holder.clear()
        try:
            issued, deadline = int(c["issued_ms"]), int(c["deadline_ms"])
            trust = mismatch_log_trustworthy(
                d, b.inputs + b.input_chunks, issued_ms=issued, deadline_ms=deadline, anchor_ms=anchor)
        except (KeyError, TypeError, ValueError):
            trust = False
        row = rerun(c, presses, onsets_for, log_trustworthy=trust)
        local = row["local"]
        if local.get("reason") == "pause_not_seen_on_video":
            local["note"] = M.pause_not_seen_note(str(local.get("phase") or "challenge"), holder.get("scan"),
                                                  local.get("pause_press_ms"), prior=local.get("note"),
                                                  thresholds=thresholds, rule=rule)
        out.append(row)
    return out
