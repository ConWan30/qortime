"""Structural re-checks of a QorCap bundle's metadata (Python port of `qorcap verify-bundle`, minus video bytes).

Everything here is recomputed from the submitted files; no field the laptop computed is trusted on its own.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any

from qor_receipt.jcs import sha256_hex

GENESIS = "0" * 64
META_FILES = ("bundle.json", "manifest.jsonl", "checkpoints.jsonl", "countersigned.jsonl", "inputs.jsonl",
              "haptics.jsonl", "input_proof.json", "receipt.json", "receipt.draft.json", "timeline.jsonl",
              "challenges.jsonl", "official_challenges.jsonl", "anchors.jsonl", "referee.pub",
              "challenge_gates.jsonl", "challenge_results.jsonl", "pairing.json", "device_attest.v0.json")
REQUIRED_FILES = ("bundle.json", "manifest.jsonl", "checkpoints.jsonl", "receipt.json")


def link(prev: str, seq: int, sha: str) -> str:
    """QorCap chain rule: head_n = SHA-256(JCS({prev, seq, sha256}))."""
    return sha256_hex({"prev": prev, "seq": seq, "sha256": sha})


def file_sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def parse_jsonl(b: bytes, name: str, errors: list[str], max_bad: int = 3) -> list[dict[str, Any]]:
    out, bad = [], 0
    for i, line in enumerate(b.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            v = json.loads(line)
        except ValueError:
            bad += 1
            errors.append(f"{name}:{i}: corrupt JSONL line")
            if bad > max_bad:
                break
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def read_meta_zip(data: bytes, max_bytes: int) -> dict[str, bytes]:
    """Read the known meta files from a submitted zip (flat or one top-level folder). Size-capped."""
    files: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if name not in META_FILES or name in files:
                continue
            total += info.file_size
            if total > max_bytes:
                raise ValueError(f"meta files exceed {max_bytes} bytes")
            files[name] = z.read(info)
    return files


@dataclass
class Bundle:
    files: dict[str, bytes]
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    draft: dict[str, Any] = field(default_factory=dict)
    segments: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    countersigned: list[dict[str, Any]] = field(default_factory=list)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    input_chunks: list[dict[str, Any]] = field(default_factory=list)
    chain_head: str = GENESIS
    input_head: str | None = None
    heads: dict[str, int] = field(default_factory=dict)  # chain head -> segment seq

    @property
    def role(self) -> str:
        return str(self.meta.get("role") or (self.receipt.get("players") or [{}])[0].get("role") or "home")

    @property
    def match_id(self) -> str:
        return str((self.receipt.get("match") or {}).get("match_id") or self.meta.get("match_id") or "")

    def json(self, name: str) -> dict[str, Any]:
        b = self.files.get(name)
        if b is None:
            return {}
        try:
            v = json.loads(b.decode("utf-8"))
        except ValueError:
            self.errors.append(f"{name}: not valid JSON")
            return {}
        return v if isinstance(v, dict) else {}

    def jsonl(self, name: str) -> list[dict[str, Any]]:
        b = self.files.get(name)
        return parse_jsonl(b, name, self.errors) if b is not None else []


def load(files: dict[str, bytes]) -> Bundle:
    b = Bundle(files=files)
    for f in REQUIRED_FILES:
        if f not in files:
            b.errors.append(f"missing_file:{f}")
    b.meta = b.json("bundle.json")
    b.receipt = b.json("receipt.json")
    b.draft = b.json("receipt.draft.json")
    b.segments = [m for m in b.jsonl("manifest.jsonl") if m.get("type") == "segment"]
    b.checkpoints = b.jsonl("checkpoints.jsonl")
    b.countersigned = b.jsonl("countersigned.jsonl")
    recs = b.jsonl("inputs.jsonl")
    b.inputs = [r for r in recs if r.get("type") != "chunk"]
    b.input_chunks = [r for r in recs if r.get("type") == "chunk"]
    return b


def check_manifest(b: Bundle) -> list[str]:
    """Segment seq/offset continuity per file and the SHA-256 chain over the *claimed* segment hashes.
    (Whether the real bytes match those hashes is the video check, done on uploaded bytes.)"""
    errs: list[str] = []
    head, expect_off = GENESIS, {}
    b.heads = {GENESIS: -1}
    if not b.segments:
        return ["manifest_has_no_segments"]
    for i, m in enumerate(b.segments):
        try:
            seq, f, off, n, sha = int(m["seq"]), str(m["file"]), int(m["offset"]), int(m["bytes"]), str(m["sha256"])
        except (KeyError, TypeError, ValueError):
            errs.append(f"segment[{i}]:malformed")
            continue
        if seq != i:
            errs.append(f"segment {seq}: seq gap (expected {i})")
        if off != expect_off.get(f, 0):
            errs.append(f"segment {seq}: offset gap in {f}")
        expect_off[f] = off + n
        if m.get("prev") != head:
            errs.append(f"segment {seq}: prev link broken")
        head = link(head, seq, sha)
        if m.get("chain_head") != head:
            errs.append(f"segment {seq}: chain head mismatch")
        b.heads[head] = seq
    b.chain_head = head
    want = b.meta.get("chain_head")
    if want and want != head:
        errs.append("bundle.json chain_head does not match the manifest chain")
    return errs


def check_inputs(b: Bundle) -> tuple[dict[int, str], list[str]]:
    """Re-hash inputs.jsonl chunks + chain (port of inputs.rs verify_chunks)."""
    errs: list[str] = []
    by_seg: dict[int, list[dict[str, Any]]] = {}
    for r in b.inputs:
        by_seg.setdefault(int(r.get("seg", -1)), []).append(r)
    head, out = GENESIS, {}
    for i, c in enumerate(b.input_chunks):
        seg = int(c.get("seg", -1))
        rs = by_seg.pop(seg, [])
        sha = sha256_hex(rs)
        if c.get("sha256") != sha:
            errs.append(f"inputs chunk {i} (segment {seg}): SHA-256 mismatch")
        if c.get("n") != len(rs):
            errs.append(f"inputs chunk {i}: record count mismatch")
        if c.get("prev") != head:
            errs.append(f"inputs chunk {i}: prev link broken")
        head = link(head, i, sha)
        if c.get("head") != head:
            errs.append(f"inputs chunk {i}: chain head mismatch")
        out[seg] = sha
    for seg, rs in by_seg.items():
        errs.append(f"inputs: {len(rs)} record(s) for segment {seg} not covered by any chunk")
    b.input_head = head if b.input_chunks else None
    return out, errs


def check_checkpoints(b: Bundle, input_by_seg: dict[int, str]) -> list[str]:
    """QorCap checkpoint chain: body hash, frames_hash == JCS-sha(qorcap), chain-head refs, input binding."""
    errs: list[str] = []
    prev = GENESIS
    empty = sha256_hex([])
    committed = 0
    if not b.checkpoints:
        return ["no_checkpoints"]
    for i, c in enumerate(b.checkpoints):
        try:
            body = {k: c[k] for k in ("seq", "t_ms", "frames_hash", "input_hash", "prev")}
        except KeyError:
            errs.append(f"checkpoint {i}: malformed")
            continue
        if c["seq"] != i:
            errs.append(f"checkpoint {i}: seq gap")
        if c["prev"] != prev:
            errs.append(f"checkpoint {i}: prev link broken")
        h = sha256_hex(body)
        if c.get("hash") != h:
            errs.append(f"checkpoint {i}: hash mismatch")
        q = c.get("qorcap")
        if not isinstance(q, dict) or c["frames_hash"] != sha256_hex(q):
            errs.append(f"checkpoint {i}: qorcap evidence does not match frames_hash")
            q = q if isinstance(q, dict) else {}
        ch = q.get("chain_head")
        if ch is not None and ch not in b.heads:
            errs.append(f"checkpoint {i}: refers to unknown video chain head")
        seg = q.get("input_seg")
        if isinstance(seg, int) and b.input_chunks:
            want = input_by_seg.get(seg, empty)
            if c["input_hash"] != want:
                errs.append(f"checkpoint {i}: input_hash does not match inputs.jsonl chunk for segment {seg}")
            if seg in input_by_seg:
                committed += 1
        prev = c.get("hash", h)
    if b.input_chunks and committed < len(input_by_seg):
        errs.append(f"inputs: {len(input_by_seg) - committed} chunk(s) not committed by any checkpoint")
    want = b.meta.get("checkpoint_head")
    if want and want != prev:
        errs.append("bundle.json checkpoint_head does not match checkpoints.jsonl")
    return errs


def check_receipt_binding(b: Bundle) -> list[str]:
    """receipt.json must carry exactly this bundle's checkpoints (same hashes, same order)."""
    errs: list[str] = []
    r = b.receipt
    cps = (r.get("checkpoints") or {}).get(b.role) or []
    if len(cps) != len(b.checkpoints):
        errs.append(f"receipt.json has {len(cps)} checkpoints for {b.role}, bundle has {len(b.checkpoints)}")
    for i, (x, y) in enumerate(zip(cps, b.checkpoints)):
        if x.get("hash") != y.get("hash"):
            errs.append(f"receipt checkpoint {i} hash differs from checkpoints.jsonl")
            break
    if b.countersigned:
        # v0.3.28 seals at the stop checkpoint and refuses later ones. Start Match puts the
        # whole live log in the receipt, so a completed match has equal lengths. Finalize still
        # signs live[:len(checkpoints.jsonl)] when the log is longer and does not truncate the
        # file. That extra line is a signed checkpoint the receipt does not carry. Accepting the
        # prefix would hide it. A shorter log is a truncated chain. Equal length still requires
        # each hash and referee_sig (match 021003 stays a rejection).
        n, m = len(cps), len(b.countersigned)
        if m < n:
            errs.append(
                f"countersigned.jsonl ends before the receipt chain "
                f"(receipt has {n} checkpoints for {b.role}, countersigned.jsonl has {m})"
            )
        elif m > n:
            errs.append(
                f"countersigned.jsonl continues past the receipt chain "
                f"(receipt has {n} checkpoints for {b.role}, countersigned.jsonl has {m}); "
                f"a signed checkpoint the receipt does not carry"
            )
        else:
            for i, (x, y) in enumerate(zip(cps, b.countersigned)):
                if x.get("hash") != y.get("hash") or x.get("referee_sig") != y.get("referee_sig"):
                    errs.append(f"receipt checkpoint {i} differs from countersigned.jsonl")
                    break
    if b.meta.get("match_id") and b.match_id and b.meta["match_id"] != b.match_id and b.meta["match_id"] != "unassigned":
        errs.append("receipt match_id differs from bundle.json")
    return errs


