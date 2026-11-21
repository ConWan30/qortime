"""Muse Spark score cross-check: lower-only (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026). API mocked."""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import pytest

from qor_receipt.ai_ruling import score_crosscheck as X
from qor_receipt.ai_ruling.muse import MuseError
from qor_receipt.ai_ruling.signing import sign_ruling
from qor_receipt.keys import SoftwareKey
from qor_receipt.receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
MATCH = ROOT / "site" / "matches" / "170123"
RECEIPT = json.loads((MATCH / "receipt.json").read_text(encoding="utf-8"))
PUB = RECEIPT["referee"]["pubkey"]
STATUSES = ["VERIFIED", "PARTIAL", "SELF_ATTESTED", "FAIL"]
SECRET = "sk-test-NOT-A-REAL-KEY-55555"


def answer(away, home, confident=True):
    return 200, {"choices": [{"message": {"content": json.dumps({"away_score": away, "home_score": home, "quarter": 4, "clock": None,
                                                                   "confident": confident})}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 500, "completion_tokens": 300}}


class Seq:
    def __init__(self, items):
        self.items, self.calls = list(items), []

    def __call__(self, url, key, body, timeout):
        self.calls.append((url, key, body))
        it = self.items.pop(0)
        if isinstance(it, Exception):
            raise it
        return it


def run(answers, locks, **kw):
    t = Seq(answers)
    sc = X.crosscheck(RECEIPT, locks, reader=X.MuseScoreReader(api_key=SECRET, transport=t), crop=lambda b: b"crop:" + b, **kw)
    return sc, t


def lock(i, away, home, n=2):
    return {"id": f"L{i}", "expected": {"away": away, "home": home}, "frames": [b"f%d_%d" % (i, k) for k in range(n)]}


# ---- lower-only ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,cap", itertools.product(STATUSES, [None, "PARTIAL"]))
def test_apply_cap_lower_only(status, cap):
    assert X.apply_cap(status, cap) == ("PARTIAL" if status == "VERIFIED" and cap else status)


@pytest.mark.parametrize("cap", ["FAIL", "SELF_ATTESTED", "VERIFIED"])
def test_other_caps_refused(cap):
    with pytest.raises(ValueError):
        X.apply_cap("VERIFIED", cap)


def test_agreement_keeps_status():
    sc, t = run([answer(3, 0), answer(3, 0), answer(10, 0), answer(10, 0)], [lock(1, 3, 0), lock(2, 10, 0)])
    assert sc["cap"] is None and sc["agree"] == 2 and sc["disagree"] == 0 and "agrees with 2 of 2" in sc["summary"]
    v = verify_receipt(RECEIPT, referee_pub=PUB)
    assert X.apply_to_result(v, sc)["status"] == "VERIFIED"
    assert sc["affects_verdict"] == "lower_only" and sc["max_cap"] == "PARTIAL" and sc["reads"] == 4 and sc["cost_micro_usd"] > 0


def test_disagreement_lowers_verified_only():
    sc, _ = run([answer(3, 0), answer(3, 0), answer(17, 0), answer(17, 0)], [lock(1, 3, 0), lock(2, 10, 0)])
    assert sc["cap"] == "PARTIAL" and sc["disagree"] == 1
    v = verify_receipt(RECEIPT, referee_pub=PUB)
    before = copy.deepcopy(v)
    a = X.apply_to_result(v, sc)
    assert a["status"] == "PARTIAL" and X.CAP_CODE in a["partial"] and v == before
    for st in ["PARTIAL", "SELF_ATTESTED", "FAIL"]:
        assert X.apply_to_result({**v, "status": st}, sc)["status"] == st


def test_mixed_or_unsure_reads_are_unchecked():
    sc, _ = run([answer(3, 0), answer(4, 0), answer(None, None, False), answer(9, 0, False)], [lock(1, 3, 0), lock(2, 10, 0)])
    assert [r["result"] for r in sc["locks"]] == ["unchecked", "unchecked"] and sc["cap"] is None
    assert sc["summary"].startswith("Muse unchecked")


def test_unconfident_wrong_read_never_counts():
    sc, _ = run([answer(99, 99, False)], [lock(1, 3, 0, n=1)])
    assert sc["cap"] is None and sc["locks"][0]["reads"][0]["result"] == "unsure"


# ---- Muse unavailable => exactly today's behaviour ----------------------------------------------------------
def test_api_errors_mean_unchecked_no_penalty():
    sc, _ = run([(500, None), MuseError("network:URLError"), (200, {"choices": []}), RuntimeError("x")], [lock(1, 3, 0), lock(2, 10, 0)])
    assert sc["cap"] is None and sc["checked"] == 0 and sc["summary"].startswith("Muse unchecked")
    v = verify_receipt(RECEIPT, referee_pub=PUB)
    a = X.apply_to_result(v, sc)
    assert a["status"] == v["status"] and a["partial"] == v["partial"] and a["muse_crosscheck"] == "unchecked"


def test_missing_key_makes_no_call(monkeypatch):
    for k in X.KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    t = Seq([])
    sc = X.crosscheck(RECEIPT, [lock(1, 3, 0)], reader=X.MuseScoreReader(transport=t), crop=lambda b: b)
    assert t.calls == [] and sc["cap"] is None and sc["reads"] == 0


def test_truncated_answer_is_unchecked():
    sc, _ = run([(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})], [lock(1, 3, 0, n=1)])
    assert sc["locks"][0]["result"] == "unchecked"


