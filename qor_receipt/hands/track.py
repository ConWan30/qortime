"""Overlay track: verified inputs resampled to video frames (hackathon work)."""
from __future__ import annotations

import bisect
from typing import Any

# DualSense bit map, same as review/bundle.BUTTON_BITS (QorCap hid.rs).
BUTTON_BITS = [("CROSS", 0), ("CIRCLE", 1), ("SQUARE", 2), ("TRIANGLE", 3), ("L1", 4), ("R1", 5), ("L2", 6), ("R2", 7),
               ("CREATE", 8), ("OPTIONS", 9), ("L3", 10), ("R3", 11), ("UP", 12), ("DOWN", 13), ("LEFT", 14),
               ("RIGHT", 15), ("PS", 16), ("TOUCHPAD", 17)]


def buttons(mask: int) -> list[str]:
    return [n for n, b in BUTTON_BITS if mask & (1 << b)]


def _stick(v: Any) -> int:
    """Stick axis in permille, -1000..1000 (centre 128). Integers only: sidecars are JCS-signed (no floats)."""
    return max(-1000, min(1000, round((int(v) - 128) * 1000 / 127))) if isinstance(v, int) else 0


def _trig(v: Any) -> int:
    """Trigger in permille, 0..1000."""
    return max(0, min(1000, round(int(v) * 1000 / 255))) if isinstance(v, int) else 0


def frame_state(r: dict[str, Any] | None) -> dict[str, Any]:
    if r is None:
        return {"buttons": [], "ls": [0, 0], "rs": [0, 0], "l2": 0, "r2": 0}
    return {"buttons": buttons(int(r.get("b") or 0)), "ls": [_stick(r.get("lx")), _stick(r.get("ly"))],
            "rs": [_stick(r.get("rx")), _stick(r.get("ry"))], "l2": _trig(r.get("l2")), "r2": _trig(r.get("r2"))}


def build_track(records: list[dict[str, Any]], start_mono_ms: int, duration_s: float = 12.0, fps: int = 30) -> list[dict[str, Any]]:
    """State held at each frame time (the latest record at or before it). ``records`` must be verified and sorted."""
    ts = [r["t_us"] for r in records]
    out = []
    for f in range(int(round(duration_s * fps))):
        t_us = (start_mono_ms * 1000) + round(f * 1_000_000 / fps)
        i = bisect.bisect_right(ts, t_us) - 1
        out.append({"f": f, **frame_state(records[i] if i >= 0 else None)})
    return out