def check_input_files(b: Bundle) -> tuple[dict[str, Any], list[str]]:
    """Input-proof integrity: file hashes, inputs chain head and haptic counts recomputed from the raw log."""
    errs: list[str] = []
    proof = b.json("input_proof.json")
    inp = ((b.receipt.get("signals") or {}).get("input")) or {}
    summary: dict[str, Any] = {"present": bool(b.input_chunks)}
    if not b.input_chunks:
        return summary, errs
    fs = file_sha(b.files["inputs.jsonl"])
    summary["inputs_sha256"] = fs
    summary["inputs_chain_head"] = b.input_head
    for src, d in (("input_proof.json", proof), ("receipt signals.input", inp)):
        if d.get("inputs_sha256") and d["inputs_sha256"] != fs:
            errs.append(f"{src}: inputs_sha256 does not match inputs.jsonl")
        if d.get("inputs_chain_head") and d["inputs_chain_head"] != b.input_head:
            errs.append(f"{src}: inputs_chain_head does not match recomputed chain")
    haptic = [r for r in b.inputs if r.get("type") == "haptic"]
    high = sum(1 for r in haptic if r.get("confidence_label") == "high")
    summary["haptic_events"] = len(haptic)
    summary["haptic_high_confidence"] = high
    if isinstance(inp.get("haptic_events"), int) and inp["haptic_events"] != len(haptic):
        errs.append("signals.input haptic_events does not match the committed haptic records")
    if "haptics.jsonl" in b.files:
        hs = file_sha(b.files["haptics.jsonl"])
        claimed = (proof.get("haptics") or {}).get("haptics_sha256") or inp.get("haptics_sha256")
        if claimed and claimed != hs:
            errs.append("haptics_sha256 does not match haptics.jsonl")
    ips = b.meta.get("input_proof_sha256")
    if ips and "input_proof.json" in b.files and file_sha(b.files["input_proof.json"]) != ips:
        errs.append("bundle.json input_proof_sha256 does not match input_proof.json")
    summary["presses"] = len(press_events(b))
    return summary, errs


