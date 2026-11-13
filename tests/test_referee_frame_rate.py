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
          audio: bool = True, sizes: bool = False, mdat: int = 4096) -> bytes:
    tfhd = _full(b"tfhd", 0x020000 | (0x08 if default is not None else 0), _u32(tid) + (_u32(default) if default is not None else b""))
    if durs is not None:
        trun = _full(b"trun", 0x000101, _u32(len(durs)) + _u32(0) + b"".join(_u32(d) for d in durs))
    elif sizes:  # sample sizes only (0x200), no durations anywhere
        trun = _full(b"trun", 0x000201, _u32(count) + _u32(0) + _u32(100) * count)
    else:
        trun = _full(b"trun", 0x000001, _u32(count) + _u32(0))
    trafs = _box(b"traf", tfhd + _full(b"tfdt", 0, _u32(0)) + trun)
    if audio:
        atfhd = _full(b"tfhd", 0x020008, _u32(2) + _u32(1024))
        trafs += _box(b"traf", atfhd + _full(b"tfdt", 0, _u32(0)) + _full(b"trun", 0x000001, _u32(94) + _u32(0)))
    return _box(b"moof", _full(b"mfhd", 0, _u32(1)) + trafs) + _box(b"mdat", bytes(mdat))


def test_fmp4_rate_reads_the_video_track_sample_durations():
    assert RT.fmp4_rate(_init(), [_frag(default=256)]) == 60          # tfhd default (ffmpeg's usual form)
    assert RT.fmp4_rate(_init(), [_frag(default=512)]) == 30
    assert RT.fmp4_rate(_init(), [_frag(durs=[256] * 120)]) == 60  # per-sample, exact 60/1
    assert RT.fmp4_rate(_init(trex_dur=256), [_frag()]) == 60           # trex default from the init
    assert RT.fmp4_rate(_init(timescale=60000), [_frag(default=1001)]) == Fraction(60000, 1001)
    assert RT.fmp4_rate(_init(), [_frag(default=256, audio=False)]) == 60
    # no duration anywhere, or no video track: no information (the legacy default applies upstream)
    assert RT.fmp4_rate(_init(), [_frag()]) is None
    assert RT.fmp4_rate(b"", [_frag(default=256)]) is None
    assert RT.fmp4_rate(_init(), []) is None
    # stated but unusable: fail closed
    for bad in (_frag(default=0), _frag(default=1), _frag(durs=[15360 * 2] * 10),
                _frag(durs=[256] * 119 + [300]),        # variable: mean 59.92 does not snap
                _frag(default=256, count=5)):           # fewer than MIN_SAMPLES
        with pytest.raises(RT.RateError):
            RT.fmp4_rate(_init(), [bad])


def test_m1_sizes_without_durations_and_huge_counts_never_crash():
    # (a) sample sizes but no duration source anywhere: skipped, no information, no TypeError
    assert RT.fmp4_rate(_init(), [_frag(sizes=True)]) is None
    assert RT.bundle_rate({}, [("a.mp4", _init(), [_frag(sizes=True)])]) == RT.Resolved(RT.LEGACY, "legacy")
    # (b) a u32 count with a default duration: rejected against the bytes present, nothing materialised
    with pytest.raises(RT.RateError, match="states 4294967295 samples"):
        RT.fmp4_rate(_init(), [_frag(default=256, count=0xFFFFFFFF)])
    # per-sample count larger than the trun carries (still under the per-trun cap)
    f = bytearray(_frag(durs=[256] * 20, audio=False))
    i = f.index(b"trun") + 8
    f[i:i + 4] = (25).to_bytes(4, "big")
    with pytest.raises(RT.RateError, match="carries fields for 20"):
        RT.fmp4_rate(_init(), [bytes(f)])
    # a u32 count on a per-sample trun hits the cap, not a walk of 2^32 rows
    f[i:i + 4] = (0xFFFFFFFF).to_bytes(4, "big")
    with pytest.raises(RT.RateError, match="cap 65536"):
        RT.fmp4_rate(_init(), [bytes(f)])
    # truncated / garbage boxes: a RateError or no information, never another exception
    for junk in (b"\x00\x00\x00\x10moof" + b"\xff" * 8, _frag(default=256)[:40], b"\xff" * 64):
        try:
            RT.fmp4_rate(_init(), [junk])
        except RT.RateError:
            pass


