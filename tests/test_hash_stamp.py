"""Tests for auxiliary hash-only timestamp endpoint and fail-closed verifier (T-0037)."""
from __future__ import annotations

import copy
import json

from fastapi.testclient import TestClient

from qor_receipt.hash_stamp import (
    CLAIMS,
    GENESIS,
    MAX_BODY_BYTES,
    SCHEMA_REQUEST,
    SCHEMA_STAMP,
    SERVICE_ID,
    StampLedger,
    verify_hash_stamp,
)
from qor_receipt.keys import SoftwareKey
from qor_receipt.referee.app import create_app

GOLDEN_SEED = "11" * 32
GOLDEN_NONCE = "aa" * 32
GOLDEN_DIGESTS = {
    "chunks": 6,
    "frame_chain_head": "44" * 32,
    "hand_timeline_head": "22" * 32,
    "hand_timeline_sha256": "33" * 32,
    "receipt_sha256": "55" * 32,
}
GOLDEN_STAMP_ID = "64781e246bba612b3c835ca321465fa66a518ee9c7f0f603be929e7eba1ebcc6"
GOLDEN_PUBKEY = "d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737"
GOLDEN_KEY_ID = "10ba682c8ad13513"
GOLDEN_SIG = (
    "a8eac2cb31f697900a217dee08944021e92669bcf23b784c0f760f7846dcf888"
    "2b874a1374bcffc085e1cc090c8d5268f2dd5b0c61ee4ff8d13602058a28560e"
)


def _valid_req(*, nonce: str = GOLDEN_NONCE, digests: dict | None = None) -> dict:
    return {
        "schema": SCHEMA_REQUEST,
        "client_nonce": nonce,
        "digests": copy.deepcopy(digests if digests is not None else GOLDEN_DIGESTS),
    }


def test_deterministic_golden_vector():
    key = SoftwareKey.from_seed_hex(GOLDEN_SEED)
    ledger = StampLedger(key, clock=lambda: 1_764_000_000_000)
    stamp = ledger.issue(_valid_req())

    assert stamp["schema"] == SCHEMA_STAMP
    assert stamp["public_key"] == GOLDEN_PUBKEY
    assert stamp["stamp_id"] == GOLDEN_STAMP_ID
    assert stamp["sig"] == GOLDEN_SIG
    assert stamp["unsigned_payload"]["key_id"] == GOLDEN_KEY_ID
    assert stamp["unsigned_payload"]["ledger_seq"] == 0
    assert stamp["unsigned_payload"]["prev_ledger_sha256"] == GENESIS

    checked = verify_hash_stamp(
        stamp,
        trusted_key_ids={GOLDEN_KEY_ID},
        expected_digests=GOLDEN_DIGESTS,
    )
    assert checked["ok"] is True
    assert checked["key_trusted"] is True
    assert checked["stamp_id"] == GOLDEN_STAMP_ID


def test_fastapi_round_trip_and_ledger_chaining():
    now_ms = [1_764_000_000_000]
    key = SoftwareKey.from_seed_hex(GOLDEN_SEED)
    client = TestClient(create_app(key=key, clock=lambda: now_ms[0]))

    info = client.get("/aux/v1/hash-stamp/key")
    assert info.status_code == 200
    assert info.json() == {
        "service": SERVICE_ID,
        "alg": "Ed25519",
        "key_id": GOLDEN_KEY_ID,
        "public_key": GOLDEN_PUBKEY,
        "claims": CLAIMS,
    }

    r1 = client.post("/aux/v1/hash-stamp", json=_valid_req())
    assert r1.status_code == 200
    s1 = r1.json()
    assert s1["stamp_id"] == GOLDEN_STAMP_ID

    fetched = client.get(f"/aux/v1/hash-stamp/{GOLDEN_STAMP_ID}")
    assert fetched.status_code == 200
    assert fetched.json() == s1

    v1 = verify_hash_stamp(s1, trusted_pubkeys={GOLDEN_PUBKEY}, expected_digests=GOLDEN_DIGESTS)
    assert v1["ok"] is True
    assert v1["key_trusted"] is True

    # Identical retry is idempotent and does not advance ledger_seq.
    now_ms[0] += 5_000
    r1_retry = client.post("/aux/v1/hash-stamp", json=_valid_req())
    assert r1_retry.status_code == 200
    assert r1_retry.json() == s1

    # Reusing the same client_nonce with different digests fails closed (409).
    altered = _valid_req()
    altered["digests"]["chunks"] = 7
    conflict = client.post("/aux/v1/hash-stamp", json=altered)
    assert conflict.status_code == 409

    # A second distinct nonce advances ledger_seq and chains prev_ledger_sha256.
    req2 = _valid_req(
        nonce="bb" * 32,
        digests={"hand_timeline_head": "66" * 32, "hand_timeline_sha256": "77" * 32},
    )
    r2 = client.post("/aux/v1/hash-stamp", json=req2)
    assert r2.status_code == 200
    s2 = r2.json()
    assert s2["unsigned_payload"]["ledger_seq"] == 1
    assert s2["unsigned_payload"]["prev_ledger_sha256"] != GENESIS
    assert verify_hash_stamp(s2)["ok"] is True

    assert client.get("/aux/v1/hash-stamp/not-a-hex-id").status_code == 404
    assert client.get("/aux/v1/hash-stamp/" + "f" * 64).status_code == 404


