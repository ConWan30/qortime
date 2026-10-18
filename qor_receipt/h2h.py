"""Head-to-head challenge links (free; no money, no wagering, not for betting).

A player creates a challenge (game, rules, both handles) and shares the link. The opponent opens it and accepts; no
install needed. The creator records with QorCap and submits the signed receipt: one receipt is enough to settle, since
an online Madden game shows both players the same scoreboard. If the opponent also submits a receipt, the two must agree.

Fail closed:
  * Only a receipt the QorTime verifier computes as VERIFIED (co-signed by the allowlisted or delegated QorTime referee)
    can name a winner. SELF_ATTESTED, PARTIAL, FAIL, or no receipt: the challenge shows "unverified", never a winner.
  * The receipt must be for the challenge's game, its match must start after the challenge was created, and its final must
    be confirmed. A receipt can settle one challenge only.
  * Two verified receipts that disagree (score, or which handle won) make the challenge "disputed", with no winner.
Handles are what the players typed, not verified accounts. Rules such as quarter length are not checked by QorTime.
The challenge store never signs anything: settlement rests entirely on the receipts' own signatures.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from qor_receipt.receipt import VERIFIED, verify_receipt

SCHEMA = "qortime-h2h/v1"
GAMES = {"madden27": "Madden NFL 27"}
HANDLE = re.compile(r"^@?[A-Za-z0-9_.-]{1,32}$")
ID_RE = re.compile(r"^[a-z0-9]{10}$")
MAX_RECEIPT_BYTES = 4_000_000
START_SLACK_MS = 5 * 60_000
NOT_FOR_BETTING = ("Free head-to-head challenge. No money, no wagering, no entry fees, no payouts. QorTime challenges "
                   "are not for betting.")
UNCHECKED = ["quarter length and other rules are agreed between the players; QorTime does not check them",
             "handles are typed by the players, not verified accounts"]


class H2HError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def norm_handle(h: Any) -> str:
    if not isinstance(h, str) or not HANDLE.match(h.strip()):
        raise H2HError(400, "handle must be 1-32 letters, digits, _ . - (optionally starting with @)")
    return "@" + h.strip().lstrip("@")


def _sha(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def default_verify(r: dict) -> dict:
    ref = (r.get("referee") or {}) if isinstance(r, dict) else {}
    pub = ref.get("pubkey") if isinstance(ref.get("pubkey"), str) else None
    # Trust comes from the allowlist / delegation, never from this pin; pinning the receipt's own key only removes the
    # "not pinned" note so an allowlisted official receipt can reach VERIFIED.
    return verify_receipt(r, referee_pub=pub)


class Store:
    def __init__(self, root: str | Path, verify: Callable[[dict], dict] = default_verify, now: Callable[[], int] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.verify = verify
        self.now = now or (lambda: int(time.time() * 1000))
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- persistence
    def _p(self, cid: str) -> Path:
        if not ID_RE.match(cid or ""):
            raise H2HError(404, "no such challenge")
        return self.root / f"{cid}.json"

    def _load(self, cid: str) -> dict:
        p = self._p(cid)
        if not p.exists():
            raise H2HError(404, "no such challenge")
        return json.loads(p.read_text(encoding="utf-8"))

    def _save(self, c: dict) -> None:
        p = self._p(c["id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def _used(self) -> Path:
        return self.root / "receipts-used.jsonl"

    def _receipt_used_by(self, rsha: str) -> str | None:
        p = self._used()
        if not p.exists():
            return None
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("sha256") == rsha:
                return d.get("challenge")
        return None

    # ---------------------------------------------------------------- API
    def create(self, body: dict) -> dict:
        game = body.get("game") or "madden27"
        if game not in GAMES:
            raise H2HError(400, f"game must be one of {sorted(GAMES)}")
        creator, opponent = norm_handle(body.get("creator")), norm_handle(body.get("opponent"))
        if creator.lower() == opponent.lower():
            raise H2HError(400, "creator and opponent must be different handles")
        rules_in = body.get("rules") or {}
        if not isinstance(rules_in, dict):
            raise H2HError(400, "rules must be an object")
        q = rules_in.get("quarter_min", 6)
        if isinstance(q, bool) or not isinstance(q, int) or not 1 <= q <= 15:
            raise H2HError(400, "rules.quarter_min must be 1-15 minutes")
        rules = {"quarter_min": q}
        for k, n in (("difficulty", 24), ("notes", 160)):
            v = rules_in.get(k)
            if v is not None:
                if not isinstance(v, str) or len(v) > n:
                    raise H2HError(400, f"rules.{k} must be text up to {n} characters")
                rules[k] = re.sub(r"[\x00-\x1f<>]", "", v).strip()
        for word in ("bet", "wager", "$", "money", "cash", "stake", "payout"):
            if word in json.dumps(rules).lower():
                raise H2HError(400, "challenges are free and not for betting; remove money/wager terms from the rules")
        token = secrets.token_urlsafe(24)
        with self.lock:
            while True:
                cid = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
                if not self._p(cid).exists():
                    break
            c = {"schema": SCHEMA, "id": cid, "created_ms": self.now(), "game": game, "rules": rules,
                 "creator": {"handle": creator, "token_sha256": _sha(token)},
                 "opponent": {"handle": opponent, "token_sha256": None, "accepted_ms": None},
                 "submissions": []}
            self._save(c)
        return {"challenge": self.public(c), "creator_token": token}

    def accept(self, cid: str, handle: str | None = None) -> dict:
        with self.lock:
            c = self._load(cid)
            if handle is not None and norm_handle(handle).lower() != c["opponent"]["handle"].lower():
                raise H2HError(403, f"this challenge is for {c['opponent']['handle']}")
            if c["opponent"]["token_sha256"]:
                raise H2HError(409, "already accepted (use the browser you accepted from)")
            token = secrets.token_urlsafe(24)
            c["opponent"].update(token_sha256=_sha(token), accepted_ms=self.now())
            self._save(c)
        return {"challenge": self.public(c), "opponent_token": token}

    def _who(self, c: dict, token: str | None) -> str:
        t = _sha((token or "").removeprefix("Bearer ").strip())
        if hmac.compare_digest(t, c["creator"]["token_sha256"]):
            return "creator"
        if c["opponent"]["token_sha256"] and hmac.compare_digest(t, c["opponent"]["token_sha256"]):
            return "opponent"
        raise H2HError(401, "not a player in this challenge (missing or wrong player token)")

    def submit(self, cid: str, token: str | None, raw: bytes) -> dict:
        if len(raw) > MAX_RECEIPT_BYTES:
            raise H2HError(413, "receipt too large")
        try:
            receipt = json.loads(raw)
            if not isinstance(receipt, dict):
                raise ValueError
        except ValueError:
            raise H2HError(400, "body must be a receipt.json (JSON object)") from None
        rsha = _sha(raw)
        with self.lock:
            c = self._load(cid)
            who = self._who(c, token)
            if any(s["by"] == who and s.get("counts") for s in c["submissions"]):
                raise H2HError(409, "you already submitted a verified receipt for this challenge")
            # an earlier receipt that did not count (e.g. the SELF_ATTESTED one before `qorcap submit`) is replaced
            c["submissions"] = [s for s in c["submissions"] if s["by"] != who]
            other = self._receipt_used_by(rsha)
            if other:
                raise H2HError(409, "this receipt was already used for a challenge")
            try:
                v = self.verify(receipt)
            except Exception as e:  # noqa: BLE001 - a malformed receipt is unverified, not a server error
                v = {"status": "FAIL", "errors": [f"malformed:{type(e).__name__}"], "partial": []}
            m = receipt.get("match") if isinstance(receipt.get("match"), dict) else {}
            res = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
            players = receipt.get("players") if isinstance(receipt.get("players"), list) else []
            role = next((p.get("role") for p in players if isinstance(p, dict) and p.get("role") in ("home", "away")), None)
            problems = []
            if v.get("status") != VERIFIED:
                problems.append(f"receipt is {v.get('status')}, not VERIFIED by the QorTime referee")
            if m.get("game") != c["game"]:
                problems.append(f"receipt game {m.get('game')!r} is not {c['game']!r}")
            if not isinstance(m.get("started_ms"), int) or m["started_ms"] < c["created_ms"] - START_SLACK_MS:
                problems.append("match started before the challenge was created")
            if res.get("final_confirmed") is not True:
                problems.append("final score not confirmed")
            if role is None:
                problems.append("receipt has no home/away player role")
            sub = {"by": who, "handle": c[who]["handle"], "submitted_ms": self.now(), "receipt_sha256": rsha,
                   "receipt_id": receipt.get("receipt_id") or m.get("match_id"), "status": v.get("status"),
                   "verify_errors": (v.get("errors") or [])[:8], "verify_partial": (v.get("partial") or [])[:8],
                   "role": role, "started_ms": m.get("started_ms"),
                   "result": {"away": res.get("away_score"), "home": res.get("home_score"), "winner": res.get("winner"),
                              "method": res.get("method"), "final_confirmed": res.get("final_confirmed")},
                   "counts": not problems, "problems": problems}
            c["submissions"].append(sub)
            (self.root / f"{cid}.{rsha[:16]}.receipt.json").write_bytes(raw)
            with open(self._used(), "a", encoding="utf-8") as f:
                f.write(json.dumps({"sha256": rsha, "challenge": cid}) + "\n")
            self._save(c)
        return self.public(c)

    def get(self, cid: str) -> dict:
        return self.public(self._load(cid))

    def receipt(self, cid: str, rsha16: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{16}", rsha16 or ""):
            raise H2HError(404, "no such receipt")
        self._p(cid)
        p = self.root / f"{cid}.{rsha16}.receipt.json"
        if not p.exists():
            raise H2HError(404, "no such receipt")
        return p.read_bytes()

    # ---------------------------------------------------------------- views
    def public(self, c: dict) -> dict:
        subs = [{k: v for k, v in s.items()} for s in c["submissions"]]
        return {"schema": SCHEMA, "id": c["id"], "created_ms": c["created_ms"], "game": c["game"],
                "game_name": GAMES.get(c["game"], c["game"]), "rules": c["rules"],
                "creator": {"handle": c["creator"]["handle"]},
                "opponent": {"handle": c["opponent"]["handle"], "accepted": bool(c["opponent"]["token_sha256"]),
                             "accepted_ms": c["opponent"]["accepted_ms"]},
                "submissions": subs, "settlement": settle(c), "not_for_betting": NOT_FOR_BETTING,
                "not_checked": UNCHECKED}


def _winner_handle(c: dict, s: dict) -> str | None:
    """Handle that won according to one verified receipt (the recorder plays the receipt's role)."""
    r = s["result"]
    if not isinstance(r.get("away"), int) or not isinstance(r.get("home"), int):
        return None
    if r["away"] == r["home"]:
        return "tie"
    win_side = "home" if r["home"] > r["away"] else "away"
    me = c[s["by"]]["handle"]
    them = c["opponent" if s["by"] == "creator" else "creator"]["handle"]
    return me if s["role"] == win_side else them


def settle(c: dict) -> dict:
    """status: open | accepted | unverified | recorded | confirmed | disputed. Only 'recorded'/'confirmed' name a winner."""
    subs = c["submissions"]
    good = [s for s in subs if s.get("counts")]
    base = {"winner": None, "score": None, "label": None}
    if not subs:
        st = "accepted" if c["opponent"]["token_sha256"] else "open"
        return base | {"status": st, "label": "waiting for the opponent to accept" if st == "open" else
                       f"waiting for {c['creator']['handle']}'s QorCap receipt"}
    if not good:
        why = "; ".join(p for s in subs for p in s.get("problems") or []) or "no verified receipt"
        return base | {"status": "unverified", "label": "unverified · no receipt verified by the QorTime referee", "why": why}
    if len(good) == 1:
        s = good[0]
        w = _winner_handle(c, s)
        if w is None:
            return base | {"status": "unverified", "label": "unverified · receipt has no final score"}
        ignored = [x["handle"] for x in subs if not x.get("counts")]
        return {"status": "recorded", "winner": w, "score": {"away": s["result"]["away"], "home": s["result"]["home"]},
                "recorder_side": {s["handle"]: s["role"]},
                "label": f"recorded by {s['handle']}", "receipt_sha256": s["receipt_sha256"],
                **({"ignored_unverified": ignored} if ignored else {})}
    a, b = good[0], good[1]
    sa, sb = (a["result"]["away"], a["result"]["home"]), (b["result"]["away"], b["result"]["home"])
    wa, wb = _winner_handle(c, a), _winner_handle(c, b)
    if sa != sb or wa != wb or wa is None:
        why = "the two receipts show different scores" if sa != sb else "the two receipts disagree on who won (check each player's home/away side)"
        return base | {"status": "disputed", "label": "disputed · the two verified receipts disagree", "why": why}
    return {"status": "confirmed", "winner": wa, "score": {"away": sa[0], "home": sa[1]},
            "recorder_side": {a["handle"]: a["role"], b["handle"]: b["role"]},
            "label": "confirmed by both players", "receipt_sha256": [a["receipt_sha256"], b["receipt_sha256"]]}
