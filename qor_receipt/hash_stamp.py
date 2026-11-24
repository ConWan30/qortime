"""Auxiliary hash-only timestamp ledger and verifier (``qortime.aux.hash_stamp.v1``).

Records when a QorTime service observed a set of SHA-256 session digests
(``hand_timeline_head``, ``hand_timeline_sha256``, optional ``frame_chain_head``,
``receipt_sha256``, and ``chunks`` count). Zero video bytes, raw binaries, or local
paths cross this boundary. An external hash stamp is not a referee match ruling,
never awards gold, and does not establish capture validity or who held the
controller.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from typing import Any, Callable

from qor_receipt.jcs import MAX_SAFE, canonicalize
from qor_receipt.keys import KeyProvider, kid, verify

SCHEMA_REQUEST = "qortime.aux.hash_stamp_request.v1"
SCHEMA_STAMP = "qortime.aux.hash_stamp.v1"
DOMAIN_STAMP = "QORTIME-AUX-HASH-STAMP-v1"
DOMAIN_STAMP_ID = "QORTIME-AUX-STAMP-ID-v1"
DOMAIN_LEDGER = "QORTIME-AUX-STAMP-LEDGER-v1"
SERVICE_ID = "qortime-aux-hash-stamp"
CLAIMS = (
    "Auxiliary hash-only timestamp: records when this QorTime service observed "
    "these SHA-256 digests. Not a referee match ruling, no gold, and does not "
    "establish capture validity or who held the controller."
)

MAX_BODY_BYTES = 16 * 1024
MAX_CHUNKS = 10_000_000
GENESIS = "0" * 64

_REQ_KEYS = frozenset({"schema", "client_nonce", "digests"})
_REQ_HEX_DIGEST_KEYS = ("hand_timeline_head", "hand_timeline_sha256")
_OPT_HEX_DIGEST_KEYS = ("frame_chain_head", "receipt_sha256")
_ALLOWED_DIGEST_KEYS = frozenset((*_REQ_HEX_DIGEST_KEYS, *_OPT_HEX_DIGEST_KEYS, "chunks"))
_STAMP_TOP_KEYS = frozenset({"public_key", "schema", "sig", "stamp_id", "unsigned_payload"})
_UNSIGNED_KEYS = frozenset(
    {
        "alg",
        "claims",
        "client_nonce",
        "digests",
        "issued_wall_ms",
        "key_id",
        "ledger_seq",
        "prev_ledger_sha256",
        "schema",
        "service",
    }
)


class HashStampError(ValueError):
    """Fail-closed validation or conflict error with an HTTP status code."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def is_hex(s: Any, length: int) -> bool:
    return isinstance(s, str) and len(s) == length and all(c in "0123456789abcdef" for c in s)


def is_hex16(s: Any) -> bool:
    return is_hex(s, 16)


def is_hex64(s: Any) -> bool:
    return is_hex(s, 64)


def is_hex128(s: Any) -> bool:
    return is_hex(s, 128)


def frame(label: str, data: bytes) -> bytes:
    """Length-prefixed domain framing: ``u64_be(len(label)) || label || u64_be(len(data)) || data``."""
    lb = label.encode("utf-8")
    return len(lb).to_bytes(8, "big") + lb + len(data).to_bytes(8, "big") + data


def framed_sha256_hex(label: str, obj: Any) -> str:
    return hashlib.sha256(frame(label, canonicalize(obj))).hexdigest()


def _reject_json_constant(val: str) -> Any:
    raise HashStampError(400, "invalid_json_constant", f"non-standard JSON constant not allowed: {val}")


