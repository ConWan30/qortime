"""Regenerate the 30 fps goldens for the referee frame-rate fix.

The committed goldens were generated from pre-fix main fda7b51 with:

    python tests/fixtures/referee_fps/make_goldens.py clock        > tests/fixtures/referee_fps/clock_30fps_fda7b51.txt
    python tests/fixtures/referee_fps/make_goldens.py match_index  > tests/fixtures/referee_fps/match_index_30fps_fda7b51.json
    python tests/fixtures/referee_fps/make_goldens.py referee      > tests/fixtures/referee_fps/referee_30fps_fda7b51.json

The referee golden was regenerated on fda7b51's product code (qor_receipt/ at fda7b51, this tests/
tree) after encoder-size fields were stripped and ``review.frame_rate`` was added to the projection.
``referee`` needs ffmpeg (it builds the synthetic fMP4 bundle in tests/review_fixture.py).
Only public call shapes that exist on fda7b51 are used, so the same script runs before and after
the fix. Synthetic data only.
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

W0 = 1_800_000_000_000


def _f(x: float) -> str:
    # 6 decimals: sum() of floats is compensated on Python >= 3.12 and plain on 3.11, so the
    # last bits of a fit can differ by version. Every published value is an int (rounded ms).
    return f"{x:.6f}"


def clock_manifests() -> list[tuple[str, list[dict]]]:
    rng = random.Random(30)
    out: list[tuple[str, list[dict]]] = []
    segs = [{"kind": "init", "frames": 129, "mono_ms": 6768}]
    for fr in range(129, 129 + 61 * 20, 61):
        segs.append({"kind": "media", "frames": fr, "mono_ms": int(round(2646.3 + 33.3346 * fr))})
    out.append(("challenge_v2_test_counter", segs))
    for name, slope, n, step, jitter in (("jittered_nominal", 1000.0 / 30.0, 48, 60, 3),
                                         ("jittered_slow_4pct", 34.6, 30, 61, 5),
                                         ("jittered_fast_4pct", 32.0, 30, 59, 5),
                                         ("off_by_6pct", 35.4, 30, 60, 0),
                                         ("long_match", 1000.0 / 30.0, 400, 60, 8)):
        segs = [{"kind": "init", "frames": 0, "mono_ms": 0}]
        fr = 17
        for _ in range(n):
            fr += step + rng.randint(-1, 1)
            segs.append({"kind": "media", "frames": fr, "mono_ms": int(round(1234.5 + slope * fr + rng.randint(-jitter, jitter)))})
        out.append((name, segs))
    out.append(("constant_counter", [{"kind": "media", "frames": 60, "mono_ms": i * 2000} for i in range(1, 10)]))
    out.append(("four_media", [{"kind": "media", "frames": 60 * i, "mono_ms": i * 2000} for i in range(1, 5)]))
    out.append(("span_299_frames", [{"kind": "media", "frames": 1 + 299 * i // 5, "mono_ms": 10 * i} for i in range(6)]))
    out.append(("span_300_frames", [{"kind": "media", "frames": 1 + 60 * i, "mono_ms": 2000 * i} for i in range(6)]))
    out.append(("junk_rows", [{"kind": "media", "frames": True, "mono_ms": 5}, {"kind": "media", "frames": "60", "mono_ms": 9},
                              {"kind": "media", "frames": 0, "mono_ms": 1}, {"kind": "media", "frames": 30, "mono_ms": False}]
                + [{"kind": "media", "frames": 60 * i, "mono_ms": 2000 * i + 7} for i in range(1, 12)]))
    out.append(("empty", []))
    return out


def clock_golden() -> str:
    from qor_receipt.review.clock import capture_clock, frame_mono_ms, motion_mono_ms

    lines = []
    for name, segs in clock_manifests():
        a, s = capture_clock(segs)
        lines.append(f"capture_clock {name} {_f(a)} {_f(s)}")
        for pts0 in (0.0, 1500.0, 63_366.667):
            fm = [frame_mono_ms((a, s), pts0, n, 30.0) for n in range(0, 600, 37)]
            m10 = [motion_mono_ms((a, s), pts0, n, 10.0) for n in range(0, 200, 13)]
            m30 = [motion_mono_ms((a, s), pts0, n, 30.0) for n in range(0, 600, 37)]
            lines.append(f"  pts0={pts0} frame30={fm}")
            lines.append(f"  pts0={pts0} motion10={m10}")
            lines.append(f"  pts0={pts0} motion30={m30}")
    return "\n".join(lines) + "\n"


def match_index_bundle(d: Path, *, fps: int = 30, extra_meta: dict | None = None) -> Path:
    from qor_receipt.jcs import sha256_hex

    def read(t, a, h, q=1, c=100):
        return {"t_ms": W0 + t, "away": a, "home": h, "quarter": q, "clock_s": c, "clock_red": False}

    d.mkdir(parents=True, exist_ok=True)
    tl = [read(i * 1000, 0, 0) for i in range(10)] + [read(i * 1000, 0, 7) for i in range(10, 40)]
    tl += [read(i * 1000, 3, 7, q=2) for i in range(40, 70)] + [read(i * 1000, 3, 7, q=3) for i in range(70, 90)]
    (d / "timeline.jsonl").write_text("\n".join(json.dumps(x) for x in tl))
    cps = []
    for i in range(0, 92, 2):
        q = {"trigger": "segment", "segment_seq": i, "segment_hash": "ab" * 32, "chain_head": "cd" * 32, "score": None}
        if i == 10:
            q.update(trigger="score_change", score={"away": 0, "home": 7, "quarter": 1})
        cps.append({"seq": len(cps), "t_ms": W0 + i * 1000 + 100, "hash": f"{i:064x}", "prev": "0" * 64,
                    "frames_hash": sha256_hex(q), "qorcap": q})
    (d / "checkpoints.jsonl").write_text("\n".join(json.dumps(x) for x in cps))
    # Close stamps 2 s apart with a 37 ms lag and a frame counter at the recording's own rate.
    man = [{"type": "segment", "seq": i, "mono_ms": i * 2000 + 37, "wall_ms": W0 + i * 2000 + 37, "frames": i * 2 * fps}
           for i in range(47)]
    (d / "manifest.jsonl").write_text("\n".join(json.dumps(x) for x in man))
    ins = [{"type": "header", "wall_anchor_ms": W0, "t_us": 0}]
    ins += [{"type": "in", "t_us": t * 1000, "b": b} for t, b in [(9000, 0), (9500, 1), (9700, 0), (41_000, 1 << 9), (41_200, 0)]]
    (d / "inputs.jsonl").write_text("\n".join(json.dumps(x) for x in ins))
    (d / "haptics.jsonl").write_text(json.dumps({"type": "haptic", "t_wall_ms": W0 + 10200, "energy": 500, "duration_ms": 90,
                                                 "confidence": 800}))
    meta = {"schema": "qorcap-bundle/v0", "match_id": "fps-golden", "final": {"away": 3, "home": 7}, "final_confirmed": True,
            "chain_head": "cd" * 32, "clips": [{"name": "score-a0-h7-q1", "sha256": "ee" * 32}]}
    meta.update(extra_meta or {})
    (d / "bundle.json").write_text(json.dumps(meta))
    (d / "receipt.json").write_text(json.dumps({"result": {"away_score": 3, "home_score": 7, "final_confirmed": True,
                                                           "method": "end_menu_read", "final_t_ms": W0 + 89_000}}))
    return d


def match_index_golden() -> str:
    from qor_receipt.match_index import build_index

    with tempfile.TemporaryDirectory() as t:
        b = match_index_bundle(Path(t) / "b")
        build_index(b, Path(t) / "o", frames=False)
        return (Path(t) / "o" / "match_index.json").read_text(encoding="utf-8")


VOLATILE = {"pubkey", "key_id", "sig", "signature", "submission", "id", "nonce", "pattern", "issuer", "hid_sha256", "commit",
            "reviewed_ms", "capture_referee", "local_receipt_sha256", "chain_head", "checkpoint_head", "inputs_chain_head", "inputs_sha256",
            # encoder output sizes depend on the ffmpeg/libx264 build, not on the referee
            "bytes_total", "bytes_verified", "bytes_required", "bytes"}


def _strip(v):
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if k not in VOLATILE}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


def referee_scenarios(tmp: Path, fps: int = 30, pts_offset_ms: int = 0, extra_meta: dict | None = None):
    """(name, env, review output) for the synthetic fMP4 bundle: rule v1, rule v2 on a live counter, rule v2
    with a 4 s close lag. ``fps``/``pts_offset_ms``/``extra_meta`` are only used by the 60 fps tests; the
    30 fps golden uses the defaults."""
    from test_referee_service import Env

    def run(name, rule, lag):
        kw = {"fps": fps} if fps != 30 else {}
        if pts_offset_ms:
            kw["pts_offset_ms"] = pts_offset_ms
        e = Env(tmp / name, tail_ms=8_000, k=2, **kw)
        meta = json.loads(e.fx["files"]["bundle.json"])
        if rule == 2:
            lines = []
            for line in e.fx["files"]["manifest.jsonl"].decode().splitlines():
                if not line.strip():
                    continue
                m = json.loads(line)
                if m.get("kind") != "init":
                    m["mono_ms"] = int(m["mono_ms"]) + lag
                    m["wall_ms"] = int(m["wall_ms"]) + lag
                    # The live counter: frame (PTS origin + media ms) * fps, stamped at the lagged close.
                    m["frames"] = int(round((m["mono_ms"] + pts_offset_ms) * fps / 1000))
                lines.append(json.dumps(m))
            e.fx["files"]["manifest.jsonl"] = ("\n".join(lines) + "\n").encode()
            meta["challenge_rule"] = 2
        meta.update(extra_meta or {})
        if rule == 2 or extra_meta:
            e.fx["files"]["bundle.json"] = json.dumps(meta).encode()
        sub = e.submit()
        e.upload_all(sub)
        out = e.ref.review(sub)
        return name, e, out

    yield run("rule1", 1, 0)
    yield run("rule2_live_counter", 2, 0)
    yield run("rule2_close_lag_4s", 2, 4_000)


def referee_projection(out: dict) -> dict:
    rec = out.get("receipt") or {}
    return _strip({"state": out.get("state"), "errors": out.get("errors"), "checks": (out.get("review") or {}).get("checks"),
                   "frame_rate": (out.get("review") or {}).get("frame_rate"),
                   "challenges": [c.get("result") for c in rec.get("challenges") or []],
                   "signals": rec.get("signals"), "result": rec.get("result")})


def referee_golden() -> str:
    from qor_receipt.review.local import challenge_results

    doc = {}
    with tempfile.TemporaryDirectory() as t:
        for name, e, out in referee_scenarios(Path(t)):
            doc[name] = referee_projection(out)
        # Recorder-side re-run (review/local.py) on the rule-1 bundle as written to disk.
        e_dir = Path(t) / "rule1" / "bundle"
        rec = json.loads((e_dir / "receipt.json").read_text())
        doc["local_rule1"] = _strip([r["local"] for r in challenge_results(e_dir, rec["challenges"])])
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    what = sys.argv[1]
    sys.stdout.write({"clock": clock_golden, "match_index": match_index_golden, "referee": referee_golden}[what]())
