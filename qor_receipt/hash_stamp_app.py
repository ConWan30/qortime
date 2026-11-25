"""Isolated FastAPI ASGI app exposing ONLY the QorTime auxiliary hash-stamp endpoints.

Designed for Cloud Run / container deployment of ``/aux/v1/hash-stamp`` with:
* Fail-closed signing-key loading from a mounted secret file or env var
  (never generated ephemerally at runtime, never logged on failure).
* Fail-closed append-only ledger persistence and startup ``verify_chain()``.
* Strict request-size ceiling before buffering request bodies (HTTP 413).
* Zero referee match endpoints and zero API docs schema routes exposed.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request

from qor_receipt.hash_stamp import (
    GENESIS,
    MAX_BODY_BYTES,
    SERVICE_ID,
    HashStampError,
    StampLedger,
    is_hex64,
)
from qor_receipt.keys import SoftwareKey


def load_signing_key_from_env() -> SoftwareKey:
    """Load the 32-byte Ed25519 hex seed from Secret Manager file mount or env var.

    Precedence:
      1. ``QOR_HASH_STAMP_SEED_FILE`` (recommended Secret Manager volume mount path)
      2. ``QOR_HASH_STAMP_SEED`` (Secret Manager env var)

    Fails closed without logging or echoing any secret material.
    """
    seed_file = os.getenv("QOR_HASH_STAMP_SEED_FILE", "").strip()
    seed_raw = ""
    if seed_file:
        try:
            seed_raw = Path(seed_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"failed reading QOR_HASH_STAMP_SEED_FILE: {exc}") from None
    else:
        seed_raw = os.getenv("QOR_HASH_STAMP_SEED", "").strip()

    if not seed_raw:
        raise RuntimeError(
            "hash-stamp signing seed not configured; set QOR_HASH_STAMP_SEED_FILE or QOR_HASH_STAMP_SEED"
        )

    seed_hex = seed_raw.lower()
    if not is_hex64(seed_hex):
        raise RuntimeError("configured hash-stamp signing seed must be 64 hex characters")
    return SoftwareKey.from_seed_hex(seed_hex)


def create_hash_stamp_app(
    key: SoftwareKey | None = None,
    clock: Callable[[], int] | None = None,
    *,
    stamp_jsonl_path: str | Path | None = None,
    stamp_db_path: str | Path | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> FastAPI:
    """Build the isolated hash-stamp FastAPI app and run ``verify_chain()`` at startup."""
    resolved_key = key or load_signing_key_from_env()
    now = clock or (lambda: int(time.time() * 1000))
    jsonl_target = stamp_jsonl_path if stamp_jsonl_path is not None else (os.getenv("QOR_HASH_STAMP_JSONL") or None)
    db_target = stamp_db_path if stamp_db_path is not None else (os.getenv("QOR_HASH_STAMP_DB") or None)

    stamps = StampLedger(resolved_key, clock=now, db_path=db_target, jsonl_path=jsonl_target)
    startup_check = stamps.verify_chain()
    if not startup_check.get("ok"):
        stamps.close()
        raise RuntimeError(f"hash-stamp ledger verify_chain failed at startup: {startup_check.get('reason')}")

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            yield
        finally:
            stamps.close()

    app = FastAPI(
        title="QorTime hash-stamp service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.stamp_ledger = stamps

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        chk = stamps.verify_chain()
        if not chk.get("ok"):
            raise HTTPException(
                503,
                {
                    "ok": False,
                    "service": SERVICE_ID,
                    "chain_ok": False,
                    "reason": chk.get("reason"),
                },
            )
        return {
            "ok": True,
            "service": SERVICE_ID,
            "key_id": resolved_key.key_id,
            "entries": chk.get("entries", 0),
            "head_ledger_sha256": chk.get("head_ledger_sha256") or GENESIS,
            "chain_ok": True,
        }

    @app.get("/aux/v1/hash-stamp/key")
    def get_hash_stamp_key() -> dict[str, str]:
        return stamps.key_info()

    @app.post("/aux/v1/hash-stamp")
    async def post_hash_stamp(request: Request) -> dict[str, Any]:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                raise HTTPException(400, "invalid Content-Length header") from None
            if declared < 0:
                raise HTTPException(400, "invalid Content-Length header")
            if declared > max_body_bytes:
                raise HTTPException(413, f"request body exceeds {max_body_bytes} bytes")

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_body_bytes:
                raise HTTPException(413, f"request body exceeds {max_body_bytes} bytes")
            chunks.append(chunk)

        try:
            return stamps.issue_raw(b"".join(chunks))
        except HashStampError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None

    @app.get("/aux/v1/hash-stamp/{stamp_id}")
    def get_hash_stamp(stamp_id: str) -> dict[str, Any]:
        found = stamps.get(stamp_id)
        if found is None:
            raise HTTPException(404, "no such hash stamp")
        return found

    return app


app = create_hash_stamp_app() if os.getenv("QOR_HASH_STAMP_AUTOSTART", "0") == "1" else None
