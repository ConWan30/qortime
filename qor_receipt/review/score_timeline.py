"""Optional QorCap sidecar ``score_timeline.json`` (``qorcap-score-timeline/v0``).

This is the score timeline QorCap PR #59 writes. It is not a field of
``receipt.json``. ``receipt.v0.json`` rejects extra fields, so the events stay
here.

Signed bytes, matching ``sidecar/qorcap_score_timeline.py`` ``signing_message``:

RFC 8785 JCS of ``{type, signals, chain, chain_head, checkpoint_head}``.
``type`` is ``qorcap-score-timeline/v0``. The signature object is not included.

Each chain link is SHA-256 of JCS ``{t_ms, home, away, quarter, prev}``.
Genesis ``prev`` is 64 zero hex characters.

Two signatures are accepted over those exact bytes:

1. Ed25519, hex ``signature.pub`` and hex ``signature.sig``, where ``pub`` is
   the receipt player key. That is what ``write_score_timeline(bundle, device, rec)``
   writes today: ``device`` is the same key that signs the receipt, not the
   optional TPM key.
2. The public key in ``device_attest.v0.json`` (ECDSA P-256 SHA-256 or
   RSA-2048 RSASSA-PKCS1-v1_5 SHA-256), when that sidecar is present, its own
   signature over the checkpoint head verifies (status ``software`` or
   ``tpm_claimed``, never ``tpm_attested``), and ``signature.pub`` is that SPKI
   (standard base64) or the hex of the SPKI DER. ``signature.sig`` is hex or
   standard base64. PSS is not accepted.

A missing file, a bad chain, a bad checkpoint head, or a bad signature is not
a timeline. Corroboration then keeps today's ``unsigned_score_timeline`` /
``single_sided`` behavior. This module does not change VERIFIED or FAIL.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from qor_receipt.jcs import canonicalize, sha256_hex
from qor_receipt.keys import verify
from qor_receipt.review.device_attest import assess_device_attest, device_attest_verifies

SCHEMA = "qorcap-score-timeline/v0"
FILENAME = "score_timeline.json"
GENESIS = "0" * 64
_MAX_FILE = 1_048_576
_MAX_EVENTS = 400


def signing_message(*, signals: dict[str, Any], chain: list[dict[str, Any]], chain_head: str,
                    checkpoint_head: str) -> bytes:
    """Bytes QorCap signs. The signature object is not included."""
    return canonicalize({
        "type": SCHEMA,
        "signals": signals,
        "chain": chain,
        "chain_head": chain_head,
        "checkpoint_head": checkpoint_head,
    })


def chain_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Each link is SHA-256 of JCS({t_ms, home, away, quarter, prev})."""
    prev = GENESIS
    rows = []
    for event in events:
        body = {"t_ms": event["t_ms"], "home": event["home"], "away": event["away"],
                "quarter": event["quarter"], "prev": prev}
        digest = sha256_hex(body)
        rows.append(dict(body, hash=digest))
        prev = digest
    return rows, prev


def events_from_sidecar(raw: bytes | None, *, player_pub: str, checkpoint_head: str,
                        device_attest: bytes | None = None) -> list[dict[str, Any]] | None:
    """Verified events, or None when the file is absent or does not check out. Never raises."""
    if raw is None:
        return None
    try:
        return _events(raw, player_pub=player_pub, checkpoint_head=checkpoint_head, device_attest=device_attest)
    except Exception:
        return None


def _events(raw: bytes, *, player_pub: str, checkpoint_head: str,
            device_attest: bytes | None) -> list[dict[str, Any]] | None:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MAX_FILE:
        return None
    try:
        doc = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    stored_head = doc.get("checkpoint_head")
    if not isinstance(checkpoint_head, str) or _hex64(stored_head) != checkpoint_head.lower():
        return None
    signals = doc.get("signals")
    if not isinstance(signals, dict):
        return None
    block = signals.get("score_timeline")
    raw_events = block.get("events") if isinstance(block, dict) else None
    events = _plain_events(raw_events)
    if events is None:
        return None
    chained, head = chain_events(events)
    if doc.get("chain") != chained or doc.get("chain_head") != head:
        return None
    message = signing_message(signals={"score_timeline": {"events": events}}, chain=chained,
                              chain_head=head, checkpoint_head=stored_head)
    sig = doc.get("signature") if isinstance(doc.get("signature"), dict) else None
    if not isinstance(sig, dict):
        return None
    if not _signature_ok(sig, message, player_pub=player_pub, device_attest=device_attest,
                         checkpoint_head=checkpoint_head):
        return None
    return events


def _signature_ok(sig: dict[str, Any], message: bytes, *, player_pub: str, device_attest: bytes | None,
                  checkpoint_head: str) -> bool:
    pub, raw_sig = sig.get("pub"), sig.get("sig")
    if not isinstance(pub, str) or not isinstance(raw_sig, str):
        return False
    player = player_pub.lower() if isinstance(player_pub, str) else ""
    if player and pub.lower() == player and verify(player, raw_sig, message):
        return True
    signature = _sig_bytes(raw_sig)
    if signature is None or device_attest is None:
        return False
    if not _pub_is_device_key(pub, device_attest):
        return False
    if not _device_key_holds(device_attest, checkpoint_head):
        return False
    return device_attest_verifies(device_attest, message, signature)


def _device_key_holds(raw: bytes, checkpoint_head: str) -> bool:
    """The sidecar key must have signed the checkpoint head. A swapped key does not count."""
    out = assess_device_attest(raw, chain_head_hex=checkpoint_head)
    return isinstance(out, dict) and out.get("status") in ("tpm_claimed", "software")


def _pub_is_device_key(pub: str, device_attest: bytes) -> bool:
    try:
        doc = json.loads(bytes(device_attest).decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        return False
    if not isinstance(doc, dict):
        return False
    spki_b64 = doc.get("public_key_spki_b64")
    if not isinstance(spki_b64, str) or not spki_b64:
        return False
    if pub == spki_b64:
        return True
    try:
        der = base64.b64decode(spki_b64, validate=True)
    except (ValueError, binascii.Error):
        return False
    return pub.lower() == der.hex()


def _sig_bytes(value: str) -> bytes | None:
    if not value or len(value) > 4096:
        return None
    if all(c in "0123456789abcdefABCDEF" for c in value) and len(value) % 2 == 0:
        try:
            return bytes.fromhex(value)
        except ValueError:
            return None
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    if any(c not in alphabet for c in value):
        return None
    padded = value + ("=" * (-len(value) % 4))
    try:
        out = base64.b64decode(padded, validate=True)
    except (ValueError, binascii.Error):
        return None
    return out or None


def _plain_events(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list) or len(raw) > _MAX_EVENTS:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            t, h, a, q = item["t_ms"], item["home"], item["away"], item["quarter"]
        except KeyError:
            return None
        if not all(_nonneg(x) for x in (t, h, a, q)) or q < 1:
            return None
        out.append({"t_ms": int(t), "home": int(h), "away": int(a), "quarter": int(q)})
    return out


def _hex64(value: Any) -> str:
    if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower()):
        return value.lower()
    return ""


def _nonneg(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
