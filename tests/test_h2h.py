"""Head-to-head challenge links: single-recorder settlement, both-player confirm/dispute, fail-closed unverified."""
import json

import pytest

from qor_receipt import h2h as H

T0 = 1_792_000_000_000


def rec(away, home, role="home", started=T0 + 60_000, confirmed=True, game="madden27", mid="m1"):
    return {"schema": "qor-receipt/v0", "match": {"match_id": mid, "game": game, "started_ms": started},
            "result": {"away_score": away, "home_score": home, "winner": "home" if home > away else "away",
                       "final_confirmed": confirmed, "method": "end_menu_read"},
            "players": [{"role": role, "pubkey": "00" * 32}], "referee": {"pubkey": "11" * 32}}


def stub(status_by_mid):
    return lambda r: {"status": status_by_mid.get(r["match"]["match_id"], "VERIFIED"), "errors": [], "partial": []}


@pytest.fixture
def store(tmp_path):
    return H.Store(tmp_path, verify=stub({"self": "SELF_ATTESTED", "part": "PARTIAL"}), now=lambda: T0)


def mk(store, **kw):
    out = store.create({"creator": "@con", "opponent": "rival_1", "rules": {"quarter_min": 6}, **kw})
    return out["challenge"]["id"], out["creator_token"]


def body(r):
    return json.dumps(r).encode()


def test_create_accept_and_single_recorder_settles(store):
    cid, tok = mk(store)
    c = store.get(cid)
    assert c["settlement"]["status"] == "open" and c["opponent"]["handle"] == "@rival_1"
    assert "not for betting" in c["not_for_betting"] and "token" not in json.dumps(c)
    with pytest.raises(H.H2HError):
        store.accept(cid, "@someone_else")
    opp = store.accept(cid, "@Rival_1")["opponent_token"]
    assert store.get(cid)["settlement"]["status"] == "accepted"
    with pytest.raises(H.H2HError):
        store.accept(cid)  # only once
    c = store.submit(cid, f"Bearer {tok}", body(rec(17, 24, role="home")))
    s = c["settlement"]
    assert s["status"] == "recorded" and s["winner"] == "@con" and s["label"] == "recorded by @con"
    assert s["score"] == {"away": 17, "home": 24}
    assert opp


def test_recorder_on_away_side_losing(store):
    cid, tok = mk(store)
    s = store.submit(cid, tok, body(rec(10, 31, role="away")))["settlement"]
    assert s["winner"] == "@rival_1"


@pytest.mark.parametrize("kw,why", [({"mid": "self"}, "SELF_ATTESTED"), ({"mid": "part"}, "PARTIAL"),
                                    ({"confirmed": False}, "not confirmed"), ({"game": "fifa"}, "game"),
                                    ({"started": T0 - 3_600_000}, "before the challenge")])
def test_fail_closed_unverified_never_names_a_winner(store, kw, why):
    cid, tok = mk(store)
    c = store.submit(cid, tok, body(rec(7, 14, **kw)))
    s = c["settlement"]
    assert s["status"] == "unverified" and s["winner"] is None and s["score"] is None
    assert why in s["why"]


def test_no_receipt_is_not_a_result(store):
    cid, _ = mk(store)
    assert store.get(cid)["settlement"]["winner"] is None


def test_both_agree_confirmed_and_disagree_disputed(store):
    cid, tok = mk(store)
    opp = store.accept(cid)["opponent_token"]
    store.submit(cid, tok, body(rec(17, 24, role="home", mid="a")))
    s = store.submit(cid, opp, body(rec(17, 24, role="away", mid="b")))["settlement"]
    assert s["status"] == "confirmed" and s["winner"] == "@con" and s["label"] == "confirmed by both players"

    cid, tok = mk(store)
    opp = store.accept(cid)["opponent_token"]
    store.submit(cid, tok, body(rec(17, 24, role="home", mid="c")))
    s = store.submit(cid, opp, body(rec(17, 21, role="away", mid="d")))["settlement"]
    assert s["status"] == "disputed" and s["winner"] is None and s["score"] is None

    cid, tok = mk(store)  # same score, but both claim the home side -> each says they won -> disputed
    opp = store.accept(cid)["opponent_token"]
    store.submit(cid, tok, body(rec(3, 9, role="home", mid="e")))
    s = store.submit(cid, opp, body(rec(3, 9, role="home", mid="f")))["settlement"]
    assert s["status"] == "disputed" and s["winner"] is None


