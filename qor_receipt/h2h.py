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

PSN name check (on-screen evidence, not account proof): the creator enters both players' PSN Online IDs. QorCap reads
the PSN names Madden draws on screen in an online game (``qorcap psn-names``) and signs them into the receipt as
``match.observed_handles``. A receipt only names a winner when those on-screen names match the challenge's two PSN IDs
(case and look-alike characters such as 0/O, 1/l, g/y are treated as equal). Names that reliably differ: "players don't match", no
winner. Names that could not be read (or are missing): "players unverified", no winner. Sony and EA offer no public login
or API, so this cannot prove who owns or held an account.
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
PSN_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,15}$")
ON_SCREEN = ("PSN names are on-screen evidence, not account proof: they are read from what Madden showed in the "
             "recorded video. Sony and EA offer no public login or API.")
UNCHECKED = ["quarter length and other rules are agreed between the players; QorTime does not check them",
             "handles are typed by the players, not verified accounts",
             "PSN IDs are only compared with the names Madden showed on screen in the recording, not with PSN accounts"]
# look-alike characters at the size Madden draws banner names (same groups as QorCap's reader)
CONFUSABLE = ["0OoQDd", "1lIi|j", "gyq9", "5Ss", "2Zz", "8B", "uvUV", "cC", "kK", "pP", "wW", "xX", "6b", "nh", "rt",
              "ae", "-_"]
OBS_MIN_AGREE = 3
OBS_RELIABLE = 850   # confidence (permille) an unwildcarded read needs before it can say "players don't match"
MAX_WILDCARDS = 2


class H2HError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def norm_handle(h: Any) -> str:
    if not isinstance(h, str) or not HANDLE.match(h.strip()):
        raise H2HError(400, "handle must be 1-32 letters, digits, _ . - (optionally starting with @)")
    return "@" + h.strip().lstrip("@")


def norm_psn(p: Any) -> str:
    if not isinstance(p, str) or not PSN_ID.match(p.strip()):
        raise H2HError(400, "PSN ID must be 3-16 letters, digits, - or _, starting with a letter")
    return p.strip()


def same_char(a: str, b: str) -> bool:
    """Equal ignoring case, or a look-alike pair (position-wise; not transitive on purpose)."""
    return a.lower() == b.lower() or any(a in g and b in g for g in CONFUSABLE)


def same_name(a: str, b: str) -> bool:
    return len(a) == len(b) and all(same_char(x, y) for x, y in zip(a, b))


def compare_name(expected: str, obs: dict | None) -> dict:
    """match | mismatch | unread for one PSN ID against one observed banner name. Fail closed: only a reliable,
    complete read can say mismatch; anything unclear is unread."""
    if not isinstance(obs, dict) or not isinstance(obs.get("pattern"), str) or not obs["pattern"]:
        return {"result": "unread", "why": "no name read on screen"}
    pat, handle = obs["pattern"], obs.get("handle")
    conf = obs.get("confidence_permille") if isinstance(obs.get("confidence_permille"), int) else 0
    if not isinstance(obs.get("agree"), int) or obs["agree"] < OBS_MIN_AGREE:
        return {"result": "unread", "why": "too few sightings agree"}
    reliable = isinstance(handle, str) and handle == pat and "?" not in pat and conf >= OBS_RELIABLE
    if len(pat) != len(expected):
        return {"result": "mismatch" if reliable else "unread", "why": "different length on screen"}
    contra, wild = 0, 0
    for ch, want in zip(pat, expected):
        if ch == "?":
            wild += 1
        elif not same_char(ch, want):
            contra += 1
    if contra:
        return {"result": "mismatch" if reliable else "unread",
                "why": f"{contra} character(s) differ" + ("" if reliable else " in an unreliable read")}
    if wild == 0:
        return {"result": "match"}
    if wild <= MAX_WILDCARDS and len(expected) >= 6:
        return {"result": "match", "why": f"{wild} character(s) unreadable, the rest match"}
    return {"result": "unread", "why": f"{wild} character(s) unreadable"}


