"""Device attestation sidecar: informational status, defensive parse, verdict unchanged."""
from __future__ import annotations

import base64
import copy
import hashlib
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from qor_receipt import receipt as R
from qor_receipt.demo import run_match
from qor_receipt.review.device_attest import (
    DEVICE_ATTEST_SCHEMA,
    STATUSES,
    assess_device_attest,
    device_key_view,
    device_signed_bytes,
)
from qor_receipt.review.policy import REVIEW_TYPE

HEAD = "ab" * 32


def _spki(priv) -> bytes:
    return priv.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _sign(priv, message: bytes, algorithm: str) -> bytes:
    if algorithm == "ed25519":
        return priv.sign(message)
    return priv.sign(message, ec.ECDSA(hashes.SHA256()))


def _doc(priv, algorithm, provider, *, non_exportable, message, attestation=None, schema=DEVICE_ATTEST_SCHEMA):
    body = {
        "schema": schema,
        "key_provider": provider,
        "algorithm": algorithm,
        "public_key_spki_b64": _b64(_spki(priv)),
        "non_exportable": non_exportable,
        "signature_b64": _b64(_sign(priv, message, algorithm)),
    }
    if attestation is not None:
        body["tpm_attestation"] = attestation
    return body


def _self_made_blob(device_priv, device_alg: str, ak, ak_alg: str) -> dict:
    """A software attestation key signing a statement about the device key. Not an EK chain."""
    device_spki = _spki(device_priv)
    statement = (
        "qorcap-tpm-key-attest/v0\n"
        f"spki_sha256={hashlib.sha256(device_spki).hexdigest()}\n"
        "non_exportable=true\n"
        f"algorithm={device_alg}\n"
        "key_provider=tpm\n"
    ).encode("ascii")
    return {
        "schema": "qorcap-tpm-key-attest/v0",
        "ak_algorithm": ak_alg,
        "ak_public_key_spki_b64": _b64(_spki(ak)),
        "signature_b64": _b64(_sign(ak, statement, ak_alg)),
        "ek_cert_chain": ["-----BEGIN CERTIFICATE-----\nMIIB-not-a-real-ek\n-----END CERTIFICATE-----"],
        "manufacturer_root": "not-a-pinned-root",
    }


def test_absent_sidecar_is_not_a_status():
    assert assess_device_attest(None, chain_head_hex=HEAD) is None


def test_software_ed25519_signature():
    priv = Ed25519PrivateKey.generate()
    doc = _doc(priv, "ed25519", "software", non_exportable=False, message=device_signed_bytes(HEAD))
    out = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
    assert out["status"] == "software"
    assert out["affects_verdict"] is False
    assert "device_signature_invalid" not in out["flags"]


def test_tpm_claimed_when_signature_valid_and_attestation_absent():
    priv = ec.generate_private_key(ec.SECP256R1())
    doc = _doc(priv, "ecdsa-p256-sha256", "tpm", non_exportable=True, message=device_signed_bytes(HEAD))
    out = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
    assert out["status"] == "tpm_claimed"
    assert out["non_exportable"] is True
    assert out["affects_verdict"] is False


