"""Referee service (FastAPI). In-memory dev store; one process = one referee key.

Run: QOR_REFEREE_SEED=<hex32> uvicorn qor_receipt.referee.app:app
Flow: POST /matches -> POST /matches/{id}/challenges {phase} (per role) ->
POST .../challenges/{cid}/response -> POST .../checkpoints (every few s) ->
POST .../timeline -> POST .../finalize (referee-signed) -> POST .../sign (players).
Player requests carry an Ed25519 signature over JCS(request body) by the player key.
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from qor_receipt import receipt as R
from qor_receipt.challenge import (
    DEFAULT_DEADLINE_MS,
    PHASES,
    menu_safe_nonce,
    motion_correlation_permille,
    pattern_for,
    verify_response,
)
from qor_receipt.checkpoints import body_of, countersign
from qor_receipt.final_detect import detect_final, timelines_agree, weakest_basis
from qor_receipt.jcs import canonicalize, sha256_hex
from qor_receipt.keys import SoftwareKey, verify


class Player(BaseModel):
    role: str
    player_id: str
    pubkey: str
    key_kind: str = "software"


class StartReq(BaseModel):
    players: list[Player]
    game: str = "madden27"
    platform: str = "ps5"


class Signed(BaseModel):
    role: str
    payload: dict[str, Any]
    sig: str


def create_app(key: SoftwareKey | None = None, clock: Callable[[], int] | None = None,
               nonce_fn: Callable[[str, str], str] | None = None) -> FastAPI:
    """nonce_fn(phase, role) is ONLY for agent replay of a recorded session; production draws a menu-safe nonce."""
    key = key or (SoftwareKey.from_seed_hex(os.environ["QOR_REFEREE_SEED"]) if os.getenv("QOR_REFEREE_SEED") else SoftwareKey())
    now = clock or (lambda: int(time.time() * 1000))
    app = FastAPI(title="QorTime referee", version="0.1.0")
    M: dict[str, dict[str, Any]] = {}

    def get(mid: str) -> dict[str, Any]:
        if mid not in M:
            raise HTTPException(404, "no such match")
        return M[mid]

    def auth(m: dict, req: Signed) -> None:
        p = m["players"].get(req.role)
        if not p or not verify(p["pubkey"], req.sig, canonicalize(req.payload)):
            raise HTTPException(401, "bad player signature")
        if m["final"] is not None and "receipt_sha256" not in req.payload:
            raise HTTPException(409, "match finalized")

    @app.get("/referee")
    def info():
        return {"pubkey": key.pubkey_hex, "key_id": key.key_id, "key_kind": key.kind}

    @app.post("/matches")
    def start(req: StartReq):
        roles = [p.role for p in req.players]
        if not roles or len(set(roles)) != len(roles) or not set(roles) <= {"home", "away"}:
            raise HTTPException(400, "players must be home and/or away")
        mid = secrets.token_hex(8)
        M[mid] = {"id": mid, "game": req.game, "platform": req.platform, "started_ms": now(),
                  "players": {p.role: p.model_dump() for p in req.players}, "challenges": {},
                  "checkpoints": {r: [] for r in roles}, "timelines": {}, "motion": {}, "final": None}
        return {"match_id": mid, "mode": "dual" if len(roles) == 2 else "single", "referee_pubkey": key.pubkey_hex}

    @app.post("/matches/{mid}/challenges")
    def issue(mid: str, body: dict[str, str]):
        m = get(mid)
        phase, role = body.get("phase"), body.get("role")
        if phase not in PHASES or role not in m["players"]:
            raise HTTPException(400, "bad phase/role")
        nonce, t = (nonce_fn(phase, role) if nonce_fn else menu_safe_nonce()), now()
        cid = secrets.token_hex(6)
        c = {"id": cid, "role": role, "phase": phase, "nonce": nonce, "pattern": pattern_for(nonce),
             "issued_ms": t, "deadline_ms": t + DEFAULT_DEADLINE_MS, "result": None}
        m["challenges"][cid] = c
        return {k: c[k] for k in ("id", "phase", "nonce", "pattern", "issued_ms", "deadline_ms")}

    @app.post("/matches/{mid}/challenges/{cid}/response")
    def respond(mid: str, cid: str, req: Signed):
        m = get(mid)
        auth(m, req)
        c = m["challenges"].get(cid)
        if not c or c["role"] != req.role:
            raise HTTPException(404, "no such challenge")
        if c["result"] is not None:
            raise HTTPException(409, "already answered")
        if now() > c["deadline_ms"] + 5_000:
            c["result"] = {"ok": False, "reason": "late_submission", "pause_press_ms": None, "done_ms": None, "latency_ms": None}
        else:
            c["result"] = verify_response(c["pattern"], req.payload["hid_events"], issued_ms=c["issued_ms"],
                                          deadline_ms=c["deadline_ms"], pause_seen_ms=req.payload.get("pause_seen_ms")).to_dict()
        c["result"]["hid_sha256"] = sha256_hex(req.payload["hid_events"])
        return c["result"]

    @app.post("/matches/{mid}/checkpoints")
    def checkpoint(mid: str, req: Signed):
        m = get(mid)
        auth(m, req)
        chain = m["checkpoints"][req.role]
        cp = body_of(req.payload)
        if cp["seq"] != len(chain) or cp["prev"] != (chain[-1]["hash"] if chain else "0" * 64):
            raise HTTPException(409, "chain break")
        if sha256_hex(cp) != req.payload.get("hash"):
            raise HTTPException(400, "hash mismatch")
        cp["hash"] = req.payload["hash"]
        signed = countersign(key, mid, req.role, cp, now())
        chain.append(signed)
        return signed

    @app.post("/matches/{mid}/timeline")
    def timeline(mid: str, req: Signed):
        m = get(mid)
        auth(m, req)
        m["timelines"][req.role] = req.payload["reads"]
        if "input_energy" in req.payload and "video_motion" in req.payload:
            m["motion"][req.role] = motion_correlation_permille(req.payload["input_energy"], req.payload["video_motion"])
        return {"ok": True, "motion_permille": m["motion"].get(req.role)}

    @app.post("/matches/{mid}/finalize")
    def finalize(mid: str):
        m = get(mid)
        if m["final"] is not None:
            return m["final"]
        roles = sorted(m["players"])
        finals = {r: detect_final(m["timelines"].get(r, [])) for r in roles}
        for r, f in finals.items():
            if not f.final:
                raise HTTPException(409, f"{r}: not final ({f.reason})")
        mode = "dual" if len(roles) == 2 else "single"
        mism: list[str] = []
        if mode == "dual":
            a, b = (finals[r] for r in roles)
            if (a.home, a.away) != (b.home, b.away):
                mism.append("final_score_differs")
            mism += timelines_agree(m["timelines"][roles[0]], m["timelines"][roles[1]])
            if mism:
                raise HTTPException(409, {"dual_mismatch": mism})
        f = finals[roles[0]]
        rec = R.build(
            match={"match_id": mid, "game": m["game"], "platform": m["platform"], "scorebug_profile": "madden27_standard",
                   "mode": mode, "started_ms": m["started_ms"], "ended_ms": now()},
            result={"home_score": f.home, "away_score": f.away, "quarter": f.quarter, "final_t_ms": f.t_ms,
                    "winner": R.winner_for(f.home, f.away), "method": "agreed_scorebug_final",
                    "final_basis": weakest_basis(x.basis for x in finals.values())},
            players=[m["players"][r] for r in roles],
            referee={"pubkey": key.pubkey_hex, "key_id": key.key_id, "key_kind": key.kind},
            challenges=sorted(m["challenges"].values(), key=lambda c: (c["issued_ms"], c["id"])),
            checkpoints=m["checkpoints"],
            agreement={"timelines_agree": (not mism) if mode == "dual" else None, "mismatches": mism},
            signals={"motion_permille": {r: m["motion"].get(r) for r in roles}},
        )
        m["final"] = R.sign_referee(rec, key)
        return m["final"]

    @app.post("/matches/{mid}/sign")
    def player_sign(mid: str, req: Signed):
        m = get(mid)
        if m["final"] is None:
            raise HTTPException(409, "not finalized")
        auth(m, req)
        if req.payload.get("receipt_sha256") != sha256_hex({k: v for k, v in m["final"].items() if k != "signatures"}):
            raise HTTPException(409, "receipt hash mismatch")
        sig = req.payload["sig"]
        if not verify(m["players"][req.role]["pubkey"], sig, R.signing_bytes(m["final"])):
            raise HTTPException(401, "bad receipt signature")
        m["final"]["signatures"]["players"][req.role] = {"key_id": R.kid(m["players"][req.role]["pubkey"]), "sig": sig}
        return m["final"]

    @app.get("/matches/{mid}/receipt")
    def get_receipt(mid: str):
        return get(mid)["final"]

    return app


app = create_app() if os.getenv("QOR_REFEREE_AUTOSTART", "1") == "1" else None
