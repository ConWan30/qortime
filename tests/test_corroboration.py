"""Two-sided corroboration: synthetic paired bundles, and the verdict stays put."""
from __future__ import annotations

import copy
import json

from qor_receipt import receipt as R
from qor_receipt.demo import run_match
from qor_receipt.keys import SoftwareKey
from qor_receipt.review import bundle as B
from qor_receipt.review.corroboration import (
    SCORE_EVENT_TOLERANCE_MS,
    accept_pairing_token,
    build_corroboration,
    side_from_receipt,
)
from qor_receipt.review.engine import Referee, Submission
from qor_receipt.review.policy import PAIRING_TYPE, REVIEW_TYPE


def _events(shift=0, away=7):
    return [
        {"t_ms": 1_000 + shift, "home": 0, "away": 0, "quarter": 1},
        {"t_ms": 50_000 + shift, "home": 7, "away": 0, "quarter": 1},
        {"t_ms": 120_000 + shift, "home": 14, "away": away, "quarter": 4},
    ]


def _challenge(role, nonce, hid):
    return {"id": nonce[:8], "role": role, "phase": "kickoff", "nonce": nonce,
            "pattern": ["OPTIONS", "CROSS", "CIRCLE", "SQUARE", "TRIANGLE"],
            "issued_ms": 1_000, "deadline_ms": 20_000,
            "result": {"ok": True, "reason": "ok", "hid_sha256": hid, "pause_press_ms": 1_500, "done_ms": 2_000}}


def _signed_receipt(role, device, capture, *, final=(14, 7), events=None, challenges=None, head="ab" * 32):
    home, away = final
    events = _events() if events is None else events
    challenges = challenges if challenges is not None else [_challenge(role, "11" * 16, "aa" * 32)]
    rec = R.build(
        match={"match_id": "pair-test", "game": "madden27", "platform": "ps5",
               "scorebug_profile": "madden27_standard", "mode": "single", "started_ms": 1_000, "ended_ms": 130_000},
        result={"home_score": home, "away_score": away, "quarter": 4, "final_t_ms": 130_000,
                "winner": R.winner_for(home, away), "method": "end_menu_read", "final_basis": "end_menu",
                "final_confirmed": True},
        players=[{"role": role, "player_id": f"local:{role}", "pubkey": device.pubkey_hex, "key_kind": "software"}],
        referee={"pubkey": capture.pubkey_hex, "key_id": capture.key_id, "key_kind": "local-self-attested"},
        challenges=challenges,
        checkpoints={role: [{"seq": 0, "t_ms": 1_000, "frames_hash": "cd" * 32, "input_hash": "ef" * 32,
                             "prev": "0" * 64, "hash": head, "referee_ts_ms": 1_030, "referee_sig": "0" * 128}]},
        agreement={"timelines_agree": None, "mismatches": []},
        signals={"motion_permille": {role: None}, "score_timeline": {"events": events}},
    )
    return R.sign_player(R.sign_referee(rec, capture), role, device)


def _side(role, **kw):
    device, capture = SoftwareKey(), SoftwareKey()
    rec = _signed_receipt(role, device, capture, **kw)
    side = side_from_receipt(rec, own_checks="pass")
    assert side is not None, role
    return side, rec, device


def test_agree_within_tolerance():
    home, _, _ = _side("home", head="ab" * 32, challenges=[_challenge("home", "11" * 16, "aa" * 32)])
    away, _, _ = _side("away", head="cd" * 32, events=_events(SCORE_EVENT_TOLERANCE_MS),
                       challenges=[_challenge("away", "22" * 16, "bb" * 32)])
    out = build_corroboration(home, away, home_present=True, away_present=True)
    assert out["status"] == "corroborated", out["reasons"]
    assert out["reasons"] == []
    assert out["affects_verdict"] is False
    assert out["tolerance_ms"] == SCORE_EVENT_TOLERANCE_MS


def test_score_mismatch_is_disagreed():
    home, _, _ = _side("home", challenges=[_challenge("home", "11" * 16, "aa" * 32)])
    away, _, _ = _side("away", final=(14, 3), events=_events(away=3), head="cd" * 32,
                       challenges=[_challenge("away", "22" * 16, "bb" * 32)])
    out = build_corroboration(home, away, home_present=True, away_present=True)
    assert out["status"] == "disagreed"
    assert "final_score_mismatch" in out["reasons"]


def test_timing_skew_beyond_tolerance_is_disagreed():
    home, _, _ = _side("home", challenges=[_challenge("home", "11" * 16, "aa" * 32)])
    away, _, _ = _side("away", head="cd" * 32, events=_events(SCORE_EVENT_TOLERANCE_MS + 1),
                       challenges=[_challenge("away", "22" * 16, "bb" * 32)])
    out = build_corroboration(home, away, home_present=True, away_present=True)
    assert out["status"] == "disagreed"
    assert any("time_differs" in r for r in out["reasons"])


