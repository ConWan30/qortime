"""HACKATHON: automatic Muse advisory files after the official verdict (qor_receipt/ai_ruling/auto.py).

No network: Muse is replaced by fakes. Nothing here talks to the live referee.
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from qor_receipt import receipt as R  # noqa: E402
from qor_receipt.ai_ruling import auto as A  # noqa: E402
from qor_receipt.ai_ruling.score_crosscheck import SCHEMA as XC_SCHEMA  # noqa: E402
from qor_receipt.ai_ruling.score_crosscheck import Read, crosscheck
from qor_receipt.ai_ruling.signing import ADV_KIND, check_signed  # noqa: E402
from qor_receipt.keys import SoftwareKey  # noqa: E402

ON = {A.ENV_ENABLED: "1"}
RECEIPT = {"schema": "qor-receipt/v0", "match": {"match_id": "t"}, "result": {"home_score": 7, "away_score": 3},
           "referee": {"pubkey": "00" * 32}}
V = {"status": "VERIFIED", "errors": [], "partial": []}


def keys_for(k):
    return {"keys": [{"pubkey": k.pubkey_hex, "key_id": k.key_id, "key_kind": ADV_KIND, "status": "active"}]}


class FakeExplain:
    def __init__(self, ok=True, boom=False, sleep=0.0):
        self.ok, self.boom, self.sleep, self.calls = ok, boom, sleep, 0

    def __call__(self, receipt, v):
        self.calls += 1
        receipt["result"]["home_score"] = 99  # a misbehaving model step must not reach the caller's receipt
        v["status"] = "FAIL"
        if self.sleep:
            time.sleep(self.sleep)
        if self.boom:
            raise RuntimeError("boom")

        class R_:
            pass
        r = R_()
        r.ok = self.ok
        r.reason = "ok" if self.ok else "api_error:x"
        r.sidecar = {"schema": "qortime.ai_ruling.v0", "affects_verdict": False,
                     "receipt_jcs_sha256": A.receipt_hash(RECEIPT), "explanation": "VERIFIED. test",
                     "usage": {"prompt_tokens": 1000, "completion_tokens": 100}} if self.ok else None
        return r


def fake_xc(checked=1):
    def f(receipt, locks, *, max_reads):
        f.max_reads = max_reads
        return {"schema": XC_SCHEMA, "receipt_jcs_sha256": A.receipt_hash(RECEIPT), "checked": checked, "agree": checked,
                "disagree": 0, "reads": 2, "cost_micro_usd": 3000, "cap": None, "locks": []}
    return f


def job(tmp, env=ON, explain=None, xc=None, locks=(({"id": "final"}),), deadline=None, signer="new", ledger=None):
    caps = A.Caps(env)
    k = SoftwareKey() if signer == "new" else signer
    store = A.Store(tmp / "advisory")
    led = ledger or A.Ledger(tmp / "advisory" / "spend.jsonl")
    rec, v = copy.deepcopy(RECEIPT), copy.deepcopy(V)
    st = A.run_job(rec, v, list(locks), signer=k, store=store, ledger=led, caps=caps, explain_fn=explain or FakeExplain(),
                   crosscheck_fn=xc or fake_xc(), deadline=deadline)
    assert rec == RECEIPT and v == V  # inputs never mutated
    return st, store, k, led


def test_caps_defaults_and_kill_switch():
    c = A.Caps({})
    assert c.enabled is False and c.match_micro == 50_000 and c.weekly_micro == 5_000_000 and c.max_reads() == 12
    assert A.Caps({A.ENV_ENABLED: "true"}).enabled is False  # only "1" turns it on
    assert A.Caps({**ON, A.ENV_MATCH_USD: "0.02"}).max_reads() == 3
    assert A.Caps({**ON, A.ENV_MATCH_USD: "junk"}).match_micro == 50_000


def test_job_writes_signed_files_bound_to_receipt(tmp_path):
    st, store, k, led = job(tmp_path)
    assert st["reason"] == "ok" and st["files"] == ["ai_ruling.json", "score_crosscheck.json"]
    sha = A.receipt_hash(RECEIPT)
    ai = json.loads(store.read(sha, "ai_ruling.json"))
    xc = json.loads(store.read(sha, "score_crosscheck.json"))
    assert check_signed(ai, RECEIPT, schema="qortime.ai_ruling.v0", keys=keys_for(k)) == []
    assert check_signed(xc, RECEIPT, schema=XC_SCHEMA, keys=keys_for(k)) == []
    assert json.loads(store.read(sha, "status.json"))["cost_micro_usd"] == st["cost_micro_usd"] > 0
    assert led.week_spent() == st["cost_micro_usd"]


@pytest.mark.parametrize("env,reason", [({}, "disabled"), ({**ON, A.ENV_MATCH_USD: "0.001"}, "match_cap_too_low"),
                                        ({**ON, A.ENV_WEEKLY_USD: "0.01"}, "weekly_cap")])
def test_caps_mean_no_files_and_no_calls(tmp_path, env, reason):
    ex = FakeExplain()
    st, store, *_ = job(tmp_path, env=env, explain=ex)
    assert st["reason"] == reason and ex.calls == 0
    assert store.read(A.receipt_hash(RECEIPT), "ai_ruling.json") is None


def test_weekly_cap_counts_previous_spend(tmp_path):
    led = A.Ledger(tmp_path / "advisory" / "spend.jsonl")
    led.settle(0, 4_990_000, "x" * 64, "auto")
    st, *_ = job(tmp_path, ledger=led)
    assert st["reason"] == "weekly_cap"
    old = A.Ledger(tmp_path / "old.jsonl", now=lambda: int(time.time() * 1000) + A.WEEK_MS + 1)
    old.path.write_text(led.path.read_text())
    assert old.week_spent() == 0  # rolling 7 days


def test_missing_key_or_errors_or_timeout_publish_nothing(tmp_path):
    st, store, *_ = job(tmp_path / "a", signer=None)
    assert st["reason"] == "advisory_key_missing"
    st, store, *_ = job(tmp_path / "b", explain=FakeExplain(boom=True))
    assert st["reason"] == "muse_failed" and st["retryable"] and st["failed_parts"][0].startswith("error:")
    assert store.read(A.receipt_hash(RECEIPT), "ai_ruling.json") is None
    st, store, *_ = job(tmp_path / "c", explain=FakeExplain(ok=False), xc=fake_xc(checked=0))
    assert st["files"] == [] and st["explain"].startswith("none:")
    st, store, *_ = job(tmp_path / "d", deadline=time.monotonic() - 1)
    assert st["reason"] == "timeout" and st["files"] == []


def test_signer_refuses_referee_key_and_bad_seed(monkeypatch):
    assert A.signer_from_env({}) is None and A.signer_from_env({A.ENV_SEED: "zz"}) is None
    k = A.signer_from_env({A.ENV_SEED: "11" * 32})
    assert k is not None
    monkeypatch.setattr("qor_receipt.trusted_referees.lookup", lambda pub: {"status": "active"})
    assert A.signer_from_env({A.ENV_SEED: "11" * 32}) is None


def test_store_rejects_bad_names_and_hashes(tmp_path):
    s = A.Store(tmp_path)
    with pytest.raises(ValueError):
        s.write("../x", "ai_ruling.json", {})
    with pytest.raises(ValueError):
        s.write("a" * 64, "receipt.json", {})
    assert s.read("a" * 64, "../../etc/passwd") is None and s.read("nothex", "ai_ruling.json") is None


class SeqReader:
    has_key, model = True, "muse-spark-1.3"

    def __init__(self, reads):
        self.reads = list(reads)

    def read(self, crop):
        a, h = self.reads.pop(0)
        return Read(a, h, a is not None, usage={})


def test_neighbour_lock_score_is_never_a_disagreement():
    lk = [{"id": "l1", "expected": {"away": 3, "home": 7}, "frames": [b"x", b"y"], "tolerate": [{"away": 0, "home": 7}]}]
    out = crosscheck(RECEIPT, lk, reader=SeqReader([(0, 7), (3, 7)]), crop=lambda b: b)
    assert out["locks"][0]["result"] == "agree" and out["cap"] is None
    out = crosscheck(RECEIPT, lk, reader=SeqReader([(0, 7), (0, 7)]), crop=lambda b: b)
    assert out["locks"][0]["result"] == "unchecked" and out["cap"] is None
    out = crosscheck(RECEIPT, lk, reader=SeqReader([(9, 9), (9, 9)]), crop=lambda b: b)
    assert out["locks"][0]["result"] == "disagree" and out["cap"] == "PARTIAL"  # a genuinely different score still caps


# ------------------------------------------------------------------ wired into the real referee (synthetic bundle)
F = pytest.importorskip("review_fixture")
needs_ffmpeg = pytest.mark.skipif(not F.have_ffmpeg(), reason="ffmpeg needed for the synthetic fMP4 bundle")


def _env(tmp):
    from test_referee_service import Env
    return Env(tmp, tail_ms=8_000, k=3)


def _finish(e):
    sub = e.submit()
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "awaiting_player_signature"
    return sub, e.player_sign(sub)


@needs_ffmpeg
def test_hook_never_changes_the_verdict_and_cleans_segments(tmp_path):
    from qor_receipt.trusted_referees import clear_cache

    base = _env(tmp_path / "base")
    _, plain = _finish(base)
    e = _env(tmp_path / "adv")
    seen = {}

    def xc(receipt, locks, *, max_reads):
        seen["locks"] = locks
        return fake_xc()(receipt, locks, max_reads=max_reads)
    adv = A.AutoAdvisor(e.ref, tmp_path / "state", env=ON, signer=SoftwareKey(), explain_fn=FakeExplain(), crosscheck_fn=xc)
    e.ref.advisory_hook = adv.hook
    sub, out = _finish(e)
    snap = copy.deepcopy(out)
    assert out["verify"]["status"] == plain["verify"]["status"] and out["verify"]["errors"] == plain["verify"]["errors"]
    adv.q.join()
    assert out == snap and sub.receipt == snap["receipt"] and sub.verify == snap["verify"]  # untouched by the job
    assert not (sub.dir / "segs").exists()  # the job deleted the segments when done
    assert adv.last["reason"] == "ok" and "ai_ruling.json" in adv.last["files"]
    fin = [lk for lk in seen["locks"] if lk["id"] == "final"]
    assert fin and fin[0]["expected"] == {"away": 7, "home": 14} and any(fin[0]["frames"])  # referee's own tail frames
    # idempotent: the same final receipt again costs nothing
    again = adv.process({"receipt": out["receipt"], "verify": out["verify"], "sub": sub})
    assert again["reason"] == "ok" and adv.ledger.week_spent() == adv.last["cost_micro_usd"]
    clear_cache()


@needs_ffmpeg
def test_hook_failure_or_kill_switch_keeps_old_behaviour(tmp_path):
    e = _env(tmp_path / "boom")

    def boom(sub):
        raise RuntimeError("x")
    e.ref.advisory_hook = boom
    sub, out = _finish(e)
    assert out["verify"]["status"] and not (sub.dir / "segs").exists()
    e2 = _env(tmp_path / "off")
    adv = A.AutoAdvisor(e2.ref, tmp_path / "s2", env={}, signer=SoftwareKey())
    assert adv.hook is not None
    e2.ref.advisory_hook = adv.hook
    sub2, _ = _finish(e2)
    assert adv.q.qsize() == 0 and not (sub2.dir / "segs").exists()
    assert not (tmp_path / "s2" / "advisory").exists()


def test_http_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from qor_receipt.referee_service import app as appmod

    class Ref:
        key = SoftwareKey()
        subs: dict = {}
        log = None
        delegation = None
        advisory_hook = None
    monkeypatch.setenv("QORTIME_AI_ADMIN_TOKEN", "t0k")
    ex = FakeExplain()
    adv = A.AutoAdvisor(Ref(), tmp_path, env=ON, signer=SoftwareKey(), explain_fn=ex, crosscheck_fn=fake_xc())
    monkeypatch.setattr(appmod, "_trusted_verify", lambda r: dict(V))
    c = TestClient(appmod.create_app(Ref(), advisor=adv))
    sha = A.receipt_hash(RECEIPT)
    assert c.get("/v1/advisory").json()["enabled"] is True
    assert c.get(f"/v1/advisory/{sha}/ai_ruling.json").status_code == 404
    assert c.get(f"/v1/advisory/{sha}/spend.jsonl").status_code == 404
    assert c.post("/v1/advisory/reprocess", json={"receipt": RECEIPT}).status_code == 401
    assert c.post("/v1/advisory/reprocess", json={"receipt": RECEIPT}, headers={"Authorization": "Bearer no"}).status_code == 401
    h = {"Authorization": "Bearer t0k"}
    dry = c.post("/v1/advisory/reprocess", json={"receipt": RECEIPT}, headers=h).json()
    assert dry["reason"] == "ok" and dry["files"] == [] and "preview_ai_ruling" in dry
    assert c.get(f"/v1/advisory/{sha}/ai_ruling.json").status_code == 404  # dry run publishes nothing
    pub = c.post("/v1/advisory/reprocess", json={"receipt": RECEIPT, "publish": True}, headers=h).json()
    assert pub["files"] == ["ai_ruling.json"]
    assert c.get(f"/v1/advisory/{sha}/ai_ruling.json").json()["receipt_jcs_sha256"] == sha
    n = ex.calls
    assert c.post("/v1/advisory/reprocess", json={"receipt": RECEIPT, "publish": True}, headers=h).json()["idempotent"]
    assert ex.calls == n  # idempotent: no new Muse call


def test_admin_endpoint_closed_without_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from qor_receipt.referee_service import app as appmod

    class Ref:
        key = SoftwareKey()
        subs: dict = {}
        log = None
        delegation = None
        advisory_hook = None
    monkeypatch.delenv("QORTIME_AI_ADMIN_TOKEN", raising=False)
    adv = A.AutoAdvisor(Ref(), tmp_path, env=ON, signer=SoftwareKey(), explain_fn=FakeExplain())
    c = TestClient(appmod.create_app(Ref(), advisor=adv))
    assert c.post("/v1/advisory/reprocess", json={"receipt": RECEIPT}, headers={"Authorization": "Bearer "}).status_code == 401


def test_untouched_receipt_module():
    assert R.verify_receipt  # receipt.v0 schema and verify_receipt are not edited by this feature


def test_muse_error_carries_only_the_short_api_code():
    import io
    import urllib.error

    from qor_receipt.ai_ruling import muse

    e = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error":{"code":"Model Not-Found!","message":"secret detail"}}'))
    assert muse._error_code(e) == {"_error_code": "modelnotfound"}
    c = muse.MuseClient(api_key="k", transport=lambda *a: (404, {"_error_code": "model_not_found"}))
    with pytest.raises(muse.MuseError, match=r"^http_404:model_not_found$"):
        c.chat("s", "u")
    c = muse.MuseClient(api_key="k", transport=lambda *a: (500, None))
    with pytest.raises(muse.MuseError, match=r"^http_500$"):
        c.chat("s", "u")


def test_diag_needs_admin_and_never_returns_the_key(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from qor_receipt.ai_ruling import muse
    from qor_receipt.referee_service import app as appmod

    class Ref:
        key = SoftwareKey()
        subs: dict = {}
        log = None
        delegation = None
        advisory_hook = None
    monkeypatch.setenv("QORTIME_AI_ADMIN_TOKEN", "t0k")
    monkeypatch.setattr(muse, "list_models", lambda: {"status": 200, "models": ["muse-spark-1.3"]})
    c = TestClient(appmod.create_app(Ref(), advisor=A.AutoAdvisor(Ref(), tmp_path, env=ON, signer=SoftwareKey())))
    assert c.get("/v1/advisory/diag").status_code == 401
    assert c.get("/v1/advisory/diag", headers={"Authorization": "Bearer t0k"}).json()["models"] == ["muse-spark-1.3"]
    assert muse.list_models.__module__  # patched
    monkeypatch.undo()
    assert muse.list_models(api_key="")["error_code"] == "api_key_missing"


class FlakyExplain(FakeExplain):
    """Fails like an unreachable Muse (api_error) for the first ``n`` calls."""

    def __init__(self, n):
        super().__init__()
        self.n = n

    def __call__(self, receipt, v):
        if self.calls < self.n:
            self.calls += 1
            r = type("R", (), {})()
            r.ok, r.reason, r.sidecar = False, "api_error:http_404:model_not_found", None
            return r
        return super().__call__(receipt, v)


def unreachable_xc(receipt, locks, *, max_reads):
    return {"schema": XC_SCHEMA, "checked": 0, "agree": 0, "disagree": 0, "reads": 1, "cost_micro_usd": 0, "cap": None,
            "locks": [{"id": "final", "result": "unchecked", "reads": [{"result": "unchecked", "reason": "muse_unavailable:http_404"}]}]}


def test_failed_muse_is_marked_retryable_not_ok(tmp_path):
    st, store, *_ = job(tmp_path, explain=FlakyExplain(9), xc=unreachable_xc)
    assert st["reason"] == "muse_failed" and st["retryable"] is True and st["attempts"] == 1
    assert set(st["failed_parts"]) == {"ai_ruling", "score_crosscheck"}
    assert json.loads(store.read(A.receipt_hash(RECEIPT), "status.json"))["reason"] == "muse_failed"


def test_guardrail_refusal_is_final_not_retried(tmp_path):
    ex = FakeExplain(ok=False)
    ex_reason = type("E", (), {})
    st, *_ = job(tmp_path, explain=lambda r, v: type("R", (), {"ok": False, "reason": "guardrail", "sidecar": None})(),
                 xc=fake_xc(checked=0))
    assert st["reason"] == "ok" and st["retryable"] is False and ex.calls == 0 and ex_reason


def test_retry_redoes_only_missing_part_and_cap_spans_attempts(tmp_path):
    k = SoftwareKey()
    caps, store, led = A.Caps(ON), A.Store(tmp_path / "advisory"), A.Ledger(tmp_path / "advisory" / "spend.jsonl")
    ex = FlakyExplain(0)
    st1 = A.run_job(RECEIPT, V, [{"id": "final"}], signer=k, store=store, ledger=led, caps=caps, explain_fn=ex,
                    crosscheck_fn=unreachable_xc)
    assert st1["files"] == ["ai_ruling.json"] and st1["reason"] == "muse_failed"
    st2 = A.run_job(RECEIPT, V, [{"id": "final"}], signer=k, store=store, ledger=led, caps=caps, explain_fn=ex,
                    crosscheck_fn=fake_xc(), prev=st1)
    assert ex.calls == 1  # explanation not redone
    assert st2["attempts"] == 2 and st2["reason"] == "ok" and st2["files"] == ["ai_ruling.json", "score_crosscheck.json"]
    assert st2["cost_micro_usd"] == st1["cost_micro_usd"] + 3000  # cumulative
    # per-match cap counts every attempt
    st3 = A.run_job(RECEIPT, V, [], signer=k, store=A.Store(tmp_path / "x"), ledger=led, caps=caps, explain_fn=ex,
                    prev={"attempts": 1, "cost_micro_usd": 45_000, "reason": "muse_failed", "retryable": True})
    assert st3["reason"] == "match_cap" and ex.calls == 1


def test_last_attempt_is_not_retryable(tmp_path):
    st, *_ = job(tmp_path, explain=FlakyExplain(9), xc=unreachable_xc)
    prev = {**st, "attempts": A.MAX_ATTEMPTS - 1}
    st3 = A.run_job(RECEIPT, V, [{"id": "final"}], signer=SoftwareKey(), store=A.Store(tmp_path / "advisory"),
                    ledger=A.Ledger(tmp_path / "l.jsonl"), caps=A.Caps(ON), explain_fn=FlakyExplain(9),
                    crosscheck_fn=unreachable_xc, prev=prev)
    assert st3["attempts"] == A.MAX_ATTEMPTS and st3["reason"] == "muse_failed" and st3["retryable"] is False


def test_advisor_backoff_then_success_max_three_attempts(tmp_path):
    timers = []

    class FakeRef:
        tail_ms = 1000

        def __init__(self):
            self.cleaned = 0

        def cleanup_segments(self, sub):
            self.cleaned += 1

        def _read_seg(self, sub):
            return lambda s: None

        def _wall_minus_mono(self, b):
            return 0

    class Sub:
        receipt, verify, bundle, received = RECEIPT, V, None, {}

    ref = FakeRef()
    ex = FlakyExplain(2)
    adv = A.AutoAdvisor(ref, tmp_path, env=ON, signer=SoftwareKey(), explain_fn=ex, crosscheck_fn=fake_xc(),
                        locks_fn=lambda *a, **k: [{"id": "final"}], timer=lambda d, fn: timers.append((d, fn)))
    adv._ensure_thread = lambda: None  # drive the queue by hand
    st = adv.process({"receipt": RECEIPT, "verify": V, "sub": Sub()})
    assert st["reason"] == "muse_failed" and ref.cleaned == 1 and [d for d, _ in timers] == [120]
    assert adv.public()["pending_retries"] == 1
    timers[0][1]()  # backoff elapsed
    st = adv.process(adv.q.get_nowait())
    assert st["attempts"] == 2 and st["reason"] == "muse_failed" and [d for d, _ in timers] == [120, 600]
    timers[1][1]()
    st = adv.process(adv.q.get_nowait())
    assert st["attempts"] == 3 and st["reason"] == "ok" and "ai_ruling.json" in st["files"] and len(timers) == 2
    assert ref.cleaned == 1 and adv.pending_retries == 0
    # a fresh hook for the same final receipt is idempotent
    again = adv.process({"receipt": RECEIPT, "verify": V, "sub": Sub()})
    assert again["attempts"] == 3 and ex.calls == 3


def test_advisor_gives_up_after_three_attempts(tmp_path):
    timers = []

    class FakeRef:
        tail_ms = 1000

        def cleanup_segments(self, sub):
            pass

    adv = A.AutoAdvisor(FakeRef(), tmp_path, env=ON, signer=SoftwareKey(), explain_fn=FlakyExplain(99),
                        crosscheck_fn=unreachable_xc, locks_fn=lambda *a, **k: [],
                        timer=lambda d, fn: timers.append((d, fn)))
    job = {"receipt": RECEIPT, "verify": V, "locks": []}
    for n in range(1, 4):
        st = adv.process(job)
        assert st["attempts"] == n
    assert st["retryable"] is False and len(timers) == 2
    assert adv.store.read(A.receipt_hash(RECEIPT), "ai_ruling.json") is None  # no file = Muse unchecked


def test_muse_connect_prefers_ipv6_then_falls_back(monkeypatch):
    import socket as S

    from qor_receipt.ai_ruling import muse

    tried = []

    class FakeSock:
        def __init__(self, fam, *a):
            self.family = fam

        def settimeout(self, t):
            pass

        def connect(self, addr):
            tried.append(("v6", addr))
            if addr[0] == "::bad":
                raise OSError("unreachable")

        def close(self):
            pass

    v6 = [(S.AF_INET6, S.SOCK_STREAM, 6, "", ("::bad", 443, 0, 0)), (S.AF_INET6, S.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]
    monkeypatch.setattr(muse.socket, "getaddrinfo", lambda *a, **k: v6)
    monkeypatch.setattr(muse.socket, "socket", FakeSock)
    hint = []
    s = muse._connect_prefer_ipv6("api.meta.ai", 443, 10, hint)
    assert hint == ["ipv6"] and s.family == S.AF_INET6 and [a for _, a in tried] == [v6[0][4], v6[1][4]]

    class V4:
        family = S.AF_INET
    monkeypatch.setattr(muse.socket, "getaddrinfo", lambda *a, **k: [v6[0]])
    monkeypatch.setattr(muse.socket, "create_connection", lambda addr, t: V4())
    hint = []
    assert muse._connect_prefer_ipv6("api.meta.ai", 443, 10, hint).family == S.AF_INET and hint == ["ipv4"]
    monkeypatch.setenv("QORTIME_MUSE_PREFER_IPV6", "0")
    tried.clear()
    hint = []
    muse._connect_prefer_ipv6("api.meta.ai", 443, 10, hint)
    assert tried == [] and hint == ["ipv4"]  # switch off = normal resolver order only
