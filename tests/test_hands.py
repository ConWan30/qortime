"""Show Your Hands (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026). SYNTHETIC inputs only."""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import pytest

from qor_receipt.hands import press_to_pixel as P
from qor_receipt.hands import sidecar as S
from qor_receipt.hands.inputs import EMPTY_CHUNK, make_chunks, verify_inputs
from qor_receipt.hands.synthetic import SYNTHETIC, human_session, receipt_for
from qor_receipt.hands.track import build_track, buttons

ROOT = Path(__file__).resolve().parents[1]
STATUSES = ["VERIFIED", "PARTIAL", "SELF_ATTESTED", "FAIL"]


@pytest.fixture
def human():
    recs, presses = human_session()
    return recs, presses, make_chunks(recs), receipt_for(recs)


def clip(presses, lag=90, start=100_000, drop=(), extra=(), stimuli=()):
    acts = [p + lag for i, p in enumerate(presses) if i not in drop] + list(extra)
    return {"id": "c0", "start_mono_ms": start, "duration_s": 40, "actions_ms": sorted(acts), "stimuli_ms": list(stimuli)}


def test_fixtures_are_labelled_synthetic():
    assert SYNTHETIC is True and "SYNTHETIC" in receipt_for(human_session()[0])["match"]["match_id"]


# ---- integrity ---------------------------------------------------------------------------------------------
def test_inputs_verify_against_receipt(human):
    recs, _, chunks, receipt = human
    chk = verify_inputs(recs, chunks, receipt)
    assert chk.ok and chk.problems == [] and len(chk.records) == len(recs)
    assert EMPTY_CHUNK == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def test_tampered_record_is_refused(human):
    recs, presses, chunks, receipt = human
    bad = copy.deepcopy(recs); bad[123]["b"] = 1
    doc = S.build(receipt, bad, chunks, [clip(presses)])
    assert not doc["inputs_verified"] and doc["clips"] == [] and doc["cap"] is None and "sha256 mismatch" in doc["problems"][0]


def test_broken_chain_and_uncommitted_chunk(human):
    recs, _, chunks, receipt = human
    c2 = copy.deepcopy(chunks); c2[3]["prev"] = "00" * 32
    assert any("prev link" in p for p in verify_inputs(recs, c2, receipt).problems)
    r2 = copy.deepcopy(receipt); r2["checkpoints"]["away"][2]["input_hash"] = "11" * 32
    probs = verify_inputs(recs, chunks, r2).problems
    assert any("not committed" in p for p in probs) and any("no matching chunk" in p for p in probs)
    assert not verify_inputs(recs, chunks, {"checkpoints": {}}).ok
    assert not verify_inputs(recs[:-5], chunks, receipt).ok
    assert verify_inputs([], [], receipt).problems == ["no_inputs"]


# ---- overlay track -----------------------------------------------------------------------------------------
def test_track_resamples_to_30fps(human):
    recs, presses, _, _ = human
    tr = build_track(recs, presses[0] - 100, duration_s=1.0)
    assert len(tr) == 30 and tr[0]["buttons"] == [] and tr[3]["buttons"] == ["CROSS"] and tr[0]["ls"] == [0, 0]
    assert buttons(0b1000_0000_0000_0011) == ["CROSS", "CIRCLE", "RIGHT"]


# ---- press-to-pixel ----------------------------------------------------------------------------------------
def test_legit_human_clip_has_no_flags(human):
    recs, presses, chunks, receipt = human
    doc = S.build(receipt, recs, chunks, [clip(presses)])
    c = doc["clips"][0]
    assert doc["inputs_verified"] and c["scorable"] and c["flags"] == [] and doc["cap"] is None
    assert abs(c["lag_ms"] - 90) <= 4 and c["press_interval_cv_permille"] > 200 and len(c["track"]) == 1200


def test_actions_with_no_press_are_flagged(human):
    recs, presses, chunks, receipt = human
    ghosts = [presses[5] + 300, presses[12] + 250, presses[20] + 400]
    # ghost actions sit well after the press window only if no press precedes them within lag+700 ms
    far = [p for p in ghosts if all(not (0 <= p - q <= 790) for q in presses)]
    doc = S.build(receipt, recs, chunks, [clip(presses, extra=far or [presses[-1] + 5000, presses[-1] + 6000])])
    kinds = [f["kind"] for f in doc["clips"][0]["flags"]]
    assert kinds.count("no_press") >= 2 and doc["cap"] == "PARTIAL"