def test_m1_engine_parse_failure_is_unmeasured(monkeypatch):
    from qor_receipt.review.engine import Referee

    def boom(*a, **k):
        raise TypeError("crafted")
    monkeypatch.setattr(RT, "_add_fragment", boom)
    sub, rd = _sub({}, {0: _init(), 1: _frag(default=256)})
    r = Referee._frame_rate(None, sub, rd)
    assert isinstance(r, RT.RateError) and "parse error: TypeError" in str(r)


def _alternating(timescale: int, per_frame: Fraction, n: int) -> list[int]:
    """Integer sample durations whose running sum tracks n * per_frame exactly (what a muxer writes)."""
    out, prev = [], 0
    for k in range(1, n + 1):
        t = int(k * per_frame)
        out.append(t - prev)
        prev = t
    return out


@pytest.mark.parametrize("timescale,rate", [(90_000, Fraction(60000, 1001)), (90_000, Fraction(30000, 1001)),
                                            (15_360, Fraction(30000, 1001)), (15_360, Fraction(60000, 1001)),
                                            (90_000, Fraction(60)), (90_000, Fraction(30)), (15_360, Fraction(60)),
                                            (15_360, Fraction(30))])
def test_m2_ntsc_at_an_inexact_timescale_is_exact(timescale, rate):
    durs = _alternating(timescale, timescale / rate, 240)
    if rate.denominator == 1001 and timescale == 90_000 and rate == Fraction(60000, 1001):
        assert set(durs) == {1501, 1502}
    assert RT.fmp4_rate(_init(timescale=timescale), [_frag(durs=durs[:120]), _frag(durs=durs[120:])]) == rate
    # the same through frame_clock.jsonl (90 kHz PTS)
    pts = [0] + list(__import__("itertools").accumulate(_alternating(90_000, 90_000 / rate, 60)))
    rows = [{"type": "frame", "file": "p.ts", "frame": i, "t_mono_us": i, "pts_90k": p} for i, p in enumerate(pts)]
    assert RT.frame_clock_rates(rows) == [("frame_clock.jsonl p.ts", rate)]


def test_m2_snapped_ntsc_clock_drift_at_one_hour_is_under_a_millisecond():
    rate = Fraction(60000, 1001)
    n = int(3600 * rate)                                   # frames in one hour
    durs = _alternating(90_000, 90_000 / rate, n)
    got = RT.fmp4_rate(_init(timescale=90_000), [_frag(durs=durs[i:i + 600]) for i in range(0, 6000, 600)])
    assert got == rate
    pts_ms = sum(durs) * 1000.0 / 90_000                    # PTS of the frame after the last one
    fps = float(got)
    clock = (0.0, 1000.0 / float(rate))  # the manifest counter fit: the true ms per frame
    true_ms = n * 1000.0 / float(rate)
    # PTS -> frame index on the snapped rate -> fit: under 1 ms from the true time at 1 h
    assert abs(frame_mono_ms(clock, pts_ms, 0, 30.0, fps) - true_ms) < 1.0
    # the old modal estimate (90000/1501) is about 1.2 s off at the same point
    assert abs(frame_mono_ms(clock, pts_ms, 0, 30.0, 90_000 / 1501) - true_ms) > 1000


