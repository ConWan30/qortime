"""Signed advisory ai_ruling.json (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026). No network."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from qor_receipt.ai_ruling import check_ruling, check_text, explain, sign_ruling
from qor_receipt.ai_ruling import signing as S
from qor_receipt.ai_ruling.muse import MuseClient
from qor_receipt.keys import SoftwareKey
from qor_receipt.receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
MATCH = ROOT / "site" / "matches" / "170123"
RECEIPT = json.loads((MATCH / "receipt.json").read_text(encoding="utf-8"))
PUB = RECEIPT["referee"]["pubkey"]


def _reply(body):
    return 200, {"model": "muse-spark-1.3", "choices": [{"message": {"content": "VERIFIED. All seven referee checks passed."}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def keys_for(k):
    return {"keys": [{"id": "t", "pubkey": k.pubkey_hex, "key_id": k.key_id, "key_kind": S.ADV_KIND, "status": "active"}]}


@pytest.fixture
def signed():
    k = SoftwareKey()
    v = verify_receipt(RECEIPT, referee_pub=PUB)
    res = explain(RECEIPT, v, client=MuseClient(api_key="x", transport=lambda *a: _reply(None)))
    return k, sign_ruling(res.sidecar, k)


def test_roundtrip(signed):
    k, d = signed
    assert check_ruling(d, RECEIPT, status="VERIFIED", keys=keys_for(k)) == []
    assert d["advisory_key"]["key_kind"] == "qortime-ai-advisory" and "not a referee key" in d["advisory_key"]["role"]
    assert "signature" not in d and "signatures" not in d and "advisory_signature" in d


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.__setitem__("explanation", d["explanation"] + " Also VERIFIED twice."), "bad_advisory_signature"),
    (lambda d: d["verdict_echo"].__setitem__("status", "PARTIAL"), "bad_advisory_signature"),
    (lambda d: d.__setitem__("affects_verdict", True), "affects_verdict_not_false"),
    (lambda d: d.__setitem__("signature", d.pop("advisory_signature")), "forbidden_field"),
    (lambda d: d["advisory_key"].__setitem__("key_kind", "qortime-official-review"), "key_kind"),
    (lambda d: d.__setitem__("receipt_jcs_sha256", "0" * 64), "receipt_mismatch"),
])
def test_tamper_hides(signed, mutate, code):
    k, d = signed
    d2 = copy.deepcopy(d)
    mutate(d2)
    assert code in check_ruling(d2, RECEIPT, status="VERIFIED", keys=keys_for(k))


def test_unlisted_key_stale_status_and_other_receipt(signed):
    k, d = signed
    assert "advisory_key_not_allowlisted" in check_ruling(d, RECEIPT, status="VERIFIED")  # random key, real allowlist
    assert "verdict_echo_stale" in check_ruling(d, RECEIPT, status="PARTIAL", keys=keys_for(k))
    other = copy.deepcopy(RECEIPT); other["result"]["home_score"] = 3
    assert "receipt_mismatch" in check_ruling(d, other, status="VERIFIED", keys=keys_for(k))
    assert check_ruling("x", RECEIPT) == ["not_an_object"]


def test_refuses_referee_keys(signed, monkeypatch):
    k, d = signed
    monkeypatch.setattr(S, "referee_lookup", lambda pub, **kw: {"id": "qortime-v1"} if pub.lower() == k.pubkey_hex else None)
    with pytest.raises(ValueError):
        sign_ruling({"schema": "qortime.ai_ruling.v0"}, k)
    assert "advisory_key_is_a_referee_key" in check_ruling(d, RECEIPT, status="VERIFIED", keys=keys_for(k))
    with pytest.raises(ValueError):
        sign_ruling({"signatures": {}}, SoftwareKey())


def test_receipts_referee_key_is_never_an_advisory_key():
    allow = json.loads((ROOT / "verify" / "trusted_referees.json").read_text(encoding="utf-8"))
    referee_pubs = {r["pubkey"].lower() for r in allow["referees"]} | {PUB.lower()}
    for row in S.load_keys()["keys"]:
        assert row["pubkey"].lower() not in referee_pubs and row["key_kind"] == "qortime-ai-advisory"


def test_published_170123_ruling_checks_out_and_changes_nothing():
    d = json.loads((MATCH / "ai_ruling.json").read_text(encoding="utf-8"))
    assert check_ruling(d, RECEIPT, status="VERIFIED") == []
    assert check_text(d["explanation"], "VERIFIED") == []
    assert d["verdict_echo"] == {"status": "VERIFIED", "errors": [], "partial": []}
    assert check_ruling(d, RECEIPT, status="PARTIAL") == ["verdict_echo_stale"]
    # the verifier never reads the sidecar: same result with or without it on disk
    assert verify_receipt(RECEIPT, referee_pub=PUB)["status"] == "VERIFIED"
    assert verify_receipt(RECEIPT)["status"] == "PARTIAL"


def test_advisory_keys_parity_with_browser_copy():
    py = S.load_keys()
    js = (ROOT / "verify" / "ai_advisory_keys.inc.js").read_text(encoding="utf-8")
    body = re.search(r"export const AI_ADVISORY_KEYS = (\{.*\});", js, re.S).group(1)
    assert json.loads(body) == py


def test_cli_signs_with_advisory_key(tmp_path, monkeypatch):
    from qor_receipt.ai_ruling import __main__ as cli
    from qor_receipt.keys import FileKey
    kp = tmp_path / "adv.key"
    k = FileKey(kp)
    monkeypatch.setattr(cli, "MuseClient", lambda model: MuseClient(api_key="x", model=model, transport=lambda *a: _reply(None)))
    rp = tmp_path / "receipt.json"; rp.write_bytes((MATCH / "receipt.json").read_bytes())
    assert cli.main([str(rp), "--pub", PUB, "--sign-key", str(kp)]) == 0
    d = json.loads((tmp_path / "ai_ruling.json").read_text(encoding="utf-8"))
    assert check_ruling(d, RECEIPT, status="VERIFIED", keys=keys_for(k)) == []
    assert rp.read_bytes() == (MATCH / "receipt.json").read_bytes()
