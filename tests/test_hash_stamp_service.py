"""Tests for isolated QorTime hash-stamp ASGI service and Cloud Run configs (T-0049 / T-0050 Part A2)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qor_receipt.hash_stamp import MAX_BODY_BYTES, SCHEMA_REQUEST, SCHEMA_STAMP, SERVICE_ID, StampLedger, verify_hash_stamp
from qor_receipt.hash_stamp_app import create_hash_stamp_app, load_signing_key_from_env
from qor_receipt.keys import SoftwareKey

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_SEED = "11" * 32
GOLDEN_NONCE = "aa" * 32
GOLDEN_DIGESTS = {
    "chunks": 6,
    "frame_chain_head": "44" * 32,
    "hand_timeline_head": "22" * 32,
    "hand_timeline_sha256": "33" * 32,
    "receipt_sha256": "55" * 32,
}


def _valid_req(*, nonce: str = GOLDEN_NONCE, digests: dict | None = None) -> dict:
    return {
        "schema": SCHEMA_REQUEST,
        "client_nonce": nonce,
        "digests": copy.deepcopy(digests if digests is not None else GOLDEN_DIGESTS),
    }


def test_signing_key_loading_fail_closed_and_no_secret_echo(monkeypatch, tmp_path):
    monkeypatch.delenv("QOR_HASH_STAMP_SEED_FILE", raising=False)
    monkeypatch.delenv("QOR_HASH_STAMP_SEED", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        load_signing_key_from_env()

    bad_secret = "not-a-hex-seed-SUPERSECRET-TOKEN"
    monkeypatch.setenv("QOR_HASH_STAMP_SEED", bad_secret)
    with pytest.raises(RuntimeError, match="64 hex") as exc_info:
        load_signing_key_from_env()
    assert bad_secret not in str(exc_info.value)

    seed_file = tmp_path / "seed"
    seed_file.write_text(f"{GOLDEN_SEED}\n", encoding="utf-8")
    monkeypatch.setenv("QOR_HASH_STAMP_SEED_FILE", str(seed_file))
    key = load_signing_key_from_env()
    assert key.key_id == SoftwareKey.from_seed_hex(GOLDEN_SEED).key_id


def test_isolated_routes_only_and_request_size_limit(tmp_path):
    jsonl = tmp_path / "hash_stamps.jsonl"
    app = create_hash_stamp_app(
        key=SoftwareKey.from_seed_hex(GOLDEN_SEED),
        clock=lambda: 1_764_000_000_000,
        stamp_jsonl_path=jsonl,
    )
    with TestClient(app) as client:
        h = client.get("/healthz")
        assert h.status_code == 200
        assert h.json()["ok"] is True
        assert h.json()["ready"] is True
        assert h.json()["chain_ok"] is True
        assert h.json()["service"] == SERVICE_ID

        k = client.get("/aux/v1/hash-stamp/key")
        assert k.status_code == 200
        assert k.json()["service"] == SERVICE_ID

        for hidden in ("/referee", "/matches", "/docs", "/redoc", "/openapi.json"):
            assert client.get(hidden).status_code == 404

        oversized = b'{"schema":"qortime.aux.hash_stamp_request.v1","pad":"' + (b"a" * MAX_BODY_BYTES) + b'"}'
        r_big = client.post("/aux/v1/hash-stamp", content=oversized, headers={"Content-Type": "application/json"})
        assert r_big.status_code == 413

        # Streaming chunked upload without Content-Length is also capped fail-closed at MAX_BODY_BYTES.
        def _chunk_stream():
            yield b'{"pad":"'
            yield b"b" * (MAX_BODY_BYTES + 64)
            yield b'"}'

        r_stream_big = client.post(
            "/aux/v1/hash-stamp",
            content=_chunk_stream(),
            headers={"Content-Type": "application/json"},
        )
        assert r_stream_big.status_code == 413


def test_healthz_does_no_chain_read_or_verify_once_started(monkeypatch, tmp_path):
    jsonl = tmp_path / "hash_stamps.jsonl"
    key = SoftwareKey.from_seed_hex(GOLDEN_SEED)
    verify_calls = 0
    orig_verify = StampLedger.verify_chain

    def _counting_verify(self):
        nonlocal verify_calls
        verify_calls += 1
        return orig_verify(self)

    monkeypatch.setattr(StampLedger, "verify_chain", _counting_verify)
    app = create_hash_stamp_app(key=key, clock=lambda: 1_764_000_000_000, stamp_jsonl_path=jsonl)
    # Neither startup nor /healthz calls StampLedger.verify_chain(); startup verifies exactly once inside __init__.
    assert verify_calls == 0

    with TestClient(app) as client:
        for _ in range(5):
            r = client.get("/healthz")
            assert r.status_code == 200
            assert r.json()["ready"] is True
            assert r.json()["chain_ok"] is True
        assert verify_calls == 0


def test_jsonl_persistence_restart_and_bad_startup_chain_fail_closed(tmp_path):
    jsonl = tmp_path / "hash_stamps.jsonl"
    key = SoftwareKey.from_seed_hex(GOLDEN_SEED)
    now_ms = [1_764_000_000_000]

    with TestClient(create_hash_stamp_app(key=key, clock=lambda: now_ms[0], stamp_jsonl_path=jsonl)) as c1:
        r1 = c1.post("/aux/v1/hash-stamp", json=_valid_req())
        assert r1.status_code == 200
        s1 = r1.json()
        assert s1["schema"] == SCHEMA_STAMP
        assert s1["unsigned_payload"]["ledger_seq"] == 0

    # Simulate container scale-from-zero cold restart against the same append-only JSONL ledger.
    now_ms[0] += 10_000
    with TestClient(create_hash_stamp_app(key=key, clock=lambda: now_ms[0], stamp_jsonl_path=jsonl)) as c2:
        h = c2.get("/healthz")
        assert h.status_code == 200 and h.json()["ready"] is True and h.json()["chain_ok"] is True
        fetched = c2.get(f"/aux/v1/hash-stamp/{s1['stamp_id']}")
        assert fetched.status_code == 200 and fetched.json() == s1
        v = verify_hash_stamp(fetched.json(), trusted_key_ids={key.key_id}, expected_digests=GOLDEN_DIGESTS)
        assert v["ok"] is True

        r2 = c2.post(
            "/aux/v1/hash-stamp",
            json=_valid_req(
                nonce="bb" * 32,
                digests={"hand_timeline_head": "66" * 32, "hand_timeline_sha256": "77" * 32},
            ),
        )
        assert r2.status_code == 200
        assert r2.json()["unsigned_payload"]["ledger_seq"] == 1

    # Corrupting JSONL before startup -> app reports not-ready (503) and refuses POST/GET stamps fail-closed.
    raw = jsonl.read_text(encoding="utf-8")
    jsonl.write_text(raw.replace(s1["stamp_id"], "f" * 64, 1), encoding="utf-8")
    bad_app = create_hash_stamp_app(key=key, clock=lambda: now_ms[0], stamp_jsonl_path=jsonl)
    with TestClient(bad_app) as bad_client:
        hz = bad_client.get("/healthz")
        assert hz.status_code == 503
        assert hz.json()["detail"]["ready"] is False
        assert hz.json()["detail"]["chain_ok"] is False

        post_refused = bad_client.post(
            "/aux/v1/hash-stamp",
            json=_valid_req(
                nonce="cc" * 32,
                digests={"hand_timeline_head": "88" * 32, "hand_timeline_sha256": "99" * 32},
            ),
        )
        assert post_refused.status_code == 503
        assert bad_client.get(f"/aux/v1/hash-stamp/{s1['stamp_id']}").status_code == 503
        assert bad_client.get("/aux/v1/hash-stamp/key").status_code == 503


def test_cloud_run_deploy_configs_enforce_hardening_constraints():
    dockerfile = (REPO_ROOT / "services" / "hash-stamp" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "QOR_HASH_STAMP_AUTOSTART=1" in dockerfile
    assert "--workers 1" in dockerfile
    assert "qor_receipt.hash_stamp_app:app" in dockerfile

    cloudbuild = (REPO_ROOT / "services" / "hash-stamp" / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "services/hash-stamp/Dockerfile" in cloudbuild
    assert "qortime-hash-stamp" in cloudbuild

    svc = (REPO_ROOT / "services" / "hash-stamp" / "service.yaml").read_text(encoding="utf-8")
    assert 'autoscaling.knative.dev/minScale: "0"' in svc
    assert 'autoscaling.knative.dev/maxScale: "1"' in svc
    assert "run.googleapis.com/execution-environment: gen2" in svc
    assert "gcsfuse.run.googleapis.com" in svc
    assert "runAsNonRoot: true" in svc
    assert "secretName: qortime-hash-stamp-seed" in svc
    assert "QOR_HASH_STAMP_JSONL" in svc
    assert "QOR_HASH_STAMP_DB" not in svc
    assert "timeoutSeconds: 5" in svc
    assert "periodSeconds: 10" in svc
    assert "failureThreshold: 6" in svc
    assert "--no-allow-unauthenticated" in svc
    assert "allUsers" not in svc or "NEVER grant roles/run.invoker to allUsers" in svc
    assert "/healthz" in svc
