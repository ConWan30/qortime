"""Muse Spark score cross-check: every locked score is re-read by Meta Muse Spark. LOWER-ONLY.
Built during the Meta AI Developer Hackathon, Oct 26 - Nov 4 2026.

The local scorebug reader locks each score change into the checkpoint chain. This module takes a frame from just
after each lock, crops the scorebug strip and asks ``muse-spark-1.3`` to read it independently.

Rules (tests: tests/test_score_crosscheck.py):
* Agreement keeps the status. A confident disagreement lowers VERIFIED to PARTIAL. Nothing here can raise a
  status or produce FAIL (``apply_cap`` refuses any cap other than PARTIAL).
* Muse unavailable, errored, unsure, or no frame: that lock is ``unchecked`` and costs nothing. If no lock could be
  checked the whole result is "Muse unchecked" with no penalty, i.e. exactly today's behaviour.
* Per lock, two frames are read (lock + 1.0 s and + 2.5 s by default). The lock disagrees only if every confident read
  disagrees with the locked score; mixed reads make it ``unchecked``.
* Output is a sidecar ``score_crosscheck.json`` (``affects_verdict: "lower_only"``, ``max_cap: "PARTIAL"``), signed
  with the AI advisory key (``signing.py``). ``receipt.v0.json`` is never edited and ``verify_receipt`` is unchanged.
* Cost: about $0.002 per read; at most ``max_reads`` (default 12) per match, so worst case about $0.03.

Measured on non-held-out frames (box benchmark, same prompt and crop): 73/73 visible scores read correctly and
confidently, 0 confident-wrong, and 0 confident reads on 18 no-score frames (including 8 stats-overlay traps).
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from qor_receipt.ai_ruling.muse import BASE_URL, DEFAULT_MODEL, KEY_ENVS, MuseError, _urllib_transport, api_key_from_env
from qor_receipt.jcs import canonicalize

SCHEMA = "qortime.score_crosscheck.v0"
FILENAME = "score_crosscheck.json"
VERIFIED, PARTIAL = "VERIFIED", "PARTIAL"
CAP_CODE = "muse_score_crosscheck_disagrees"
MAX_READS = 12
USD_IN, USD_OUT = 1.25e-6, 4.25e-6
LABEL = ("ADVISORY, LOWER-ONLY. Meta Muse Spark re-read the scorebug after each locked score. Agreement keeps the "
         "status; a disagreement lowers VERIFIED to PARTIAL. It never raises a status and never FAILs a match.")

# Same prompt and schema that scored 111/111 in the scorebug benchmark (do not edit without re-measuring).
PROMPT = """This image is a crop of the bottom strip of a Madden NFL 27 game frame, where the TV-broadcast-style scoreboard bar (scorebug) normally sits. It may or may not contain the scorebug.
The AWAY team is the panel on the LEFT of the scorebug and the HOME team is the panel on the RIGHT.
Read the scorebug and return ONLY a JSON object, no prose, with exactly these keys:
{"away_score": int|null, "home_score": int|null, "quarter": int|null, "clock": "M:SS"|null, "confident": bool}
- quarter is 1-4 (5 for overtime). clock is the game clock (not the play clock).
- If the scorebug is not visible, or the team score numbers are not shown (e.g. replaced by a stats overlay like RUSHING YDS or TOTAL YDS), use null for what you cannot see and set confident to false.
- Set confident to true ONLY if you are certain of both team scores. A wrong confident answer is far worse than confident=false."""
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "scorebug", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["away_score", "home_score", "quarter", "clock", "confident"],
    "properties": {"away_score": {"type": ["integer", "null"]}, "home_score": {"type": ["integer", "null"]},
                   "quarter": {"type": ["integer", "null"]}, "clock": {"type": ["string", "null"]}, "confident": {"type": "boolean"}}}}}

# Crop: frame -> 1280x720, strip y 640:720, x 300:980 (covers the Madden 27 standard, SNF and TNF bars), 2x upscale.
CROP = {"size": (1280, 720), "y": (640, 720), "x": (300, 980), "scale": 2}


def crop_scorebug(image: bytes) -> bytes:
    """JPEG bytes of the scorebug strip. Needs opencv (vision extra)."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("unreadable image")
    img = cv2.resize(img, CROP["size"])[CROP["y"][0]:CROP["y"][1], CROP["x"][0]:CROP["x"][1]]
    img = cv2.resize(img, None, fx=CROP["scale"], fy=CROP["scale"], interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise ValueError("encode failed")
    return buf.tobytes()


def frame_at(video_path: str, t_s: float) -> bytes | None:
    """One JPEG frame at ``t_s`` seconds (opencv). None if it can't be read."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_s) * 1000.0)
        ok, img = cap.read()
        if not ok or img is None:
            return None
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return buf.tobytes() if ok else None
    finally:
        cap.release()


@dataclass
class Read:
    away: int | None
    home: int | None
    confident: bool
    quarter: int | None = None
    usage: dict | None = None


class MuseScoreReader:
    def __init__(self, *, api_key: str | None = None, model: str = DEFAULT_MODEL, base_url: str = BASE_URL,
                 max_tokens: int = 2000, timeout_s: float = 60.0, transport: Callable | None = None) -> None:
        if model.endswith("-contributor"):
            raise ValueError("contributor-mode models are not allowed here")
        self._key = api_key if api_key is not None else api_key_from_env()
        self.model, self.base_url, self.max_tokens, self.timeout_s = model, base_url.rstrip("/"), max_tokens, timeout_s
        self._transport = transport or _urllib_transport

    def __repr__(self) -> str:
        return f"MuseScoreReader(model={self.model!r}, key={'set' if self._key else 'missing'})"

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def read(self, crop_jpeg: bytes) -> Read:
        import base64

        if not self._key:
            raise MuseError("api_key_missing")
        body = {"model": self.model, "temperature": 0, "max_completion_tokens": self.max_tokens, "reasoning_effort": "minimal",
                "response_format": RESPONSE_FORMAT,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(crop_jpeg).decode()}}]}]}
        status, d = self._transport(f"{self.base_url}/chat/completions", self._key, body, self.timeout_s)
        if status != 200 or not isinstance(d, dict):
            raise MuseError(f"http_{status}")
        try:
            ch = d["choices"][0]
            if ch.get("finish_reason") == "length":
                raise MuseError("truncated_finish_reason_length")
            p = json.loads(ch["message"]["content"])
        except MuseError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            raise MuseError(f"malformed_response:{type(e).__name__}") from None
        u = d.get("usage") or {}
        usage = {k: int(u[k]) for k in ("prompt_tokens", "completion_tokens") if isinstance(u.get(k), int)}
        a, h = p.get("away_score"), p.get("home_score")
        ok_ints = isinstance(a, int) and isinstance(h, int) and not isinstance(a, bool) and not isinstance(h, bool)
        return Read(a if ok_ints else None, h if ok_ints else None, bool(p.get("confident")) and ok_ints,
                    p.get("quarter") if isinstance(p.get("quarter"), int) else None, usage)


def _usd_micro(usage: dict) -> int:
    return int(round((usage.get("prompt_tokens", 0) * USD_IN + usage.get("completion_tokens", 0) * USD_OUT) * 1e6))


def crosscheck(receipt: dict[str, Any], locks: Iterable[dict[str, Any]], *, reader: MuseScoreReader | None = None,
               max_reads: int = MAX_READS, crop: Callable[[bytes], bytes] = crop_scorebug) -> dict[str, Any]:
    """locks = [{"id", "expected": {"away", "home"}, "frames": [jpeg bytes, ...], "tolerate"?: [{"away", "home"}]}].

    ``tolerate`` lists neighbouring lock scores: a confident read equal to one of them is ``unsure``, never a
    disagreement (frame timing is approximate). Returns the sidecar. Never raises."""
    reader = reader or MuseScoreReader()
    rows: list[dict[str, Any]] = []
    reads = 0
    cost = 0
    for lk in locks:
        exp = lk.get("expected") or {}
        row: dict[str, Any] = {"id": str(lk.get("id")), "expected": {"away": exp.get("away"), "home": exp.get("home")}, "reads": []}
        if not (isinstance(exp.get("away"), int) and isinstance(exp.get("home"), int)):
            row.update(result="unchecked", reason="no_expected_score"); rows.append(row); continue
        if not reader.has_key:
            row.update(result="unchecked", reason="muse_unavailable:api_key_missing"); rows.append(row); continue
        tolerate = {(t.get("away"), t.get("home")) for t in lk.get("tolerate") or [] if isinstance(t, dict)}
        for img in lk.get("frames") or []:
            if reads >= max_reads:
                row["reads"].append({"result": "skipped", "reason": "max_reads"}); continue
            if not img:
                row["reads"].append({"result": "unchecked", "reason": "no_frame"}); continue
            try:
                c = crop(img)
                reads += 1
                r = reader.read(c)
                cost += _usd_micro(r.usage or {})
                if not r.confident:
                    row["reads"].append({"result": "unsure", "crop_sha256": hashlib.sha256(c).hexdigest()})
                elif (r.away, r.home) != (exp["away"], exp["home"]) and (r.away, r.home) in tolerate:
                    # Frame timing is approximate: a read equal to a neighbouring lock's score is not evidence of
                    # a wrong lock, so it never counts as a disagreement.
                    row["reads"].append({"result": "unsure", "reason": "neighbour_lock_score",
                                         "crop_sha256": hashlib.sha256(c).hexdigest()})
                else:
                    agree = (r.away, r.home) == (exp["away"], exp["home"])
                    row["reads"].append({"result": "agree" if agree else "disagree", "read": {"away": r.away, "home": r.home},
                                         "crop_sha256": hashlib.sha256(c).hexdigest()})
            except MuseError as e:
                row["reads"].append({"result": "unchecked", "reason": f"muse_unavailable:{e}"})
            except Exception as e:  # noqa: BLE001  never take anything down
                row["reads"].append({"result": "unchecked", "reason": f"error:{type(e).__name__}"})
        res = [x["result"] for x in row["reads"]]
        if "agree" in res and "disagree" in res:
            row["result"] = "unchecked"; row["reason"] = "mixed_reads"
        elif "disagree" in res:
            row["result"] = "disagree"
        elif "agree" in res:
            row["result"] = "agree"
        else:
            row["result"] = "unchecked"; row.setdefault("reason", "no_confident_read")
        rows.append(row)
    agree = sum(r["result"] == "agree" for r in rows)
    dis = [r["id"] for r in rows if r["result"] == "disagree"]
    if dis:
        summary, cap = f"Muse Spark disagrees with {len(dis)} of {agree + len(dis)} checked locked scores", PARTIAL
    elif agree:
        summary, cap = f"Muse Spark agrees with {agree} of {agree} checked locked scores", None
    else:
        summary, cap = "Muse unchecked (local reader only, no penalty)", None
    return {"schema": SCHEMA, "label": LABEL, "built_during": "Meta AI Developer Hackathon, Oct 26 - Nov 4 2026",
            "affects_verdict": "lower_only", "max_cap": PARTIAL, "model": getattr(reader, "model", DEFAULT_MODEL),
            "receipt_jcs_sha256": hashlib.sha256(canonicalize(receipt)).hexdigest(),
            "locks": rows, "checked": agree + len(dis), "agree": agree, "disagree": len(dis),
            "cap": cap, "summary": summary, "reads": reads, "cost_micro_usd": cost}


def apply_cap(status: str, cap: str | None) -> str:
    """Only ever VERIFIED -> PARTIAL. Never raises a status, never FAIL."""
    if cap is None:
        return status
    if cap != PARTIAL:
        raise ValueError("the score cross-check can only cap at PARTIAL")
    return PARTIAL if status == VERIFIED else status


def apply_to_result(v: dict[str, Any], sidecar: dict[str, Any] | None) -> dict[str, Any]:
    """Display-layer view: a copy of the verifier output with the cap applied. The input is never mutated."""
    out = copy.deepcopy(v)
    if not sidecar or sidecar.get("cap") is None:
        out["muse_crosscheck"] = "unchecked" if not sidecar or not sidecar.get("checked") else "agree"
        return out
    new = apply_cap(out["status"], sidecar["cap"])
    if new != out["status"]:
        out["status"] = new
        out["partial"] = list(out.get("partial") or []) + [CAP_CODE]
    out["muse_crosscheck"] = "disagree"
    return out


def locks_from_clips(clips_manifest: dict[str, Any], clip_dir, *, offsets_s=(1.0, 2.5), max_locks: int = 6) -> list[dict[str, Any]]:
    """Locks from a QorCap highlight-clips manifest: expected = lock.score_after, frames just after lock_at_s."""
    from pathlib import Path

    out = []
    for c in (clips_manifest.get("clips") or [])[:max_locks]:
        lk = c.get("lock") or {}
        sa = lk.get("score_after") or {}
        path = Path(clip_dir) / str(c.get("file"))
        at = c.get("lock_at_s")
        frames = [frame_at(str(path), float(at) + o) for o in offsets_s] if path.exists() and isinstance(at, (int, float)) else []
        out.append({"id": f"{c.get('id')}#lock{lk.get('seq')}", "expected": {"away": sa.get("away"), "home": sa.get("home")}, "frames": frames})
    return out


def check_crosscheck(doc: Any, receipt: dict[str, Any], *, keys: dict[str, Any] | None = None) -> list[str]:
    """Problems with a signed score_crosscheck.json for this receipt. Empty list = it may be applied (lower-only)."""
    from qor_receipt.ai_ruling.signing import check_signed

    p = check_signed(doc, receipt, schema=SCHEMA, keys=keys)
    if p == ["not_an_object"]:
        return p
    if doc.get("affects_verdict") != "lower_only" or doc.get("max_cap") != PARTIAL:
        p.append("not_lower_only")
    if doc.get("cap") not in (None, PARTIAL):
        p.append("bad_cap")
    locks = doc.get("locks") if isinstance(doc.get("locks"), list) else []
    if (doc.get("cap") == PARTIAL) != any(isinstance(x, dict) and x.get("result") == "disagree" for x in locks):
        p.append("cap_inconsistent")
    return p


def main(argv: list[str] | None = None) -> int:
    """python -m qor_receipt.ai_ruling.score_crosscheck RECEIPT --clips DIR [--sign-key PATH] [--out FILE]"""
    import argparse
    from pathlib import Path

    from qor_receipt.receipt import verify_receipt

    ap = argparse.ArgumentParser(prog="python -m qor_receipt.ai_ruling.score_crosscheck")
    ap.add_argument("receipt")
    ap.add_argument("--clips", required=True, help="folder with clips/manifest.json and the clip mp4s")
    ap.add_argument("--pub")
    ap.add_argument("--sign-key", help="AI ADVISORY key file (never a referee key)")
    ap.add_argument("--out")
    ap.add_argument("--max-reads", type=int, default=MAX_READS)
    a = ap.parse_args(argv)
    receipt = json.loads(Path(a.receipt).read_text(encoding="utf-8"))
    man = json.loads((Path(a.clips) / "manifest.json").read_text(encoding="utf-8"))
    v = verify_receipt(receipt, referee_pub=a.pub)
    sc = crosscheck(receipt, locks_from_clips(man, a.clips), max_reads=a.max_reads)
    final = apply_to_result(v, sc)["status"]
    print(f"verifier: {v['status']}  ->  with Muse cross-check: {final}   ({sc['summary']}; ${sc['cost_micro_usd'] / 1e6:.4f})")
    if a.sign_key:
        from qor_receipt.ai_ruling.signing import sign_ruling
        from qor_receipt.keys import FileKey
        sc = sign_ruling(sc, FileKey(Path(a.sign_key)))
    out = Path(a.out) if a.out else Path(a.receipt).with_name(FILENAME)
    out.write_text(json.dumps(sc, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

__all__ = ["SCHEMA", "FILENAME", "KEY_ENVS", "MuseScoreReader", "crosscheck", "apply_cap", "apply_to_result",
           "crop_scorebug", "frame_at", "locks_from_clips", "check_crosscheck"]
