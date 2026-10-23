"""Write tests/vectors/viewer/*.json: synthetic receipts for the verifier page panels.

Each fixture is the official review vector (tests/vectors/review_receipt.json) re-signed by a DEMO referee key and
a DEMO player key that only the fixture's own allowlist trusts. The device_key and corroboration blocks come from
the real backend functions (assess_device_attest, build_corroboration). Nothing here is a published match, and the
shipped allowlist never trusts these keys. Run: python scripts/make_viewer_fixtures.py
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from qor_receipt import receipt as R
from qor_receipt.keys import SoftwareKey
from qor_receipt.review.corroboration import build_corroboration
from qor_receipt.review.device_attest import DEVICE_ATTEST_SCHEMA, assess_device_attest, device_signed_bytes
from qor_receipt.review.policy import REVIEW_KEY_KIND

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "vectors" / "viewer"
NOTE = ("SYNTHETIC test fixture for the verifier page. Demo keys, trusted only by the allowlist in this file. "
        "Not a published match and not a referee result.")
REFEREE = SoftwareKey.from_seed_hex("d0" * 32)
PLAYER = SoftwareKey.from_seed_hex("d1" * 32)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _spki(priv) -> bytes:
    return priv.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def device_sidecar(kind: str, head: str) -> dict:
    msg = device_signed_bytes(head)
    if kind == "software":
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("d2" * 32))
        return {"schema": DEVICE_ATTEST_SCHEMA, "key_provider": "software", "algorithm": "ed25519",
                "public_key_spki_b64": _b64(_spki(priv)), "non_exportable": False, "signature_b64": _b64(priv.sign(msg))}
    priv = ec.derive_private_key(0xD3D3D3, ec.SECP256R1())
    signed = msg if kind == "tpm" else b"some other bytes"
    return {"schema": DEVICE_ATTEST_SCHEMA, "key_provider": "windows-tpm", "algorithm": "ecdsa-p256-sha256",
            "public_key_spki_b64": _b64(_spki(priv)), "non_exportable": True,
            "signature_b64": _b64(priv.sign(signed, ec.ECDSA(hashes.SHA256())))}


def side(rec: dict, role: str, final: tuple[int, int], events: list[dict], pub: str, head: str) -> dict:
    return {"role": role, "player_pubkey": pub, "checkpoint_head": head, "final": final, "events": events,
            "challenges": [] if role == "away" else rec["challenges"], "own_checks": "pass",
            "timeline_file_mismatch": False}


def resign(rec: dict) -> dict:
    rec = copy.deepcopy(rec)
    rec["signatures"] = {"players": {}, "referee": None}
    rec = R.sign_referee(rec, REFEREE)
    return R.sign_player(rec, rec["players"][0]["role"], PLAYER)


def main() -> None:
    vec = json.loads((ROOT / "tests" / "vectors" / "review_receipt.json").read_text())
    base = copy.deepcopy(vec["receipt"])
    base["referee"] = {"pubkey": REFEREE.pubkey_hex, "key_id": REFEREE.key_id, "key_kind": REVIEW_KEY_KIND}
    base["players"][0]["pubkey"] = PLAYER.pubkey_hex
    head = base["checkpoints"]["home"][-1]["hash"]
    allow = copy.deepcopy(vec["allowlist"])
    allow["referees"].append({"id": "demo-official", "pubkey": REFEREE.pubkey_hex, "key_id": REFEREE.key_id,
                              "status": "active"})
    t0 = base["match"]["started_ms"]
    ev = [{"t_ms": t0 + 9_000, "home": 7, "away": 0, "quarter": 1}, {"t_ms": t0 + 21_000, "home": 7, "away": 7, "quarter": 2},
          {"t_ms": t0 + 33_000, "home": 14, "away": 7, "quarter": 4}]
    away_pub, away_head = "a1" * 32, "b2" * 32
    out: dict[str, dict] = {}

    r = copy.deepcopy(base)
    r["signals"]["input"]["device"] = "DualSense Edge (USB)"
    r["review"]["device_key"] = assess_device_attest(json.dumps(device_sidecar("tpm", head)).encode(), chain_head_hex=head)
    r["review"]["corroboration"] = build_corroboration(
        side(r, "home", (14, 7), ev, PLAYER.pubkey_hex, head),
        side(r, "away", (14, 7), [dict(e, t_ms=e["t_ms"] + 1_200) for e in ev], away_pub, away_head),
        home_present=True, away_present=True, pairing_id="5ad1e7c0ffee5ad1e7c0ffee5ad1e7c0")
    out["verified-tpm-claimed-corroborated"] = {"receipt": resign(r), "expect": "VERIFIED"}
    tampered = copy.deepcopy(out["verified-tpm-claimed-corroborated"]["receipt"])
    tampered["result"]["home_score"] = 21
    tampered["result"]["away_score"] = 7
    out["fail-tampered-after-signing"] = {"receipt": tampered, "expect": "FAIL"}

    r = copy.deepcopy(base)
    r["match"]["platform"] = "xbox-series"
    r["signals"]["input"]["device"] = "Xbox Wireless Controller (USB)"
    r["signals"]["motion_permille"]["home"] = 120
    r["review"]["checks"]["motion"] = {"state": "partial", "detail": {
        "bins": 40, "motion_permille": 120, "basis": r["review"]["checks"]["motion"]["detail"]["basis"]}}
    r["review"]["device_key"] = assess_device_attest(json.dumps(device_sidecar("bad", head)).encode(), chain_head_hex=head)
    r["review"]["corroboration"] = build_corroboration(
        side(r, "home", (14, 7), ev, PLAYER.pubkey_hex, head),
        side(r, "away", (14, 3), [ev[0], dict(ev[1], t_ms=ev[1]["t_ms"] + 9_000), dict(ev[2], away=3)], away_pub, away_head),
        home_present=True, away_present=True, pairing_id="5ad1e7c0ffee5ad1e7c0ffee5ad1e7c1")
    out["partial-motion-low-device-invalid-disagreed"] = {"receipt": resign(r), "expect": "PARTIAL"}

    r = copy.deepcopy(base)
    r["match"]["platform"] = "pc"
    r["signals"]["input"]["device"] = "Generic USB gamepad (HID)"
    r["review"]["device_key"] = assess_device_attest(json.dumps(device_sidecar("software", head)).encode(),
                                                     chain_head_hex=head)
    r["review"]["corroboration"] = build_corroboration(None, None, home_present=False, away_present=False)
    r["review"]["checks"]["example_vendor_probe"] = {"state": "pass", "detail": {
        "schema": "example-vendor-probe/v9", "note": "synthetic unknown check name, used to test the generic panel"}}
    r["review"]["example_sidecar"] = {"schema": "example-sidecar/v3", "frames": 12,
                                      "note": "synthetic unknown review block, used to test the generic panel"}
    out["verified-software-single-sided-unknown-evidence"] = {"receipt": resign(r), "expect": "VERIFIED"}

    OUT.mkdir(parents=True, exist_ok=True)
    for name, item in out.items():
        doc = {"note": NOTE, "expect": item["expect"], "receipt": item["receipt"], "referee_pub": REFEREE.pubkey_hex,
               "allowlist": allow}
        (OUT / f"{name}.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
