"""Referee service (verified uploads): spot-check / full review, official countersign, verifier review mode."""
from __future__ import annotations

import copy
import io
import json
import random
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import review_fixture as F  # noqa: E402

from qor_receipt import receipt as R  # noqa: E402
from qor_receipt.keys import SoftwareKey  # noqa: E402
from qor_receipt.review.engine import FULL, SPOT, Referee, verify_token  # noqa: E402
from qor_receipt.review.policy import CHALLENGE_TOKEN_TYPE, REVIEW_KEY_KIND  # noqa: E402
from qor_receipt.trusted_referees import clear_cache  # noqa: E402

pytestmark = pytest.mark.skipif(not F.have_ffmpeg(), reason="ffmpeg needed for the synthetic fMP4 bundle")


def allowlist_for(tmp: Path, key: SoftwareKey, status: str = "active") -> str:
    p = tmp / "allow.json"
    p.write_text(json.dumps({"schema": "qortime-trusted-referees/v1", "version": 1,
                             "referees": [{"id": "test-official", "pubkey": key.pubkey_hex, "key_id": key.key_id,
                                           "status": status}]}))
    clear_cache()
    return str(p)


class Env:
    def __init__(self, tmp: Path, *, tokens: bool = True, claim=(14, 7), core=None, tail_ms=240_000, k=16):
        self.tmp = tmp
        self.key = SoftwareKey()
        self.clock = {"t": F.W0}
        self.ref = Referee(self.key, workdir=tmp / "work", now=lambda: self.clock["t"], rng=random.Random(7),
                           core_factory=core or F.fake_factory(), tail_ms=tail_ms, random_segments=k)
        toks = []
        if tokens:
            for ph, iss, *_ in F.CHALLENGES:
                self.clock["t"] = F.W0 + iss
                toks.append(self.ref.issue_challenge(ph, "home", "local-test-review"))
        self.fx = F.build(tmp / "bundle", challenge_tokens=toks or None, claim=claim)
        self.clock["t"] = F.W0 + 120_000
        self.allow = allowlist_for(tmp, self.key)

    def submit(self, mode=SPOT, files=None):
        return self.ref.create(files or self.fx["files"], mode)

    def upload_all(self, sub, tamper: int | None = None):
        for s in sub.required:
            data = self.fx["segments"][s]
            if s == tamper:
                data = data[:-1] + bytes([data[-1] ^ 1])
            self.ref.put_segment(sub, s, [data[: len(data) // 2], data[len(data) // 2:]])

    def player_sign(self, sub):
        return self.ref.add_player_signature(sub, self.fx["device"].sign(R.signing_bytes(sub.receipt)))


@pytest.fixture(autouse=True)
def _reset_allowlist():
    yield
    clear_cache()


def test_spot_check_review_countersigns_and_verifies(tmp_path):
    e = Env(tmp_path, tail_ms=8_000, k=4)
    sub = e.submit()
    assert sub.state == "awaiting_segments", sub.errors
    assert len(sub.random_sample) == 4 and sub.commit
    assert len(sub.required) < len(e.fx["segments"])  # spot-check: not everything
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "awaiting_player_signature", json.dumps(out["review"]["checks"], indent=1)
    rec = out["receipt"]
    assert rec["referee"]["pubkey"] == e.key.pubkey_hex and rec["referee"]["key_kind"] == REVIEW_KEY_KIND
    assert rec["review"]["checks"]["video_bytes"]["detail"]["method"] == SPOT
    final = e.player_sign(sub)
    v = R.verify_receipt(final["receipt"], referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)
    assert v["status"] == "VERIFIED", v
    assert v["verification_basis"] == "official-post-hoc-review"
    assert v["review"]["video_method"] == SPOT
    # Default (real) allowlist does not know this test key -> never VERIFIED.
    clear_cache()
    assert R.verify_receipt(final["receipt"], referee_pub=e.key.pubkey_hex)["status"] == "SELF_ATTESTED"
    # Segments are discarded once the receipt is complete.
    assert not (sub.dir / "segs").exists()


def test_full_mode_requires_every_segment(tmp_path):
    e = Env(tmp_path)
    sub = e.submit(FULL)
    assert sub.required == list(range(len(e.fx["segments"])))
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "awaiting_player_signature"
    assert out["receipt"]["review"]["checks"]["video_bytes"]["detail"]["segments_verified"] == len(e.fx["segments"])


def test_altered_video_byte_is_refused(tmp_path):
    e = Env(tmp_path, tail_ms=8_000, k=4)
    sub = e.submit()
    e.upload_all(sub, tamper=sub.required[-1])
    assert sub.required[-1] in sub.bad
    out = e.ref.review(sub)
    assert out["state"] == "refused"
    assert out["review"]["checks"]["video_bytes"]["state"] == "fail"
    assert sub.receipt is None


def test_missing_segments_refused(tmp_path):
    e = Env(tmp_path, tail_ms=8_000, k=4)
    sub = e.submit()
    for s in sub.required[:-2]:
        e.ref.put_segment(sub, s, [e.fx["segments"][s]])
    assert e.ref.review(sub)["state"] == "refused"


def test_spot_check_rejects_unrequested_segment(tmp_path):
    e = Env(tmp_path, tail_ms=8_000, k=2)
    sub = e.submit()
    extra = next(s for s in range(len(e.fx["segments"])) if s not in sub.required)
    with pytest.raises(ValueError):
        e.ref.put_segment(sub, extra, [e.fx["segments"][extra]])


def test_random_sample_differs_per_submission(tmp_path):
    e = Env(tmp_path, tail_ms=4_000, k=3)
    samples = {tuple(e.submit().random_sample) for _ in range(6)}
    assert len(samples) > 1


def test_final_mismatch_refused(tmp_path):
    e = Env(tmp_path, core=F.fake_factory(home=14, away=10))
    sub = e.submit()
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "refused"
    assert out["review"]["checks"]["final_reread"]["state"] == "fail"
    assert "referee read HOME 14 - AWAY 10" in out["review"]["checks"]["final_reread"]["detail"]


def test_scorebug_only_final_is_partial(tmp_path):
    e = Env(tmp_path, core=F.fake_factory(basis="clock_zero", confirmed=False))
    sub = e.submit()
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "awaiting_player_signature"
    final = e.player_sign(sub)
    v = R.verify_receipt(final["receipt"], referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)
    assert v["status"] == "PARTIAL" and "review_partial:final_reread" in v["partial"]


def test_no_official_challenges_is_partial(tmp_path):
    e = Env(tmp_path, tokens=False)
    sub = e.submit()
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "awaiting_player_signature"
    final = e.player_sign(sub)
    v = R.verify_receipt(final["receipt"], referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)
    assert v["status"] == "PARTIAL"
    assert "review_partial:challenges" in v["partial"]
    assert any(p.endswith("challenge_nonce_not_official") for p in v["partial"])


def test_unreadable_final_fails_closed_without_reader(tmp_path):
    e = Env(tmp_path)
    e.ref.core_factory = None
    sub = e.submit()
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "refused" and "unavailable" in out["review"]["checks"]["final_reread"]["detail"]


def test_countersigned_mismatch_is_http_422(tmp_path, monkeypatch):
    """Match 021003's open 422 is this rejection, not a parser bug.

    The published receipt was re-signed offline after a side correction, so checkpoint 0
    does not match countersigned.jsonl. The service must keep failing that closed.
    """
    from fastapi.testclient import TestClient

    from qor_receipt.referee_service.app import create_app

    monkeypatch.setenv("QORTIME_SUBMIT_TOKEN", "admin-token")
    e = Env(tmp_path)
    files = dict(e.fx["files"])
    lines = files["countersigned.jsonl"].decode().splitlines()
    first = json.loads(lines[0])
    first["referee_sig"] = "ab" * 64
    lines[0] = json.dumps(first)
    files["countersigned.jsonl"] = ("\n".join(lines) + "\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    res = TestClient(create_app(e.ref)).post(
        "/v1/submissions", content=buf.getvalue(), headers={"Authorization": "Bearer admin-token"})
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["state"] == "rejected"
    assert "receipt checkpoint 0 differs from countersigned.jsonl" in detail["errors"]
    assert "upload_token" not in detail


def test_precheck_rejects_broken_bundle(tmp_path):
    e = Env(tmp_path)
    files = dict(e.fx["files"])
    man = files["manifest.jsonl"].decode().splitlines()
    m = json.loads(man[3])
    m["sha256"] = "0" * 64
    man[3] = json.dumps(m)
    files["manifest.jsonl"] = ("\n".join(man) + "\n").encode()
    sub = e.submit(files=files)
    assert sub.state == "rejected" and any("chain head mismatch" in x for x in sub.errors)
    files = dict(e.fx["files"])
    inp = files["inputs.jsonl"].replace(b'"b": 512', b'"b": 1', 1)
    files["inputs.jsonl"] = inp
    sub = e.submit(files=files)
    assert sub.state == "rejected" and any("inputs" in x for x in sub.errors)


def test_precheck_rejects_already_official_and_bad_local_receipt(tmp_path):
    e = Env(tmp_path)
    files = dict(e.fx["files"])
    r = json.loads(files["receipt.json"])
    r["result"]["home_score"] = 99  # breaks the laptop's own signatures
    files["receipt.json"] = json.dumps(r).encode()
    sub = e.submit(files=files)
    assert sub.state == "rejected" and any(x.startswith("local_receipt:") for x in sub.errors)


def test_player_signature_must_match_device(tmp_path):
    e = Env(tmp_path)
    sub = e.submit()
    e.upload_all(sub)
    e.ref.review(sub)
    with pytest.raises(PermissionError):
        e.ref.add_player_signature(sub, SoftwareKey().sign(R.signing_bytes(sub.receipt)))


def test_challenge_and_anchor_tokens(tmp_path):
    e = Env(tmp_path, tokens=False)
    t = e.ref.issue_challenge("kickoff", "home", "m1", {"chain_head": "a" * 64, "segment_seq": 3})
    assert verify_token(t, e.key.pubkey_hex, CHALLENGE_TOKEN_TYPE)
    assert t["anchor"] == {"chain_head": "a" * 64, "segment_seq": 3}
    forged = dict(t, issued_ms=t["issued_ms"] - 60_000)
    assert not verify_token(forged, e.key.pubkey_hex, CHALLENGE_TOKEN_TYPE)
    a = e.ref.issue_anchor("m1", {"chain_head": "b" * 64})
    assert a["referee_ts_ms"] == e.clock["t"] and a["sig"]
    with pytest.raises(ValueError):
        e.ref.issue_challenge("overtime")


# ---------------------------------------------------------------- verifier: forged / tampered review receipts
def _verified(tmp_path):
    e = Env(tmp_path)
    sub = e.submit()
    e.upload_all(sub)
    e.ref.review(sub)
    return e, e.player_sign(sub)["receipt"]


def _resign(rec, ref_key, device):
    r = copy.deepcopy(rec)
    r["signatures"] = {"players": {}, "referee": None}
    return R.sign_player(R.sign_referee(r, ref_key), "home", device)


def test_forged_review_by_unlisted_key_is_self_attested(tmp_path):
    e, rec = _verified(tmp_path)
    attacker = SoftwareKey()
    r = copy.deepcopy(rec)
    r["referee"] = {"pubkey": attacker.pubkey_hex, "key_id": attacker.key_id, "key_kind": REVIEW_KEY_KIND}
    r = _resign(r, attacker, e.fx["device"])
    v = R.verify_receipt(r, referee_pub=attacker.pubkey_hex, allowlist_path=e.allow)
    assert v["status"] == "SELF_ATTESTED"


def test_review_tampering_fails(tmp_path):
    e, rec = _verified(tmp_path)
    r = copy.deepcopy(rec)
    r["review"]["checks"]["video_bytes"]["detail"]["method"] = "full"
    assert R.verify_receipt(r, referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)["status"] == "FAIL"
    # A "fail" check can never be VERIFIED even when signed.
    r = copy.deepcopy(rec)
    r["review"]["checks"]["final_reread"]["state"] = "fail"
    r = _resign(r, e.key, e.fx["device"])
    v = R.verify_receipt(r, referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)
    assert v["status"] == "FAIL" and "review_check_failed:final_reread" in v["errors"]
    # Missing review block under review key_kind -> FAIL.
    r = copy.deepcopy(rec)
    del r["review"]
    r = _resign(r, e.key, e.fx["device"])
    assert "review_missing_or_wrong_type" in R.verify_receipt(r, referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)["errors"]
    # Swapping the capture key breaks every checkpoint countersignature -> FAIL.
    r = copy.deepcopy(rec)
    r["review"]["capture_referee"]["pubkey"] = SoftwareKey().pubkey_hex
    r = _resign(r, e.key, e.fx["device"])
    v = R.verify_receipt(r, referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)
    assert v["status"] == "FAIL" and any("bad_referee_sig" in x for x in v["errors"])
    # Revoked official key -> at most SELF_ATTESTED.
    rev = allowlist_for(tmp_path, e.key, status="revoked")
    assert R.verify_receipt(rec, referee_pub=e.key.pubkey_hex, allowlist_path=rev)["status"] == "SELF_ATTESTED"


def test_challenge_token_from_other_key_not_official(tmp_path):
    e, rec = _verified(tmp_path)
    other = Referee(SoftwareKey(), workdir=tmp_path / "o", now=lambda: rec["challenges"][0]["issued_ms"], core_factory=None)
    r = copy.deepcopy(rec)
    c = r["challenges"][0]
    t = other.issue_challenge(c["phase"], "home", nonce=c["nonce"])
    t["issued_ms"], t["deadline_ms"] = c["issued_ms"], c["deadline_ms"]  # (sig no longer matches either way)
    c["issuer"] = t
    r = _resign(r, e.key, e.fx["device"])
    v = R.verify_receipt(r, referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)
    assert v["status"] == "PARTIAL" and any("challenge_nonce_not_official" in p for p in v["partial"])


def test_write_js_vector(tmp_path):
    """Vector for verify/test.mjs: a review receipt + the allowlist that trusts its (test) key."""
    e, rec = _verified(tmp_path)
    out = Path(__file__).parent / "vectors" / "review_receipt.json"
    want = {"receipt": rec, "referee_pub": e.key.pubkey_hex,
            "allowlist": json.loads(Path(e.allow).read_text())}
    if not out.exists() or json.loads(out.read_text()).get("receipt", {}).get("schema") != R.SCHEMA:
        out.write_text(json.dumps(want, indent=1))
    v = json.loads(out.read_text())
    clear_cache()
    p = tmp_path / "allow2.json"
    p.write_text(json.dumps(v["allowlist"]))
    assert R.verify_receipt(v["receipt"], referee_pub=v["referee_pub"], allowlist_path=str(p))["status"] == "VERIFIED"


# ---------------------------------------------------------------- HTTP layer
def test_rule_v2_presentation_clock_ignores_a_close_stamp_lag(tmp_path):
    """A 4 s lag on every close stamp must not move the pause once the bundle opts into rule v2.

    segment_ranges on this shifted manifest dates the menu about 4.2 s after OPTIONS, which fails
    even the 2500 ms ceiling. The fragment PTS does not move, so the measured latency stays the
    fixture's ~200 ms and result.rule is 2.
    """
    e = Env(tmp_path, tail_ms=8_000, k=2)
    lines = []
    for line in e.fx["files"]["manifest.jsonl"].decode().splitlines():
        if not line.strip():
            continue
        m = json.loads(line)
        if m.get("kind") != "init":
            m["mono_ms"] = int(m["mono_ms"]) + 4_000
            m["wall_ms"] = int(m["wall_ms"]) + 4_000
            m["frames"] = int(round(m["mono_ms"] * 30 / 1000))
        lines.append(json.dumps(m))
    e.fx["files"]["manifest.jsonl"] = ("\n".join(lines) + "\n").encode()
    meta = json.loads(e.fx["files"]["bundle.json"])
    meta["challenge_rule"] = 2
    e.fx["files"]["bundle.json"] = json.dumps(meta).encode()
    sub = e.submit()
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "awaiting_player_signature", json.dumps(out.get("review", out), indent=1)[:2000]
    ch = out["review"]["checks"]["challenges"]
    assert ch["state"] == "pass" and ch["detail"]["rule"] == 2, ch
    lats = []
    for c in out["receipt"]["challenges"]:
        res = c["result"]
        assert res["ok"] and res["rule"] == 2 and res["reverified_by"] == "official-referee", res
        assert 0 <= res["latency_ms"] <= 400, res
        lats.append(res["latency_ms"])
    assert len(lats) == 3
    final = e.player_sign(sub)
    v = R.verify_receipt(final["receipt"], referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)
    # The stamp lag also shifts motion bins relative to the controller log. That can lower the
    # motion score; it must not create a challenge error or partial.
    assert not any("challenge" in e for e in v["errors"]), v
    assert not any("challenge" in p for p in v["partial"]), v
    assert v["status"] in ("VERIFIED", "PARTIAL"), v


def test_http_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from qor_receipt.referee_service.app import create_app

    e = Env(tmp_path, tail_ms=8_000, k=3)
    c = TestClient(create_app(e.ref))
    assert c.get("/health").json()["ok"] is True
    info = c.get("/v1/referee").json()
    assert info["pubkey"] == e.key.pubkey_hex and "seed" not in json.dumps(info).lower()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in e.fx["files"].items():
            z.writestr(f"match-x/{k}", v)
    assert c.post("/v1/submissions", content=buf.getvalue()).status_code == 426  # unsigned (<= 0.3.9 client)
    import hashlib

    from qor_receipt.referee_service import submit_auth as A

    dev = e.fx["device"]
    ch = c.get(f"/v1/submit/challenge?pub={dev.pubkey_hex}").json()
    hdr = A.sign_request(dev, method="POST", path="/v1/submissions", body=buf.getvalue(), nonce=ch["nonce"],
                         pow_=A.solve_pow(ch["nonce"], dev.pubkey_hex, ch["pow_bits"]),
                         bundle_head=hashlib.sha256(e.fx["files"]["manifest.jsonl"]).hexdigest())
    r = c.post("/v1/submissions", content=buf.getvalue(), headers={"Content-Type": "application/zip", "X-Qor-Submit": hdr})
    assert r.status_code == 200, r.text
    s = r.json()
    h = {"Authorization": f"Bearer {s['upload_token']}"}
    assert c.put(f"/v1/submissions/{s['id']}/segments/{s['required'][0]}", content=b"x").status_code == 401
    assert c.get(f"/v1/submissions/{s['id']}").status_code == 401
    for seq in s["required"]:
        assert c.put(f"/v1/submissions/{s['id']}/segments/{seq}", content=e.fx["segments"][seq], headers=h).json()["ok"]
    rv = c.post(f"/v1/submissions/{s['id']}/review", headers=h).json()
    assert rv["state"] == "awaiting_player_signature"
    sig = e.fx["device"].sign(R.signing_bytes(rv["receipt"]))
    done = c.post(f"/v1/submissions/{s['id']}/player-signature", json={"sig": sig}, headers=h).json()
    assert R.verify_receipt(done["receipt"], referee_pub=e.key.pubkey_hex, allowlist_path=e.allow)["status"] == "VERIFIED"
    ch = c.post("/v1/challenges", json={"phase": "halftime"}).json()
    assert verify_token(ch, e.key.pubkey_hex, CHALLENGE_TOKEN_TYPE)
    bad = c.post("/v1/submissions", content=b"not a zip")
    assert bad.status_code == 426  # unsigned: rejected before the body is even parsed