def test_max_reads_caps_cost():
    sc, t = run([answer(3, 0)] * 3, [lock(1, 3, 0), lock(2, 3, 0)], max_reads=3)
    assert len(t.calls) == 3 and sc["reads"] == 3 and sc["cost_micro_usd"] <= 3 * 4000


def test_no_expected_score_is_unchecked():
    sc, t = run([], [{"id": "x", "expected": {}, "frames": [b"a"]}])
    assert t.calls == [] and sc["locks"][0]["result"] == "unchecked"


# ---- request shape, key ------------------------------------------------------------------------------------
def test_request_shape_key_not_leaked():
    sc, t = run([answer(3, 0)], [lock(1, 3, 0, n=1)])
    url, key, body = t.calls[0]
    assert url.endswith("/chat/completions") and key == SECRET and body["model"] == "muse-spark-1.3"
    assert body["response_format"]["json_schema"]["name"] == "scorebug" and body["max_completion_tokens"] >= 1024
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert SECRET not in json.dumps(sc) and SECRET not in repr(X.MuseScoreReader(api_key=SECRET))
    with pytest.raises(ValueError):
        X.MuseScoreReader(api_key="x", model="muse-spark-1.3-contributor")


# ---- signed sidecar ----------------------------------------------------------------------------------------
def keys_for(k):
    return {"keys": [{"id": "t", "pubkey": k.pubkey_hex, "key_id": k.key_id, "key_kind": "qortime-ai-advisory", "status": "active"}]}


def test_signed_sidecar_checks_and_tamper():
    k = SoftwareKey()
    sc, _ = run([answer(3, 0), answer(17, 0)], [lock(1, 3, 0, n=1), lock(2, 10, 0, n=1)])
    d = sign_ruling(sc, k)
    assert X.check_crosscheck(d, RECEIPT, keys=keys_for(k)) == []
    flipped = copy.deepcopy(d); flipped["cap"] = None
    assert "bad_advisory_signature" in X.check_crosscheck(flipped, RECEIPT, keys=keys_for(k))
    assert "advisory_key_not_allowlisted" in X.check_crosscheck(d, RECEIPT)
    other = copy.deepcopy(RECEIPT); other["result"]["home_score"] = 3
    assert "receipt_mismatch" in X.check_crosscheck(d, other, keys=keys_for(k))
    bad = copy.deepcopy(sc); bad["cap"] = "FAIL"
    assert "bad_cap" in X.check_crosscheck(sign_ruling(bad, k), RECEIPT, keys=keys_for(k))
    incons = copy.deepcopy(sc); incons["locks"][1]["result"] = "agree"
    assert "cap_inconsistent" in X.check_crosscheck(sign_ruling(incons, k), RECEIPT, keys=keys_for(k))


def test_published_170123_crosscheck_agrees():
    d = json.loads((MATCH / "score_crosscheck.json").read_text(encoding="utf-8"))
    assert X.check_crosscheck(d, RECEIPT) == []
    assert d["cap"] is None and d["agree"] >= 1 and d["disagree"] == 0 and d["cost_micro_usd"] < 500_000
    v = verify_receipt(RECEIPT, referee_pub=PUB)
    assert X.apply_to_result(v, d)["status"] == "VERIFIED"


def test_crop_shape():
    np = pytest.importorskip("numpy"); cv2 = pytest.importorskip("cv2")
    img = np.zeros((720, 1280, 3), np.uint8); img[640:, :] = 200
    out = cv2.imdecode(np.frombuffer(X.crop_scorebug(cv2.imencode(".png", img)[1].tobytes()), np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[:2] == (160, 1360) and out.mean() > 150


def test_verifier_never_imports_crosscheck():
    for p in [ROOT / "qor_receipt" / "receipt.py", ROOT / "verify" / "verify.js"]:
        t = p.read_text(encoding="utf-8")
        assert "crosscheck" not in t and "score_crosscheck" not in t