def test_sub_human_reaction_flagged(human):
    recs, presses, chunks, receipt = human
    doc = S.build(receipt, recs, chunks, [clip(presses, stimuli=[presses[3] - 40, presses[9] - 60, presses[15] - 250])])
    tf = [f for f in doc["clips"][0]["flags"] if f["kind"] == "too_fast"]
    assert [round(f["reaction_ms"] / 10) for f in tf] == [4, 6] and doc["cap"] == "PARTIAL"


def test_machine_regular_timing_flagged():
    recs, presses = human_session(regular=True)
    c = P.score_clip(recs, [p + 90 for p in presses])
    assert c["scorable"] and c["press_interval_cv_permille"] < P.MAX_CV * 1000 and [f["kind"] for f in c["flags"]] == ["machine_regular"]
    assert S.decide_cap([c]) is None   # one flag alone never caps


def test_unscorable_clips_never_count(human):
    recs, presses, chunks, receipt = human
    few = S.build(receipt, recs, chunks, [{"id": "few", "start_mono_ms": 100_000, "actions_ms": [presses[0] + 90, 999_999]}])
    assert few["clips"][0]["why"] == "too_few_anchors" and few["cap"] is None
    jitter = [p + (40 if i % 2 else 300) for i, p in enumerate(presses)]
    c = P.score_clip(recs, jitter)
    assert not c["scorable"] and c["why"] in ("alignment_residual_too_large", "implausible_lag")
    late = P.score_clip(recs, [p + 450 for p in presses])
    assert late["why"] == "implausible_lag" and late["flags"] == []


# ---- lower-only ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,cap", list(itertools.product(STATUSES, [None, "PARTIAL"])))
def test_apply_cap_lower_only(status, cap):
    assert S.apply_cap(status, cap) == ("PARTIAL" if status == "VERIFIED" and cap else status)


@pytest.mark.parametrize("cap", ["FAIL", "SELF_ATTESTED", "VERIFIED"])
def test_other_caps_refused(cap):
    with pytest.raises(ValueError):
        S.apply_cap("VERIFIED", cap)


def test_apply_to_result_copy_and_unverified_ignored(human):
    recs, presses, chunks, receipt = human
    doc = S.build(receipt, recs, chunks, [clip(presses, stimuli=[presses[3] - 40, presses[9] - 60])])
    v = {"status": "VERIFIED", "partial": []}
    a = S.apply_to_result(v, doc)
    assert a["status"] == "PARTIAL" and S.CAP_CODE in a["partial"] and v == {"status": "VERIFIED", "partial": []}
    forged = {**doc, "inputs_verified": False}
    assert S.apply_to_result(v, forged)["status"] == "VERIFIED"
    assert S.apply_to_result(v, None)["status"] == "VERIFIED"


# ---- privacy ---------------------------------------------------------------------------------------------------
def test_sidecar_never_carries_device_path(human):
    recs, presses, chunks, receipt = human
    receipt["signals"] = {"input": {"device_instance_path": "HID\\VID_054C&PID_0DF2\\7&abc&0&0000"}}
    doc = S.build(receipt, recs, chunks, [clip(presses)])
    text = json.dumps(doc)
    for bad in ("HID\\", "VID_", "PID_", "device_instance_path", "054C"):
        assert bad not in text
    dirty = S.sanitize({"device_instance_path": "x", "serial": "y", "gamertag": "z", "pad": "USB\\VID_054C", "provider": 1, "ok": [1]})
    assert dirty == {"pad": "[removed]", "provider": 1, "ok": [1]}


def test_not_wired_into_verifier():
    # The page shows a signed hands.json through verify/hands-view.js + the lower-only guard; the verifiers never import it.
    for p in [ROOT / "qor_receipt" / "receipt.py", ROOT / "verify" / "verify.js", ROOT / "verify" / "viewer-ui.js"]:
        assert "hands" not in p.read_text(encoding="utf-8").lower().replace("handshake", "")