def test_bundle_rate_legacy_mixed_and_garbage():
    v60, v30 = ("a.mp4", _init(), [_frag(default=256)]), ("b.mp4", _init(), [_frag(default=512)])
    legacy = ("a.mp4", _init(), [_frag()])
    assert RT.bundle_rate({}, []) == RT.Resolved(RT.LEGACY, "legacy")
    assert RT.bundle_rate({}, [legacy]).legacy
    assert RT.bundle_rate({}, [v60]).rate == 60
    assert RT.bundle_rate({}, [v60, ("c.mp4", _init(), [_frag(default=256)])]).rate == 60
    # L1: bundle.json is declared, not measured: it only cross-checks
    with pytest.raises(RT.RateError, match="declared only"):
        RT.bundle_rate({"frame_rate": "60/1"}, [legacy])
    assert RT.bundle_rate({"frame_rate": "60000/1001"}, [v60]).rate == 60   # declared within 1 %: the media wins
    # L2: measured files must agree exactly
    ntsc = ("n.mp4", _init(timescale=60000), [_frag(default=1001)])
    with pytest.raises(RT.RateError, match="mixed"):
        RT.bundle_rate({}, [v60, ntsc])
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
    assert Referee._frame_rate(None, sub, rd) == RT.Resolved(Fraction(60), "video.mp4 fMP4 sample durations", Fraction(60))
    sub, rd = _sub({}, {0: _init(), 1: _frag(), 2: _frag()})
    assert Referee._frame_rate(None, sub, rd).legacy
    sub, rd = _sub({"frame_rate": "30/1"}, {0: _init(), 1: _frag(default=256)})
    assert isinstance(Referee._frame_rate(None, sub, rd), RT.RateError)
    sub, rd = _sub({"frame_rate": "garbage"}, {0: _init(), 1: _frag()})
    assert isinstance(Referee._frame_rate(None, sub, rd), RT.RateError)
    sub, rd = _sub({"frame_rate": "60/1"}, {0: _init(), 1: _frag()})   # declared only
    assert isinstance(Referee._frame_rate(None, sub, rd), RT.RateError)


def test_l3_rate_record_is_omitted_at_30_and_written_otherwise():
    from qor_receipt.review.engine import Referee

    segs60 = [dict(s, type="segment") for s in _counter(60)]
    assert Referee._rate_record(RT.Resolved(Fraction(30), "legacy"), segs60) is None
    assert Referee._rate_record(RT.Resolved(Fraction(30), "x fMP4 sample durations"), segs60) is None
    assert Referee._rate_record(RT.Resolved(Fraction(60), "x", Fraction(60)), segs60) == {
        "rate": "60/1", "unsnapped_mean": "60/1", "source": "x", "clock_fit": "fitted", "gaps": 0,
    }
    assert Referee._rate_record(RT.Resolved(Fraction(60), "x"), segs60) == {
        "rate": "60/1", "unsnapped_mean": None, "source": "x", "clock_fit": "fitted", "gaps": 0,
    }
    # a 30/1 rate with a recorded hole still omits the record (goldens stay byte-identical)
    assert Referee._rate_record(RT.Resolved(Fraction(30), "x", Fraction(30), gaps=1), segs60) is None
    flat = [{"kind": "media", "frames": 120, "mono_ms": i * 2000} for i in range(1, 10)]
    assert Referee._rate_record(RT.Resolved(Fraction(60), "x", Fraction(60)), flat)["clock_fit"] == "fallback"
    rec = Referee._rate_record(RT.RateError("mixed frame rates: a"), flat)
    assert rec["rate"] == "UNMEASURED" and "mixed" in rec["reason"] and rec["clock_fit"] == "not_computed"


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
    b = G.match_index_bundle(tmp_path / "fc", fps=60, extra_meta={"frame_rate": "60/1"})  # declared, cross-checked
    (b / "frame_clock.jsonl").write_text(_frame_clock())
    i60 = build_index(b, tmp_path / "ofc", frames=False)
    assert [e["recording_s"] for e in i60["events"]] == [e["recording_s"] for e in i30["events"]]
    assert i60["video"]["recording_duration_s"] == i30["video"]["recording_duration_s"]
    assert i60["video"]["frame_rate"] == "60/1" and "frames/(60/1)" in i60["video"]["alignment_note"]
    assert "frame_rate" not in i30["video"]


def test_l4_default_60fps_bundle_without_a_rate_source_is_marked(tmp_path):
    """No --video, no frame_clock, no declared rate: legacy 30/1, but the manifest counter runs at 60."""
    _, idx = _index(tmp_path, 60)
    assert all(e["recording_s"] is None for e in idx["events"])
    assert idx["video"]["frame_rate"].startswith("UNMEASURED: manifest frames counter runs at 60.00 fps, not legacy 30/1")