def test_request_rejects_unknown_fields_pii_floats_and_oversize():
    key = SoftwareKey.from_seed_hex(GOLDEN_SEED)
    client = TestClient(create_app(key=key, clock=lambda: 1_764_000_000_000))

    bad_pad = _valid_req()
    bad_pad["digests"]["pad_id"] = "usb-serial-123"
    assert client.post("/aux/v1/hash-stamp", json=bad_pad).status_code == 400

    bad_dir = _valid_req()
    bad_dir["hand_dir"] = "D:\\QorCap\\practice-hand\\secret"
    assert client.post("/aux/v1/hash-stamp", json=bad_dir).status_code == 400

    bad_hex = _valid_req()
    bad_hex["digests"]["hand_timeline_head"] = "22" * 31
    assert client.post("/aux/v1/hash-stamp", json=bad_hex).status_code == 400

    bad_float = _valid_req()
    bad_float["digests"]["chunks"] = 6.5
    assert client.post("/aux/v1/hash-stamp", json=bad_float).status_code == 400

    bad_bool = _valid_req()
    bad_bool["digests"]["chunks"] = True
    assert client.post("/aux/v1/hash-stamp", json=bad_bool).status_code == 400

    bad_nan = json.dumps(_valid_req()).replace("6", "NaN")
    assert client.post("/aux/v1/hash-stamp", content=bad_nan.encode("utf-8")).status_code == 400

    oversize = ("{" + " " * (MAX_BODY_BYTES + 10) + "}").encode("utf-8")
    assert client.post("/aux/v1/hash-stamp", content=oversize).status_code == 400


def test_verify_hash_stamp_fails_closed_on_tampering():
    key = SoftwareKey.from_seed_hex(GOLDEN_SEED)
    stamp = StampLedger(key, clock=lambda: 1_764_000_000_000).issue(_valid_req())

    tampered_sig = copy.deepcopy(stamp)
    tampered_sig["sig"] = ("0" if tampered_sig["sig"][0] != "0" else "1") + tampered_sig["sig"][1:]
    assert verify_hash_stamp(tampered_sig)["ok"] is False

    tampered_id = copy.deepcopy(stamp)
    tampered_id["stamp_id"] = "0" * 64
    assert verify_hash_stamp(tampered_id)["ok"] is False

    tampered_pub = copy.deepcopy(stamp)
    other = SoftwareKey.from_seed_hex("99" * 32)
    tampered_pub["public_key"] = other.pubkey_hex
    assert verify_hash_stamp(tampered_pub)["ok"] is False

    tampered_claims = copy.deepcopy(stamp)
    tampered_claims["unsigned_payload"]["claims"] = "forged"
    assert verify_hash_stamp(tampered_claims)["ok"] is False

    wrong_expect = verify_hash_stamp(stamp, expected_digests={"hand_timeline_head": "ff" * 32})
    assert wrong_expect["ok"] is False

    unpinned = verify_hash_stamp(stamp, trusted_key_ids={"0" * 16})
    assert unpinned["ok"] is True
    assert unpinned["key_trusted"] is False
