"""Referee final re-read: QorCap's standard scorebug reader + the fail-closed end-of-game-menu fallback.

match-20261018-084523: the referee built ReaderCore with QorTime's bare ScorebugReader (not QorCap's
``qorcap_standard.standard_reader``), read no scorebug in the tail, rejected the 6-14 end menu ("no Q4 scorebug read
before the end-of-game menu") and failed final_reread. The fallback accepts such a menu only at high confidence, over
2+ frames, when no locked read conflicts with it and it is reachable from the last locked read.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import review_fixture as F  # noqa: E402

from qor_receipt.review import media as M  # noqa: E402
from qor_receipt.review.engine import PARTIAL_CHECK, PASS, _final_check  # noqa: E402

REASON = M.END_MENU_FALLBACK_REASON


def rej(away=6, home=14, conf=0.953, frames=31, why=REASON, sources=("real",)):
    return {"away": away, "home": home, "t_ms": 1_000_000, "until_ms": 1_015_000, "frames": frames, "conf": conf,
            "margin": 0.215, "template_sources": list(sources), "xcheck": "qortime_scorebug_pack", "why": why}


def lock(away, home, q, t):
    return {"n": 3, "away": away, "home": home, "quarter": q, "t_ms": t, "until_ms": t + 500}


# 084523's committed offline timeline: ... 0-14 Q3, 6-14 Q3, 6-14 Q4 (live timeline.jsonl stopped at 0-13 Q2).
LOCKS_084523 = [lock(0, 13, 2, 100), lock(0, 14, 2, 200), lock(0, 14, 3, 300), lock(6, 14, 3, 400), lock(6, 14, 4, 500)]
CLAIM = {"home_score": 14, "away_score": 6}


# ---------------------------------------------------------------- accepted
def test_084523_shape_confirmed_against_last_q4_lock():
    f, why = M.end_menu_fallback(rej(), [], LOCKS_084523)
    assert why == "accepted" and f["basis"] == "end_menu" and (f["away"], f["home"]) == (6, 14)
    assert f["final_confirmed"] is True and f["fallback"] == "end_menu_without_tail_scorebug"
    assert f["quarter"] == 4 and f["fallback_reference"] == "bundle timeline lock"
    assert _final_check({"final": f, "last_read": None}, CLAIM)["state"] == PASS


def test_one_play_past_q3_lock_is_posted_unconfirmed_partial():
    f, _ = M.end_menu_fallback(rej(), [], LOCKS_084523[:3])  # last lock 0-14 Q3: +6 for away, not confirmed
    assert f is not None and f["final_confirmed"] is False
    assert _final_check({"final": f, "last_read": None}, CLAIM)["state"] == PARTIAL_CHECK


def test_one_play_past_q4_lock_is_confirmed():
    f, _ = M.end_menu_fallback(rej(away=13, home=14), [], [lock(6, 14, 4, 1)])
    assert f["final_confirmed"] is True


def test_reachable_multi_play_jump_is_unconfirmed():
    f, _ = M.end_menu_fallback(rej(), [], [lock(0, 7, 2, 1)])  # +6 / +7: several plays, reachable
    assert f is not None and f["final_confirmed"] is False


def test_lone_plus_one_beside_another_play_is_refused():
    # 084523's live timeline stopped at 0-13 Q2. 0-13 -> 6-14 needs a +1 that is only legal right after a TD the
    # referee cannot see; fail closed rather than guess.
    f, why = M.end_menu_fallback(rej(), [], [lock(0, 13, 2, 1)])
    assert f is None and "not reachable" in why


def test_referee_tail_read_is_preferred_reference():
    tail = [{"type": "score", "away": 6, "home": 14, "quarter": 4, "t_ms": 900}]
    f, _ = M.end_menu_fallback(rej(), tail, [lock(0, 13, 2, 1)])
    assert f["final_confirmed"] is True and f["fallback_reference"] == "referee tail read"


# ---------------------------------------------------------------- refused (fail closed)
@pytest.mark.parametrize("kw,locks,needle", [
    ({"why": "scorebug clock not 0:00 before the end-of-game menu"}, LOCKS_084523, "another reason"),
    ({"why": "tied score on the end-of-game menu"}, LOCKS_084523, "another reason"),
    ({"conf": 0.89}, LOCKS_084523, "confidence"),
    ({"frames": 1}, LOCKS_084523, "frame"),
    ({"sources": ("corroborated",)}, LOCKS_084523, "partly covered"),
    ({"away": 14, "home": 14}, [lock(7, 14, 4, 1)], "tied"),
    ({"away": 1, "home": 14}, [lock(0, 14, 4, 1)], "1 point"),
    ({}, [], "no locked score"),
    ({}, LOCKS_084523 + [lock(7, 14, 4, 600)], "conflicts"),                     # a later lock is above the menu
    ({"away": 6, "home": 13}, LOCKS_084523, "conflicts"),                          # menu below a locked 14
    ({"away": 7, "home": 21}, [lock(6, 14, 4, 1)], "not reachable"),              # +1 together with +7
])
def test_fallback_refuses(kw, locks, needle):
    f, why = M.end_menu_fallback(rej(**kw), [], locks)
    assert f is None and needle in why


def test_conflicting_referee_tail_read_refuses():
    tail = [{"type": "score", "away": 10, "home": 14, "quarter": 4, "t_ms": 900}]
    f, why = M.end_menu_fallback(rej(), tail, LOCKS_084523)
    assert f is None and "conflicts" in why


def test_timeline_locks_from_bytes_ignores_obs_and_junk():
    raw = b'{"n":1,"obs":"score_unsure","t_ms":1}\nnot json\n' + json.dumps(lock(0, 7, 1, 5)).encode() + b"\n" \
        + json.dumps({"home": True, "away": 0, "t_ms": 6}).encode() + b"\n"
    assert [(r["away"], r["home"]) for r in M.timeline_locks_from_bytes(raw)] == [(0, 7)]
    assert M.timeline_locks_from_bytes(None) == []


# ---------------------------------------------------------------- reread_final integration (fake ReaderCore)
class MenuOnlyCore:
    """ReaderCore that never reads a scorebug and rejects the menu like QorCap does without a Q4 scorebug read."""

    def __init__(self, post, menu=None):
        self.post, self.final, self.end_menu, self.delta_corroboration = post, None, None, []
        self.end_menu_rejected = None
        self.menu = menu
        self.stats = {"frames": 0}

    def on_frame(self, t, img):
        self.stats["frames"] += 1
        if self.menu and self.stats["frames"] >= 3:
            self.end_menu_rejected = dict(self.menu)


def frames(n=6):
    return ((i * 500, None) for i in range(n))


def test_unreadable_tail_still_refuses():
    out = M.reread_final(frames(), lambda post: MenuOnlyCore(post), timeline_locks=LOCKS_084523)
    assert out["final"] is None and out["last_read"] is None and out["end_menu_fallback"] is None
    assert _final_check(out, CLAIM)["state"] == "fail"


def test_menu_without_locks_refuses():
    out = M.reread_final(frames(), lambda post: MenuOnlyCore(post, rej()), timeline_locks=[])
    assert out["final"] is None and out["end_menu_fallback"]["accepted"] is False
    assert _final_check(out, CLAIM)["state"] == "fail"


def test_conflicting_menu_refuses_through_reread():
    out = M.reread_final(frames(), lambda post: MenuOnlyCore(post, rej(away=3)), timeline_locks=LOCKS_084523)
    assert out["final"] is None and "conflicts" in out["end_menu_fallback"]["why"]
    assert _final_check(out, CLAIM)["state"] == "fail"


def test_menu_with_consistent_locks_passes_through_reread():
    out = M.reread_final(frames(), lambda post: MenuOnlyCore(post, rej()), timeline_locks=LOCKS_084523)
    assert out["final"]["final_confirmed"] is True and out["end_menu_fallback"]["accepted"] is True
    assert _final_check(out, CLAIM)["state"] == PASS


def test_menu_disagreeing_with_claim_fails():
    out = M.reread_final(frames(), lambda post: MenuOnlyCore(post, rej()), timeline_locks=LOCKS_084523)
    assert _final_check(out, {"home_score": 21, "away_score": 6})["state"] == "fail"


# ---------------------------------------------------------------- QorCap standard reader is used when shipped
def test_standard_reader_from_sidecar(tmp_path):
    (tmp_path / "qorcap_standard.py").write_text("def standard_reader():\n    return 'qorcap-standard'\n")
    sys.modules.pop("qorcap_standard", None)
    try:
        make = M.sidecar_standard_reader(str(tmp_path))
        assert make() == "qorcap-standard"
    finally:
        sys.modules.pop("qorcap_standard", None)
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_standard_reader_falls_back_without_qorcap_standard(tmp_path):
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader

    assert M.sidecar_standard_reader(str(tmp_path)) is ScorebugReader


# ---------------------------------------------------------------- end to end through the referee (synthetic bundle)
@pytest.mark.skipif(not F.have_ffmpeg(), reason="ffmpeg needed for the synthetic fMP4 bundle")
def test_referee_menu_only_tail_with_timeline_locks(tmp_path):
    from test_referee_service import Env

    menu = rej(away=7, home=14)
    e = Env(tmp_path, core=lambda post: MenuOnlyCore(post, menu))
    files = dict(e.fx["files"])
    files["timeline.jsonl"] = (json.dumps(lock(7, 14, 4, F.W0 + 1_000)) + "\n").encode()
    sub = e.submit(files=files)
    e.upload_all(sub)
    out = e.ref.review(sub)
    chk = out["review"]["checks"]["final_reread"]
    assert chk["state"] == "pass" and "no Q4 scorebug" in chk["detail"]


@pytest.mark.skipif(not F.have_ffmpeg(), reason="ffmpeg needed for the synthetic fMP4 bundle")
def test_referee_menu_only_tail_without_timeline_refuses(tmp_path):
    from test_referee_service import Env

    e = Env(tmp_path, core=lambda post: MenuOnlyCore(post, rej(away=7, home=14)))
    sub = e.submit()
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "refused" and out["review"]["checks"]["final_reread"]["state"] == "fail"
