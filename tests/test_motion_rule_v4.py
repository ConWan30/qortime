"""Motion rule v4: content-head deterministic sampling, 48-seg draws, median of 3."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from qor_receipt.review.motion import (
    MOTION_BASIS_V4,
    MOTION_MEDIAN_DRAWS,
    MOTION_RULE,
    MOTION_SAMPLE_MAX_SEGMENTS,
    MOTION_SAMPLE_MAX_SEGMENTS_V3,
    median_permille,
    motion_content_seed,
    motion_sample_draws,
    score_motion_v4,
)

ROOT = Path(__file__).resolve().parents[1]


def _long_manifest(n: int = 120):
    return [{"seq": 0, "offset": 0, "mono_ms": 0, "file": "video.mp4", "bytes": 100, "sha256": "00"},
            *[{"seq": i, "offset": i * 1000, "mono_ms": (i - 1) * 2000, "file": "video.mp4",
               "bytes": 100, "sha256": f"{i:02x}"}
              for i in range(1, n + 1)]]


def test_v4_constants_and_basis():
    assert MOTION_RULE == 4
    assert MOTION_SAMPLE_MAX_SEGMENTS == 48
    assert MOTION_SAMPLE_MAX_SEGMENTS_V3 == 24
    assert MOTION_MEDIAN_DRAWS == 3
    assert "never submission id" in MOTION_BASIS_V4
    assert "median of 3" in MOTION_BASIS_V4
    receipt = json.loads((ROOT / "schema" / "receipt.v0.json").read_text(encoding="utf-8"))
    # frozen schema: no new required fields; motion still lives under signals
    assert "motion_permille" in receipt["properties"]["signals"]["properties"]


def test_content_seed_ignores_submission_id_material():
    a = motion_content_seed(chain_head="aa", checkpoint_head="bb", input_head="cc", mode="spot-check")
    b = motion_content_seed(chain_head="aa", checkpoint_head="bb", input_head="cc", mode="spot-check")
    c = motion_content_seed(chain_head="aa", checkpoint_head="bb", input_head="cc", mode="full")
    d = motion_content_seed(chain_head="aa", checkpoint_head="ZZ", input_head="cc", mode="spot-check")
    assert a == b and a != c and a != d


def test_sample_draws_are_deterministic_and_independent_of_submission():
    segs = _long_manifest()
    kwargs = dict(chain_head="h1", checkpoint_head="h2", input_head="h3", mode="spot-check")
    d1 = motion_sample_draws(segs, **kwargs)
    d2 = motion_sample_draws(segs, **kwargs)
    assert d1 == d2
    assert len(d1) == 3
    assert all(len(x) == 48 for x in d1)
    # draws differ from each other (different salts)
    assert len({tuple(x) for x in d1}) >= 2
    # changing only a fake submission-like string cannot be passed in — API has no such arg
    other = motion_sample_draws(segs, chain_head="h1", checkpoint_head="h2", input_head="h3", mode="spot-check")
    assert other == d1


def test_median_permille_and_v4_score():
    assert median_permille([100, 300, 500]) == 300
    assert median_permille([None, 400, 200]) == 300
    assert median_permille([None, None]) is None
    # Perfect correlation on three identical draws
    motion = {i: (i % 17) * 3 for i in range(80)}
    energy = dict(motion)
    bin_segs = {i: {1 + (i // 10)} for i in range(80)}
    draws = [[1, 2, 3, 4, 5, 6, 7, 8], [1, 2, 3, 4, 5, 6, 7, 8], [1, 2, 3, 4, 5, 6, 7, 8]]
    mp, detail = score_motion_v4(energy, motion, bin_segs, draws)
    assert mp == 1000
    assert detail["rule"] == 4
    assert detail["median_draws"] == 3
    assert detail["draw_permilles"] == [1000, 1000, 1000]
    assert detail["basis"] == MOTION_BASIS_V4


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg")
def test_engine_motion_stable_across_submission_ids(tmp_path):
    """Same bundle bytes → same motion sample/draws even when submission id (and commit) differ."""
    import review_fixture as F
    from test_referee_service import Env

    from qor_receipt.review.engine import SPOT, Referee
    from qor_receipt.review.motion import motion_sample_draws

    if not F.have_ffmpeg():
        return
    e = Env(tmp_path, tail_ms=8_000, k=4)

    def once(tag: str):
        ref = Referee(
            e.key,
            workdir=tmp_path / f"work-{tag}",
            now=lambda: e.clock["t"],
            rng=random.Random(999),  # spot-check RNG — must not affect motion draws
            core_factory=F.fake_factory(),
            tail_ms=8_000,
            random_segments=4,
        )
        sub = ref.create(e.fx["files"], SPOT)
        assert sub.state == "awaiting_segments", sub.errors
        plan = json.loads((sub.dir / "plan.json").read_text(encoding="utf-8"))
        expected = motion_sample_draws(
            sub.bundle.segments,
            chain_head=sub.bundle.chain_head,
            checkpoint_head=sub.bundle.checkpoints[-1]["hash"],
            input_head=sub.bundle.input_head,
            mode=sub.mode,
        )
        assert plan["motion_draws"] == expected == sub.motion_draws
        assert plan["motion"] == sub.motion_sample == sorted(set().union(*map(set, expected)))
        assert "submission" not in json.dumps(expected)
        return sub.id, sub.commit, plan["motion_draws"], plan["motion"]

    id1, c1, draws1, union1 = once("a")
    id2, c2, draws2, union2 = once("b")
    assert id1 != id2
    assert c1 != c2  # commit includes submission id
    assert draws1 == draws2
    assert union1 == union2


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg")
def test_review_uses_motion_rule_v4(tmp_path):
    import review_fixture as F
    from test_referee_service import Env

    from qor_receipt import receipt as R

    if not F.have_ffmpeg():
        return
    e = Env(tmp_path, tail_ms=8_000, k=4)
    sub = e.submit()
    plan = json.loads((sub.dir / "plan.json").read_text(encoding="utf-8"))
    assert len(plan["motion_draws"]) == 3
    assert all(len(d) <= 48 for d in plan["motion_draws"])
    assert len(plan["motion"]) <= 48 * 3
    e.upload_all(sub)
    out = e.ref.review(sub)
    assert out["state"] == "awaiting_player_signature", json.dumps(out.get("review", out), indent=1)[:2000]
    detail = out["review"]["checks"]["motion"]["detail"]
    assert detail["rule"] == 4
    assert detail["basis"] == MOTION_BASIS_V4
    assert out["receipt"]["signals"]["motion_basis"] == MOTION_BASIS_V4
    assert detail["sample_max_segments"] == 48
    assert detail["median_draws"] == 3
    assert len(detail["draw_permilles"]) == 3
    assert detail["sample_draws"] == plan["motion_draws"]
    assert detail["motion_permille"] >= R.MOTION_MIN_PERMILLE
    assert "motion-rule/v4" in out["receipt"]["review"]["scope"]
    assert "content-head seeded" in out["receipt"]["review"]["scope"]