def test_replayed_bundle_submitted_as_own_is_not_independent():
    """The away player re-signs a copy of the home evidence and submits it as their own recording."""
    home, home_rec, _ = _side("home", head="ab" * 32, challenges=[_challenge("home", "11" * 16, "aa" * 32)])
    away_device, capture = SoftwareKey(), SoftwareKey()
    copied = copy.deepcopy(home_rec)
    # New key, other role, same timeline, same challenges, same checkpoint head.
    copied["players"] = [{"role": "away", "player_id": "local:away", "pubkey": away_device.pubkey_hex,
                          "key_kind": "software"}]
    copied["checkpoints"] = {"away": copy.deepcopy(home_rec["checkpoints"]["home"])}
    copied["challenges"][0]["role"] = "away"
    copied = R.sign_player(R.sign_referee(copied, capture), "away", away_device)
    away = side_from_receipt(copied, own_checks="pass")
    assert away is not None
    out = build_corroboration(home, away, home_present=True, away_present=True)
    assert out["status"] == "disagreed"
    assert "shared_challenge_nonce" in out["reasons"]
    assert "same_checkpoint_head" in out["reasons"]
    assert "copied_challenge_response" in out["reasons"]
    assert out["status"] != "corroborated"


def test_one_side_is_single_sided():
    home, _, _ = _side("home")
    out = build_corroboration(home, None, home_present=True, away_present=False)
    assert out["status"] == "single_sided"
    assert out["affects_verdict"] is False


def test_forged_pairing_id_is_not_accepted(tmp_path):
    from fastapi.testclient import TestClient

    from qor_receipt.referee_service.app import create_app

    ref = Referee(SoftwareKey(), workdir=tmp_path, core_factory=None)
    token = ref.issue_pairing()
    assert token["type"] == PAIRING_TYPE
    assert accept_pairing_token(token, ref.key.pubkey_hex)["pairing_id"] == token["pairing_id"]
    forged = dict(token, pairing_id="ab" * 16)
    assert accept_pairing_token(forged, ref.key.pubkey_hex) is None
    client_chosen = {"type": PAIRING_TYPE, "pairing_id": "cd" * 16, "nonce": "ef" * 16, "issued_ms": 1}
    assert accept_pairing_token(client_chosen, ref.key.pubkey_hex) is None
    issued = TestClient(create_app(ref)).post("/v1/pairings", json={"pairing_id": "11" * 16, "nonce": "22" * 16}).json()
    assert issued["pairing_id"] != "11" * 16 and issued["nonce"] != "22" * 16
    assert accept_pairing_token(issued, ref.key.pubkey_hex)["pairing_id"] == issued["pairing_id"]


def _bundle(role, receipt, token=None):
    files = {
        "bundle.json": json.dumps({"schema": "qorcap-bundle/v0", "role": role, "match_id": "pair-test"}).encode(),
        "receipt.json": json.dumps(receipt).encode(),
    }
    if token is not None:
        files["pairing.json"] = json.dumps(token).encode()
    return B.load(files)


def _sub(ref, sid, bundle):
    sub = Submission(id=sid, dir=ref.workdir / sid, mode="spot-check", created_ms=0, upload_token="t", bundle=bundle)
    (ref.workdir / sid).mkdir(parents=True, exist_ok=True)
    ref.subs[sid] = sub
    return sub


