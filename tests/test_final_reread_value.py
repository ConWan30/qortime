"""Optional structured value on final_reread. State and detail stay. The verifier ignores the value.

Synthetic keys in this file are demo seeds. They are not published matches.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from qor_receipt import receipt as R
from qor_receipt.keys import SoftwareKey
from qor_receipt.review.engine import PARTIAL_CHECK, PASS, _final_check

ROOT = Path(__file__).resolve().parents[1]
REFEREE = SoftwareKey.from_seed_hex("d0" * 32)
PLAYER = SoftwareKey.from_seed_hex("d1" * 32)

# Verdicts of receipts that already exist, taken before this change. A logic edit that moves one fails here.
PINNED = {
    "site/matches/021003/receipt.json": (
        "SELF_ATTESTED", "single-attested", [],
        ["referee_key_not_pinned", "referee_not_on_allowlist", "referee_local_self_attested",
         "away:challenge_missing:kickoff", "away:challenge_missing:halftime", "away:challenge_missing:end",
         "away:motion_signal_missing"]),
    "site/matches/021003/receipt-pre-psn-20261018.json": (
        "SELF_ATTESTED", "single-attested", [],
        ["referee_key_not_pinned", "referee_not_on_allowlist", "referee_local_self_attested",
         "home:challenge_unanswered:kickoff", "home:challenge_missing:halftime", "home:challenge_missing:end",
         "home:motion_signal_missing"]),
    "site/matches/125430/receipt.json": (
        "FAIL", "single-attested",
        ["home:cp[0]:gap_too_long", "home:cp[591]:gap_too_long", "home:cp[591]:clock_skew", "home:cp[592]:clock_skew"],
        ["referee_key_not_pinned", "referee_not_on_allowlist", "referee_local_self_attested",
         "home:challenge_missing:kickoff", "home:challenge_missing:halftime", "home:challenge_missing:end",
         "home:motion_signal_missing"]),
    "tests/vectors/agent_replay_receipt.json": (
        "SELF_ATTESTED", "single-attested", [],
        ["referee_not_on_allowlist", "referee_pin_not_allowlisted"]),
    "tests/vectors/delegated_review_receipt.json": (
        "SELF_ATTESTED", "single-attested", [],
        ["delegation_root_not_trusted", "referee_pin_not_allowlisted",
         "home:kickoff:challenge_nonce_not_official", "home:halftime:challenge_nonce_not_official",
         "home:end:challenge_nonce_not_official"]),
    "tests/vectors/demo_receipt_dual.json": (
        "SELF_ATTESTED", "dual-attested", [],
        ["referee_not_on_allowlist", "referee_pin_not_allowlisted"]),
    "tests/vectors/review_receipt.json": (
        "SELF_ATTESTED", "single-attested", [],
        ["referee_not_on_allowlist", "referee_pin_not_allowlisted",
         "home:kickoff:challenge_nonce_not_official", "home:halftime:challenge_nonce_not_official",
         "home:end:challenge_nonce_not_official"]),
    "verify/sample-receipt.json": (
        "SELF_ATTESTED", "dual-attested", [],
        ["referee_key_not_pinned", "referee_not_on_allowlist"]),
}


def _load(rel: str) -> tuple[dict, str | None]:
    doc = json.loads((ROOT / rel).read_text(encoding="utf-8"))
    if isinstance(doc, dict) and isinstance(doc.get("receipt"), dict):
        return doc["receipt"], doc.get("referee_pub")
    return doc, None


def _verdict(rec: dict, pub: str | None = None, allowlist: str | None = None) -> tuple:
    out = R.verify_receipt(rec, referee_pub=pub, allowlist_path=allowlist)
    return out["status"], out.get("trust_tier"), out["errors"], out["partial"]


def _resign(rec: dict) -> dict:
    rec = copy.deepcopy(rec)
    rec["signatures"] = {"players": {}, "referee": None}
    rec = R.sign_referee(rec, REFEREE)
    return R.sign_player(rec, rec["players"][0]["role"], PLAYER)


def test_existing_receipts_keep_their_verdicts():
    for rel, expect in PINNED.items():
        rec, pub = _load(rel)
        assert _verdict(rec, pub) == expect, rel


def test_value_does_not_change_a_resigned_review_receipt(tmp_path):
    """Same bytes except final_reread.value, both signed by the synthetic demo keys."""
    vec = json.loads((ROOT / "tests/vectors/review_receipt.json").read_text(encoding="utf-8"))
    base = copy.deepcopy(vec["receipt"])
    base["referee"] = {"pubkey": REFEREE.pubkey_hex, "key_id": REFEREE.key_id, "key_kind": base["referee"]["key_kind"]}
    base["players"][0]["pubkey"] = PLAYER.pubkey_hex
    base["review"]["checks"]["final_reread"].pop("value", None)
    allow = copy.deepcopy(vec["allowlist"])
    allow["referees"].append({"id": "demo-official", "pubkey": REFEREE.pubkey_hex, "key_id": REFEREE.key_id, "status": "active"})
    path = tmp_path / "allow.json"
    path.write_text(json.dumps(allow), encoding="utf-8")
    plain = _resign(base)
    valued = copy.deepcopy(base)
    valued["review"]["checks"]["final_reread"]["value"] = {"away": 99, "home": 0, "quarter": 4}
    valued = _resign(valued)
    a = _verdict(plain, REFEREE.pubkey_hex, str(path))
    b = _verdict(valued, REFEREE.pubkey_hex, str(path))
    assert a == b
    assert a[0] == "VERIFIED"
    assert "value" not in plain["review"]["checks"]["final_reread"]
    assert valued["review"]["checks"]["final_reread"]["value"]["away"] == 99


@pytest.mark.parametrize("path", sorted((ROOT / "tests/vectors/viewer").glob("*.json")), ids=lambda p: p.stem)
def test_viewer_fixtures_match_with_and_without_value(path, tmp_path):
    fx = json.loads(path.read_text(encoding="utf-8"))
    assert fx["note"].startswith("SYNTHETIC")
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps(fx["allowlist"]), encoding="utf-8")
    plain = copy.deepcopy(fx["receipt"])
    plain["review"]["checks"]["final_reread"].pop("value", None)
    valued = copy.deepcopy(plain)
    valued["review"]["checks"]["final_reread"]["value"] = {"away": 7, "home": 14}
    left = _verdict(_resign(plain), REFEREE.pubkey_hex, str(allow))
    right = _verdict(_resign(valued), REFEREE.pubkey_hex, str(allow))
    assert left == right
    # The tampered fixture's stored FAIL is a broken signature. Re-signing that body is a different receipt.
    if path.stem != "fail-tampered-after-signing":
        assert left[0] == fx["expect"]


def test_schema_accepts_the_optional_value_and_the_registry_omits_the_sidecars():
    js = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schema/receipt.v0.json").read_text(encoding="utf-8"))
    rec, _ = _load("tests/vectors/review_receipt.json")
    rec = copy.deepcopy(rec)
    rec["review"]["checks"]["final_reread"]["value"] = {"away": 7, "home": 14, "quarter": 4}
    js.validate(rec, schema)
    receipt_text = (ROOT / "schema/receipt.v0.json").read_text(encoding="utf-8")
    compat = json.loads((ROOT / "schema/contracts/compat.json").read_text(encoding="utf-8"))
    meta = compat["bundle_files"]["meta"]
    for name in ("ai_proposals.jsonl", "ai_outcomes.jsonl", "consolidation.json", "ai_stance.json"):
        assert name not in meta
        assert name not in receipt_text
    assert "qorcap-ai-proposal" not in receipt_text


def test_docs_say_the_verdict_file_is_not_a_training_label():
    text = (ROOT / "docs/REFEREE_AI.md").read_text(encoding="utf-8")
    assert "referee_verdict.json" in text
    assert "never a training label" in text
    assert "review.checks" in text
    assert "final_reread" in text


def test_final_check_keeps_detail_and_adds_value_only_for_integer_scores():
    claimed = {"home_score": 14, "away_score": 7}
    menu = {"basis": "end_menu", "home": 14, "away": 7, "quarter": 4, "final_confirmed": True}
    row = _final_check({"final": menu, "last_read": None}, claimed)
    assert row["state"] == PASS
    assert row["detail"] == "end-of-game menu re-read by the referee matches the claimed final"
    assert row["value"] == {"away": 7, "home": 14, "quarter": 4}

    fallback = {**menu, "fallback": "end_menu_without_tail_scorebug"}
    row = _final_check({"final": fallback, "last_read": None}, claimed)
    assert row["state"] == PASS
    assert row["detail"].startswith("end-of-game menu re-read by the referee matches the claimed final (no Q4 ")
    assert row["value"]["quarter"] == 4

    unconfirmed = {**fallback, "final_confirmed": False}
    row = _final_check({"final": unconfirmed, "last_read": None}, claimed)
    assert row["state"] == PARTIAL_CHECK
    assert row["detail"] == ("end-of-game menu read with no Q4 scorebug in the tail; not confirmed "
                             "against a locked read")
    assert row["value"] == {"away": 7, "home": 14, "quarter": 4}

    partial_menu = {"basis": "end_menu", "home": 14, "away": 7, "final_confirmed": False}
    row = _final_check({"final": partial_menu, "last_read": None}, claimed)
    assert row["state"] == PARTIAL_CHECK
    assert row["detail"] == "end-of-game menu read but one-legal-step delta not corroborated"
    assert "quarter" not in row["value"]

    scorebug = {"basis": "clock_zero", "home": 14, "away": 7}
    row = _final_check({"final": scorebug, "last_read": None}, claimed)
    assert row["state"] == PARTIAL_CHECK
    assert row["detail"] == "scorebug final (clock_zero) only; end-of-game menu not read"

    last = {"home": 14, "away": 7, "quarter": 2}
    row = _final_check({"final": None, "last_read": last}, claimed)
    assert row["state"] == PARTIAL_CHECK
    assert row["detail"] == "last scorebug read only; no final detected in the recorded tail"
    assert row["value"] == {"away": 7, "home": 14, "quarter": 2}

    mismatch = _final_check({"final": menu, "last_read": None}, {"home_score": 21, "away_score": 7})
    assert mismatch["state"] == "fail"
    assert mismatch["detail"] == "referee read HOME 14 - AWAY 7 but the laptop claimed HOME 21 - AWAY 7"
    assert mismatch["value"] == {"away": 7, "home": 14, "quarter": 4}

    empty = _final_check({"final": None, "last_read": None}, claimed)
    assert empty == {"state": "fail", "detail": "referee could not read any score from the recorded tail"}
    assert "value" not in empty

    fuzzy = _final_check({"final": {"basis": "end_menu", "home": 14.0, "away": 7, "final_confirmed": True},
                          "last_read": None}, {"home_score": 14.0, "away_score": 7})
    assert fuzzy["state"] == PASS
    assert "value" not in fuzzy
    assert fuzzy["detail"] == "end-of-game menu re-read by the referee matches the claimed final"
