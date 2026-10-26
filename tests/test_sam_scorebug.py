"""SAM 3.1 scorebug check: lower-only, advisory (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026). API mocked."""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import pytest

from qor_receipt.ai_ruling import sam_scorebug as S
from qor_receipt.receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = json.loads((ROOT / "site" / "matches" / "170123" / "receipt.json").read_text(encoding="utf-8"))
PUB = RECEIPT["referee"]["pubkey"]
STRIP = "<0f>0<|box;x1=0;y1=676;x2=1279;y2=719;w=1280;h=720|><|mask;x=0;y=0;data=1,~|>"
TOPLEFT = "<0f>0<|box;x1=20;y1=10;x2=680;y2=130;w=1280;h=720|><|mask;x=0;y=0;data=1,~|>"
STATUSES = ["VERIFIED", "PARTIAL", "SELF_ATTESTED", "FAIL"]


def reply(text):
    return 200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}], "usage": {"images_processed": 1}}


class Seq:
    def __init__(self, items):
        self.items, self.calls = list(items), []

    def __call__(self, url, key, body, timeout):
        self.calls.append((url, key, body))
        it = self.items.pop(0)
        if isinstance(it, Exception):
            raise it
        return it


def run(items, n=None, **kw):
    t = Seq(items)
    frames = [(f"f{i}", b"\xff\xd8jpeg%d" % i) for i in range(n or len(items))]
    return S.check(RECEIPT, frames, client=S.SamClient(api_key="k", transport=t), **kw), t


# ---- cap can only ever be PARTIAL, and only lowers VERIFIED ------------------------------------------------------
@pytest.mark.parametrize("status,cap", itertools.product(STATUSES, [None, "PARTIAL"]))
def test_apply_cap_is_lower_only(status, cap):
    out = S.apply_cap(status, cap)
    order = {"FAIL": 0, "SELF_ATTESTED": 1, "PARTIAL": 2, "VERIFIED": 3}
    assert order[out] <= order[status], "never raises"
    assert out != "FAIL" or status == "FAIL", "never FAILs on its own"
    assert out == ("PARTIAL" if (status == "VERIFIED" and cap) else status)


@pytest.mark.parametrize("bad_cap", ["FAIL", "SELF_ATTESTED", "VERIFIED"])
def test_cap_below_or_above_partial_is_refused(bad_cap):
    with pytest.raises(ValueError):
        S.apply_cap("VERIFIED", bad_cap)


def test_decide_cap_values_are_only_partial_or_none():
    seen = set()
    for combo in itertools.product(["scorebug_in_region", "no_scorebug", "scorebug_out_of_region", "unchecked"], repeat=4):
        seen.add(S.decide_cap(combo)[0])
    assert seen == {None, "PARTIAL"}


# ---- decision rule ----------------------------------------------------------------------------------------------
def test_all_frames_show_scorebug_no_cap():
    sc, t = run([reply(STRIP)] * 3)
    assert sc["cap"] is None and [f["result"] for f in sc["frames"]] == ["scorebug_in_region"] * 3
    assert sc["affects_verdict"] == "lower_only" and sc["max_cap"] == "PARTIAL" and sc["calls"] == 3


def test_majority_missing_caps_partial():
    sc, _ = run([reply(""), reply(TOPLEFT), reply(STRIP)])
    assert sc["cap"] == "PARTIAL" and sc["reasons"]
    assert S.apply_cap(verify_receipt(RECEIPT, referee_pub=PUB)["status"], sc["cap"]) == "PARTIAL"


def test_single_miss_or_tie_does_not_cap():
    assert run([reply(""), reply(STRIP), reply(STRIP)])[0]["cap"] is None
    assert run([reply(""), reply(STRIP)])[0]["cap"] is None          # 1 bad < MIN_BAD
    assert run([reply(""), reply(""), reply(STRIP), reply(STRIP)])[0]["cap"] is None   # tie is not a majority


def test_burn_in_label_only_is_out_of_region():
    sc, _ = run([reply(TOPLEFT)])
    assert sc["frames"][0]["result"] == "scorebug_out_of_region"


# ---- fail safe: errors never count against the player ------------------------------------------------------------
def test_api_errors_are_unchecked_and_never_cap():
    sc, _ = run([(500, None), S.SamError("network:URLError"), RuntimeError("x"), (200, {"output": "bad"})])
    assert sc["cap"] is None and all(f["result"] == "unchecked" for f in sc["frames"])


def test_missing_key_makes_no_call(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    t = Seq([reply("")] * 3)
    sc = S.check(RECEIPT, [("a", b"x"), ("b", b"y")], client=S.SamClient(transport=t))
    assert t.calls == [] and sc["cap"] is None and sc["calls"] == 0


def test_unknown_profile_is_unchecked():
    r = copy.deepcopy(RECEIPT); r["match"]["scorebug_profile"] = "some_other_game"
    t = Seq([reply("")] * 3)
    sc = S.check(r, [("a", b"x"), ("b", b"y"), ("c", b"z")], client=S.SamClient(api_key="k", transport=t))
    assert t.calls == [] and sc["cap"] is None


def test_max_images_bounds_spend():
    sc, t = run([reply("")] * 2, n=5, max_images=2)
    assert len(t.calls) == 2 and sc["cost_micro_usd"] == 2 * S.USD_PER_IMAGE_MICRO
    assert [f["result"] for f in sc["frames"]][2:] == ["unchecked"] * 3


# ---- request shape, key handling, isolation ---------------------------------------------------------------------
def test_request_shape_and_key_not_leaked():
    secret = "sk-test-NOT-A-REAL-KEY-998877"
    t = Seq([reply(STRIP)])
    sc = S.check(RECEIPT, [("a", b"\xff\xd8x")], client=S.SamClient(api_key=secret, transport=t))
    url, key, body = t.calls[0]
    assert url == "https://api.meta.ai/v1/responses" and body["model"] == "sam-3.1" and key == secret
    parts = body["input"][0]["content"]
    assert parts[0] == {"type": "input_text", "text": "scoreboard"} and parts[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert secret not in json.dumps(sc) and secret not in json.dumps(body) and secret not in repr(S.SamClient(api_key=secret))


def test_parse_boxes():
    assert S.parse_boxes("") == []
    assert S.parse_boxes(STRIP + TOPLEFT)[1]["x2"] == 680
    assert S.parse_boxes("<|box;x1=5;y1=5;x2=1;y2=1;w=10;h=10|>") == []


def test_receipt_and_verdict_untouched():
    before = copy.deepcopy(RECEIPT)
    v = verify_receipt(RECEIPT, referee_pub=PUB)
    run([reply(""), reply("")])
    assert RECEIPT == before and verify_receipt(RECEIPT, referee_pub=PUB) == v
    for p in [ROOT / "qor_receipt" / "receipt.py", *(ROOT / "verify").glob("*.js")]:
        assert "sam_scorebug" not in p.read_text(encoding="utf-8"), p


def test_mask_burn_in_blanks_label():
    np = pytest.importorskip("numpy"); cv2 = pytest.importorskip("cv2")
    img = np.full((720, 1280, 3), 255, np.uint8)
    out = cv2.imdecode(np.frombuffer(S.mask_burn_in(cv2.imencode(".png", img)[1].tobytes(), "madden27_standard"), np.uint8), cv2.IMREAD_COLOR)
    assert out[50, 50].max() < 30 and out[700, 1200].min() > 220
