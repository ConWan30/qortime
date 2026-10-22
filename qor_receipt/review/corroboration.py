"""Two-sided corroboration of QorCap submissions.

Additive to the receipt verdict. A corroborated pair does not make a failing
receipt pass, and a missing partner does not change a single-sided VERIFIED
result. The status is ``corroborated``, ``disagreed``, or ``single_sided``.

The pairing id and nonce are taken only from a referee-signed
``qortime-pairing/v1`` token. A client-supplied id is not a link.

Score timelines are the events inside the player-signed receipt at
``signals.score_timeline.events``. Each event is
``{t_ms, home, away, quarter}``. Consecutive identical (home, away, quarter)
states are one change. Those changes must occur in the same order on both
sides, and each pair of timestamps must differ by at most
``SCORE_EVENT_TOLERANCE_MS`` (5_000, the same window as
``final_detect.timelines_agree``). Timestamps are compared as signed. A
constant clock offset is not removed.

Challenge responses are independent only when the player keys differ, the
final checkpoint heads differ, no challenge nonce is shared, and no challenge
response fingerprint is shared. A copy of the other player's bundle fails
that test.
"""
from __future__ import annotations

import json
from typing import Any

from qor_receipt.final_detect import timelines_agree
from qor_receipt.jcs import canonicalize
from qor_receipt.keys import kid, verify
from qor_receipt.review.policy import FAIL_CHECK, PAIRING_TYPE, PARTIAL_CHECK

CORROBORATION_SCHEMA = "qortime-corroboration/v0"
CORROBORATED, DISAGREED, SINGLE_SIDED = "corroborated", "disagreed", "single_sided"
STATUSES = frozenset({CORROBORATED, DISAGREED, SINGLE_SIDED})
# Same absolute window as dual-mode timeline agreement. Documented in docs/CORROBORATION.md.
SCORE_EVENT_TOLERANCE_MS = 5_000
_MAX_EVENTS = 400
_MAX_REASONS = 24
_MAX_PAIRING_BYTES = 8_192
_DETAIL = (
    "Final scores must be equal. Score-change events must be the same sequence "
    f"within {SCORE_EVENT_TOLERANCE_MS} ms (no clock offset is removed). "
    "Challenge responses must be independent: different player keys, checkpoint heads, "
    "nonces, and response fingerprints. This status does not change VERIFIED, PARTIAL, "
    "SELF_ATTESTED, or FAIL."
)


def own_checks_label(checks: dict[str, Any] | None) -> str:
    if not isinstance(checks, dict) or not checks:
        return "absent"
    states = [c.get("state") for c in checks.values() if isinstance(c, dict)]
    if any(s == FAIL_CHECK for s in states):
        return "fail"
    if any(s == PARTIAL_CHECK for s in states):
        return "partial"
    return "pass"


def corroboration_view(review: dict[str, Any] | None) -> dict[str, Any] | None:
    """Copy a referee corroboration block for display. Never a verdict input.

    ``affects_verdict`` is forced false. Unknown statuses are dropped.
    """
    if not isinstance(review, dict):
        return None
    raw = review.get("corroboration")
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if status not in STATUSES:
        return None
    reasons = [x for x in (raw.get("reasons") or []) if isinstance(x, str)][:_MAX_REASONS]
    tol = raw.get("tolerance_ms")
    out: dict[str, Any] = {
        "schema": CORROBORATION_SCHEMA,
        "status": status,
        "reasons": reasons,
        "tolerance_ms": tol if isinstance(tol, int) and not isinstance(tol, bool) else SCORE_EVENT_TOLERANCE_MS,
        "affects_verdict": False,
    }
    detail = raw.get("detail")
    if isinstance(detail, str) and detail:
        out["detail"] = detail[:500]
    pairing_id = raw.get("pairing_id")
    if isinstance(pairing_id, str) and pairing_id:
        out["pairing_id"] = pairing_id[:64]
    sides = raw.get("sides")
    if isinstance(sides, dict):
        cleaned = {}
        for role in ("home", "away"):
            side = sides.get(role)
            if isinstance(side, dict) and isinstance(side.get("own_checks"), str):
                cleaned[role] = {"own_checks": side["own_checks"][:32]}
        if cleaned:
            out["sides"] = cleaned
    return out


def _token_signed(token: Any, pubkey_hex: str) -> bool:
    if not isinstance(token, dict) or token.get("type") != PAIRING_TYPE or not isinstance(token.get("sig"), str):
        return False
    try:
        if token.get("key_id") != kid(pubkey_hex):
            return False
        payload = canonicalize({k: v for k, v in token.items() if k != "sig"})
    except (TypeError, ValueError):
        return False
    return verify(pubkey_hex, token["sig"], payload)


