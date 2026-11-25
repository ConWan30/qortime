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
import os
import sqlite3
import threading
import time
from pathlib import Path
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


def _canonical_entry_hash(ledger_seq: int, prev_hash: str, stamp_id: str) -> str:
    ledger_entry = {
        "ledger_seq": ledger_seq,
        "prev": prev_hash,
        "stamp_id": stamp_id,
    }
    return hashlib.sha256(frame(DOMAIN_LEDGER, canonicalize(ledger_entry))).hexdigest()


def _verify_chain_records(records: list[dict[str, Any]]) -> tuple[int, str, list[tuple[dict[str, Any], bytes, str]]]:
    """Verify full tamper-evident chain from seq 0 / GENESIS. Raises HashStampError on any break."""
    prev = GENESIS
    verified: list[tuple[dict[str, Any], bytes, str]] = []
    for expected_seq, rec in enumerate(records):
        seq = rec.get("ledger_seq")
        rec_prev = rec.get("prev_ledger_sha256")
        rec_hash = rec.get("entry_ledger_sha256")
        stamp = rec.get("stamp")
        if type(seq) is not int or seq != expected_seq:
            raise HashStampError(500, "ledger_corrupted", f"expected ledger_seq {expected_seq}, got {seq!r}")
        if not is_hex64(rec_prev) or rec_prev != prev:
            raise HashStampError(500, "ledger_corrupted", f"broken prev_ledger_sha256 at seq {expected_seq}")
        checked = verify_hash_stamp(stamp)
        if not checked.get("ok"):
            raise HashStampError(500, "ledger_corrupted", f"invalid stamp at seq {expected_seq}: {checked.get('reason')}")
        u = stamp["unsigned_payload"]
        if u["ledger_seq"] != expected_seq or u["prev_ledger_sha256"] != prev:
            raise HashStampError(500, "ledger_corrupted", f"payload ledger binding mismatch at seq {expected_seq}")
        req_canon = canonicalize({"client_nonce": u["client_nonce"], "digests": u["digests"]})
        stored_req_canon = rec.get("req_canon")
        if stored_req_canon is not None and bytes(stored_req_canon) != req_canon:
            raise HashStampError(500, "ledger_corrupted", f"request canonical bytes mismatch at seq {expected_seq}")
        expected_hash = _canonical_entry_hash(expected_seq, prev, stamp["stamp_id"])
        if not is_hex64(rec_hash) or rec_hash != expected_hash:
            raise HashStampError(500, "ledger_corrupted", f"ledger entry hash mismatch at seq {expected_seq}")
        verified.append((copy.deepcopy(stamp), req_canon, expected_hash))
        prev = expected_hash
    return len(verified), prev, verified


