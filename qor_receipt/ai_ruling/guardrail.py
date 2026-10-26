"""Deterministic checks on the model's text. If any check fails, the explanation is dropped (fail closed).

The model never sees or decides the verdict as an open question: it is told the status. These checks make sure the
text it writes back cannot overstate it.
"""
from __future__ import annotations

import re

MAX_CHARS = 1500

# Words the explainer must never use. Device keys top out at "TPM claimed" (unverified); nothing is hardware proof.
BANNED = [
    (re.compile(r"attest", re.I), "word_attested"),
    (re.compile(r"hardware[\s-]*(verified|backed|proof|proven|attested|secured)", re.I), "hardware_claim"),
    (re.compile(r"\btpm[\s_-]*(verified|attested|proven)", re.I), "tpm_overclaim"),
    (re.compile(r"\bcoach", re.I), "coaching"),
    (re.compile(r"\bimprove your\b|\btips?\b(?! of)|\byou should (practice|play|try)\b", re.I), "coaching"),
    (re.compile(r"cheat[\s-]*free|guarantee", re.I), "overclaim"),
    (re.compile(r"\b(identity|who (was )?(holding|playing))\b[^.]{0,40}\b(confirmed|proven|verified)\b", re.I), "identity_claim"),
]

# Display names for the verifier's statuses (the page shows SELF_ATTESTED as SELF-SIGNED).
DISPLAY = {"VERIFIED": "VERIFIED", "PARTIAL": "PARTIAL", "SELF_ATTESTED": "SELF-SIGNED", "FAIL": "FAIL"}
_STATUS_WORD = re.compile(r"\b(VERIFIED|PARTIAL|SELF[-_ ]SIGNED|SELF_ATTESTED|FAIL(?:ED)?)\b")
_LOWER_VERIFIED_CLAIM = re.compile(r"\b(is|was|been|fully|officially)\s+verified\b", re.I)
_NEGATED = re.compile(r"\b(not|never|isn't|wasn't|hasn't)\s+(been\s+|fully\s+|officially\s+)?$", re.I)


def _norm_status(w: str) -> str:
    w = w.upper().replace(" ", "-").replace("_", "-")
    return {"SELF-ATTESTED": "SELF-SIGNED", "FAILED": "FAIL"}.get(w, w)


def check_text(text: str | None, status: str) -> list[str]:
    """Return a list of problems. Empty list = the text may be shown."""
    probs: list[str] = []
    want = DISPLAY.get(status)
    if want is None:
        return ["unknown_status"]
    t = (text or "").strip()
    if not t:
        return ["empty"]
    if len(t) > MAX_CHARS:
        probs.append("too_long")
    for rx, code in BANNED:
        if rx.search(t) and code not in probs:
            probs.append(code)
    words = {_norm_status(m.group(1)) for m in _STATUS_WORD.finditer(t)}
    if want not in words:
        probs.append("status_not_stated")
    if words - {want}:
        probs.append("status_contradiction:" + ",".join(sorted(words - {want})))
    if status != "VERIFIED":
        for m in _LOWER_VERIFIED_CLAIM.finditer(t):
            if not _NEGATED.search(t[max(0, m.start() - 25):m.start()]):
                probs.append("claims_verified")
                break
    return probs