def parse_json_body(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BODY_BYTES:
        raise HashStampError(400, "oversized", f"request body exceeds {MAX_BODY_BYTES} bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HashStampError(400, "bad_utf8", f"request body must be UTF-8: {exc}") from None
    try:
        parsed = json.loads(text, parse_constant=_reject_json_constant)
    except HashStampError:
        raise
    except ValueError as exc:
        raise HashStampError(400, "bad_json", f"malformed JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise HashStampError(400, "not_object", "request JSON must be an object")
    return parsed


def _validate_digests(digests: Any) -> dict[str, Any]:
    if not isinstance(digests, dict):
        raise HashStampError(400, "bad_digests", "digests must be an object")
    extra = set(digests.keys()) - _ALLOWED_DIGEST_KEYS
    if extra:
        raise HashStampError(400, "unknown_digest_field", f"unsupported digests field(s): {sorted(extra)}")
    out: dict[str, Any] = {}
    for k in _REQ_HEX_DIGEST_KEYS:
        val = digests.get(k)
        if not is_hex64(val):
            raise HashStampError(400, "bad_digest", f"digests.{k} must be 64-char lowercase hex")
        out[k] = val
    for k in _OPT_HEX_DIGEST_KEYS:
        if k in digests:
            val = digests[k]
            if not is_hex64(val):
                raise HashStampError(400, "bad_digest", f"digests.{k} must be 64-char lowercase hex when present")
            out[k] = val
    if "chunks" in digests:
        c = digests["chunks"]
        if type(c) is not int or c < 0 or c > MAX_CHUNKS:
            raise HashStampError(400, "bad_chunks", f"digests.chunks must be an int in 0..{MAX_CHUNKS}")
        out["chunks"] = c
    return out


def validate_request_dict(req: Any) -> dict[str, Any]:
    if not isinstance(req, dict):
        raise HashStampError(400, "not_object", "request must be an object")
    if set(req.keys()) != _REQ_KEYS:
        extra = sorted(set(req.keys()) - _REQ_KEYS)
        missing = sorted(_REQ_KEYS - set(req.keys()))
        raise HashStampError(400, "bad_request_keys", f"invalid request keys (missing={missing}, extra={extra})")
    if req.get("schema") != SCHEMA_REQUEST:
        raise HashStampError(400, "wrong_schema", f"schema must be {SCHEMA_REQUEST}")
    nonce = req.get("client_nonce")
    if not is_hex64(nonce):
        raise HashStampError(400, "bad_nonce", "client_nonce must be 64-char lowercase hex")
    return {
        "schema": SCHEMA_REQUEST,
        "client_nonce": nonce,
        "digests": _validate_digests(req.get("digests")),
    }


class StampLedger:
    """In-memory append-only hash-stamp ledger keyed by Ed25519 ``KeyProvider``."""

    def __init__(
        self,
        key: KeyProvider,
        clock: Callable[[], int] | None = None,
        *,
        max_entries: int = 100_000,
    ) -> None:
        self._key = key
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_ledger_sha256 = GENESIS
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_nonce: dict[str, tuple[bytes, dict[str, Any]]] = {}

    def key_info(self) -> dict[str, str]:
        return {
            "service": SERVICE_ID,
            "alg": "Ed25519",
            "key_id": self._key.key_id,
            "public_key": self._key.pubkey_hex,
            "claims": CLAIMS,
        }

    def issue_raw(self, raw: bytes) -> dict[str, Any]:
        return self.issue(parse_json_body(raw))

    def issue(self, req: Any) -> dict[str, Any]:
        clean = validate_request_dict(req)
        req_canon = canonicalize({"client_nonce": clean["client_nonce"], "digests": clean["digests"]})

        with self._lock:
            existing = self._by_nonce.get(clean["client_nonce"])
            if existing is not None:
                prev_canon, prev_stamp = existing
                if prev_canon != req_canon:
                    raise HashStampError(
                        409,
                        "nonce_conflict",
                        "client_nonce already stamped with different digests",
                    )
                return copy.deepcopy(prev_stamp)

            if self._seq >= self._max_entries:
                raise HashStampError(503, "ledger_full", "hash-stamp ledger capacity reached")

            wall_ms = int(self._clock())
            if wall_ms < 0 or wall_ms > MAX_SAFE:
                raise HashStampError(500, "bad_clock", "server wall clock outside safe range")

            unsigned = {
                "alg": "Ed25519",
                "claims": CLAIMS,
                "client_nonce": clean["client_nonce"],
                "digests": clean["digests"],
                "issued_wall_ms": wall_ms,
                "key_id": self._key.key_id,
                "ledger_seq": self._seq,
                "prev_ledger_sha256": self._prev_ledger_sha256,
                "schema": SCHEMA_STAMP,
                "service": SERVICE_ID,
            }
            u_bytes = canonicalize(unsigned)
            stamp_id = hashlib.sha256(frame(DOMAIN_STAMP_ID, u_bytes)).hexdigest()
            sig = self._key.sign(frame(DOMAIN_STAMP, u_bytes))
            stamp = {
                "public_key": self._key.pubkey_hex,
                "schema": SCHEMA_STAMP,
                "sig": sig,
                "stamp_id": stamp_id,
                "unsigned_payload": unsigned,
            }

            ledger_entry = {
                "ledger_seq": self._seq,
                "prev": self._prev_ledger_sha256,
                "stamp_id": stamp_id,
            }
            next_prev = hashlib.sha256(frame(DOMAIN_LEDGER, canonicalize(ledger_entry))).hexdigest()
            self._seq += 1
            self._prev_ledger_sha256 = next_prev
            stored = copy.deepcopy(stamp)
            self._by_id[stamp_id] = stored
            self._by_nonce[clean["client_nonce"]] = (req_canon, stored)
            return copy.deepcopy(stamp)

    def get(self, stamp_id: str) -> dict[str, Any] | None:
        if not is_hex64(stamp_id):
            return None
        with self._lock:
            found = self._by_id.get(stamp_id)
            return copy.deepcopy(found) if found is not None else None


def verify_hash_stamp(
    stamp: Any,
    *,
    trusted_pubkeys: set[str] | None = None,
    trusted_key_ids: set[str] | None = None,
    expected_digests: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail-closed verification of a ``qortime.aux.hash_stamp.v1`` document."""
    def _fail(reason: str) -> dict[str, Any]:
        return {
            "ok": False,
            "reason": reason,
            "stamp_id": None,
            "key_id": None,
            "public_key": None,
            "key_trusted": None,
            "digests": None,
        }

    if not isinstance(stamp, dict):
        return _fail("stamp must be an object")
    if set(stamp.keys()) != _STAMP_TOP_KEYS:
        return _fail("invalid top-level stamp keys")
    if stamp.get("schema") != SCHEMA_STAMP:
        return _fail(f"wrong schema: expected {SCHEMA_STAMP}")

    pub = stamp.get("public_key")
    sig = stamp.get("sig")
    stamp_id = stamp.get("stamp_id")
    u = stamp.get("unsigned_payload")
    if not is_hex64(pub):
        return _fail("public_key must be 64-char lowercase hex")
    if not is_hex128(sig):
        return _fail("sig must be 128-char lowercase hex")
    if not is_hex64(stamp_id):
        return _fail("stamp_id must be 64-char lowercase hex")
    if not isinstance(u, dict) or set(u.keys()) != _UNSIGNED_KEYS:
        return _fail("invalid unsigned_payload keys")

    if u.get("schema") != SCHEMA_STAMP or u.get("alg") != "Ed25519" or u.get("service") != SERVICE_ID:
        return _fail("unsupported stamp payload metadata")
    if u.get("claims") != CLAIMS:
        return _fail("unexpected claims text")
    if not is_hex64(u.get("client_nonce")) or not is_hex16(u.get("key_id")) or not is_hex64(u.get("prev_ledger_sha256")):
        return _fail("invalid hex identifier in unsigned_payload")

    wall_ms = u.get("issued_wall_ms")
    seq = u.get("ledger_seq")
    if type(wall_ms) is not int or wall_ms < 0 or wall_ms > MAX_SAFE:
        return _fail("invalid issued_wall_ms")
    if type(seq) is not int or seq < 0 or seq > MAX_SAFE:
        return _fail("invalid ledger_seq")

    if kid(pub) != u["key_id"]:
        return _fail("key_id does not match public_key")

    try:
        digests = _validate_digests(u.get("digests"))
        u_bytes = canonicalize(u)
    except (HashStampError, ValueError) as exc:
        return _fail(f"invalid payload: {exc}")

    recomputed_id = hashlib.sha256(frame(DOMAIN_STAMP_ID, u_bytes)).hexdigest()
    if recomputed_id != stamp_id:
        return _fail("stamp_id mismatch")

    if not verify(pub, sig, frame(DOMAIN_STAMP, u_bytes)):
        return _fail("invalid Ed25519 stamp signature")

    if expected_digests is not None:
        if not isinstance(expected_digests, dict):
            return _fail("expected_digests must be an object")
        for k, want in expected_digests.items():
            if k not in digests or type(digests[k]) is not type(want) or digests[k] != want:
                return _fail(f"digest binding mismatch for {k}")

    trusted: bool | None = None
    if trusted_pubkeys is not None or trusted_key_ids is not None:
        trusted = (pub in (trusted_pubkeys or set())) or (u["key_id"] in (trusted_key_ids or set()))

    return {
        "ok": True,
        "reason": None,
        "stamp_id": stamp_id,
        "key_id": u["key_id"],
        "public_key": pub,
        "key_trusted": trusted,
        "issued_wall_ms": wall_ms,
        "ledger_seq": seq,
        "digests": digests,
    }
