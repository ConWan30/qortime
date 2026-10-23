"""Nonce challenges -> DualSense button patterns, and the HID/vision verifier.

The referee issues a random nonce for three phases: ``kickoff`` (game start), ``halftime`` (second half) and
``end`` (4th quarter). The nonce maps
deterministically to: OPTIONS (pause), then N face/d-pad buttons. The player
performs it on the DualSense Edge (Bluetooth to the PS5). The same controller
is also on USB to the PC, so the agent's HID log records the presses, and the
capture card shows the pause menu opening. The verifier requires:

1. HID log contains OPTIONS then exactly the pattern (next distinct presses),
   all after issue time and before the deadline.
2. Vision saw the pause menu open 0..max_latency ms after the OPTIONS press
   (before the press => video not live / not this console; too late => relayed).

A pre-recorded or replayed stream cannot contain a nonce issued just now.

v0.3.11 (pause-capable windows). In online Madden 27 a player cannot pause during a kickoff or a kickoff return:
OPTIONS only opens the pause menu once a play has been called (pre-snap). So the recorder asks for a phase's nonce
only at a pause-capable moment (after the kickoff of that half/quarter and a play call, see QorCap's
``qorcap_playcall``), and the window is ``DEFAULT_DEADLINE_MS`` = 90 s so a quick snap by the opponent does not
burn it: the player pauses at the next pre-snap. Phase names are unchanged for receipt compatibility.
Inside the window the response is the first OPTIONS press that is followed by exactly the pattern, so an
OPTIONS press that did nothing (live play) does not void the challenge. Security is unchanged: the pattern is
unknown before ``issued_ms``, every press must fall inside [issued_ms, deadline_ms], the pause menu must appear
0..600 ms after THAT press on video, and windows longer than ``MAX_WINDOW_MS`` are refused by the verifiers.

Rule v2 (``bundle.json`` ``challenge_rule`` = 2) is the capture-path measurement. On a real Madden/PS5
HDMI capture the pause menu is drawn about 1.5–1.75 s after OPTIONS, so this 600 ms ceiling cannot
pass an honest press. Rule v2 dates frames from the fragment PTS, accepts 0..2500 ms, and requires
the successful latencies to sit within ±300 ms of their median. A receipt result with no ``rule``
field keeps the 600 ms meaning.

Rule v3 keeps that clock, that ceiling, and that ±300 ms spread. The only change is the dark-menu
luma caps: 26 full-frame and 16 lower-half, instead of the 16/8 defaults. Those defaults miss the
menu on the cheap USB3.0 Video path (measured pause about 16.6/6.7, end menu about 21/13; gameplay
on the same path is about 60–110). The text-fraction window, confirm-frame hold, and press ordering
stay put. A bundle opts in with ``challenge_rule`` = 3. A rule-2 bundle from QorCap >= 0.3.31 is
measured the same way and stamped ``result.rule`` = 3: that recorder already used 26/16 and its
bundle schema cannot write 3. Older rule-2 bundles stay on 16/8 and stay rule 2. Anything else
stays rule v1.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any

PAUSE = "OPTIONS"
ALPHABET = ["CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT"]
PHASES = ("kickoff", "halftime", "end")
DEFAULT_LEN = 4
DEFAULT_DEADLINE_MS = 90_000   # v0.3.11: was 20 s (passable only if the prompt landed pre-snap)
LEGACY_DEADLINE_MS = 20_000
MAX_WINDOW_MS = 120_000          # verifiers refuse longer windows (partial: challenge_window_too_long)
MIN_LATENCY_MS = 0
MAX_LATENCY_MS = 600
# Rule v2 is opt-in. These do not change verify_response's default ceiling.
# Rule v3 uses the same ceiling and spread. It does not widen verify_response's default either.
RULE_V1 = 1
RULE_V2 = 2
RULE_V3 = 3
RULE2_MAX_LATENCY_MS = 2_500
RULE2_CONSISTENCY_MS = 300
# First QorCap that measured this capture path at the rule-3 caps and still wrote challenge_rule 2.
RULE3_RECORDER_FROM = (0, 3, 31)
# Leave the pause menu or snap a live play. Allowed only in the last pattern slot.
MENU_EXIT_BUTTONS = ("CROSS", "CIRCLE")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def new_nonce() -> str:
    return secrets.token_hex(16)


def pattern_for(nonce_hex: str, length: int = DEFAULT_LEN) -> list[str]:
    """Deterministic: sha256(nonce) bytes mod 8. No immediate repeats (easier to perform/detect).

    Verifiers recompute this from the nonce. Do not change the alphabet or the derivation.
    New issues that need a menu-safe pattern redraw the nonce (``new_challenge_nonce``).
    """
    d = hashlib.sha256(bytes.fromhex(nonce_hex)).digest()
    out: list[str] = []
    i = 0
    while len(out) < length:
        b = ALPHABET[d[i % len(d)] % len(ALPHABET)]
        i += 1
        if out and out[-1] == b:
            b = ALPHABET[(ALPHABET.index(b) + 1) % len(ALPHABET)]
        out.append(b)
    return [PAUSE] + out


def pattern_menu_safe(pattern: list[str]) -> bool:
    """CIRCLE and CROSS may sit in the last slot only.

    In Madden's pause menu CIRCLE backs out and CROSS selects a menu item (and can snap the ball
    once play is live). Either button earlier in the pattern makes the remaining presses happen
    after that side effect. OPTIONS is the pause itself and is not one of these buttons.
    """
    if not pattern or pattern[0] != PAUSE or len(pattern) < 2:
        return False
    return all(b not in MENU_EXIT_BUTTONS for b in pattern[1:-1])


def new_challenge_nonce(length: int = DEFAULT_LEN, *, attempts: int = 128) -> str:
    """Draw nonces until ``pattern_for`` keeps CIRCLE and CROSS out of every slot but the last."""
    for _ in range(attempts):
        nonce = new_nonce()
        if pattern_menu_safe(pattern_for(nonce, length)):
            return nonce
    raise RuntimeError("could not draw a menu-safe challenge nonce")


def recorder_version(meta: dict | None) -> tuple[int, int, int] | None:
    """Leading ``major.minor.patch`` of ``qorcap_version``, or None when it is not that shape."""
    raw = (meta or {}).get("qorcap_version")
    if not isinstance(raw, str):
        return None
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@dataclass
class ChallengeResult:
    ok: bool
    reason: str
    pause_press_ms: int | None = None
    done_ms: int | None = None
    latency_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "pause_press_ms": self.pause_press_ms,
                "done_ms": self.done_ms, "latency_ms": self.latency_ms}


def response_start(pattern: list[str], presses: list[dict[str, Any]]) -> int | None:
    """Index (into time-sorted down presses) of the OPTIONS press that answers the challenge: the first one followed
    by exactly the pattern; else the first OPTIONS press (so the failure reason stays pattern_mismatch); else None."""
    first = None
    for i, e in enumerate(presses):
        if e["button"] != PAUSE:
            continue
        if first is None:
            first = i
        if [x["button"] for x in presses[i : i + len(pattern)]] == pattern:
            return i
    return first


def response_press_ms(pattern: list[str], hid_events: list[dict[str, Any]], *, issued_ms: int,
                      deadline_ms: int) -> int | None:
    """t_ms of the answering OPTIONS press inside the window (what the video pause frame is matched against)."""
    presses = sorted((e for e in hid_events if e.get("down", True) and issued_ms <= e["t_ms"] <= deadline_ms),
                     key=lambda e: e["t_ms"])
    i = response_start(pattern, presses)
    return presses[i]["t_ms"] if i is not None else None


def verify_response(pattern: list[str], hid_events: list[dict[str, Any]], *, issued_ms: int,
                    deadline_ms: int, pause_seen_ms: int | None,
                    min_latency_ms: int = MIN_LATENCY_MS, max_latency_ms: int = MAX_LATENCY_MS) -> ChallengeResult:
    """hid_events: [{"t_ms": int, "button": str, "down": bool}] in capture order."""
    presses = sorted((e for e in hid_events if e.get("down") and issued_ms <= e["t_ms"] <= deadline_ms),
                     key=lambda e: e["t_ms"])
    start = response_start(pattern, presses)
    if start is None:
        return ChallengeResult(False, "no_pause_press")
    seq = [e["button"] for e in presses[start : start + len(pattern)]]
    if seq != pattern:
        return ChallengeResult(False, "pattern_mismatch", presses[start]["t_ms"])
    p_ms = presses[start]["t_ms"]
    done = presses[start + len(pattern) - 1]["t_ms"]
    if pause_seen_ms is None:
        return ChallengeResult(False, "pause_not_seen_on_video", p_ms, done)
    lat = pause_seen_ms - p_ms
    if lat < min_latency_ms:
        return ChallengeResult(False, "video_before_input", p_ms, done, lat)
    if lat > max_latency_ms:
        return ChallengeResult(False, "video_latency_too_high", p_ms, done, lat)
    return ChallengeResult(True, "ok", p_ms, done, lat)


def challenge_rule_of(meta: dict | None) -> int:
    """Bundle opt-in. Unknown values stay v1 (fail closed).

    Integer 3 is rule v3. Integer 2 is rule v2, except a QorCap >= 0.3.31 bundle: that recorder
    asked for the capture-path measurement and its own detector already used the rule-3 caps, but
    it can only write ``challenge_rule`` 2. The referee measures those bundles as rule v3 and
    stamps 3. A version string alone does not opt in. Older rule-2 bundles stay rule v2.
    """
    raw = (meta or {}).get("challenge_rule")
    if raw == RULE_V3:
        return RULE_V3
    if raw == RULE_V2:
        ver = recorder_version(meta)
        if ver is not None and ver >= RULE3_RECORDER_FROM:
            return RULE_V3
        return RULE_V2
    return RULE_V1


def result_rule_id(res: dict | None) -> int | None:
    """Rule stamped on a signed result. Absent means v1. An unknown value is not a wider ceiling."""
    if not isinstance(res, dict) or "rule" not in res or res.get("rule") is None:
        return RULE_V1
    rule = res.get("rule")
    if isinstance(rule, bool) or not isinstance(rule, int) or rule not in (RULE_V1, RULE_V2, RULE_V3):
        return None
    return rule


def latency_ceiling_ms(rule_id: int | None) -> int:
    """Unknown rules use the v1 ceiling, so a made-up rule id cannot widen the bound.

    Rule v3 uses the rule v2 ceiling. It does not add a new timing window.
    """
    return RULE2_MAX_LATENCY_MS if rule_id in (RULE_V2, RULE_V3) else MAX_LATENCY_MS


def median_ms(values: list[int]) -> float:
    """Middle value, or the average of the two middle values when the count is even."""
    s = sorted(int(v) for v in values)
    mid = len(s) // 2
    if len(s) % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def deviations_over(latencies: list[int], window_ms: int = RULE2_CONSISTENCY_MS) -> list[int]:
    """Indexes more than ``window_ms`` from the median. A difference of exactly ``window_ms`` passes.

    Fewer than two latencies cannot be compared, so a single challenge is not judged here.
    """
    if len(latencies) < 2:
        return []
    med = median_ms(latencies)
    return [i for i, lat in enumerate(latencies) if abs(lat - med) > window_ms]


def apply_consistency(measured: list[dict], *, window_ms: int = RULE2_CONSISTENCY_MS) -> list[int]:
    """Flip individually-ok results that sit outside the consistency window.

    ``measured`` items are challenge dicts with a ``result`` dict. Returns the indexes flipped to
    ``ok`` false, reason ``latency_inconsistent``. The latency number is kept. Callers run this
    for rule v2 and rule v3, and before the phase-pass check, so an outlier does not count as passed.
    """
    ok_at: list[int] = []
    lats: list[int] = []
    for i, c in enumerate(measured):
        res = c.get("result") or {}
        lat = res.get("latency_ms")
        if res.get("ok") and isinstance(lat, int) and not isinstance(lat, bool):
            ok_at.append(i)
            lats.append(int(lat))
    flipped: list[int] = []
    for j in deviations_over(lats, window_ms):
        res = measured[ok_at[j]]["result"]
        res["ok"] = False
        res["reason"] = "latency_inconsistent"
        flipped.append(ok_at[j])
    return flipped


def motion_correlation_permille(input_energy: list[int], video_motion: list[int], max_lag: int = 3) -> int:
    """Best Pearson correlation (x1000, int) between per-bin stick/trigger activity and frame-diff
    motion energy, video lagging input by 0..max_lag bins. A supporting signal, not a proof."""
    best = -1000
    for lag in range(0, max_lag + 1):
        x = input_energy[: len(input_energy) - lag] if lag else input_energy
        y = video_motion[lag : lag + len(x)]
        n = min(len(x), len(y))
        if n < 8:
            continue
        x, y = x[:n], y[:n]
        mx, my = sum(x) / n, sum(y) / n
        sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
        sxx = sum((a - mx) ** 2 for a in x)
        syy = sum((b - my) ** 2 for b in y)
        if sxx == 0 or syy == 0:
            continue
        best = max(best, int(round(1000 * sxy / (sxx * syy) ** 0.5)))
    return best