def accept_pairing_token(token: Any, pubkey_hex: str) -> dict[str, Any] | None:
    """Return the pairing id and nonce iff ``token`` was signed by ``pubkey_hex``.

    The id is read from the signed body. A client that edits ``pairing_id`` or
    ``nonce`` after the signature does not get a link.
    """
    if not _token_signed(token, pubkey_hex):
        return None
    pairing_id, nonce = token.get("pairing_id"), token.get("nonce")
    issued = token.get("issued_ms")
    if not _hex32(pairing_id) or not _hex32(nonce):
        return None
    if not isinstance(issued, int) or isinstance(issued, bool) or issued < 0:
        return None
    return {"pairing_id": pairing_id.lower(), "nonce": nonce.lower(), "issued_ms": issued}


def read_pairing_file(files: dict[str, bytes]) -> dict[str, Any] | None:
    """Parse ``pairing.json``. ``None`` if the file is absent. ``{"_malformed": True}`` if it is junk."""
    raw = files.get("pairing.json")
    if raw is None:
        return None
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MAX_PAIRING_BYTES:
        return {"_malformed": True}
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, ValueError):
        return {"_malformed": True}
    if not isinstance(value, dict) or len(value) > 16:
        return {"_malformed": True}
    return value


def side_from_receipt(receipt: dict[str, Any], *, own_checks: str, timeline_file: bytes | None = None) -> dict[str, Any] | None:
    """A signed score timeline, or None when the player signature does not cover the receipt."""
    if not isinstance(receipt, dict) or not _player_signed(receipt):
        return None
    players = receipt.get("players") or []
    player = players[0] if isinstance(players, list) and players else {}
    role = player.get("role")
    if role not in ("home", "away"):
        return None
    events = _signed_events(receipt)
    if events is None:
        return None
    final = _final(receipt)
    head = _checkpoint_head(receipt, role)
    challenges = receipt.get("challenges") if isinstance(receipt.get("challenges"), list) else []
    file_events = _timeline_file_events(timeline_file) if timeline_file is not None else None
    file_mismatch = False
    if file_events is not None and timelines_agree(events, file_events, tol_ms=0):
        file_mismatch = True
    return {
        "role": role,
        "player_pubkey": str(player.get("pubkey") or ""),
        "checkpoint_head": head,
        "final": final,
        "events": events,
        "challenges": challenges,
        "own_checks": own_checks,
        "timeline_file_mismatch": file_mismatch,
    }


def side_from_bundle(bundle: Any, own_checks: str) -> dict[str, Any] | None:
    receipt = getattr(bundle, "receipt", None)
    if not isinstance(receipt, dict):
        return None
    files = getattr(bundle, "files", {}) or {}
    return side_from_receipt(receipt, own_checks=own_checks, timeline_file=files.get("timeline.jsonl"))


def build_corroboration(home: dict[str, Any] | None, away: dict[str, Any] | None, *,
                        home_present: bool, away_present: bool, extra_reasons: list[str] | None = None,
                        pairing_id: str | None = None) -> dict[str, Any]:
    """Cross-check two signed sides. Missing partner -> ``single_sided`` (unless a role conflict was already found)."""
    reasons = [r for r in (extra_reasons or []) if isinstance(r, str)]
    if home_present and away_present:
        if home is None or away is None:
            reasons.append("unsigned_score_timeline")
        else:
            reasons.extend(_compare(home, away))
        status = DISAGREED if reasons else CORROBORATED
    elif "role_conflict" in reasons:
        status = DISAGREED
    else:
        status = SINGLE_SIDED
    # De-duplicate, keep order, cap.
    seen: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    sides: dict[str, Any] = {}
    if home_present:
        sides["home"] = {"own_checks": (home or {}).get("own_checks", "absent")}
    if away_present:
        sides["away"] = {"own_checks": (away or {}).get("own_checks", "absent")}
    finals = {}
    if home and home.get("final") is not None:
        finals["home"] = list(home["final"])
    if away and away.get("final") is not None:
        finals["away"] = list(away["final"])
    out: dict[str, Any] = {
        "schema": CORROBORATION_SCHEMA,
        "status": status,
        "reasons": seen[:_MAX_REASONS],
        "tolerance_ms": SCORE_EVENT_TOLERANCE_MS,
        "affects_verdict": False,
        "detail": _DETAIL,
        "sides": sides,
    }
    if pairing_id:
        out["pairing_id"] = pairing_id
    if finals:
        out["finals"] = finals
    return out