def test_rsa_2048_tpm_key_is_claimed_not_attested():
    """QorCap signs RSA-2048 with RSASSA-PKCS1-v1_5 SHA-256. That stays tpm_claimed."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    message = device_signed_bytes(HEAD)
    body = {
        "schema": DEVICE_ATTEST_SCHEMA,
        "key_provider": "platform-crypto",
        "algorithm": "RSA_2048",
        "public_key_spki_b64": _b64(_spki(priv)),
        "non_exportable": True,
        "signature_b64": _b64(priv.sign(message, padding.PKCS1v15(), hashes.SHA256())),
    }
    out = assess_device_attest(json.dumps(body).encode(), chain_head_hex=HEAD)
    assert out["status"] == "tpm_claimed", out
    assert out["status"] != "tpm_attested"
    assert out["algorithm"] == "rsa-2048"
    assert "unverified" in out["detail"].lower()
    body["signature_b64"] = _b64(b"\x00" * 256)
    bad = assess_device_attest(json.dumps(body).encode(), chain_head_hex=HEAD)
    assert bad["status"] == "invalid"
    assert "device_signature_invalid" in bad["flags"]


def test_software_keypair_self_made_blob_never_exceeds_tpm_claimed():
    """A software device key plus a software attestation key must not reach tpm_attested."""
    assert "tpm_attested" not in STATUSES
    cases = (
        (Ed25519PrivateKey.generate(), "ed25519", Ed25519PrivateKey.generate(), "ed25519", "tpm"),
        (ec.generate_private_key(ec.SECP256R1()), "ecdsa-p256-sha256", Ed25519PrivateKey.generate(), "ed25519", "platform-crypto"),
        (ec.generate_private_key(ec.SECP256R1()), "ecdsa-p256-sha256", ec.generate_private_key(ec.SECP256R1()), "ecdsa-p256-sha256", "windows-tpm"),
        (Ed25519PrivateKey.generate(), "ed25519", Ed25519PrivateKey.generate(), "ed25519", "ncrypt"),
    )
    for device, device_alg, ak, ak_alg, provider in cases:
        blob = _self_made_blob(device, device_alg, ak, ak_alg)
        doc = _doc(device, device_alg, provider, non_exportable=True, message=device_signed_bytes(HEAD),
                   attestation=blob)
        out = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
        assert out["status"] == "tpm_claimed", (provider, out)
        assert out["status"] != "tpm_attested"
        assert "unverified" in out["detail"].lower()
        assert out["affects_verdict"] is False
    # The same device key signing its own blob is still only a claim.
    device = Ed25519PrivateKey.generate()
    doc = _doc(device, "ed25519", "tpm", non_exportable=True, message=device_signed_bytes(HEAD),
               attestation=_self_made_blob(device, "ed25519", device, "ed25519"))
    out = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
    assert out["status"] == "tpm_claimed"
    # A broken blob does not upgrade, and it does not turn a valid device signature into invalid.
    device = ec.generate_private_key(ec.SECP256R1())
    doc = _doc(device, "ecdsa-p256-sha256", "tpm", non_exportable=True, message=device_signed_bytes(HEAD),
               attestation={"schema": "qorcap-tpm-key-attest/v0", "signature_b64": _b64(b"\x00" * 64)})
    out = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
    assert out["status"] == "tpm_claimed"
    assert device_key_view({"device_key": {"status": "tpm_attested", "affects_verdict": True}}) is None


def test_exportable_tpm_key_stays_claimed():
    device = ec.generate_private_key(ec.SECP256R1())
    doc = _doc(device, "ecdsa-p256-sha256", "tpm", non_exportable=False, message=device_signed_bytes(HEAD))
    out = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
    assert out["status"] == "tpm_claimed"
    assert "exportable_key" in out["flags"]
    assert "unverified" in out["detail"].lower()


def test_bad_signature_is_invalid_and_flagged():
    priv = Ed25519PrivateKey.generate()
    doc = _doc(priv, "ed25519", "software", non_exportable=False, message=b"other-message")
    out = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
    assert out["status"] == "invalid"
    assert "device_signature_invalid" in out["flags"]
    assert "flagged" in out["detail"].lower() or "Flagged" in out["detail"]


def test_parser_rejects_junk_without_raising():
    assert assess_device_attest(b"not json", chain_head_hex=HEAD)["status"] == "invalid"
    assert "device_attest_malformed" in assess_device_attest(b"not json", chain_head_hex=HEAD)["flags"]
    assert assess_device_attest(b"[]", chain_head_hex=HEAD)["status"] == "invalid"
    assert assess_device_attest(b'{"schema": "qorcap-device-attest/v0", "public_key_spki_b64": 1}',
                                chain_head_hex=HEAD)["status"] == "invalid"
    huge = b'{"schema":"qorcap-device-attest/v0","pad":"' + (b"a" * 70_000) + b'"}'
    assert assess_device_attest(huge, chain_head_hex=HEAD)["flags"] == ["device_attest_too_large"]
    priv = Ed25519PrivateKey.generate()
    doc = _doc(priv, "ed25519", "software", non_exportable=False, message=device_signed_bytes(HEAD), schema="other")
    wrong = assess_device_attest(json.dumps(doc).encode(), chain_head_hex=HEAD)
    assert wrong["status"] == "invalid"
    assert "schema_unexpected" in wrong["flags"]


def test_device_key_does_not_change_verdict(ref):
    client, clock, key = ref
    rec, pkeys = run_match(client, clock, "single")
    before = R.verify_receipt(rec, referee_pub=key.pubkey_hex)

    def with_device(status, flags):
        edited = copy.deepcopy(rec)
        edited["review"] = {"device_key": {"status": status, "flags": flags, "affects_verdict": True,
                                           "detail": "attacker text"}}
        edited = R.sign_referee(edited, key)
        for role, player in pkeys.items():
            edited = R.sign_player(edited, role, player)
        return R.verify_receipt(edited, referee_pub=key.pubkey_hex)

    invalid = with_device("invalid", ["device_signature_invalid"])
    assert invalid["status"] == before["status"] == "SELF_ATTESTED"
    assert invalid["errors"] == before["errors"]
    assert invalid["partial"] == before["partial"]
    assert invalid["device_key"]["status"] == "invalid"
    assert invalid["device_key"]["affects_verdict"] is False
    assert invalid["device_key"]["flags"] == ["device_signature_invalid"]

    hidden_word = with_device("tpm_attested", [])
    assert hidden_word["status"] == before["status"]
    assert hidden_word["errors"] == before["errors"]
    assert "device_key" not in hidden_word

    claimed = with_device("tpm_claimed", [])
    assert claimed["status"] == before["status"]
    assert claimed["errors"] == before["errors"]
    assert claimed["device_key"]["status"] == "tpm_claimed"
    assert claimed["device_key"]["affects_verdict"] is False
    assert "unverified" in claimed["device_key"]["detail"].lower()

    failing = copy.deepcopy(rec)
    failing["result"]["winner"] = "away" if rec["result"]["winner"] != "away" else "home"
    failing["review"] = {"device_key": {"status": "tpm_claimed", "flags": [], "affects_verdict": True}}
    failing = R.sign_referee(failing, key)
    for role, player in pkeys.items():
        failing = R.sign_player(failing, role, player)
    verdict = R.verify_receipt(failing, referee_pub=key.pubkey_hex)
    assert verdict["status"] == "FAIL"
    assert "winner_inconsistent" in verdict["errors"]
    assert verdict["device_key"]["status"] == "tpm_claimed"

    forged = copy.deepcopy(rec)
    forged["review"] = {"device_key": {"status": "tpm_attested", "flags": []}}
    hidden = R.verify_receipt(forged, referee_pub=key.pubkey_hex)
    assert hidden["status"] == "FAIL"
    assert "bad_referee_signature" in hidden["errors"]
    assert "device_key" not in hidden


def test_device_key_is_not_a_review_check():
    referee = {"pubkey": "ab" * 32}
    review = {
        "type": REVIEW_TYPE,
        "capture_referee": {"pubkey": "cd" * 32},
        "checks": {name: {"state": "pass"} for name in
                   ("video_bytes", "final_reread", "capture_chain", "bundle_structure")},
        "device_key": {"status": "invalid", "flags": ["device_signature_invalid"]},
    }
    _cap, errs, part = R._review_problems({"review": review, "referee": referee}, referee)
    assert errs == [] and part == []
