"""Press-to-pixel check (hackathon work). Deterministic; no AI decides anything here.

Times are capture-clock monotonic ms (the clock QorCap stamps input records with; ``review/clock.py`` maps video
frames onto it). Two kinds of on-screen event:

- **action** events: something the player made happen (a big on-screen change). A press must come BEFORE it by
  the console's pipeline lag. ``action_lag = action - nearest preceding input change``.
- **stimulus** events (optional): something the player reacts to (e.g., the snap). ``reaction = first input change
  after the stimulus - stimulus``.

Per-clip alignment: the median action lag over anchors is the clip's pipeline lag. If there are fewer than
MIN_ANCHORS anchors, the median falls outside PLAUSIBLE_LAG_MS, or the median absolute residual is over
MAX_RESID_MS, the clip is ``unscorable`` and none of its flags count.

Flags (scorable clips only):
- ``no_press``: an action with no input change in [lag - MAX_RESID_MS - 600, action] ms, i.e. nothing behind it;
- ``too_fast``: a reaction under MIN_REACTION_MS (120 ms, below human simple-reaction floor) after a stimulus;
- ``machine_regular``: >= MIN_PRESSES presses whose hardware-timestamp intervals have CV < MAX_CV (3.5 %).
"""
from __future__ import annotations

import bisect
import statistics
from typing import Any

MIN_REACTION_MS = 120
MAX_REACTION_MS = 700
PLAUSIBLE_LAG_MS = (0, 300)
MAX_RESID_MS = 100
MIN_ANCHORS = 3
MAX_CV = 0.035
MIN_PRESSES = 8
NO_PRESS_LOOKBACK_MS = 600


def input_changes(records: list[dict[str, Any]], stick_delta: int = 40, trig_delta: int = 60) -> list[int]:
    """Mono ms of every button rising edge or big stick/trigger move (verified, time-sorted records)."""
    out: list[int] = []
    prev: dict[str, Any] | None = None
    for r in records:
        if prev is not None:
            rise = int(r.get("b") or 0) & ~int(prev.get("b") or 0)
            moved = any(abs(int(r.get(k) or 0) - int(prev.get(k) or 0)) >= stick_delta for k in ("lx", "ly", "rx", "ry"))
            trig = any(int(r.get(k) or 0) - int(prev.get(k) or 0) >= trig_delta for k in ("l2", "r2"))
            if rise or moved or trig:
                out.append(r["t_us"] // 1000)
        prev = r
    return out


def presses_hw(records: list[dict[str, Any]]) -> list[int]:
    """Hardware timestamps (us) of button rising edges; falls back to t_us when hw is missing."""
    out, prev = [], 0
    for r in records:
        cur = int(r.get("b") or 0)
        if cur & ~prev:
            out.append(int(r["hw"]) if isinstance(r.get("hw"), int) else int(r["t_us"]))
        prev = cur
    return out


def interval_cv(ts: list[int]) -> float | None:
    iv = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if len(iv) < MIN_PRESSES - 1:
        return None
    m = statistics.fmean(iv)
    return statistics.pstdev(iv) / m if m > 0 else None


def score_clip(records: list[dict[str, Any]], actions_ms: list[int], stimuli_ms: list[int] | None = None,
               clip_id: str = "clip") -> dict[str, Any]:
    changes = input_changes(records)
    lags = []
    for a in actions_ms:
        i = bisect.bisect_right(changes, a) - 1
        if i >= 0 and 0 <= a - changes[i] <= MAX_REACTION_MS:
            lags.append(a - changes[i])
    out: dict[str, Any] = {"id": clip_id, "anchors": len(lags), "scorable": False, "lag_ms": None, "resid_ms": None,
                           "flags": []}
    if len(lags) < MIN_ANCHORS:
        out["why"] = "too_few_anchors"
        return out
    lag = statistics.median(lags)
    resid = statistics.median(abs(x - lag) for x in lags)
    out["lag_ms"], out["resid_ms"] = round(lag), round(resid)
    if not PLAUSIBLE_LAG_MS[0] <= lag <= PLAUSIBLE_LAG_MS[1]:
        out["why"] = "implausible_lag"
        return out
    if resid > MAX_RESID_MS:
        out["why"] = "alignment_residual_too_large"
        return out
    out["scorable"] = True
    flags = out["flags"]
    lo = round(lag) + MAX_RESID_MS + NO_PRESS_LOOKBACK_MS
    for a in actions_ms:
        i = bisect.bisect_right(changes, a) - 1
        if i < 0 or a - changes[i] > lo:
            flags.append({"kind": "no_press", "t_ms": a})
    for s in stimuli_ms or []:
        j = bisect.bisect_right(changes, s)
        if j < len(changes) and changes[j] - s < MIN_REACTION_MS:
            flags.append({"kind": "too_fast", "t_ms": s, "reaction_ms": changes[j] - s})
    cv = interval_cv(presses_hw(records))
    out["press_interval_cv"] = None if cv is None else round(cv, 4)
    if cv is not None and cv < MAX_CV:
        flags.append({"kind": "machine_regular", "cv": round(cv, 4)})
    return out