def test_referee_links_only_its_token_and_restamps_the_waiting_partner(tmp_path):
    ref = Referee(SoftwareKey(), workdir=tmp_path, core_factory=None)
    token = ref.issue_pairing()
    home_side_rec = _signed_receipt("home", SoftwareKey(), SoftwareKey(), head="ab" * 32,
                                    challenges=[_challenge("home", "11" * 16, "aa" * 32)])
    away_rec = _signed_receipt("away", SoftwareKey(), SoftwareKey(), head="cd" * 32, events=_events(1_000),
                               challenges=[_challenge("away", "22" * 16, "bb" * 32)])
    checks = {"video_bytes": {"state": "pass"}}
    home = _sub(ref, "home-sub", _bundle("home", home_side_rec, token))
    first = ref._corroboration(home, checks)
    assert first["status"] == "single_sided"
    home.corroboration = first
    home.review = {"type": REVIEW_TYPE, "checks": checks, "corroboration": first}
    home.receipt = {"review": {"corroboration": copy.deepcopy(first)}, "signatures": {"players": {}, "referee": None}}
    home.receipt = R.sign_referee(home.receipt, ref.key)
    home.state = "awaiting_player_signature"

    away = _sub(ref, "away-sub", _bundle("away", away_rec, token))
    second = ref._corroboration(away, checks)
    assert second["status"] == "corroborated", second["reasons"]
    away.corroboration = second
    ref._publish_pair(away, second)
    assert home.corroboration["status"] == "corroborated"
    assert home.receipt["review"]["corroboration"]["status"] == "corroborated"
    assert R.verify(ref.key.pubkey_hex, home.receipt["signatures"]["referee"]["sig"], R.signing_bytes(home.receipt))

    # A completed receipt is not rewritten. The live object still shows the disagreement.
    home.state = "complete"
    frozen = json.dumps(home.receipt["review"]["corroboration"])
    skewed = _signed_receipt("away", SoftwareKey(), SoftwareKey(), head="ef" * 32,
                             events=_events(SCORE_EVENT_TOLERANCE_MS + 50),
                             challenges=[_challenge("away", "33" * 16, "cc" * 32)])
    # Replace the away slot by using a fresh referee pairing state: drop the old away and bind the skew.
    ref.pairs[token["pairing_id"]]["roles"].pop("away")
    skew_sub = _sub(ref, "away-skew", _bundle("away", skewed, token))
    disagreed = ref._corroboration(skew_sub, checks)
    assert disagreed["status"] == "disagreed"
    ref._publish_pair(skew_sub, disagreed)
    assert home.corroboration["status"] == "disagreed"
    assert json.dumps(home.receipt["review"]["corroboration"]) == frozen


def test_corroboration_does_not_change_single_sided_verdict(ref):
    client, clock, key = ref
    rec, pkeys = run_match(client, clock, "single")
    before = R.verify_receipt(rec, referee_pub=key.pubkey_hex)

    def with_status(status, reasons):
        edited = copy.deepcopy(rec)
        edited["review"] = {"corroboration": {"schema": "qortime-corroboration/v0", "status": status,
                                              "reasons": reasons, "tolerance_ms": 5000, "affects_verdict": True}}
        edited = R.sign_referee(edited, key)
        for role, player in pkeys.items():
            edited = R.sign_player(edited, role, player)
        return R.verify_receipt(edited, referee_pub=key.pubkey_hex)

    single = with_status("single_sided", [])
    assert single["status"] == before["status"] == "SELF_ATTESTED"
    assert single["errors"] == before["errors"]
    assert single["partial"] == before["partial"]
    assert single["corroboration"]["status"] == "single_sided"
    assert single["corroboration"]["affects_verdict"] is False

    disagreed = with_status("disagreed", ["final_score_mismatch"])
    assert disagreed["status"] == before["status"]
    assert disagreed["errors"] == before["errors"]
    assert disagreed["partial"] == before["partial"]
    assert disagreed["corroboration"]["status"] == "disagreed"
    assert disagreed["corroboration"]["reasons"] == ["final_score_mismatch"]


def test_corroboration_never_upgrades_a_failing_receipt(ref):
    client, clock, key = ref
    rec, pkeys = run_match(client, clock, "single")
    edited = copy.deepcopy(rec)
    edited["result"]["winner"] = "away" if rec["result"]["winner"] != "away" else "home"
    edited["review"] = {"corroboration": {"status": "corroborated", "reasons": [], "affects_verdict": True}}
    edited = R.sign_referee(edited, key)
    for role, player in pkeys.items():
        edited = R.sign_player(edited, role, player)
    verdict = R.verify_receipt(edited, referee_pub=key.pubkey_hex)
    assert verdict["status"] == "FAIL"
    assert "winner_inconsistent" in verdict["errors"]
    assert verdict["corroboration"]["status"] == "corroborated"

    forged = copy.deepcopy(rec)
    forged["result"]["home_score"] = 99
    forged["review"] = {"corroboration": {"status": "corroborated", "reasons": []}}
    hidden = R.verify_receipt(forged, referee_pub=key.pubkey_hex)
    assert hidden["status"] == "FAIL"
    assert "bad_referee_signature" in hidden["errors"]
    assert "corroboration" not in hidden


def test_corroboration_is_not_a_review_check():
    referee = {"pubkey": "ab" * 32}
    review = {
        "type": REVIEW_TYPE,
        "capture_referee": {"pubkey": "cd" * 32},
        "checks": {name: {"state": "pass"} for name in
                   ("video_bytes", "final_reread", "capture_chain", "bundle_structure")},
        "corroboration": {"status": "disagreed", "reasons": ["final_score_mismatch"]},
    }
    _cap, errs, part = R._review_problems({"review": review, "referee": referee}, referee)
    assert errs == [] and part == []


def test_unsigned_timeline_cannot_corroborate():
    home, _, _ = _side("home")
    out = build_corroboration(home, None, home_present=True, away_present=True, extra_reasons=[])
    assert out["status"] == "disagreed"
    assert "unsigned_score_timeline" in out["reasons"]