def _observed(m: dict) -> dict | None:
    """Receipt-safe copy of match.observed_handles (signed by the receipt; sanitised for display)."""
    oh = m.get("observed_handles") if isinstance(m, dict) else None
    if not isinstance(oh, dict):
        return None
    out = {"source": str(oh.get("source"))[:24], "reader": str(oh.get("reader"))[:40]}
    for side in ("away", "home"):
        o = oh.get(side) if isinstance(oh.get(side), dict) else {}
        out[side] = {"handle": o["handle"][:32] if isinstance(o.get("handle"), str) else None,
                     "pattern": o["pattern"][:32] if isinstance(o.get("pattern"), str) else None,
                     **{k: o[k] for k in ("confidence_permille", "sightings", "agree")
                        if isinstance(o.get(k), int) and not isinstance(o.get(k), bool)}}
    return out


def players_check(c: dict, s: dict) -> dict:
    """match | mismatch | unverified for one counted receipt: its on-screen names vs the challenge's two PSN IDs."""
    cp, op = c["creator"].get("psn"), c["opponent"].get("psn")
    obs = s.get("observed")
    base = {"note": ON_SCREEN}
    if not cp or not op:
        return base | {"status": "unverified", "why": "this challenge has no PSN IDs"}
    if not obs:
        return base | {"status": "unverified", "why": "the receipt carries no on-screen PSN names (run qorcap psn-names, then Finalize)"}
    best = None
    for sides in ({"away": "creator", "home": "opponent"}, {"away": "opponent", "home": "creator"}):
        r = {side: compare_name(c[who]["psn"], obs.get(side)) | {"who": who} for side, who in sides.items()}
        if all(x["result"] == "match" for x in r.values()):
            best = (sides, r)
            break
    if best:
        sides, r = best
        rec_side = next(side for side, who in sides.items() if who == s["by"])
        out = base | {"status": "match", "sides": {sides["away"]: "away", sides["home"]: "home"},
                      "detail": {k: v.get("why") for k, v in r.items() if v.get("why")}}
        if s.get("role") and s["role"] != rec_side:
            return base | {"status": "unverified", "sides": out["sides"],
                           "why": f"the receipt says {s['handle']} played {s['role']}, but their PSN name was on the "
                                  f"{rec_side} side on screen"}
        return out
    tries = [{side: compare_name(c[who]["psn"], obs.get(side))["result"] for side, who in sides.items()}
             for sides in ({"away": "creator", "home": "opponent"}, {"away": "opponent", "home": "creator"})]
    if all("mismatch" in t.values() for t in tries):
        seen = " vs ".join(str((obs.get(k) or {}).get("handle") or (obs.get(k) or {}).get("pattern")) for k in ("away", "home"))
        return base | {"status": "mismatch", "why": f"on screen: {seen}; challenge: {cp} vs {op}"}
    return base | {"status": "unverified", "why": "the PSN names on screen could not be read reliably"}


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
        cpsn, opsn = norm_psn(body.get("creator_psn")), norm_psn(body.get("opponent_psn"))
        if same_name(cpsn, opsn):
            raise H2HError(400, "the two PSN IDs must be different (and not look-alikes of each other)")
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
                 "creator": {"handle": creator, "psn": cpsn, "token_sha256": _sha(token)},
                 "opponent": {"handle": opponent, "psn": opsn, "token_sha256": None, "accepted_ms": None},
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
                   "observed": _observed(m), "counts": not problems, "problems": problems}
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
        subs = [dict(s, players=players_check(c, s)) if s.get("counts") else dict(s) for s in c["submissions"]]
        return {"schema": SCHEMA, "id": c["id"], "created_ms": c["created_ms"], "game": c["game"],
                "game_name": GAMES.get(c["game"], c["game"]), "rules": c["rules"],
                "creator": {"handle": c["creator"]["handle"], "psn": c["creator"].get("psn")},
                "opponent": {"handle": c["opponent"]["handle"], "psn": c["opponent"].get("psn"),
                             "accepted": bool(c["opponent"]["token_sha256"]),
                             "accepted_ms": c["opponent"]["accepted_ms"]},
                "submissions": subs, "settlement": settle(c), "not_for_betting": NOT_FOR_BETTING,
                "on_screen_note": ON_SCREEN, "not_checked": UNCHECKED}


