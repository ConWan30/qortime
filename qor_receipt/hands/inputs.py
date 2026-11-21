"""Integrity of qorcap-inputs/v0 records against the receipt (port of review/bundle.check_inputs; hackathon work).

Rules (QorCap src/inputs.rs):
- chunk ``sha256`` = SHA-256(JCS(records of that segment, in order, each carrying ``seg``));
- chain: ``head = SHA-256(JCS({prev, seq, sha256}))`` from 64 zeros;
- every non-empty chunk sha must be committed by a receipt checkpoint ``input_hash``, and every receipt
  ``input_hash`` must be a chunk sha or the empty-chunk hash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qor_receipt.jcs import sha256_hex

GENESIS = "0" * 64
EMPTY_CHUNK = sha256_hex([])


def link(prev: str, seq: int, sha: str) -> str:
    return sha256_hex({"prev": prev, "seq": seq, "sha256": sha})


@dataclass
class InputCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)   # verified "in" records only, time-ordered


def verify_inputs(records: list[dict[str, Any]], chunks: list[dict[str, Any]], receipt: dict[str, Any],
                  role: str = "away") -> InputCheck:
    probs: list[str] = []
    if not isinstance(records, list) or not isinstance(chunks, list) or not chunks:
        return InputCheck(False, ["no_inputs"])
    by_seg: dict[int, list[dict[str, Any]]] = {}
    for r in records:
        if not isinstance(r, dict) or not isinstance(r.get("seg"), int):
            return InputCheck(False, ["malformed_record"])
        by_seg.setdefault(r["seg"], []).append(r)
    head, shas = GENESIS, set()
    for i, c in enumerate(chunks):
        if not isinstance(c, dict) or not isinstance(c.get("seg"), int):
            return InputCheck(False, ["malformed_chunk"])
        rs = by_seg.pop(c["seg"], [])
        sha = sha256_hex(rs)
        if c.get("sha256") != sha:
            probs.append(f"chunk {i}: sha256 mismatch")
        if c.get("prev") != head:
            probs.append(f"chunk {i}: prev link broken")
        head = link(head, i, sha)
        if c.get("head") is not None and c.get("head") != head:
            probs.append(f"chunk {i}: chain head mismatch")
        if rs:
            shas.add(sha)
    if by_seg:
        probs.append(f"{sum(map(len, by_seg.values()))} record(s) not covered by any chunk")
    cps = ((receipt.get("checkpoints") or {}).get(role)) or []
    committed = {c.get("input_hash") for c in cps if isinstance(c, dict)}
    if not committed:
        probs.append("receipt has no input_hash checkpoints")
    if shas - committed:
        probs.append(f"{len(shas - committed)} chunk(s) not committed by the receipt")
    if committed - shas - {EMPTY_CHUNK, None}:
        probs.append(f"{len(committed - shas - {EMPTY_CHUNK, None})} receipt input_hash value(s) with no matching chunk")
    if probs:
        return InputCheck(False, probs)
    ins = sorted((r for r in records if r.get("type", "in") == "in" and isinstance(r.get("t_us"), int)), key=lambda r: r["t_us"])
    return InputCheck(True, [], ins)


def make_chunks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chunk list for records (used by synthetic fixtures and tests)."""
    segs: dict[int, list[dict[str, Any]]] = {}
    for r in records:
        segs.setdefault(r["seg"], []).append(r)
    out, head = [], GENESIS
    for i, seg in enumerate(sorted(segs)):
        sha = sha256_hex(segs[seg])
        nxt = link(head, i, sha)
        out.append({"seq": i, "seg": seg, "n": len(segs[seg]), "sha256": sha, "prev": head, "head": nxt})
        head = nxt
    return out
