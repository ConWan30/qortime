"""Sign / check ``ai_ruling.json`` with a separate AI ADVISORY key (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026).

Why a separate key: the referee key signs verdicts. An AI explanation must never be mistakable for that, so it is
signed by a different key kind (``qortime-ai-advisory``), listed in its own allowlist (``advisory_keys.json``), under a
different field name (``advisory_signature``, never ``signature`` / ``signatures``). A key that is on the trusted
referee list is refused here, and an advisory key on the referee list would be rejected by the checks below.

The signature only says "QorTime's AI advisory service wrote this text for this exact receipt". It does not touch
the verdict, which is still computed by ``verify_receipt``.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from qor_receipt.jcs import canonicalize
from qor_receipt.keys import KeyProvider, kid, verify
from qor_receipt.trusted_referees import lookup as referee_lookup

ADV_KIND = "qortime-ai-advisory"
SIG_FIELD = "advisory_signature"
KEYS_PATH = Path(__file__).with_name("advisory_keys.json")
ROLE = "advisory only; not a referee key; cannot make or change a verdict"
FORBIDDEN_TOP = ("signature", "signatures", "referee", "referee_ai")


def load_keys(path: str | Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or KEYS_PATH).read_text(encoding="utf-8"))


def receipt_jcs_sha256(receipt: dict[str, Any]) -> str:
    """Binding used by Python and the browser: SHA-256 of the JCS form of the whole receipt (signatures included)."""
    return hashlib.sha256(canonicalize(receipt)).hexdigest()


def signing_bytes(doc: dict[str, Any]) -> bytes:
    return canonicalize({k: v for k, v in doc.items() if k != SIG_FIELD})


def sign_ruling(doc: dict[str, Any], key: KeyProvider) -> dict[str, Any]:
    if referee_lookup(key.pubkey_hex) is not None:
        raise ValueError("refusing to sign an AI explanation with a referee key")
    if any(k in doc for k in FORBIDDEN_TOP):
        raise ValueError("ai_ruling must not carry referee/verdict signature fields")
    out = copy.deepcopy(doc)
    out.pop(SIG_FIELD, None)
    out["advisory_key"] = {"pubkey": key.pubkey_hex, "key_id": key.key_id, "key_kind": ADV_KIND, "role": ROLE}
    out[SIG_FIELD] = {"alg": "Ed25519", "key_id": key.key_id, "sig": key.sign(signing_bytes(out))}
    return out


def check_ruling(doc: Any, receipt: dict[str, Any], *, status: str | None = None,
                 keys: dict[str, Any] | None = None) -> list[str]:
    """Problems with a signed ai_ruling for this receipt. Empty list = OK to show (as advisory text). Never raises."""
    from qor_receipt.ai_ruling.explainer import SCHEMA

    p: list[str] = []
    try:
        if not isinstance(doc, dict):
            return ["not_an_object"]
        if doc.get("schema") != SCHEMA:
            p.append("schema")
        if doc.get("affects_verdict") is not False:
            p.append("affects_verdict_not_false")
        if any(k in doc for k in FORBIDDEN_TOP):
            p.append("forbidden_field")
        ak, sg = doc.get("advisory_key") or {}, doc.get(SIG_FIELD) or {}
        pub = str(ak.get("pubkey") or "").lower()
        if ak.get("key_kind") != ADV_KIND:
            p.append("key_kind")
        rows = [r for r in (keys or load_keys()).get("keys", []) if str(r.get("pubkey", "")).lower() == pub]
        if not rows or rows[0].get("status") != "active" or rows[0].get("key_kind") != ADV_KIND:
            p.append("advisory_key_not_allowlisted")
        if pub and (referee_lookup(pub) is not None or pub == str((receipt.get("referee") or {}).get("pubkey", "")).lower()):
            p.append("advisory_key_is_a_referee_key")
        if len(pub) != 64 or sg.get("alg") != "Ed25519" or sg.get("key_id") != kid(pub) or not verify(pub, str(sg.get("sig", "")), signing_bytes(doc)):
            p.append("bad_advisory_signature")
        if doc.get("receipt_jcs_sha256") != receipt_jcs_sha256(receipt):
            p.append("receipt_mismatch")
        if status is not None and (doc.get("verdict_echo") or {}).get("status") != status:
            p.append("verdict_echo_stale")
        if not isinstance(doc.get("explanation"), str) or not doc["explanation"].strip():
            p.append("no_explanation")
    except (KeyError, TypeError, ValueError) as e:
        p.append(f"malformed:{type(e).__name__}")
    return p
