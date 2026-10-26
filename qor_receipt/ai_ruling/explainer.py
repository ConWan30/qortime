"""Receipt + referee results -> plain-language explanation (advisory sidecar ``ai_ruling.json``).

Rules (tests: tests/test_ai_ruling.py):
* ADVISORY ONLY. The verdict is whatever ``verify_receipt`` returned. This module copies it, never computes, edits or
  overrides it, and never edits the receipt (``receipt.v0.json`` is frozen; output is a sidecar).
* Fail closed. Missing key, API error, empty/truncated answer or a guardrail hit -> ``ok=False``, ``explanation=None``,
  no sidecar. The verdict is identical either way.
* Only allowlisted facts go to the model: no pubkeys, player ids, device paths, match ids or wall-clock times.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from qor_receipt.ai_ruling.guardrail import DISPLAY, check_text
from qor_receipt.ai_ruling.muse import MuseClient, MuseError
from qor_receipt.jcs import canonicalize

SCHEMA = "qortime.ai_ruling.v0"
FILENAME = "ai_ruling.json"
LABEL = "ADVISORY. AI-written explanation of the QorTime referee's results. It does not decide or change the verdict."
BUILT_DURING = "Meta AI Developer Hackathon, Oct 26 - Nov 4 2026"
DEVICE_STATUSES = ("tpm_claimed", "software", "invalid")   # strongest is tpm_claimed (unverified claim)
MAX_DETAIL = 240

SYSTEM_PROMPT = """You explain QorTime match receipts to players and spectators in plain language.
QorTime is anti-cheat for console matches: a capture client records the match and a referee checks the evidence.
Rules you must follow:
1. The verifier STATUS is given to you and is final. State it exactly once near the start, in capitals, using the
   given word (VERIFIED, PARTIAL, SELF-SIGNED or FAIL). Never use any other status word. Never suggest it should differ.
2. Explain only from the FACTS. Name what passed, what is missing or failed, and what is NOT proven. Do not invent.
3. Never use the words "attested" or "hardware-verified". A device key is at most "TPM claimed (unverified)".
4. This is not coaching. Do not give gameplay advice, tips or commentary on skill.
5. Do not claim who was holding the controller.
6. Plain text, no markdown, at most 120 words."""


@dataclass
class ExplainResult:
    ok: bool
    status: str                         # copied from the verifier, never changed
    explanation: str | None = None
    reason: str = "ok"
    problems: list[str] = field(default_factory=list)
    sidecar: dict[str, Any] | None = None


def _clean(s: Any) -> str:
    s = re.sub(r"attested", "signed", str(s), flags=re.I)
    return s[:MAX_DETAIL]


def facts_for(receipt: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted, PII-free facts from the receipt and the verifier output. Pure; does not mutate inputs."""
    r = receipt if isinstance(receipt, dict) else {}
    m, res = r.get("match") or {}, r.get("result") or {}
    status = v.get("status")
    f: dict[str, Any] = {
        "status": DISPLAY.get(status, "UNKNOWN"),
        "match": {k: m.get(k) for k in ("game", "platform", "mode") if m.get(k) is not None},
        "claimed_result": {k: res.get(k) for k in ("away_score", "home_score", "quarter", "winner", "method", "final_confirmed")
                           if res.get(k) is not None},
        "errors": [_clean(e) for e in v.get("errors") or []],
        "missing_or_partial": [_clean(p) for p in v.get("partial") or []],
        "referee": {"trusted": v.get("referee_trust") == "active", "id": v.get("referee_id")},
    }
    if v.get("delegation"):
        f["referee"]["delegated_online_key"] = {"serial": v["delegation"].get("serial"), "root": v["delegation"].get("delegated_by")}
    rv = v.get("review") or {}
    if rv:
        f["review"] = {"video_method": rv.get("video_method"), "segments_checked": rv.get("segments_verified"),
                       "segments_total": rv.get("segments_total"), "not_proven": [_clean(x) for x in rv.get("not_proven") or []]}
    checks = ((r.get("review") or {}).get("checks")) or {}
    if isinstance(checks, dict) and checks:
        f["referee_checks"] = {str(k): {"state": c.get("state"), "detail": _clean(c.get("detail") if isinstance(c.get("detail"), str) else "")}
                               for k, c in sorted(checks.items()) if isinstance(c, dict)}
    ia = v.get("input_advisory")
    if isinstance(ia, dict):
        f["controller_input_advisory"] = {k: ia.get(k) for k in ("ok", "flags", "human_permille", "match_permille", "chance_permille",
                                                                  "haptic_events") if ia.get(k) is not None}
        f["controller_input_advisory"]["note"] = "advisory evidence only, not proof of a human player"
    dk = v.get("device_key")
    if isinstance(dk, dict):
        st = dk.get("status")
        f["device_key"] = {"status": st if st in DEVICE_STATUSES else "invalid",
                           "note": "TPM claimed means the client says so; it is not hardware proof"}
    return json.loads(json.dumps(f))  # plain JSON, detached from inputs


def receipt_sha256(receipt: dict[str, Any], raw: bytes | None = None) -> str:
    """SHA-256 of the receipt file bytes when given, else of its JCS form."""
    return hashlib.sha256(raw if raw is not None else canonicalize(receipt)).hexdigest()


def explain(receipt: dict[str, Any], verify_result: dict[str, Any], *, client: MuseClient | None = None,
            receipt_bytes: bytes | None = None) -> ExplainResult:
    """Never raises. Never mutates ``receipt`` or ``verify_result``."""
    status = str((verify_result or {}).get("status"))
    try:
        if status not in DISPLAY:
            return ExplainResult(False, status, reason="unknown_status")
        client = client or MuseClient()
        if not client.has_key:
            return ExplainResult(False, status, reason="api_key_missing")
        facts = facts_for(copy.deepcopy(receipt), copy.deepcopy(verify_result))
        user = "STATUS: " + facts["status"] + "\nFACTS (JSON):\n" + json.dumps(facts, sort_keys=True)
        reply = client.chat(SYSTEM_PROMPT, user)
        text = reply.text.strip()
        probs = check_text(text, status)
        if probs:
            return ExplainResult(False, status, reason="guardrail", problems=probs)
        sidecar = {
            "schema": SCHEMA, "label": LABEL, "built_during": BUILT_DURING, "affects_verdict": False,
            "receipt_sha256": receipt_sha256(receipt, receipt_bytes),
            "receipt_jcs_sha256": hashlib.sha256(canonicalize(receipt)).hexdigest(),
            "verdict_echo": {"status": status, "errors": list(verify_result.get("errors") or []),
                             "partial": list(verify_result.get("partial") or [])},
            "model": reply.model, "explanation": text,
            "guardrail": {"passed": True, "rules": ["status_stated_exactly", "no_banned_trust_words", "no_hardware_claim",
                                                     "no_coaching", "no_identity_claim", "max_chars"]},
            "facts_sha256": hashlib.sha256(canonicalize(facts)).hexdigest(),
            "usage": dict(reply.usage),
        }
        return ExplainResult(True, status, explanation=text, sidecar=sidecar)
    except MuseError as e:
        return ExplainResult(False, status, reason=f"api_error:{e}")
    except Exception as e:  # noqa: BLE001  advisory layer must never take the verifier down
        return ExplainResult(False, status, reason=f"error:{type(e).__name__}")