# ---------------------------------------------------------------- controller presses (DualSense bit map, hid.rs)
BUTTON_BITS = [("CROSS", 0), ("CIRCLE", 1), ("SQUARE", 2), ("TRIANGLE", 3), ("L1", 4), ("R1", 5), ("L2", 6), ("R2", 7),
               ("CREATE", 8), ("OPTIONS", 9), ("L3", 10), ("R3", 11), ("UP", 12), ("DOWN", 13), ("LEFT", 14),
               ("RIGHT", 15), ("PS", 16), ("TOUCHPAD", 17)]


def wall_anchor(b: Bundle) -> int | None:
    hdr = next((r for r in b.inputs if r.get("type") == "header"), None)
    return int(hdr["wall_anchor_ms"]) if hdr and isinstance(hdr.get("wall_anchor_ms"), int) else None


def press_events(b: Bundle) -> list[dict[str, Any]]:
    """Rising edges of the button mask in inputs.jsonl -> [{t_ms (wall), mono_ms, button, down}]."""
    anchor = wall_anchor(b) or 0
    out, prev = [], 0
    for r in b.inputs:
        if r.get("type") != "in" or not isinstance(r.get("b"), int) or not isinstance(r.get("t_us"), int):
            continue
        cur = r["b"]
        rise = cur & ~prev
        if rise:
            mono = r["t_us"] // 1000
            for name, bit in BUTTON_BITS:
                if rise & (1 << bit):
                    out.append({"t_ms": anchor + mono, "mono_ms": mono, "button": name, "down": True})
        prev = cur
    return out


def segment_ranges(b: Bundle) -> dict[int, tuple[int, int]]:
    """seq -> (start_mono_ms, end_mono_ms) estimated from consecutive mono_ms stamps (end = when hashed)."""
    out: dict[int, tuple[int, int]] = {}
    prev_end = None
    for m in b.segments:
        end = int(m.get("mono_ms", 0))
        start = prev_end if prev_end is not None and prev_end <= end else max(0, end - 2_000)
        out[int(m["seq"])] = (start, end)
        prev_end = end
    return out