def _winner_handle(c: dict, s: dict, sides: dict | None = None) -> str | None:
    """Handle that won according to one verified receipt. ``sides`` (who -> away/home) comes from the on-screen PSN
    names; players_check already refused a receipt whose declared role contradicts them."""
    r = s["result"]
    if not isinstance(r.get("away"), int) or not isinstance(r.get("home"), int):
        return None
    if r["away"] == r["home"]:
        return "tie"
    win_side = "home" if r["home"] > r["away"] else "away"
    if sides:
        return next(c[who]["handle"] for who, side in sides.items() if side == win_side)
    me = c[s["by"]]["handle"]
    them = c["opponent" if s["by"] == "creator" else "creator"]["handle"]
    return me if s["role"] == win_side else them


def settle(c: dict) -> dict:
    """status: open | accepted | unverified | players_unverified | players_mismatch | recorded | confirmed | disputed.
    Only 'recorded'/'confirmed' name a winner, and only when the on-screen PSN names match the challenge."""
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
    checks = [players_check(c, s) for s in good]
    players = {"note": ON_SCREEN, "checks": [{"by": s["handle"], **{k: v for k, v in pc.items() if k != "note"}}
                                             for s, pc in zip(good, checks)]}
    if any(pc["status"] == "mismatch" for pc in checks):
        why = next(pc["why"] for pc in checks if pc["status"] == "mismatch")
        return base | {"status": "players_mismatch", "label": "players don't match · no winner", "why": why,
                       "players": players}
    ok = [pc for pc in checks if pc["status"] == "match"]
    if not ok:
        return base | {"status": "players_unverified", "label": "players unverified · no winner",
                       "why": "; ".join(dict.fromkeys(pc["why"] for pc in checks)), "players": players}
    sides = ok[0]["sides"]
    if any(pc.get("sides") not in (None, sides) for pc in checks):
        return base | {"status": "disputed", "label": "disputed · the two verified receipts disagree",
                       "why": "the receipts put the players on different sides", "players": players}
    if len(good) == 1:
        s = good[0]
        w = _winner_handle(c, s, sides)
        if w is None:
            return base | {"status": "unverified", "label": "unverified · receipt has no final score"}
        ignored = [x["handle"] for x in subs if not x.get("counts")]
        return {"status": "recorded", "winner": w, "score": {"away": s["result"]["away"], "home": s["result"]["home"]},
                "recorder_side": {s["handle"]: s["role"]}, "players": players,
                "label": f"recorded by {s['handle']}", "receipt_sha256": s["receipt_sha256"],
                **({"ignored_unverified": ignored} if ignored else {})}
    a, b = good[0], good[1]
    sa, sb = (a["result"]["away"], a["result"]["home"]), (b["result"]["away"], b["result"]["home"])
    wa, wb = _winner_handle(c, a, sides), _winner_handle(c, b, sides)
    if sa != sb or wa != wb or wa is None:
        why = "the two receipts show different scores" if sa != sb else "the two receipts disagree on who won (check each player's home/away side)"
        return base | {"status": "disputed", "label": "disputed · the two verified receipts disagree", "why": why}
    return {"status": "confirmed", "winner": wa, "score": {"away": sa[0], "home": sa[1]},
            "recorder_side": {a["handle"]: a["role"], b["handle"]: b["role"]}, "players": players,
            "label": "confirmed by both players", "receipt_sha256": [a["receipt_sha256"], b["receipt_sha256"]]}
