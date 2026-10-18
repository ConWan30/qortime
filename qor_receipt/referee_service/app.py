"""QorTime official referee service: verified uploads for QorCap bundles.

Run (production, Railway): ``uvicorn qor_receipt.referee_service.app:app --host 0.0.0.0 --port $PORT``

Env:
  QORTIME_SERVICE_SEED      hex Ed25519 seed of the ONLINE service key (secret; never logged or returned)
  QORTIME_DELEGATION        root-signed qortime-referee-delegation/v1 cert for that key (JSON, or base64 of it)
  QORTIME_REFEREE_SEED      legacy/dev only: sign directly with an allowlisted key (the offline root must NOT be here)
  QORTIME_REFEREE_ID        id reported for the key (default: the cert's delegate_id, else qortime-v1)
  QORTIME_STATE_DIR         persistent dir (Railway volume) for the transparency log + known install keys
  QORTIME_LOG_PATH          transparency log file (default $QORTIME_STATE_DIR/translog.jsonl)
  QORCAP_SIDECAR            path to a pinned QorCap sidecar/ (end-of-game + scorebug readers); missing -> fail-closed
  QORTIME_REFEREE_WORKDIR   scratch dir for in-flight submissions (default /tmp/qortime-referee)
  QORTIME_SUBMIT_TOKEN      optional admin override on POST /v1/submissions (Bearer) that skips install-key auth
  QORTIME_NONCE_SECRET      optional HMAC secret for submit nonces (default: random per process)
  QORTIME_POW_BITS_* / QORTIME_RATE_* / QORTIME_REQUIRE_RECORDER_MATCH   see submit_auth.py
  QORTIME_RANDOM_SEGMENTS / QORTIME_TAIL_MS   spot-check policy
  QORTIME_MAX_ACTIVE        max concurrent in-flight submissions (default 4)

Flow (spot-check):
  POST /v1/submissions (zip of bundle meta files)  -> structural re-check, commit, then random plan
  PUT  /v1/submissions/{id}/segments/{seq}          -> raw fMP4 segment bytes, hashed against the committed manifest
  POST /v1/submissions/{id}/review                 -> referee decodes, re-reads final, re-runs challenges, signs
  POST /v1/submissions/{id}/player-signature       -> device key signs the same bytes; receipt complete
Live helpers (optional, during Start Match):
  POST /v1/challenges   signed nonce (+ optional chain-head anchor)     POST /v1/anchors   signed chain-head timestamp
Head-to-head challenge links (free, not for betting; qor_receipt/h2h.py; state in $QORTIME_STATE_DIR/h2h):
  POST /v1/h2h  GET /v1/h2h/{id}  POST /v1/h2h/{id}/accept  POST /v1/h2h/{id}/receipts (Bearer player token)
  GET /v1/h2h/{id}/receipts/{sha16}
  QORTIME_CORS_ORIGINS (default https://conwan30.github.io) / QORTIME_RATE_H2H_HOUR (default 300)
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from qor_receipt import delegation as D
from qor_receipt import h2h as H
from qor_receipt.keys import SoftwareKey
from qor_receipt.referee_service.submit_auth import AuthError, SubmitGuard, Window
from qor_receipt.review import bundle as B
from qor_receipt.review.engine import FULL, SPOT, Referee
from qor_receipt.review.policy import MAX_META_BYTES, RANDOM_SEGMENTS, TAIL_MS
from qor_receipt.translog import TransparencyLog
from qor_receipt.trusted_referees import load_allowlist, lookup

VERSION = "0.2.0"


class ChallengeReq(BaseModel):
    phase: str
    role: str = "home"
    match_id: str = ""
    anchor: dict[str, Any] | None = None


class AnchorReq(BaseModel):
    match_id: str = ""
    anchor: dict[str, Any]


class PlayerSig(BaseModel):
    sig: str


def _key_from_env() -> tuple[SoftwareKey | None, dict | None]:
    """(key, delegation). Prefers the delegated online key; the legacy direct-signing seed is for dev/tests."""
    name = "QORTIME_SERVICE_SEED" if os.getenv("QORTIME_SERVICE_SEED") else "QORTIME_REFEREE_SEED"
    seed = (os.getenv(name) or "").strip()
    if not seed:
        return None, None
    try:
        key = SoftwareKey.from_seed_hex(seed)
    except ValueError:
        raise SystemExit(f"{name} is not a valid hex Ed25519 seed") from None
    raw = (os.getenv("QORTIME_DELEGATION") or "").strip()
    if not raw:
        if name == "QORTIME_SERVICE_SEED":
            raise SystemExit("QORTIME_SERVICE_SEED needs QORTIME_DELEGATION (root-signed cert)")
        return key, None
    try:
        cert = json.loads(raw if raw.startswith("{") else base64.b64decode(raw + "=" * (-len(raw) % 4)))
    except ValueError:
        raise SystemExit("QORTIME_DELEGATION is not JSON / base64 JSON") from None
    return key, cert


def delegation_status(key, cert: dict | None, now_ms: int | None = None) -> dict:
    if cert is None:
        return {"mode": "direct", "valid": None}
    now = int(time.time() * 1000) if now_ms is None else now_ms
    info, probs = D.check_delegation(cert, key.pubkey_hex, now, allowlist=load_allowlist(),
                                     revocations=D.load_revocations(), now_ms=now)
    return {"mode": "delegated", "valid": info is not None, "problems": probs, "serial": cert.get("serial"),
            "root_id": cert.get("root_id"), "delegate_id": cert.get("delegate_id"),
            "not_after_ms": cert.get("not_after_ms"),
            "days_left": (int(cert.get("not_after_ms", 0)) - now) // 86_400_000}


def _state_dir() -> str:
    d = os.getenv("QORTIME_STATE_DIR")
    if d:
        return d
    return "/data" if os.path.isdir("/data") else os.getenv("QORTIME_REFEREE_WORKDIR", "/tmp/qortime-referee")


class H2HCreate(BaseModel):
    game: str = "madden27"
    creator: str
    opponent: str
    creator_psn: str | None = None
    opponent_psn: str | None = None
    rules: dict[str, Any] = {}


class H2HAccept(BaseModel):
    handle: str | None = None


def create_app(referee: Referee | None = None, guard: SubmitGuard | None = None, h2h: H.Store | None = None) -> FastAPI:
    app = FastAPI(title="QorTime official referee", version=VERSION)
    origins = [o.strip() for o in os.getenv("QORTIME_CORS_ORIGINS", "https://conwan30.github.io").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"], max_age=600)
    if referee is None:
        key, cert = _key_from_env()
        if key is None:
            raise SystemExit("QORTIME_SERVICE_SEED (+ QORTIME_DELEGATION) is not set")
        st = delegation_status(key, cert)
        if cert is not None and not st["valid"]:
            raise SystemExit(f"delegation cert invalid for this key: {st['problems']}")
        ent = lookup(key.pubkey_hex)
        if ent and ent.get("can_delegate"):
            print("WARNING: the service holds a ROOT (can_delegate) key; use a delegated online key", flush=True)
        state = _state_dir()
        log = TransparencyLog(os.getenv("QORTIME_LOG_PATH") or os.path.join(state, "translog.jsonl"), signer=key,
                              delegation=cert)
        referee = Referee(key, workdir=os.getenv("QORTIME_REFEREE_WORKDIR", "/tmp/qortime-referee"),
                          random_segments=int(os.getenv("QORTIME_RANDOM_SEGMENTS", RANDOM_SEGMENTS)),
                          tail_ms=int(os.getenv("QORTIME_TAIL_MS", TAIL_MS)),
                          referee_id=os.getenv("QORTIME_REFEREE_ID") or (cert or {}).get("delegate_id") or "qortime-v1",
                          delegation=cert, log=log)
        if guard is None:
            sec = os.getenv("QORTIME_NONCE_SECRET")
            guard = SubmitGuard(secret=sec.encode() if sec else None, state_dir=state)
    ref = referee
    guard = guard or SubmitGuard()
    if h2h is None:
        h2h = H.Store(os.path.join(_state_dir(), "h2h"))
    h2h_cap = Window()
    h2h_cap_hour = int(os.getenv("QORTIME_RATE_H2H_HOUR", "300"))
    live_cap = Window()
    live_cap_hour = int(os.getenv("QORTIME_RATE_LIVE_HOUR", "1200"))
    sub_pub: dict[str, str] = {}
    submit_token = os.getenv("QORTIME_SUBMIT_TOKEN") or ""
    max_active = int(os.getenv("QORTIME_MAX_ACTIVE", "4"))
    lock = threading.Lock()
    entry = lookup(ref.key.pubkey_hex)
    started = int(time.time())

    def sub_or_404(sid: str, token: str | None):
        s = ref.subs.get(sid)
        if s is None:
            raise HTTPException(404, "no such submission (expired after 1 h, or the service restarted)")
        bearer = (token or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(bearer, s.upload_token):
            raise HTTPException(401, "bad upload token")
        return s

    def deleg():
        return delegation_status(ref.key, ref.delegation)

    def live_ok():
        now = int(time.time() * 1000)
        with lock:
            if live_cap.count(now, 3_600_000) >= live_cap_hour:
                raise HTTPException(429, "referee live-token cap reached; try again later")
            live_cap.t.append(now)

    @app.get("/health")
    def health():
        d = deleg()
        return {"ok": True, "version": VERSION, "uptime_s": int(time.time()) - started,
                "allowlisted": bool(entry and entry.get("status") == "active") or bool(d.get("valid")),
                "delegation": d, "log_size": len(ref.log.entries) if ref.log is not None else None,
                "submit_auth": "install-key-v1"}

    @app.get("/v1/referee")
    def info():
        i = ref.info()
        i["allowlist"] = {"id": entry.get("id"), "status": entry.get("status")} if entry else None
        i["delegation_status"] = deleg()
        i["version"] = VERSION
        i["submit_auth"] = {"scheme": "install-key-v1", "challenge": "/v1/submit/challenge?pub=<hex>",
                            "header": "X-Qor-Submit", "min_client": "qorcap 0.3.10"}
        return i

    @app.get("/v1/submit/challenge")
    def submit_challenge(pub: str = ""):
        try:
            return guard.challenge(pub)
        except AuthError as e:
            raise HTTPException(e.status, {"error": e.code, "errors": [e.msg]}) from None

    @app.get("/v1/log/head")
    def log_head():
        if ref.log is None:
            raise HTTPException(404, "no transparency log configured")
        return ref.log.signed_head()

    @app.get("/v1/log")
    def log_entries(start: int = 0, limit: int = 500):
        if ref.log is None:
            raise HTTPException(404, "no transparency log configured")
        return {"start": start, "entries": ref.log.slice(start, min(limit, 2000)), "head": ref.log.signed_head()}

    @app.get("/v1/log/inclusion/{body_hash}")
    def log_inclusion(body_hash: str):
        if ref.log is None:
            raise HTTPException(404, "no transparency log configured")
        seq = ref.log.find(body_hash.lower())
        if seq is None:
            raise HTTPException(404, "receipt not in the log")
        return {"seq": seq, "entries": ref.log.slice(seq, len(ref.log.entries)), "head": ref.log.signed_head()}

    @app.post("/v1/challenges")
    def challenge(req: ChallengeReq):
        live_ok()
        try:
            return ref.issue_challenge(req.phase, req.role, req.match_id, req.anchor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/v1/anchors")
    def anchor(req: AnchorReq):
        live_ok()
        try:
            return ref.issue_anchor(req.match_id, req.anchor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    # ---------------- head-to-head challenge links (free; no money; settlement only from VERIFIED receipts)
    def h2h_ok():
        now = int(time.time() * 1000)
        with lock:
            if h2h_cap.count(now, 3_600_000) >= h2h_cap_hour:
                raise HTTPException(429, "challenge rate limit reached; try again later")
            h2h_cap.t.append(now)

    def h2h_call(fn, *a):
        try:
            return fn(*a)
        except H.H2HError as e:
            raise HTTPException(e.status, str(e)) from None

    @app.post("/v1/h2h")
    def h2h_create(req: H2HCreate):
        h2h_ok()
        return h2h_call(h2h.create, req.model_dump())

    @app.get("/v1/h2h/{cid}")
    def h2h_get(cid: str):
        return h2h_call(h2h.get, cid)

    @app.post("/v1/h2h/{cid}/accept")
    def h2h_accept(cid: str, req: H2HAccept):
        h2h_ok()
        return h2h_call(h2h.accept, cid, req.handle)

    @app.post("/v1/h2h/{cid}/receipts")
    async def h2h_receipt(cid: str, request: Request, authorization: str | None = Header(default=None)):
        h2h_ok()
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > H.MAX_RECEIPT_BYTES:
                raise HTTPException(413, "receipt too large")
        return h2h_call(h2h.submit, cid, authorization, bytes(body))

    @app.get("/v1/h2h/{cid}/receipts/{sha16}")
    def h2h_receipt_get(cid: str, sha16: str):
        return Response(h2h_call(h2h.receipt, cid, sha16), media_type="application/json")

    @app.post("/v1/submissions")
    async def submit(request: Request, mode: str = SPOT, authorization: str | None = Header(default=None),
                     x_qor_submit: str | None = Header(default=None)):
        admin = bool(submit_token) and hmac.compare_digest(
            (authorization or "").removeprefix("Bearer ").strip(), submit_token)
        auth = None
        if not admin:
            try:  # cheap checks first: header, clock, nonce MAC/expiry/replay, PoW, rate limits (no body read yet)
                auth = guard.precheck(x_qor_submit)
            except AuthError as e:
                raise HTTPException(e.status, {"error": e.code, "errors": [e.msg]}) from None
        if mode not in (SPOT, FULL):
            raise HTTPException(400, "mode must be spot-check or full")
        ref.expire()
        with lock:
            active = sum(1 for s in ref.subs.values() if s.state in ("awaiting_segments", "awaiting_player_signature"))
        if active >= max_active:
            raise HTTPException(503, "referee busy; try again in a few minutes")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_META_BYTES:
                raise HTTPException(413, "bundle meta zip too large")
        try:
            files = B.read_meta_zip(bytes(body), MAX_META_BYTES)
        except Exception as e:
            raise HTTPException(400, f"could not read bundle meta zip: {e}") from None
        if auth is not None:
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            try:
                guard.verify_body(auth, method="POST", path=path, body=bytes(body), files=files)
            except AuthError as e:
                raise HTTPException(e.status, {"error": e.code, "errors": [e.msg]}) from None
        sub = ref.create(files, mode)
        if auth is not None:
            sub_pub[sub.id] = str(auth["pub"]).lower()
        out = sub.public()
        if sub.state == "rejected":
            raise HTTPException(422, out)
        out["upload_token"] = sub.upload_token
        return out

    @app.get("/v1/submissions/{sid}")
    def status(sid: str, authorization: str | None = Header(default=None)):
        return sub_or_404(sid, authorization).public()

    @app.put("/v1/submissions/{sid}/segments/{seq}")
    async def segment(sid: str, seq: int, request: Request, authorization: str | None = Header(default=None)):
        s = sub_or_404(sid, authorization)
        chunks: list[bytes] = []
        async for c in request.stream():
            chunks.append(c)
        try:
            return ref.put_segment(s, seq, chunks)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/v1/submissions/{sid}/review")
    def review(sid: str, authorization: str | None = Header(default=None)):
        s = sub_or_404(sid, authorization)
        try:
            return ref.review(s)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/v1/submissions/{sid}/player-signature")
    def player_signature(sid: str, req: PlayerSig, authorization: str | None = Header(default=None)):
        s = sub_or_404(sid, authorization)
        try:
            out = ref.add_player_signature(s, req.sig)
            if sid in sub_pub:
                guard.promote(sub_pub.pop(sid))
            return out
        except PermissionError as e:
            raise HTTPException(401, str(e)) from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    return app


app = (create_app() if os.getenv("QORTIME_REFEREE_AUTOSTART", "1") == "1"
       and (os.getenv("QORTIME_SERVICE_SEED") or os.getenv("QORTIME_REFEREE_SEED")) else None)
