"""SYNTHETIC qorcap-inputs/v0 fixtures for tests and the demo. Not real footage or real inputs.

Real match inputs stay on the player's PC; nothing here came from a real controller.
"""
from __future__ import annotations

import random
from typing import Any

from qor_receipt.hands.inputs import EMPTY_CHUNK, GENESIS, make_chunks
from qor_receipt.jcs import sha256_hex

SYNTHETIC = True


def human_session(seed: int = 7, start_ms: int = 100_000, seconds: int = 40, seg_s: int = 4,
                  regular: bool = False) -> tuple[list[dict[str, Any]], list[int]]:
    """250 Hz neutral reports plus presses; returns (records, press_ms). ``regular`` = bot-like fixed intervals."""
    rnd = random.Random(seed)
    press_ms, t = [], start_ms + 500
    while t < start_ms + seconds * 1000 - 500:
        press_ms.append(t)
        t += 600 if regular else rnd.randint(350, 1300)
    recs = []
    for k in range(seconds * 250):
        ms = start_ms + k * 4
        down = any(p <= ms < p + 80 for p in press_ms)
        rec = {"type": "in", "t_us": ms * 1000, "b": 1 if down else 0, "lx": 128, "ly": 128, "rx": 128, "ry": 128,
               "l2": 0, "r2": 0, "c": k % 256, "hw": ms * 1000 + (0 if regular else rnd.randint(-300, 300)),
               "seg": (ms - start_ms) // (seg_s * 1000)}
        recs.append(rec)
    return recs, press_ms


def receipt_for(records: list[dict[str, Any]], extra_empty: int = 2, role: str = "away") -> dict[str, Any]:
    chunks = make_chunks(records)
    cps, prev = [], GENESIS
    for i, h in enumerate([c["sha256"] for c in chunks] + [EMPTY_CHUNK] * extra_empty):
        body = {"seq": i, "t_ms": 1_800_000_000_000 + i * 2000, "frames_hash": "ab" * 32, "input_hash": h, "prev": prev}
        prev = sha256_hex(body)
        cps.append({**body, "hash": prev})
    return {"schema": "qor-receipt/v0", "match": {"match_id": "SYNTHETIC-hands", "game": "madden27"},
            "checkpoints": {role: cps}}
