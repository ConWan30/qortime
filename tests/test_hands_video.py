"""Show Your Hands: video action detection, Muse context filter, match-level scoring, signed sidecar
(Meta AI Developer Hackathon, Oct 26 - Nov 4 2026). SYNTHETIC inputs; Muse API mocked."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from qor_receipt.ai_ruling.muse import MuseError
from qor_receipt.ai_ruling.signing import sign_ruling
from qor_receipt.hands import muse_context as M
from qor_receipt.hands import press_to_pixel as P
from qor_receipt.hands import sidecar as S
from qor_receipt.hands import video_events as V
from qor_receipt.hands.inputs import make_chunks
from qor_receipt.hands.synthetic import human_session, receipt_for
from qor_receipt.keys import SoftwareKey
from qor_receipt.receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
MATCH = ROOT / "site" / "matches" / "170123"
SECRET = "sk-test-NOT-A-REAL-KEY-77777"


# ---- local detector ------------------------------------------------------------------------------------------
def test_onset_after_stillness_and_cut():
    e = [0.5] * 30 + [3.0, 8.0, 12.0, 12.0] + [9.0] * 20 + [1.0] * 30
    c = [1.0] * len(e); c[70] = 0.2
    ev = V.detect(e, c, 30.0)
    kinds = [(x["kind"], x["frame"]) for x in ev]
    assert ("cut", 71) in kinds and [k for k, _ in kinds].count("onset") == 1
    onset = next(x for x in ev if x["kind"] == "onset")
    assert 29 <= onset["frame"] <= 32


def test_no_onset_without_stillness_or_rise():
    assert [x for x in V.detect([5.0] * 90, [1.0] * 90) if x["kind"] == "onset"] == []
    assert V.detect([0.5] * 30 + [4.0] * 30, [1.0] * 60) == []          # never above HI


def test_onset_at_cut_is_not_an_action():
    e = [0.5] * 30 + [20.0] * 10 + [0.5] * 10
    c = [1.0] * len(e); c[30] = 0.1
    assert [x["kind"] for x in V.detect(e, c)] == ["cut"]


def test_energy_curve_on_generated_video(tmp_path):
    np = pytest.importorskip("numpy"); cv2 = pytest.importorskip("cv2")
    p = tmp_path / "SYNTHETIC.mp4"
    w = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 180))
    for i in range(90):
        f = np.full((180, 320, 3), 60, np.uint8)
        if i >= 45:
            x = (i - 45) * 12 % 300
            f[60:120, x:x + 40] = 250
        w.write(f)
    w.release()
    e, c, fps = V.energy_curve(str(p))
    ev = [x for x in V.detect(e, c, fps) if x["kind"] == "onset"]
    assert len(ev) == 1 and abs(ev[0]["t_s"] - 1.5) < 0.2


# ---- Muse context filter -----------------------------------------------------------------------------------
def reply(kind, conf=True):
    return 200, {"choices": [{"message": {"content": json.dumps({"kind": kind, "confident": conf})}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1500, "completion_tokens": 200}}


class Seq:
    def __init__(self, items):
        self.items, self.calls = list(items), []

    def __call__(self, url, key, body, timeout):
        self.calls.append((url, key, body))
        it = self.items.pop(0)
        if isinstance(it, Exception):
            raise it
        return it


CANDS = [{"kind": "onset", "clip": "a", "t_s": 1.0}, {"kind": "cut", "clip": "a", "t_s": 2.0},
         {"kind": "onset", "clip": "a", "t_s": 9.0}, {"kind": "onset", "clip": "b", "t_s": 3.0}]


def test_muse_only_keeps_confident_player_actions():
    t = Seq([reply("player_action"), reply("camera_or_cutscene"), reply("player_action", conf=False)])
    kept, rep = M.filter_actions(CANDS, lambda e: (b"b", b"a"), M.MuseContext(api_key=SECRET, transport=t))
    assert [k["t_s"] for k in kept] == [1.0] and rep["calls"] == 3 and rep["cost_micro_usd"] > 0
    assert [r["muse"] for r in rep["events"]] == ["player_action", "camera_or_cutscene", "unsure"]
    body = t.calls[0][2]
    assert body["model"] == "muse-spark-1.3" and len(body["messages"][0]["content"]) == 3
    assert SECRET not in json.dumps(rep) and SECRET not in repr(M.MuseContext(api_key=SECRET))


def test_muse_errors_and_no_key_keep_nothing(monkeypatch):
    t = Seq([(500, None), MuseError("x"), RuntimeError("y")])
    kept, rep = M.filter_actions(CANDS, lambda e: (b"b", b"a"), M.MuseContext(api_key=SECRET, transport=t))
    assert kept == [] and all(r["muse"] == "unchecked" for r in rep["events"])
    kept, rep = M.filter_actions(CANDS, lambda e: (b"b", b"a"), None)
    assert kept == [] and rep["calls"] == 0
    for k in ("META_API_KEY", "MODEL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    t2 = Seq([])
    kept, _ = M.filter_actions(CANDS, lambda e: (b"b", b"a"), M.MuseContext(transport=t2))
    assert kept == [] and t2.calls == []


def test_muse_max_calls_and_same_play_rule():
    t = Seq([reply("player_action")] * 3)
    cands = [{"kind": "onset", "clip": "a", "t_s": x} for x in (1.0, 3.0, 12.0)]
    kept, rep = M.filter_actions(cands, lambda e: (b"b", b"a"), M.MuseContext(api_key=SECRET, transport=t), max_calls=3)
    assert [k["t_s"] for k in kept] == [1.0, 12.0] and rep["events"][1]["muse"] == "player_action_dropped_same_play"
    t = Seq([reply("player_action")] * 5)
    _, rep = M.filter_actions(cands, lambda e: (b"b", b"a"), M.MuseContext(api_key=SECRET, transport=t), max_calls=1)
    assert rep["calls"] == 1
    with pytest.raises(ValueError):
        M.MuseContext(api_key="x", model="muse-spark-1.3-contributor")


# ---- windowed stick changes, match-level scoring ---------------------------------------------------------------
def test_gradual_stick_swing_is_an_input_change():
    recs = [{"t_us": (1000 + 5 * i) * 1000, "b": 0, "lx": 128, "ly": 255 - 6 * i, "rx": 128, "ry": 128, "l2": 0, "r2": 0}
            for i in range(22)]
    ch = P.input_changes(recs)
    assert ch[0] == 1000 + 5 * 7 and all(b - a >= 50 for a, b in zip(ch, ch[1:]))   # 255 -> 213 crosses 40 in the window


def test_match_level_alignment_pools_anchors():
    recs, presses = human_session()
    clips = [{"id": f"c{k}", "records": recs, "actions_ms": [presses[k] + 90]} for k in range(3)]
    rows = P.score_match(clips)
    assert all(r["scorable"] and r["match_anchors"] == 3 and r["flags"] == [] for r in rows)
    rows = P.score_match(clips[:2])
    assert all(not r["scorable"] and r["why"] == "too_few_anchors" and r["flags"] == [] for r in rows)


def test_sidecar_is_integers_only_and_signable():
    recs, presses = human_session()
    receipt = receipt_for(recs)
    doc = S.build(receipt, recs, make_chunks(recs), [{"id": "c", "start_mono_ms": 100_000, "duration_s": 40,
                                                       "actions_ms": [p + 90 for p in presses]}])
    k = SoftwareKey()
    signed = sign_ruling(doc, k)          # raises on any float (JCS)
    keys = {"keys": [{"id": "t", "pubkey": k.pubkey_hex, "key_id": k.key_id, "key_kind": "qortime-ai-advisory", "status": "active"}]}
    assert S.check_hands(signed, receipt, keys=keys) == []
    bad = copy.deepcopy(signed); bad["cap"] = "PARTIAL"
    assert "bad_advisory_signature" in S.check_hands(bad, receipt, keys=keys)
    assert "cap_inconsistent" in S.check_hands(sign_ruling({**doc, "cap": "PARTIAL"}, k), receipt, keys=keys)
    assert "identifying_data" in S.check_hands(sign_ruling({**doc, "pad_instance_path": "x"}, k), receipt, keys=keys)
    assert "advisory_key_not_allowlisted" in S.check_hands(signed, receipt)


def test_published_170123_hands_is_clean():
    d = json.loads((MATCH / "hands.json").read_text(encoding="utf-8"))
    r = json.loads((MATCH / "receipt.json").read_text(encoding="utf-8"))
    assert S.check_hands(d, r) == []
    assert d["inputs_verified"] is True and d["cap"] is None and all(c["flags"] == [] for c in d["clips"])
    assert all("track" not in c for c in d["clips"]) and d["action_filter"]["cost_micro_usd"] < 500_000
    text = json.dumps(d)
    for bad in ("HID\\\\", "VID_", "PID_", "instance", "Users", "pad_id"):
        assert bad not in text
    v = verify_receipt(r, referee_pub=r["referee"]["pubkey"])
    assert S.apply_to_result(v, d)["status"] == "VERIFIED"