def test_unverified_second_receipt_does_not_override(store):
    cid, tok = mk(store)
    opp = store.accept(cid)["opponent_token"]
    store.submit(cid, tok, body(rec(17, 24, mid="g")))
    s = store.submit(cid, opp, body(rec(30, 0, role="away", mid="self")))["settlement"]
    assert s["status"] == "recorded" and s["winner"] == "@con" and s["ignored_unverified"] == ["@rival_1"]


def test_tokens_reuse_and_replacement(store):
    cid, tok = mk(store)
    with pytest.raises(H.H2HError) as e:
        store.submit(cid, "Bearer nope", body(rec(1, 2)))
    assert e.value.status == 401
    store.submit(cid, tok, body(rec(7, 14, mid="self")))  # self-attested first ...
    s = store.submit(cid, tok, body(rec(7, 14, mid="h")))  # ... replaced by the official one
    assert s["settlement"]["status"] == "recorded" and len(s["submissions"]) == 1
    with pytest.raises(H.H2HError):
        store.submit(cid, tok, body(rec(7, 15, mid="i")))  # a counted receipt is final
    cid2, tok2 = mk(store)
    with pytest.raises(H.H2HError, match="already used"):
        store.submit(cid2, tok2, body(rec(7, 14, mid="h")))


def test_input_validation(store):
    for bad in ({"creator": "@a", "opponent": "@a"}, {"creator": "<script>", "opponent": "@b"},
                {"creator": "@a", "opponent": "@b", "rules": {"quarter_min": 99}},
                {"creator": "@a", "opponent": "@b", "rules": {"notes": "loser pays $20"}},
                {"creator": "@a", "opponent": "@b", "game": "poker"}):
        with pytest.raises(H.H2HError):
            store.create(bad)
    with pytest.raises(H.H2HError) as e:
        store.get("../../etc")
    assert e.value.status == 404


def test_real_self_attested_demo_receipt_is_unverified(tmp_path):
    from qor_receipt.demo import demo_receipt

    r, _ = demo_receipt("single")
    s = H.Store(tmp_path, now=lambda: 1_789_999_000_000)  # real verifier, real allowlist
    out = s.create({"creator": "@a", "opponent": "@b"})
    c = s.submit(out["challenge"]["id"], out["creator_token"], json.dumps(r).encode())
    assert c["settlement"]["status"] == "unverified" and c["settlement"]["winner"] is None
    assert c["submissions"][0]["status"] != "VERIFIED"


def test_http_routes_and_cors(tmp_path):
    from fastapi.testclient import TestClient

    from qor_receipt.keys import SoftwareKey
    from qor_receipt.referee_service.app import create_app
    from qor_receipt.review.engine import Referee

    app = create_app(Referee(SoftwareKey(), workdir=tmp_path / "w"), h2h=H.Store(tmp_path / "h", verify=stub({}), now=lambda: T0))
    c = TestClient(app)
    r = c.post("/v1/h2h", json={"creator": "@con", "opponent": "@rival", "rules": {"quarter_min": 5}},
               headers={"Origin": "https://conwan30.github.io"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://conwan30.github.io"
    cid, tok = r.json()["challenge"]["id"], r.json()["creator_token"]
    assert c.post(f"/v1/h2h/{cid}/receipts", content=body(rec(1, 2))).status_code == 401
    out = c.post(f"/v1/h2h/{cid}/receipts", content=body(rec(14, 21)), headers={"Authorization": f"Bearer {tok}"}).json()
    assert out["settlement"]["status"] == "recorded"
    sha16 = out["submissions"][0]["receipt_sha256"][:16]
    assert c.get(f"/v1/h2h/{cid}/receipts/{sha16}").json()["result"]["home_score"] == 21
    assert c.get("/v1/h2h/zzzzzzzzzz").status_code == 404
    bad = c.options("/v1/h2h", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert bad.headers.get("access-control-allow-origin") is None