def test_l1_match_index_declared_rate_alone_is_unmeasured(tmp_path):
    _, idx = _index(tmp_path, 60, frame_rate="60/1")
    assert all(e["recording_s"] is None for e in idx["events"]) and "declared only" in idx["video"]["frame_rate"]


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


# ---------------------------------------------------------------- QorProof #130 r2 L1 / L2 / L4
@pytest.mark.parametrize("mean", [Fraction(599941, 10_000), Fraction(60006, 1_000)])  # 59.9941, 60.006
def test_r2_l1_near_60_means_do_not_snap(mean):
    with pytest.raises(RT.RateError, match="0.002 %"):
        RT.snap(mean, "test")


@pytest.mark.parametrize("timescale,rate", [(15_360, Fraction(60)), (15_360, Fraction(30)),
                                            (90_000, Fraction(60000, 1001)), (90_000, Fraction(30000, 1001))])
def test_r2_l1_exact_60_ntsc_30_rates_are_accepted(timescale, rate):
    durs = _alternating(timescale, timescale / rate, 240)
    assert max(durs) - min(durs) <= 1
    assert RT.fmp4_rate(_init(timescale=timescale), [_frag(durs=durs[:120]), _frag(durs=durs[120:])]) == rate


def test_r2_l1_average_but_uneven_near_60_is_refused():
    """Means that used to snap at 0.01 % (59.9941, 60.006) are refused when durations are not constant."""
    # Pick a timescale equal to the mean's numerator so 10 samples of equal duration give that mean
    # exactly, then spread by more than 1 tick, keeping the sum.
    cases = [
        (Fraction(599941, 10_000), 599941, 10_000),  # 59.9941: 10 samples of 10000
        (Fraction(60006, 1_000), 60006, 1_000),      # 60.006: 10 samples of 1000
    ]
    for mean, ts, dur in cases:
        out = [dur] * 10
        out[0] += 2
        out[-1] -= 2
        assert Fraction(ts * 10, sum(out)) == mean and max(out) - min(out) > 1
        with pytest.raises(RT.RateError, match="not constant"):
            RT.fmp4_rate(_init(timescale=ts), [_frag(durs=out)])
        with pytest.raises(RT.RateError, match="0.002 %"):
            RT.snap(mean, "test")


def test_r2_l2_trun_sample_cap():
    with pytest.raises(RT.RateError, match="cap 65536"):
        RT.fmp4_rate(_init(), [_frag(default=256, count=65_537, mdat=1)])
    # the cap is inclusive: 65,536 default-duration samples at 256 ticks is exact 60/1
    assert RT.fmp4_rate(_init(), [_frag(default=256, count=65_536, mdat=65_536)]) == 60


def test_r2_l4_multipart_counter_is_fit_per_file(tmp_path):
    """QorCap resets the frames counter per file. A whole-bundle fit on two 60 fps parts is ~15 fps
    (spurious UNMEASURED against a measured 60, or a skipped check). Per-file fits both agree at 60."""
    from qor_receipt.match_index.build import counter_fps, video_rate

    b = G.match_index_bundle(tmp_path / "mp", fps=60)
    man = []
    for fi, base in enumerate((0, 24)):
        # an init row (and a frames==0 media row) must not join the fit
        man.append({"type": "segment", "kind": "init", "file": f"p{fi}.mp4", "seq": base,
                    "mono_ms": base * 2000, "frames": 0})
        for i in range(24):
            seq = base + i
            man.append({"type": "segment", "kind": "media", "file": f"p{fi}.mp4", "seq": seq,
                        "mono_ms": seq * 2000 + 37, "wall_ms": G.W0 + seq * 2000 + 37,
                        "frames": i * 2 * 60})
    (b / "manifest.jsonl").write_text("\n".join(__import__("json").dumps(x) for x in man) + "\n")
    assert abs(counter_fps(man) - 60.0) < 0.01

    # r3-L1: more than one file with media segments is UNMEASURED until Clock is per-file,
    # even when the per-file counters and a measured 60 agree.
    (b / "frame_clock.jsonl").write_text(_frame_clock())
    fps, label = video_rate(b, __import__("json").loads((b / "bundle.json").read_text()), None, man)
    assert fps is None and "multi-part bundle" in label
    idx = build_index(b, tmp_path / "omp", frames=False)
    assert all(e["recording_s"] is None for e in idx["events"])
    assert "multi-part bundle" in idx["video"]["frame_rate"]
    (b / "frame_clock.jsonl").unlink()
    idx = build_index(b, tmp_path / "omp-legacy", frames=False)
    assert all(e["recording_s"] is None for e in idx["events"])
    assert "multi-part bundle" in idx["video"]["frame_rate"]


