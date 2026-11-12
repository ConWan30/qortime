"""Referee frame rate (QorProof r2 QM1 MED and QM2 LOW): the capture clock, challenge-rule v2 and motion
timing, and match_index seek times use the recording's own rate, not a hard-coded 30.

* 30 fps output is byte-identical to pre-fix main fda7b51 (goldens in tests/fixtures/referee_fps).
* A 60 fps recording gets the right clock fit, and its millisecond timing matches the 30 fps equivalent
  within one frame.
* An unknown or mixed rate fails closed with a reason. Missing rate information stays the legacy 30/1.
Synthetic data only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "tests" / "fixtures" / "referee_fps"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(GOLD))
import make_goldens as G  # noqa: E402

from qor_receipt.match_index import build_index  # noqa: E402
from qor_receipt.review import rate as RT  # noqa: E402
from qor_receipt.review.clock import capture_clock, frame_mono_ms, motion_mono_ms  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ---------------------------------------------------------------- 30 fps goldens (pre-fix main)
def test_clock_fit_30fps_golden_is_byte_identical():
    assert G.clock_golden() == (GOLD / "clock_30fps_fda7b51.txt").read_text(encoding="utf-8")


def test_match_index_30fps_golden_is_byte_identical():
    assert G.match_index_golden() == (GOLD / "match_index_30fps_fda7b51.json").read_text(encoding="utf-8")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg needed for the synthetic fMP4 bundle")
def test_referee_verdict_and_report_30fps_golden_is_byte_identical():
    assert G.referee_golden() == (GOLD / "referee_30fps_fda7b51.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- capture clock at 60 fps
def _counter(fps: int, intercept: float = 2646.3, n: int = 40, frag_s: float = 2.0) -> list[dict]:
    """Close stamps of a live counter at ``fps`` with a few ms of deterministic jitter."""
    segs = [{"kind": "init", "frames": 0, "mono_ms": 0}]
    for i in range(1, n + 1):
        fr = int(round(i * frag_s * fps)) + (i % 3) - 1
        segs.append({"kind": "media", "frames": fr, "mono_ms": int(round(intercept + fr * 1000.0 / fps + (i * 7) % 5 - 2))})
    return segs


def test_capture_clock_fits_a_60fps_counter():
    a, s = capture_clock(_counter(60), 60.0)
    assert abs(s - 1000.0 / 60.0) < 0.01 and abs(a - 2646.3) < 5
    # The pre-fix behaviour this replaces: at the 30 fps nominal the 60 fps slope fails the 5 % check.
    assert capture_clock(_counter(60)) == (0.0, 1000.0 / 30.0)
    assert capture_clock([], 60.0) == (0.0, 1000.0 / 60.0)
    for bad in (0, -1, float("nan"), float("inf"), 1001, "60"):
        with pytest.raises(ValueError):
            capture_clock(_counter(60), bad)


@pytest.mark.parametrize("pts0", [0.0, 1500.0, 63_366.667])
def test_60fps_challenge_and_motion_ms_match_30fps_within_one_frame(pts0):
    c30, c60 = capture_clock(_counter(30), 30.0), capture_clock(_counter(60), 60.0)
    one_frame = 1000.0 / 60.0
    for n in range(0, 900, 11):
        # challenge-rule v2: decoded at 30 Hz from either recording
        assert abs(frame_mono_ms(c60, pts0, n, 30.0, 60.0) - frame_mono_ms(c30, pts0, n, 30.0, 30.0)) <= one_frame
        # motion: sampled at 10 Hz
        assert abs(motion_mono_ms(c60, pts0, n, 10.0, 60.0) - motion_mono_ms(c30, pts0, n, 10.0, 30.0)) <= one_frame
    # fps=None keeps the old one-decoded-frame-per-media-frame meaning; fps=30 at hz=30 is the same arithmetic
    assert frame_mono_ms(c30, pts0, 17, 30.0) == frame_mono_ms(c30, pts0, 17, 30.0, 30.0)


# ---------------------------------------------------------------- rate parsing and sources
@pytest.mark.parametrize("raw,want", [("60/1", Fraction(60)), ("60000/1001", Fraction(60000, 1001)), (" 30/1 ", Fraction(30)),
                                      ("1/1", Fraction(1)), ("1000/1", Fraction(1000))])
def test_parse_rate_accepts_rationals(raw, want):
    assert RT.parse_rate(raw) == want


@pytest.mark.parametrize("raw", ["0/0", "0/1", "60/0", "-60/1", "60.0/1", "6_0/1", "nan/1", "\uff16\uff10/1", "60", "", "garbage",
                                 "60/1/1", "1e3/1", "5000/1", "1/2", "9" * 400 + "/1", "60/1\n60/1", 60, 60.0, True, None,
                                 ["60/1"], {"num": 60}])
def test_parse_rate_rejects_garbage(raw):
    with pytest.raises(RT.RateError):
        RT.parse_rate(raw)


def _box(typ: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + typ + payload


def _full(typ: bytes, flags: int, payload: bytes, version: int = 0) -> bytes:
    return _box(typ, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _u32(v: int) -> bytes:
    return v.to_bytes(4, "big")


def _init(timescale: int = 15360, trex_dur: int | None = None, audio: bool = True) -> bytes:
    def trak(tid, handler, ts):
        tkhd = _full(b"tkhd", 3, _u32(0) * 2 + _u32(tid) + bytes(60))
        mdhd = _full(b"mdhd", 0, _u32(0) * 2 + _u32(ts) + _u32(0) + bytes(4))
        hdlr = _full(b"hdlr", 0, _u32(0) + handler + bytes(12))
        return _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr))
    moov = trak(1, b"vide", timescale) + (trak(2, b"soun", 48000) if audio else b"")
    if trex_dur is not None:
        moov += _box(b"mvex", _full(b"trex", 0, _u32(1) + _u32(1) + _u32(trex_dur) + _u32(0) + _u32(0)))
    return _box(b"ftyp", b"isom" + _u32(0)) + _box(b"moov", moov)


def _frag(durs: list[int] | None = None, default: int | None = None, count: int = 120, tid: int = 1,
          audio: bool = True) -> bytes:
    tfhd = _full(b"tfhd", 0x020000 | (0x08 if default is not None else 0), _u32(tid) + (_u32(default) if default is not None else b""))
    if durs is not None:
        trun = _full(b"trun", 0x000101, _u32(len(durs)) + _u32(0) + b"".join(_u32(d) for d in durs))
    else:
        trun = _full(b"trun", 0x000001, _u32(count) + _u32(0))
    trafs = _box(b"traf", tfhd + _full(b"tfdt", 0, _u32(0)) + trun)
    if audio:
        atfhd = _full(b"tfhd", 0x020008, _u32(2) + _u32(1024))
        trafs += _box(b"traf", atfhd + _full(b"tfdt", 0, _u32(0)) + _full(b"trun", 0x000001, _u32(94) + _u32(0)))
    return _box(b"moof", _full(b"mfhd", 0, _u32(1)) + trafs) + _box(b"mdat", b"")


def test_fmp4_rate_reads_the_video_track_sample_durations():
    assert RT.fmp4_rate(_init(), [_frag(default=256)]) == 60          # tfhd default (ffmpeg's usual form)
    assert RT.fmp4_rate(_init(), [_frag(default=512)]) == 30
    assert RT.fmp4_rate(_init(), [_frag(durs=[256] * 119 + [300])]) == 60  # per-sample, modal
    assert RT.fmp4_rate(_init(trex_dur=256), [_frag()]) == 60           # trex default from the init
    assert RT.fmp4_rate(_init(timescale=60000), [_frag(default=1001)]) == Fraction(60000, 1001)
    assert RT.fmp4_rate(_init(), [_frag(default=256, audio=False)]) == 60
    # no duration anywhere, or no video track: no information (the legacy default applies upstream)
    assert RT.fmp4_rate(_init(), [_frag()]) is None
    assert RT.fmp4_rate(b"", [_frag(default=256)]) is None
    assert RT.fmp4_rate(_init(), []) is None
    # stated but unusable: fail closed
    for bad in (_frag(default=0), _frag(default=1), _frag(durs=[15360 * 2] * 10)):
        with pytest.raises(RT.RateError):
            RT.fmp4_rate(_init(), [bad])


def test_bundle_rate_legacy_mixed_and_garbage():
    v60, v30 = ("a.mp4", _init(), [_frag(default=256)]), ("b.mp4", _init(), [_frag(default=512)])
    legacy = ("a.mp4", _init(), [_frag()])
    assert RT.bundle_rate({}, []) == RT.LEGACY
    assert RT.bundle_rate({}, [legacy]) == RT.LEGACY
    assert RT.bundle_rate({}, [v60]) == 60
    assert RT.bundle_rate({"frame_rate": "60/1"}, [legacy]) == 60
    assert RT.bundle_rate({"frame_rate": "60000/1001"}, [v60]) == 60    # within 1 %: the media's own rate wins
    with pytest.raises(RT.RateError, match="mixed"):
        RT.bundle_rate({}, [v60, v30])
    with pytest.raises(RT.RateError, match="mixed"):
        RT.bundle_rate({"frame_rate": "30/1"}, [v60])
    for bad in ("sixty", None, 60, "0/0"):
        with pytest.raises(RT.RateError):
            RT.bundle_rate({"frame_rate": bad}, [v60])


def test_frame_clock_rates_mirror_align_rs():
    rows = [{"type": "frame", "file": "p1.ts", "frame": i, "t_mono_us": i, "pts_90k": 1500 * i} for i in range(20)]
    assert RT.frame_clock_rates(rows) == [("frame_clock.jsonl p1.ts", Fraction(60))]
    assert RT.frame_clock_rates(rows[:10]) == []                          # fewer than 10 steps: no rate
    with pytest.raises(RT.RateError):
        RT.frame_clock_rates([dict(r, pts_90k=r["frame"]) for r in rows])  # 90000 fps


# ---------------------------------------------------------------- engine: fail closed, never a 30 guess
def _sub(meta: dict, files: dict[int, bytes]):
    segs = [{"seq": s, "file": "video.mp4", "offset": 0 if s == 0 else s * 100, "mono_ms": s * 2000} for s in sorted(files)]
    b = SimpleNamespace(meta=meta, segments=segs)
    return SimpleNamespace(bundle=b, received={s: 1 for s in files}, decode=set(files)), files.get


def test_engine_frame_rate_is_measured_legacy_or_an_error():
    from qor_receipt.review.engine import Referee

    sub, rd = _sub({}, {0: _init(), 1: _frag(default=256), 2: _frag(default=256)})
    assert Referee._frame_rate(None, sub, rd) == 60.0
    sub, rd = _sub({}, {0: _init(), 1: _frag(), 2: _frag()})
    assert Referee._frame_rate(None, sub, rd) == 30.0
    sub, rd = _sub({"frame_rate": "30/1"}, {0: _init(), 1: _frag(default=256)})
    assert isinstance(Referee._frame_rate(None, sub, rd), RT.RateError)
    sub, rd = _sub({"frame_rate": "garbage"}, {0: _init(), 1: _frag()})
    assert isinstance(Referee._frame_rate(None, sub, rd), RT.RateError)


def test_motion_with_an_unknown_rate_is_not_computed(monkeypatch):
    from qor_receipt.review import engine as E

    monkeypatch.setattr(E.M, "ffmpeg_bin", lambda: "ffmpeg")
    sub = SimpleNamespace(bundle=SimpleNamespace(inputs=[{"t_us": 0}]), motion_draws=[[1, 2]], motion_sample=[1, 2],
                          received={1: 1, 2: 1})
    mp, detail = E.Referee._motion(None, sub, lambda s: None, [], 0, RT.RateError("mixed frame rates: x"))
    assert mp is None and detail["bins"] == 0 and "frame rate UNMEASURED: mixed frame rates" in detail["note"]


# ---------------------------------------------------------------- match_index seek times
def _index(tmp: Path, fps: int, **meta):
    b = G.match_index_bundle(tmp / f"b{fps}", fps=fps, extra_meta=meta)
    return b, build_index(b, tmp / f"o{fps}", frames=False)


def _frame_clock(n: int = 30) -> str:
    return "\n".join(json.dumps({"type": "frame", "file": "v.ts", "frame": i, "t_mono_us": i, "pts_90k": 1500 * i}) for i in range(n))


def test_match_index_60fps_seek_times_equal_30fps(tmp_path):
    _, i30 = _index(tmp_path, 30)
    _, i60 = _index(tmp_path, 60, frame_rate="60/1")
    assert [e["recording_s"] for e in i60["events"]] == [e["recording_s"] for e in i30["events"]]
    assert i60["video"]["recording_duration_s"] == i30["video"]["recording_duration_s"]
    assert i60["video"]["frame_rate"] == "60/1" and "frames/(60/1)" in i60["video"]["alignment_note"]
    assert "frame_rate" not in i30["video"]
    # frame_clock.jsonl as the rate source instead of bundle.json
    b = G.match_index_bundle(tmp_path / "fc", fps=60)
    (b / "frame_clock.jsonl").write_text(_frame_clock())
    ifc = build_index(b, tmp_path / "ofc", frames=False)
    assert [e["recording_s"] for e in ifc["events"]] == [e["recording_s"] for e in i30["events"]]


@pytest.mark.parametrize("meta", [{"frame_rate": "sixty"}, {"frame_rate": None}, {"frame_rate": "0/0"}])
def test_match_index_garbage_rate_gives_no_seek_times(tmp_path, meta):
    _, idx = _index(tmp_path, 60, **meta)
    assert idx["events"] and all(e["recording_s"] is None for e in idx["events"])
    assert all(e["frames_missing_reason"].startswith("frame rate UNMEASURED") for e in idx["events"])
    assert idx["video"]["frame_rate"].startswith("UNMEASURED") and idx["video"]["recording_duration_s"] is None


def test_match_index_mixed_rates_give_no_seek_times(tmp_path):
    b = G.match_index_bundle(tmp_path / "m", fps=60, extra_meta={"frame_rate": "30/1"})
    (b / "frame_clock.jsonl").write_text(_frame_clock())
    idx = build_index(b, tmp_path / "o", frames=False)
    assert all(e["recording_s"] is None for e in idx["events"]) and "mixed" in idx["video"]["frame_rate"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe needed")
def test_match_index_probes_a_supplied_60fps_video(tmp_path):
    v = tmp_path / "v60.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=60:duration=95",
                    "-pix_fmt", "yuv420p", str(v)], check=True)
    _, i30 = _index(tmp_path, 30)
    b = G.match_index_bundle(tmp_path / "p", fps=60)
    idx = build_index(b, tmp_path / "o", video=v, frames=False)
    assert idx["video"]["frame_rate"] == "60/1"
    assert [e["recording_s"] for e in idx["events"]] == [e["recording_s"] for e in i30["events"]]


# ---------------------------------------------------------------- referee end to end (synthetic fMP4)
def _scenarios(tmp: Path, fps: int, **kw) -> dict:
    return {name: G.referee_projection(out) for name, _e, out in G.referee_scenarios(tmp, fps=fps, **kw) if name != "rule1"}


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg needed for the synthetic fMP4 bundle")
def test_referee_60fps_rule_v2_and_motion_match_30fps(tmp_path):
    """A 7 s PTS origin puts the fit intercept off zero, as on a real capture. Before the fix the 60 fps
    fit fell back to (0.0, 33.3), dating every frame 7 s early: rule v2 missed the pause and motion bins shifted."""
    r30 = _scenarios(tmp_path / "30", 30, pts_offset_ms=7_000)
    r60 = _scenarios(tmp_path / "60", 60, pts_offset_ms=7_000)
    for name in r30:
        a, b = r30[name], r60[name]
        assert b["checks"]["challenges"]["state"] == "pass" and b["checks"]["challenges"]["detail"]["notes"] == [], b["checks"]
        la = [c["latency_ms"] for c in a["challenges"]]
        lb = [c["latency_ms"] for c in b["challenges"]]
        assert len(lb) == 3 and all(abs(x - y) <= 1000 / 60 for x, y in zip(la, lb)), (la, lb)
        ma, mb = a["checks"]["motion"]["detail"], b["checks"]["motion"]["detail"]
        assert b["checks"]["motion"]["state"] == "pass" and mb["clock_fallbacks"] == 0
        assert mb["bins"] == ma["bins"] and mb["excluded"] == ma["excluded"]
        assert abs(mb["motion_permille"] - ma["motion_permille"]) <= 5, (ma["motion_permille"], mb["motion_permille"])


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg needed for the synthetic fMP4 bundle")
@pytest.mark.parametrize("meta,why", [({"frame_rate": "30/1"}, "mixed frame rates"), ({"frame_rate": "sixty"}, "not a frame rate")])
def test_referee_mixed_or_garbage_rate_fails_closed(tmp_path, meta, why):
    r = _scenarios(tmp_path, 60, pts_offset_ms=7_000, extra_meta=meta)
    for p in r.values():
        ch = p["checks"]["challenges"]
        assert ch["state"] != "pass" and not any((c or {}).get("ok") for c in p["challenges"])
        assert any("frame rate UNMEASURED" in n and why in n for n in ch["detail"]["notes"]), ch
        mo = p["checks"]["motion"]
        assert mo["state"] != "pass" and why in mo["detail"]["note"]
