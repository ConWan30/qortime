"""Action detection from a highlight clip for Show Your Hands (hackathon work, Oct 26 - Nov 4 2026).

Local and deterministic: frames are downscaled to 160x90 grey, QorCap's burned-in label (top-left) and the scorebug
strip (bottom) are masked, and the mean absolute frame difference gives a motion energy curve.

- ``onset``: motion starts after a still stretch (energy <= LO for >= STILL_S, then rises above HI within RISE_S).
  The event time is the first frame above LO. In Madden this is the snap / start of a play, which the player
  triggers with a press.
- ``cut``: a hard scene change (histogram correlation < CUT_CORR). Camera cuts are never actions.

Onsets are only *candidates*. A replay, cutscene or celebration also starts from stillness without a press, so
``muse_context.py`` can ask Muse Spark whether each candidate is live, player-controlled play; anything Muse calls
otherwise (or can't judge) is dropped, which can only remove flags, never add them.
"""
from __future__ import annotations

from typing import Any

LO, HI = 2.5, 6.0
STILL_S, RISE_S = 0.5, 1.0
CUT_CORR = 0.7
SMOOTH = 5


def energy_curve(path: str) -> tuple[list[float], list[float], float]:
    """(energy[i] between frame i and i+1, hist_corr[i], fps). Needs opencv (the ``vision`` extra)."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    prev = ph = None
    e: list[float] = []
    c: list[float] = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(cv2.resize(f, (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
        g[:25, :70] = 0
        g[80:, :] = 0
        h = cv2.calcHist([g], [0], None, [32], [0, 256])
        if prev is not None:
            e.append(float(np.mean(np.abs(g - prev))))
            c.append(float(cv2.compareHist(h, ph, cv2.HISTCMP_CORREL)))
        prev, ph = g, h
    cap.release()
    return e, c, fps


def smooth(x: list[float], k: int = SMOOTH) -> list[float]:
    h = k // 2
    return [sum(x[max(0, i - h):i + h + 1]) / len(x[max(0, i - h):i + h + 1]) for i in range(len(x))]


def detect(energy: list[float], corr: list[float], fps: float = 30.0) -> list[dict[str, Any]]:
    """Events in clip seconds: [{kind: onset|cut, t_s, frame}] (frame = first frame showing the change)."""
    ev: list[dict[str, Any]] = []
    for i, cc in enumerate(corr):
        if cc < CUT_CORR:
            ev.append({"kind": "cut", "frame": i + 1, "t_s": round((i + 1) / fps, 3)})
    cuts = {e["frame"] for e in ev}
    s = smooth(energy)
    still = int(STILL_S * fps)
    rise = int(RISE_S * fps)
    i, run = 0, 0
    while i < len(s):
        if s[i] <= LO:
            run += 1
            i += 1
            continue
        if run >= still and any(v > HI for v in s[i:i + rise]):
            f = i + 1
            if not any(abs(f - c) <= 3 for c in cuts):
                ev.append({"kind": "onset", "frame": f, "t_s": round(f / fps, 3)})
            while i < len(s) and s[i] > LO:
                i += 1
        else:
            i += 1
        run = 0
    return sorted(ev, key=lambda e: e["frame"])