def _compare(home: dict[str, Any], away: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    hf, af = home.get("final"), away.get("final")
    if hf is None or af is None:
        reasons.append("final_score_missing")
    elif tuple(hf) != tuple(af):
        reasons.append("final_score_mismatch")
    if not home.get("events") or not away.get("events"):
        reasons.append("score_timeline_missing")
    for side in (home, away):
        final = side.get("final")
        events = side.get("events") or []
        if final is not None and events:
            last = events[-1]
            if (last["home"], last["away"]) != tuple(final):
                reasons.append("timeline_result_mismatch")
                break
        if side.get("timeline_file_mismatch"):
            reasons.append("timeline_file_mismatch")
    reasons.extend(timelines_agree(home.get("events") or [], away.get("events") or [], SCORE_EVENT_TOLERANCE_MS))
    reasons.extend(_independence(home, away))
    return reasons


def _independence(home: dict[str, Any], away: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    hp, ap = (home.get("player_pubkey") or "").lower(), (away.get("player_pubkey") or "").lower()
    if hp and ap and hp == ap:
        reasons.append("same_player_key")
    hh, ah = (home.get("checkpoint_head") or "").lower(), (away.get("checkpoint_head") or "").lower()
    if _hex64(hh) and hh == ah:
        reasons.append("same_checkpoint_head")
    hn = {c.get("nonce") for c in home.get("challenges") or [] if isinstance(c, dict) and isinstance(c.get("nonce"), str)}
    an = {c.get("nonce") for c in away.get("challenges") or [] if isinstance(c, dict) and isinstance(c.get("nonce"), str)}
    if hn & an:
        reasons.append("shared_challenge_nonce")
    if _fingerprints(home) & _fingerprints(away):
        reasons.append("copied_challenge_response")
    return reasons


def _fingerprints(side: dict[str, Any]) -> set[tuple]:
    out = set()
    for c in side.get("challenges") or []:
        if not isinstance(c, dict):
            continue
        res = c.get("result") if isinstance(c.get("result"), dict) else {}
        pattern = tuple(c.get("pattern") or []) if isinstance(c.get("pattern"), list) else ()
        out.add((c.get("nonce"), pattern, res.get("hid_sha256"), res.get("pause_press_ms"), res.get("done_ms")))
    return {fp for fp in out if any(x is not None for x in fp)}


def _player_signed(receipt: dict[str, Any]) -> bool:
    players = receipt.get("players")
    if not isinstance(players, list) or len(players) != 1 or not isinstance(players[0], dict):
        return False
    player = players[0]
    role, pub = player.get("role"), player.get("pubkey")
    sigs = (receipt.get("signatures") or {}).get("players") if isinstance(receipt.get("signatures"), dict) else None
    sig = ((sigs or {}).get(role) or {}).get("sig") if isinstance(sigs, dict) else None
    if not isinstance(pub, str) or not isinstance(sig, str) or role not in ("home", "away"):
        return False
    try:
        body = canonicalize({k: v for k, v in receipt.items() if k != "signatures"})
    except (TypeError, ValueError):
        return False
    return verify(pub, sig, body)


def _signed_events(receipt: dict[str, Any]) -> list[dict[str, Any]] | None:
    signals = receipt.get("signals")
    if not isinstance(signals, dict) or "score_timeline" not in signals:
        return None
    block = signals.get("score_timeline")
    raw = block.get("events") if isinstance(block, dict) else block
    return _event_list(raw)


def _event_list(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list) or len(raw) > _MAX_EVENTS:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            t, h, a, q = item["t_ms"], item["home"], item["away"], item["quarter"]
        except KeyError:
            return None
        if not all(_nonneg_int(x) for x in (t, h, a)) or not _nonneg_int(q) or q < 1:
            return None
        out.append({"t_ms": t, "home": h, "away": a, "quarter": q})
    return out


def _timeline_file_events(raw: bytes | None) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > 2_000_000:
        return []
    events = []
    for i, line in enumerate(bytes(raw).decode("utf-8", "replace").splitlines()):
        if i > 5000:
            break
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return []
        if not isinstance(item, dict) or "obs" in item:
            continue
        ev = _event_list([item])
        if ev:
            events.extend(ev)
    return events


def _final(receipt: dict[str, Any]) -> tuple[int, int] | None:
    res = receipt.get("result")
    if not isinstance(res, dict):
        return None
    h, a = res.get("home_score"), res.get("away_score")
    if _nonneg_int(h) and _nonneg_int(a):
        return (h, a)
    return None


def _checkpoint_head(receipt: dict[str, Any], role: str) -> str:
    cps = (receipt.get("checkpoints") or {}).get(role) if isinstance(receipt.get("checkpoints"), dict) else None
    if not isinstance(cps, list) or not cps or not isinstance(cps[-1], dict):
        return ""
    head = cps[-1].get("hash")
    return head.lower() if _hex64(head) else ""


def _nonneg_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _hex32(v: Any) -> bool:
    return isinstance(v, str) and len(v) == 32 and all(c in "0123456789abcdefABCDEF" for c in v)


def _hex64(v: Any) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v.lower())