class StampLedger:
    """Append-only hash-stamp ledger keyed by Ed25519 ``KeyProvider``.

    Supports optional durable SQLite (WAL + FULL sync) and/or append-only JSONL
    persistence. When neither ``db_path`` nor ``jsonl_path`` is set, behaves as
    an in-memory ledger. On startup and on ``verify_chain()``, all persisted
    entries and hash-chain links from ``GENESIS`` are re-verified fail-closed.
    """

    def __init__(
        self,
        key: KeyProvider,
        clock: Callable[[], int] | None = None,
        *,
        max_entries: int = 100_000,
        db_path: str | Path | None = None,
        jsonl_path: str | Path | None = None,
    ) -> None:
        self._key = key
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_ledger_sha256 = GENESIS
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_nonce: dict[str, tuple[bytes, dict[str, Any]]] = {}

        self._db_target: str | None = str(db_path) if db_path is not None else None
        self._jsonl_path: Path | None = Path(jsonl_path) if jsonl_path is not None else None
        self._conn: sqlite3.Connection | None = None

        if self._db_target is not None:
            if self._db_target != ":memory:":
                Path(self._db_target).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_target, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA busy_timeout=5000")
            if self._db_target != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hash_stamps (
                    ledger_seq INTEGER PRIMARY KEY,
                    stamp_id TEXT NOT NULL UNIQUE,
                    client_nonce TEXT NOT NULL UNIQUE,
                    req_canon BLOB NOT NULL,
                    prev_ledger_sha256 TEXT NOT NULL,
                    entry_ledger_sha256 TEXT NOT NULL,
                    stamp_json TEXT NOT NULL
                )
                """
            )
        if self._jsonl_path is not None:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        self._load_and_verify()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _read_sqlite_records(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT ledger_seq, stamp_id, client_nonce, req_canon,
                   prev_ledger_sha256, entry_ledger_sha256, stamp_json
            FROM hash_stamps
            ORDER BY ledger_seq ASC
            """
        ).fetchall()
        records: list[dict[str, Any]] = []
        for seq, sid, nonce, req_canon, prev_h, entry_h, stamp_json in rows:
            try:
                stamp = json.loads(stamp_json)
            except (TypeError, ValueError) as exc:
                raise HashStampError(500, "ledger_corrupted", f"invalid stamp_json in SQLite seq {seq}: {exc}") from None
            if not isinstance(stamp, dict) or stamp.get("stamp_id") != sid:
                raise HashStampError(500, "ledger_corrupted", f"SQLite stamp_id mismatch at seq {seq}")
            if not isinstance(stamp.get("unsigned_payload"), dict) or stamp["unsigned_payload"].get("client_nonce") != nonce:
                raise HashStampError(500, "ledger_corrupted", f"SQLite client_nonce mismatch at seq {seq}")
            records.append(
                {
                    "ledger_seq": seq,
                    "prev_ledger_sha256": prev_h,
                    "entry_ledger_sha256": entry_h,
                    "req_canon": req_canon,
                    "stamp": stamp,
                }
            )
        return records

    def _read_jsonl_records(self) -> list[dict[str, Any]]:
        if self._jsonl_path is None or not self._jsonl_path.is_file():
            return []
        raw = self._jsonl_path.read_bytes()
        if not raw:
            return []
        records: list[dict[str, Any]] = []
        for idx, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                raise HashStampError(500, "ledger_corrupted", f"blank line in JSONL ledger at line {idx}")
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise HashStampError(500, "ledger_corrupted", f"corrupt JSONL ledger line {idx}: {exc}") from None
            if not isinstance(obj, dict):
                raise HashStampError(500, "ledger_corrupted", f"non-object JSONL ledger line {idx}")
            records.append(obj)
        return records

    def _verify_stores_unlocked(self) -> tuple[int, str, list[tuple[dict[str, Any], bytes, str]]]:
        sql_res: tuple[int, str, list[tuple[dict[str, Any], bytes, str]]] | None = None
        jsonl_res: tuple[int, str, list[tuple[dict[str, Any], bytes, str]]] | None = None
        if self._conn is not None:
            sql_res = _verify_chain_records(self._read_sqlite_records())
        if self._jsonl_path is not None:
            jsonl_res = _verify_chain_records(self._read_jsonl_records())

        if sql_res is not None and jsonl_res is not None:
            if sql_res[0] != jsonl_res[0] or sql_res[1] != jsonl_res[1]:
                raise HashStampError(500, "ledger_corrupted", "SQLite and JSONL hash-stamp ledgers diverged")
            for idx, ((s_sql, _, h_sql), (s_jl, _, h_jl)) in enumerate(zip(sql_res[2], jsonl_res[2])):
                if h_sql != h_jl or s_sql["stamp_id"] != s_jl["stamp_id"]:
                    raise HashStampError(500, "ledger_corrupted", f"SQLite vs JSONL entry mismatch at seq {idx}")
            return sql_res
        if sql_res is not None:
            return sql_res
        if jsonl_res is not None:
            return jsonl_res
        return 0, GENESIS, []

    def _load_and_verify(self) -> None:
        with self._lock:
            seq, prev_hash, verified = self._verify_stores_unlocked()
            self._seq = seq
            self._prev_ledger_sha256 = prev_hash
            self._by_id.clear()
            self._by_nonce.clear()
            for stamp, req_canon, _entry_hash in verified:
                sid = stamp["stamp_id"]
                nonce = stamp["unsigned_payload"]["client_nonce"]
                self._by_id[sid] = stamp
                self._by_nonce[nonce] = (req_canon, stamp)

    def verify_chain(self) -> dict[str, Any]:
        """Re-read persisted stores from disk and verify hash chain from GENESIS."""
        with self._lock:
            if self._conn is None and self._jsonl_path is None:
                return {
                    "ok": True,
                    "reason": None,
                    "entries": self._seq,
                    "head_ledger_sha256": self._prev_ledger_sha256,
                }
            try:
                count, head, _ = self._verify_stores_unlocked()
            except HashStampError as exc:
                return {
                    "ok": False,
                    "reason": exc.detail,
                    "entries": None,
                    "head_ledger_sha256": None,
                }
            return {
                "ok": True,
                "reason": None,
                "entries": count,
                "head_ledger_sha256": head,
            }

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

            prev_hash = self._prev_ledger_sha256
            next_prev = _canonical_entry_hash(self._seq, prev_hash, stamp_id)
            stamp_json = canonicalize(stamp).decode("utf-8")

            in_sql_tx = False
            try:
                if self._conn is not None:
                    self._conn.execute("BEGIN IMMEDIATE")
                    in_sql_tx = True
                    self._conn.execute(
                        """
                        INSERT INTO hash_stamps (
                            ledger_seq, stamp_id, client_nonce, req_canon,
                            prev_ledger_sha256, entry_ledger_sha256, stamp_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._seq,
                            stamp_id,
                            clean["client_nonce"],
                            sqlite3.Binary(req_canon),
                            prev_hash,
                            next_prev,
                            stamp_json,
                        ),
                    )
                if self._jsonl_path is not None:
                    record = {
                        "entry_ledger_sha256": next_prev,
                        "ledger_seq": self._seq,
                        "prev_ledger_sha256": prev_hash,
                        "stamp": stamp,
                    }
                    line = canonicalize(record) + b"\n"
                    with open(self._jsonl_path, "ab") as fh:
                        fh.write(line)
                        fh.flush()
                        os.fsync(fh.fileno())
                if in_sql_tx and self._conn is not None:
                    self._conn.execute("COMMIT")
                    in_sql_tx = False
            except Exception as exc:
                if in_sql_tx and self._conn is not None:
                    self._conn.execute("ROLLBACK")
                if isinstance(exc, HashStampError):
                    raise
                raise HashStampError(500, "ledger_write_failed", f"failed to persist hash stamp: {exc}") from None

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
