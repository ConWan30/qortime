"""Advisory Muse Spark receipt explainer (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026). No network: API mocked."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from qor_receipt.ai_ruling import SCHEMA, MuseClient, check_text, explain, facts_for
from qor_receipt.ai_ruling import muse as M
from qor_receipt.receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "site" / "matches" / "170123" / "receipt.json"
FAKE_KEY = "sk-test-NOT-A-REAL-KEY-1234567890"

GOOD = {
    "VERIFIED": "VERIFIED. The QorTime referee checked this Madden match: all seven checks passed. Not proven: who held the controller.",
    "PARTIAL": "PARTIAL. Signatures check out against a trusted referee, but the referee key was not pinned, so this is not a verified result.",
    "SELF_ATTESTED": "SELF-SIGNED. The signatures hold, but the referee key is not on QorTime's trusted list.",
    "FAIL": "FAIL. The evidence is broken: the claimed score does not match the signed data.",
}


def ok_reply(text, finish="stop"):
    return 200, {"model": "muse-spark-1.3", "choices": [{"message": {"content": text}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 700, "completion_tokens": 400, "completion_tokens_details": {"reasoning_tokens": 300}}}


class Transport:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, url, key, body, timeout):
        self.calls.append({"url": url, "key": key, "body": body})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result(body) if callable(self.result) else self.result


def client(result):
    t = Transport(result)
    return MuseClient(api_key=FAKE_KEY, transport=t), t


@pytest.fixture
def live():
    raw = LIVE.read_bytes()
    r = json.loads(raw)
    return raw, r, r["referee"]["pubkey"]


def run_cases(live):
    raw, r, pub = live
    return [(r, verify_receipt(r, referee_pub=pub)), (r, verify_receipt(r))]


# ---- advisory only: verdict and receipt never change -------------------------------------------------------------
@pytest.mark.parametrize("mode", ["ok", "http_500", "network", "empty", "guardrail", "no_key"])
def test_verdict_and_receipt_identical_whatever_the_ai_does(live, mode):
    raw, r, pub = live
    for pin in (pub, None):
        before_r = copy.deepcopy(r)
        v = verify_receipt(r, referee_pub=pin)
        v_before = copy.deepcopy(v)
        results = {"ok": ok_reply(GOOD[v["status"]]), "http_500": (500, None), "network": M.MuseError("network:URLError"),
                   "empty": ok_reply(""), "guardrail": ok_reply(GOOD[v["status"]] + " Hardware-verified."), "no_key": None}
        if mode == "no_key":
            c, t = MuseClient(api_key="", transport=Transport(ok_reply("x"))), None
        else:
            c, t = client(results[mode])
        res = explain(r, v, client=c, receipt_bytes=raw)
        assert res.status == v_before["status"]
        assert v == v_before, "explainer must not mutate the verifier output"
        assert r == before_r, "explainer must not mutate the receipt"
        assert verify_receipt(r, referee_pub=pin) == v_before, "verdict after explaining == verdict before"
        assert LIVE.read_bytes() == raw
        if mode == "ok":
            assert res.ok and res.explanation and res.sidecar["verdict_echo"]["status"] == v_before["status"]
        else:
            assert not res.ok and res.explanation is None and res.sidecar is None


def test_fail_closed_reasons(live):
    _, r, pub = live
    v = verify_receipt(r, referee_pub=pub)
    assert explain(r, v, client=client((503, None))[0]).reason == "api_error:http_503"
    assert explain(r, v, client=client(M.MuseError("network:TimeoutError"))[0]).reason == "api_error:network:TimeoutError"
    assert explain(r, v, client=client(RuntimeError("boom"))[0]).reason == "error:RuntimeError"
    assert explain(r, v, client=client(ok_reply("VERIFIED. ok", finish="length"))[0]).reason.startswith("api_error:truncated")
    assert explain(r, v, client=client((200, {"choices": []}))[0]).reason.startswith("api_error:malformed_response")
    assert explain(r, v, client=client(ok_reply("   "))[0]).problems == ["empty"]
    assert explain(r, {"status": "MAYBE"}, client=client(ok_reply("x"))[0]).reason == "unknown_status"


def test_missing_key_makes_no_call(live, monkeypatch):
    _, r, pub = live
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    t = Transport(ok_reply(GOOD["VERIFIED"]))
    res = explain(r, verify_receipt(r, referee_pub=pub), client=MuseClient(transport=t))
    assert res.reason == "api_key_missing" and t.calls == []


# ---- key handling ------------------------------------------------------------------------------------------------
def test_key_from_env_never_leaks(live, monkeypatch):
    _, r, pub = live
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    t = Transport(ok_reply(GOOD["VERIFIED"]))
    c = MuseClient(transport=t)
    assert FAKE_KEY not in repr(c)
    res = explain(r, verify_receipt(r, referee_pub=pub), client=c)
    assert res.ok and t.calls[0]["key"] == FAKE_KEY
    assert FAKE_KEY not in json.dumps(res.sidecar) and FAKE_KEY not in json.dumps(t.calls[0]["body"])
    bad = explain(r, verify_receipt(r, referee_pub=pub), client=MuseClient(transport=Transport((401, None))))
    assert FAKE_KEY not in bad.reason


def test_key_env_fallback(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "k2")
    assert M.api_key_from_env() == "k2"
    monkeypatch.setenv("META_API_KEY", "k1")
    assert M.api_key_from_env() == "k1"


def test_no_hardcoded_key_in_package():
    for p in (ROOT / "qor_receipt" / "ai_ruling").glob("*.py"):
        s = p.read_text(encoding="utf-8")
        assert "Bearer sk" not in s and "api_key=\"" not in s


def test_request_shape():
    c, t = client(ok_reply(GOOD["VERIFIED"]))
    c.chat("sys", "user")
    b = t.calls[0]["body"]
    assert t.calls[0]["url"] == "https://api.meta.ai/v1/chat/completions"
    assert b["model"] == "muse-spark-1.3" and b["max_completion_tokens"] >= 1024
    with pytest.raises(ValueError):
        MuseClient(api_key="x", model="muse-spark-1.3-contributor")


# ---- guardrail ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", list(GOOD))
def test_good_texts_pass(status):
    assert check_text(GOOD[status], status) == []


@pytest.mark.parametrize("text,status,code", [
    ("VERIFIED. The device key is hardware-verified.", "VERIFIED", "hardware_claim"),
    ("VERIFIED. This match was attested by the referee.", "VERIFIED", "word_attested"),
    ("SELF_ATTESTED. Keys hold.", "SELF_ATTESTED", "word_attested"),
    ("VERIFIED. The key is TPM attested.", "VERIFIED", "word_attested"),
    ("PARTIAL. Actually this should be VERIFIED.", "PARTIAL", "status_contradiction:VERIFIED"),
    ("The referee checked the match.", "VERIFIED", "status_not_stated"),
    ("PARTIAL. The match is verified apart from the pin.", "PARTIAL", "claims_verified"),
    ("VERIFIED. Coaching note: improve your red-zone defense.", "VERIFIED", "coaching"),
    ("VERIFIED. Here are some tips for next game.", "VERIFIED", "coaching"),
    ("VERIFIED. This match is cheat-free.", "VERIFIED", "overclaim"),
    ("VERIFIED. The player's identity is confirmed.", "VERIFIED", "identity_claim"),
    ("FAIL. " + "x" * 2000, "FAIL", "too_long"),
])
def test_guardrail_blocks(text, status, code):
    assert code in check_text(text, status)


def test_guardrail_allows_negated_verified():
    assert check_text("PARTIAL. It is not verified because the key was not pinned.", "PARTIAL") == []


def test_guardrail_hit_hides_explanation(live):
    _, r, pub = live
    v = verify_receipt(r, referee_pub=pub)
    res = explain(r, v, client=client(ok_reply("VERIFIED. The device is hardware-verified and attested."))[0])
    assert not res.ok and res.explanation is None and res.sidecar is None and res.reason == "guardrail"


# ---- facts: allowlisted, no PII, no 'attested' -------------------------------------------------------------------
def test_facts_have_no_keys_ids_or_device_paths(live):
    _, r, pub = live
    v = verify_receipt(r, referee_pub=pub)
    f = json.dumps(facts_for(r, v))
    assert r["referee"]["pubkey"] not in f and r["players"][0]["pubkey"] not in f
    assert r["match"]["match_id"] not in f and "player_id" not in f and "local:player" not in f
    assert "HID#" not in f and "instance" not in f and "\\\\" not in f
    assert "attest" not in f.lower()
    assert json.loads(f)["status"] == "VERIFIED"


def test_device_status_capped_at_tpm_claimed():
    v = {"status": "VERIFIED", "device_key": {"status": "tpm_attested"}}
    assert facts_for({}, v)["device_key"]["status"] == "invalid"
    assert facts_for({}, {"status": "VERIFIED", "device_key": {"status": "tpm_claimed"}})["device_key"]["status"] == "tpm_claimed"
    assert facts_for({}, {"status": "SELF_ATTESTED"})["status"] == "SELF-SIGNED"


def test_prompt_sent_contains_status_and_facts_only(live):
    _, r, pub = live
    c, t = client(ok_reply(GOOD["PARTIAL"]))
    explain(r, verify_receipt(r), client=c)
    msgs = t.calls[0]["body"]["messages"]
    assert msgs[1]["content"].startswith("STATUS: PARTIAL\n")
    assert r["referee"]["pubkey"] not in msgs[1]["content"]


# ---- sidecar -----------------------------------------------------------------------------------------------------
def _no_floats(x):
    if isinstance(x, float):
        return False
    if isinstance(x, dict):
        return all(_no_floats(v) for v in x.values())
    if isinstance(x, list):
        return all(_no_floats(v) for v in x)
    return True


def test_sidecar_shape(live):
    raw, r, pub = live
    v = verify_receipt(r, referee_pub=pub)
    res = explain(r, v, client=client(ok_reply(GOOD["VERIFIED"]))[0], receipt_bytes=raw)
    s = res.sidecar
    assert s["schema"] == SCHEMA and s["affects_verdict"] is False and "ADVISORY" in s["label"]
    assert s["receipt_sha256"] == hashlib.sha256(raw).hexdigest()
    assert s["verdict_echo"] == {"status": "VERIFIED", "errors": [], "partial": []}
    assert "Hackathon" in s["built_during"] and _no_floats(s)
    assert "attest" not in json.dumps(s).lower()


def test_cli_fail_closed_writes_nothing(tmp_path, monkeypatch, capsys):
    from qor_receipt.ai_ruling.__main__ import main
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    rp = tmp_path / "receipt.json"
    rp.write_bytes(LIVE.read_bytes())
    assert main([str(rp)]) == 0
    out = capsys.readouterr().out
    assert "verdict (verifier): PARTIAL" in out and "AI explanation: none (api_key_missing)" in out
    assert not (tmp_path / "ai_ruling.json").exists()
    assert rp.read_bytes() == LIVE.read_bytes()


# ---- isolation: the verdict path never imports the AI layer ------------------------------------------------------
def test_verdict_path_does_not_import_ai_ruling():
    # The browser's advisory panel (ai-ruling-view.js) is display-only; the verdict code must not know it exists.
    verdict_js = [ROOT / "verify" / n for n in ("verify.js", "viewer-ui.js", "receipt-view.js", "auto-pin.js", "frame-bind-status.js")]
    for p in [ROOT / "qor_receipt" / "receipt.py", ROOT / "qor_receipt" / "cli.py", *verdict_js]:
        t = p.read_text(encoding="utf-8")
        assert "ai_ruling" not in t and "ai-ruling" not in t and "advisory_signature" not in t, p
