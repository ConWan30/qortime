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


def input_changes(records: list[dict[str, Any]], stick_delta: int = 40, trig_delta: int = 60,
                  window_ms: int = 100) -> list[int]:
    """Mono ms of every button rising edge, or a stick/trigger move of >= delta within ``window_ms``.

    Sticks move in small steps at the report rate (a Madden kick swing is 255 -> 128 over ~100 ms in steps of 2-20),
    so moves are measured against a baseline that resets every ``window_ms``, not against the previous report.
    """
    out: list[int] = []
    prev_b = None
    base: dict[str, tuple[int, int]] = {}
    for r in records:
        t = r["t_us"] // 1000
        hit = False
        b = int(r.get("b") or 0)
        if prev_b is not None and b & ~prev_b:
            hit = True
        prev_b = b
        for k, d in (("lx", stick_delta), ("ly", stick_delta), ("rx", stick_delta), ("ry", stick_delta),
                     ("l2", trig_delta), ("r2", trig_delta)):
            v = int(r.get(k) or 0)
            if k not in base:
                base[k] = (v, t)
                continue
            bv, bt = base[k]
            if abs(v - bv) >= d and (k in ("lx", "ly", "rx", "ry") or v > bv):
                hit = True
                base[k] = (v, t)
            elif t - bt > window_ms:
                base[k] = (v, t)
        if hit and (not out or t - out[-1] >= 50):
            out.append(t)
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
            flags.append({"kind": "no_press", "t_ms": int(a)})
    for s in stimuli_ms or []:
        j = bisect.bisect_right(changes, s)
        if j < len(changes) and changes[j] - s < MIN_REACTION_MS:
            flags.append({"kind": "too_fast", "t_ms": int(s), "reaction_ms": int(changes[j] - s)})
    cv = interval_cv(presses_hw(records))
    out["press_interval_cv_permille"] = None if cv is None else round(cv * 1000)
    if cv is not None and cv < MAX_CV:
        flags.append({"kind": "machine_regular", "cv_permille": round(cv * 1000)})
    return out


def score_match(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score several clips of one match with ONE pipeline lag (console + capture lag is a session property).

    clips: [{id, records, actions_ms, stimuli_ms}]. Anchors are pooled across clips; if the pool fails the alignment
    rules, every clip is unscorable and no flag counts.
    """
    pooled: list[float] = []
    per: list[tuple[dict[str, Any], list[int]]] = []
    for c in clips:
        ch = input_changes(c["records"])
        per.append((c, ch))
        for a in c.get("actions_ms") or []:
            i = bisect.bisect_right(ch, a) - 1
            if i >= 0 and 0 <= a - ch[i] <= MAX_REACTION_MS:
                pooled.append(a - ch[i])
    why = None
    lag = resid = None
    if len(pooled) < MIN_ANCHORS:
        why = "too_few_anchors"
    else:
        lag = statistics.median(pooled)
        resid = statistics.median(abs(x - lag) for x in pooled)
        if not PLAUSIBLE_LAG_MS[0] <= lag <= PLAUSIBLE_LAG_MS[1]:
            why = "implausible_lag"
        elif resid > MAX_RESID_MS:
            why = "alignment_residual_too_large"
    out = []
    for c, ch in per:
        row: dict[str, Any] = {"id": str(c.get("id", "clip")), "actions": len(c.get("actions_ms") or []),
                               "match_anchors": len(pooled), "lag_ms": None if lag is None else round(lag),
                               "resid_ms": None if resid is None else round(resid), "scorable": why is None, "flags": []}
        cv = interval_cv(presses_hw(c["records"]))
        row["press_interval_cv_permille"] = None if cv is None else round(cv * 1000)
        if why:
            row["why"] = why
            out.append(row)
            continue
        lo = round(lag) + MAX_RESID_MS + NO_PRESS_LOOKBACK_MS
        for a in c.get("actions_ms") or []:
            i = bisect.bisect_right(ch, a) - 1
            if i < 0 or a - ch[i] > lo:
                row["flags"].append({"kind": "no_press", "t_ms": int(a)})
        for st in c.get("stimuli_ms") or []:
            j = bisect.bisect_right(ch, st)
            if j < len(ch) and ch[j] - st < MIN_REACTION_MS:
                row["flags"].append({"kind": "too_fast", "t_ms": int(st), "reaction_ms": int(ch[j] - st)})
        if cv is not None and cv < MAX_CV:
            row["flags"].append({"kind": "machine_regular", "cv_permille": round(cv * 1000)})
        out.append(row)
    return out