# ---------------------------------------------------------------- QorProof #130 r3 M1 / L1 / L2 / L3
@pytest.mark.parametrize("timescale,d,rate", [
    (15_360, 256, Fraction(60)),
    (15_360, 512, Fraction(30)),
    (90_000, 1500, Fraction(60)),
    (90_000, 3000, Fraction(30)),
])
def test_r3_m1_one_doubled_sample_is_accepted(timescale, d, rate):
    n = 1000
    durs = [d] * n
    durs[n // 2] = 2 * d  # one hole, not the last sample
    got = RT._fmp4_measure(_init(timescale=timescale), [_frag(durs=durs)])
    assert got is not None and got.gaps == 1 and RT.snap(got.mean, "t") == rate
    assert RT.fmp4_rate(_init(timescale=timescale), [_frag(durs=durs)]) == rate
    res = RT.bundle_rate({}, [("v.mp4", _init(timescale=timescale), [_frag(durs=durs)])])
    assert res.rate == rate and res.gaps == 1
    from qor_receipt.review.engine import Referee
    rec = Referee._rate_record(res, [{"kind": "media", "frames": 120, "mono_ms": i * 2000} for i in range(1, 12)])
    if rate == 30:
        assert rec is None  # still omitted at 30/1, goldens unchanged
    else:
        assert rec is not None and rec["gaps"] == 1 and rec["rate"] == "60/1"


def test_r3_m1_two_percent_doubled_is_refused():
    n = 1000
    durs = [256] * n
    for i in range(20):  # 2 %
        durs[i] = 512
    with pytest.raises(RT.RateError, match="gap"):
        RT.fmp4_rate(_init(), [_frag(durs=durs)])


def test_r3_m1_one_and_a_half_d_is_refused():
    durs = [256] * 1000
    durs[10] = 384  # 1.5 * d, not within 1 tick of d or k*d
    with pytest.raises(RT.RateError, match="not constant"):
        RT.fmp4_rate(_init(), [_frag(durs=durs)])


def test_r3_m1_short_last_sample_is_accepted():
    durs = [256] * 999 + [100]
    got = RT._fmp4_measure(_init(), [_frag(durs=durs)])
    assert got is not None and got.gaps == 0 and got.last_dur == 100 and RT.snap(got.mean, "t") == 60
    assert RT.fmp4_rate(_init(), [_frag(durs=durs)]) == 60


def test_r3_l1_multipart_seeks_are_unmeasured(tmp_path):
    """Same 2-part shape as the r2-L4 test: no seek times until Clock is per file."""
    from qor_receipt.match_index.build import media_file_count

    b = G.match_index_bundle(tmp_path / "mp", fps=60)
    man = []
    for fi, base in enumerate((0, 24)):
        man.append({"type": "segment", "kind": "init", "file": f"p{fi}.mp4", "seq": base,
                    "mono_ms": base * 2000, "frames": 0})
        for i in range(24):
            seq = base + i
            man.append({"type": "segment", "kind": "media", "file": f"p{fi}.mp4", "seq": seq,
                        "mono_ms": seq * 2000 + 37, "wall_ms": G.W0 + seq * 2000 + 37,
                        "frames": i * 2 * 60})
    assert media_file_count(man) == 2
    (b / "manifest.jsonl").write_text("\n".join(json.dumps(x) for x in man) + "\n")
    (b / "frame_clock.jsonl").write_text(_frame_clock())
    idx = build_index(b, tmp_path / "o", frames=False)
    assert all(e["recording_s"] is None for e in idx["events"])
    assert "multi-part bundle" in idx["video"]["frame_rate"]


def test_r3_l2_trun_and_moof_box_caps():
    tfhd = _full(b"tfhd", 0x020008, _u32(1) + _u32(256))
    trun = _full(b"trun", 0x000001, _u32(1) + _u32(0))
    def frag(n_truns: int) -> bytes:
        traf = _box(b"traf", tfhd + _full(b"tfdt", 0, _u32(0)) + trun * n_truns)
        return _box(b"moof", _full(b"mfhd", 0, _u32(1)) + traf) + _box(b"mdat", bytes(max(64, n_truns)))
    with pytest.raises(RT.RateError, match="1024"):
        RT.fmp4_rate(_init(), [frag(1025)])
    assert RT.fmp4_rate(_init(), [frag(1024)]) == 60

    # 1025 child boxes in a moof (mfhd + 1024 trafs)
    one = _box(b"traf", tfhd + _full(b"tfdt", 0, _u32(0)) + trun)
    too_many = _box(b"moof", _full(b"mfhd", 0, _u32(1)) + one * 1024) + _box(b"mdat", bytes(64))
    with pytest.raises(RT.RateError, match="1024"):
        RT.fmp4_rate(_init(), [too_many])
    # mfhd + 1023 trafs = 1024 boxes, 1023 samples >= MIN_SAMPLES
    ok = _box(b"moof", _full(b"mfhd", 0, _u32(1)) + one * 1023) + _box(b"mdat", bytes(64))
    assert RT.fmp4_rate(_init(), [ok]) == 60


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg needed for the QorCap mpegts then -c copy remux")
@pytest.mark.parametrize("fps,rate", [(30, Fraction(30)), (60, Fraction(60))])
def test_r3_l3_qorcap_mpegts_copy_remux_rate(tmp_path, fps, rate):
    """Encode mpegts at 90 kHz, remux with QorCap's ``-c copy`` frag flags, and read the rate."""
    from review_fixture import split_fmp4

    ts = tmp_path / f"cap{fps}.ts"
    out = tmp_path / f"cap{fps}.mp4"
    # QorCap encodes to mpegts then remuxes with these movflags / min_frag_duration (ffmpeg.rs).
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc=size=160x90:rate={fps}:duration=6",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    "-r", str(fps), "-g", str(fps), "-f", "mpegts", str(ts)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ts),
                    "-c", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                    "-min_frag_duration", "2000000", "-f", "mp4", str(out)], check=True)
    parts = split_fmp4(out.read_bytes())
    assert len(parts) >= 2
    got = RT._fmp4_measure(parts[0], parts[1:])
    assert got is not None
    assert got.timescale == 90_000
    assert RT.snap(got.mean, "t") == rate
    assert RT.fmp4_rate(parts[0], parts[1:]) == rate
    # integer -r at 90 kHz: 3000 ticks at 30, 1500 at 60. A short final sample is exempt.
    d = 3000 if fps == 30 else 1500
    assert got.base_dur == d
    assert got.last_dur is not None and (got.last_dur == d or got.last_dur < d)
    assert got.gaps == 0
    res = RT.bundle_rate({}, [("video.mp4", parts[0], parts[1:])])
    assert res.rate == rate and res.mean == got.mean and res.gaps == 0
    from qor_receipt.review.engine import Referee
    rec = Referee._rate_record(res, [{"kind": "media", "frames": fps * 2 * i, "mono_ms": i * 2000} for i in range(1, 12)])
    if fps == 30:
        assert rec is None  # 30 fps record still omitted
    else:
        assert rec == {"rate": "60/1", "unsnapped_mean": "60/1", "source": "video.mp4 fMP4 sample durations",
                       "clock_fit": "fallback", "gaps": 0}


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
        assert a["frame_rate"] is None
        assert b["frame_rate"] == {"rate": "60/1", "unsnapped_mean": "60/1", "gaps": 0,
                                  "source": "video.mp4 fMP4 sample durations", "clock_fit": "fitted"}
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
        assert p["frame_rate"]["rate"] == "UNMEASURED" and why in p["frame_rate"]["reason"]
