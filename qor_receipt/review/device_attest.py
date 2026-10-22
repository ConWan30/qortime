"""Optional QorCap sidecar ``device_attest.v0.json``.

Informational only. The status is ``tpm_claimed``, ``software``, or ``invalid``.
It is not a review check and it does not change VERIFIED / FAIL.

``tpm_attested`` is not emitted. A software keypair can sign a sidecar blob, and
that is not a TPM manufacturer EK chain or a Microsoft AIK certificate. The
strongest TPM status is ``tpm_claimed``, and the page says the claim is unverified.

The device-key signed bytes are in docs/DEVICE_ATTEST.md. They are an assumption
so the QorCap PR that adds this sidecar can match them exactly.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

DEVICE_ATTEST_SCHEMA = "qorcap-device-attest/v0"
TPM_CLAIMED, SOFTWARE, INVALID = "tpm_claimed", "software", "invalid"
STATUSES = frozenset({TPM_CLAIMED, SOFTWARE, INVALID})
TPM_PROVIDERS = frozenset({"tpm", "windows-tpm", "platform-crypto", "ncrypt"})
SOFTWARE_PROVIDERS = frozenset({"software", "software-file"})
ALGORITHMS = frozenset({"ed25519", "ecdsa-p256-sha256"})
_MAX_FILE = 65_536
_MAX_STR = 12_000
_MAX_SPKI = 1_024
_MAX_SIG = 256

_DETAILS = {
    TPM_CLAIMED: (
        "Device signature valid. The bundle claims a TPM key. That TPM claim is unverified. "
        "Does not change the receipt verdict."
    ),
    SOFTWARE: "Device signature valid for a software key. Does not change the receipt verdict.",
    INVALID: "Device attestation did not verify. Flagged. Does not change the receipt verdict.",
}


def device_signed_bytes(chain_head_hex: str) -> bytes:
    """Bytes the device key signs. Assumption: see docs/DEVICE_ATTEST.md.

    ``chain_head_hex`` is the lowercase hex of the player's final checkpoint hash
    (``receipt.checkpoints[role][-1].hash``), not the raw digest and not the video chain head.
    """
    return f"qorcap-device-attest/v0\n{chain_head_hex.lower()}\n".encode("ascii")


def chain_head_from_receipt(receipt: Any, role: str) -> str:
    if not isinstance(receipt, dict) or role not in ("home", "away"):
        return ""
    cps = receipt.get("checkpoints")
    if not isinstance(cps, dict):
        return ""
    chain = cps.get(role)
    if not isinstance(chain, list) or not chain or not isinstance(chain[-1], dict):
        return ""
    head = chain[-1].get("hash")
    if isinstance(head, str) and len(head) == 64 and all(c in "0123456789abcdef" for c in head.lower()):
        return head.lower()
    return ""


def device_key_for_bundle(bundle: Any) -> dict[str, Any] | None:
    files = getattr(bundle, "files", None) or {}
    raw = files.get("device_attest.v0.json") if isinstance(files, dict) else None
    if raw is None:
        return None
    head = chain_head_from_receipt(getattr(bundle, "receipt", None), getattr(bundle, "role", ""))
    return assess_device_attest(raw, chain_head_hex=head)


def assess_device_attest(raw: bytes | None, *, chain_head_hex: str) -> dict[str, Any] | None:
    """Parse and check one sidecar. ``None`` when the file is absent. Never raises."""
    if raw is None:
        return None
    try:
        return _assess(raw, chain_head_hex)
    except Exception:
        return _result(INVALID, ["device_attest_malformed"], detail=_DETAILS[INVALID])


def device_key_view(review: Any) -> dict[str, Any] | None:
    """Display copy. ``affects_verdict`` is forced false. Unknown statuses are dropped."""
    if not isinstance(review, dict):
        return None
    raw = review.get("device_key")
    if not isinstance(raw, dict) or raw.get("status") not in STATUSES:
        return None
    flags = [x for x in (raw.get("flags") or []) if isinstance(x, str)][:16]
    out: dict[str, Any] = {
        "status": raw["status"],
        "flags": flags,
        "affects_verdict": False,
        "detail": _DETAILS[raw["status"]],
    }
    for key in ("schema", "key_provider", "algorithm"):
        if isinstance(raw.get(key), str) and raw[key]:
            out[key] = raw[key][:80]
    if isinstance(raw.get("non_exportable"), bool):
        out["non_exportable"] = raw["non_exportable"]
    return out


def _assess(raw: bytes, chain_head_hex: str) -> dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MAX_FILE:
        return _result(INVALID, ["device_attest_too_large"])
    try:
        doc = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, ValueError):
        return _result(INVALID, ["device_attest_malformed"])
    if not isinstance(doc, dict) or len(doc) > 24:
        return _result(INVALID, ["device_attest_malformed"])
    schema = doc.get("schema") if isinstance(doc.get("schema"), str) else doc.get("schema_id")
    flags: list[str] = []
    if schema != DEVICE_ATTEST_SCHEMA:
        flags.append("schema_unexpected")
    provider = _provider(doc.get("key_provider"))
    algorithm = _algorithm(doc.get("algorithm"))
    non_exportable = doc.get("non_exportable") if isinstance(doc.get("non_exportable"), bool) else None
    if not isinstance(doc.get("non_exportable"), bool):
        flags.append("non_exportable_missing")
    spki = _b64(doc.get("public_key_spki_b64"), _MAX_SPKI)
    sig = _b64(doc.get("signature_b64"), _MAX_SIG)
    if spki is None or sig is None or algorithm is None:
        flags.append("device_fields_unreadable")
        return _result(INVALID, flags, schema=schema if isinstance(schema, str) else None,
                       provider=provider, algorithm=algorithm, non_exportable=non_exportable)
    head = chain_head_hex if isinstance(chain_head_hex, str) else ""
    if not (len(head) == 64 and all(c in "0123456789abcdef" for c in head.lower())):
        flags.append("chain_head_missing")
        return _result(INVALID, flags, schema=schema if isinstance(schema, str) else None,
                       provider=provider, algorithm=algorithm, non_exportable=non_exportable)
    if not _signature_ok(algorithm, spki, sig, device_signed_bytes(head.lower())):
        flags.append("device_signature_invalid")
        return _result(INVALID, flags, schema=schema if isinstance(schema, str) else None,
                       provider=provider, algorithm=algorithm, non_exportable=non_exportable)
    if schema != DEVICE_ATTEST_SCHEMA:
        return _result(INVALID, flags, schema=schema if isinstance(schema, str) else None,
                       provider=provider, algorithm=algorithm, non_exportable=non_exportable)
    if provider in SOFTWARE_PROVIDERS:
        return _result(SOFTWARE, flags, schema=schema, provider=provider, algorithm=algorithm,
                       non_exportable=non_exportable)
    if provider not in TPM_PROVIDERS:
        flags.append("key_provider_unknown")
        return _result(INVALID, flags, schema=schema, provider=provider, algorithm=algorithm,
                       non_exportable=non_exportable)
    # tpm_attestation is ignored. A software key can mint that blob. tpm_attested
    # is reserved for a future check against a pinned TPM EK root or Microsoft AIK
    # certificate, and this referee does not perform that check.
    if non_exportable is not True:
        flags.append("exportable_key")
    return _result(TPM_CLAIMED, flags, schema=schema, provider=provider, algorithm=algorithm,
                   non_exportable=non_exportable)


def _signature_ok(algorithm: str, spki: bytes, sig: bytes, message: bytes) -> bool:
    try:
        pub = load_der_public_key(spki)
    except (ValueError, TypeError):
        return False
    try:
        if algorithm == "ed25519":
            if not isinstance(pub, Ed25519PublicKey):
                return False
            pub.verify(sig, message)
            return True
        if algorithm == "ecdsa-p256-sha256":
            if not isinstance(pub, ec.EllipticCurvePublicKey) or pub.curve.name != "secp256r1":
                return False
            pub.verify(sig, message, ec.ECDSA(hashes.SHA256()))
            return True
    except InvalidSignature:
        return False
    except Exception:
        return False
    return False


def _provider(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _algorithm(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    norm = value.strip().lower().replace("_", "-")
    aliases = {"ecdsa-p256": "ecdsa-p256-sha256", "p256": "ecdsa-p256-sha256", "ed25519": "ed25519"}
    norm = aliases.get(norm, norm)
    return norm if norm in ALGORITHMS else None


def _b64(value: Any, limit: int) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_STR:
        return None
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-")
    if any(c not in alphabet or c.isspace() for c in value):
        return None
    padded = value + ("=" * (-len(value) % 4))
    try:
        out = base64.urlsafe_b64decode(padded) if ("-" in value or "_" in value) else base64.b64decode(padded, validate=True)
    except Exception:
        return None
    if not out or len(out) > limit:
        return None
    return out


def _result(status: str, flags: list[str], *, schema: str | None = None, provider: str | None = None,
            algorithm: str | None = None, non_exportable: bool | None = None, detail: str | None = None) -> dict[str, Any]:
    seen: list[str] = []
    for flag in flags:
        if flag not in seen:
            seen.append(flag)
    out: dict[str, Any] = {
        "status": status,
        "flags": seen[:16],
        "affects_verdict": False,
        "detail": detail or _DETAILS[status],
    }
    if schema:
        out["schema"] = schema[:80]
    if provider:
        out["key_provider"] = provider
    if algorithm:
        out["algorithm"] = algorithm
    if isinstance(non_exportable, bool):
        out["non_exportable"] = non_exportable
    return out
