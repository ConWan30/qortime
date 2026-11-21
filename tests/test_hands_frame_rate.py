"""hands sidecar: highlight clip start on the capture clock at the clip's own frame rate (60 fps fix).

qorcap #164 records ``source.frame_rate`` (a rational string, e.g. "60/1") on highlight clips that are not 30/1.
Absent means 30/1 (every older manifest). Present but unreadable or not a positive rate fails closed.
SYNTHETIC data only. The 30 fps goldens were produced by the pre-fix code at qortime main fda7b51
(docs/hands-clip-clock-goldens.md).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from qor_receipt.hands import sidecar as S
from qor_receipt.hands import video_events as V
from qor_receipt.review.clock import capture_clock

GOLDEN_FILE = Path(__file__).resolve().parent / "fixtures" / "hands_clip_clock" / "golden_30fps_fda7b51.txt"
GOLDEN_SHA256 = "1d47f90ddc8b7888da491aa8baf17551bff9508e87652c70ac365ab4930d510c"


# ---------------------------------------------------------------- golden harness (runs on pre-fix and fixed code)
def _segs(kind: str) -> list[dict]:
    init = [{"type": "segment", "seq": 0, "kind": "init", "file": "video.mp4", "frames": 0, "mono_ms": 0}]
    if kind == "regular":      # 30 fps capture counter, 2 s fragments
        media = [(i * 60, 5_000 + i * 2000) for i in range(1, 41)]
    elif kind == "jitter":     # drifting counter and stamps (still within 5 % of nominal)
        media = [(round(i * 59.7) + i % 3, 7_000 + i * 2000 + (i * 53) % 190) for i in range(1, 41)]
    elif kind == "short":      # under 10 s of frames: nominal fallback
        media = [(i * 30, 1_000 + i * 1000) for i in range(1, 6)]
    elif kind == "flat":       # synthetic review fixture: constant frames -> PTS is the mono clock
        media = [(60, 2_000 + i * 2000) for i in range(1, 41)]
    else:                      # sixty: a 60 fps capture counter
        media = [(i * 120, 5_000 + i * 2000) for i in range(1, 41)]
    return init + [{"type": "segment", "seq": i + 1, "kind": "media", "file": "video.mp4", "frames": f, "mono_ms": m}
                   for i, (f, m) in enumerate(media)]


STARTS = [0.0, 0.5, 1.234, 12.0, 25.333, 39.891, 53.111, 71.0]


def _run_main(tmp: Path, segs: list[dict], clips: list[dict]) -> tuple[int, str, str, list]:
    """Run sidecar.main on a synthetic bundle with video decoding and build() stubbed; return (rc, out, err, clips)."""
    (tmp / "clips").mkdir(parents=True, exist_ok=True)
    for c in clips:
        (tmp / "clips" / c["file"]).write_bytes(b"SYNTHETIC")
    (tmp / "receipt.json").write_text(json.dumps({"match": {"match_id": "SYNTHETIC"}}))
    (tmp / "inputs.jsonl").write_text("")
    (tmp / "segments.jsonl").write_text("".join(json.dumps(s) + "\n" for s in segs))
    (tmp / "clips_manifest.json").write_text(json.dumps({"schema": "qorcap-highlights/v0", "clips": clips}))
    seen: list = []
    saved = (V.energy_curve, V.detect, S.build)
    V.energy_curve = lambda path: ([], [], 30.0)
    V.detect = lambda e, c, fps: []
    S.build = lambda receipt, data, chunks, cl, **kw: (seen.append(cl), {"clips": cl})[1]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = S.main([str(tmp / "receipt.json"), str(tmp / "inputs.jsonl"), str(tmp / "clips_manifest.json"),
                             str(tmp / "segments.jsonl"), "--clips", str(tmp / "clips"), "--no-track"])
            except SystemExit as e:  # argparse or an explicit exit
                rc = e.code
    finally:
        V.energy_curve, V.detect, S.build = saved
    return rc, out.getvalue(), err.getvalue(), seen


_REPO_ROOT = Path(__file__).resolve().parents[1]


def write_sidecar_bundle(tmp: Path, segs: list[dict], clips: list) -> list[str]:
    """Write a synthetic sidecar bundle. Only materializes clip files whose ``file`` is a string."""
    (tmp / "clips").mkdir(parents=True, exist_ok=True)
    for c in clips:
        name = c.get("file") if isinstance(c, dict) else None
        # Only materialize a single path component inside tmp/clips. Never follow '..',
        # separators, or control characters (NUL in a path raises ValueError on open).
        if (
            not isinstance(name, str)
            or not name
            or name != Path(name).name
            or name in (".", "..")
            or any(ord(ch) < 32 or ch in "  " for ch in name)
        ):
            continue
        dest = tmp / "clips" / name
        dest.write_bytes(b"SYNTHETIC")
    (tmp / "receipt.json").write_text(json.dumps({"match": {"match_id": "SYNTHETIC"}}))
    (tmp / "inputs.jsonl").write_text("")
    (tmp / "segments.jsonl").write_text("".join(json.dumps(s) + "\n" for s in segs))
    (tmp / "clips_manifest.json").write_text(json.dumps({"schema": "qorcap-highlights/v0", "clips": clips}))
    return [str(tmp / "receipt.json"), str(tmp / "inputs.jsonl"), str(tmp / "clips_manifest.json"),
            str(tmp / "segments.jsonl"), "--clips", str(tmp / "clips"), "--no-track"]


def run_main_subprocess(tmp: Path, segs: list[dict], clips: list) -> tuple[int, str, str]:
    """Run sidecar.main in a child process so a traceback lands in stderr (QorProof INFO QT2).

    Used only on paths that refuse before video decode / build(), because the clip files are stubs.
    """
    argv = write_sidecar_bundle(tmp, segs, clips)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run(
        [sys.executable, "-m", "qor_receipt.hands.sidecar", *argv],
        cwd=str(_REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def hands_golden_text() -> str:
    import tempfile
    lines = []
    for kind in ("regular", "jitter", "short", "flat"):
        segs = _segs(kind)
        # 9 decimals, not repr: Python 3.12 made sum() of floats compensated, which moves the last digit or two
        # of the jitter fit between 3.11 and 3.13. The rounded start_mono_ms values are the same on both.
        cc = capture_clock([s for s in segs if s.get("type") == "segment"])
        lines.append(f"## {kind} capture_clock=({cc[0]:.9f}, {cc[1]:.9f})")
        clips = [{"id": f"hl{n:02d}", "file": f"hl{n:02d}.mp4", "duration_s": 12.0,
                  "source": {"file": "video.mp4", "start_s": s, "end_s": s + 12.0}} for n, s in enumerate(STARTS)]
        with tempfile.TemporaryDirectory() as d:
            rc, out, err, seen = _run_main(Path(d), segs, clips)
        lines.append(f"rc={rc}")
        lines += [f"{c['id']} start_mono_ms={c['start_mono_ms']!r} duration_s={c['duration_s']!r}" for c in seen[0]]
        lines.append(out.replace("\n", "|"))
    return "\n".join(lines) + "\n"


_ABSENT = object()


def _clips(starts, frame_rate=_ABSENT):
    out = []
    for n, s in enumerate(starts):
        src = {"file": "video.mp4", "start_s": s, "end_s": s + 12.0}
        if frame_rate is not _ABSENT:
            src["frame_rate"] = frame_rate
        out.append({"id": f"hl{n:02d}", "file": f"hl{n:02d}.mp4", "duration_s": 12.0, "source": src})
    return out


# ---------------------------------------------------------------- tests
def test_thirty_fps_is_byte_identical_to_the_pre_fix_code():
    golden = GOLDEN_FILE.read_bytes()
    assert hashlib.sha256(golden).hexdigest() == GOLDEN_SHA256
    assert golden.count(b"start_mono_ms=") == 32 and b"frame_rate" not in golden
    assert hands_golden_text().encode("utf-8") == golden


def test_sixty_fps_clip_lands_on_the_real_mono_offset(tmp_path):
    # Same match, same wall-clock clip windows; the capture counter runs at 60. qorcap #164 now writes start_s in
    # true seconds with frame_rate "60/1", so the clip start on the capture clock must equal the 30 fps answer.
    rc30, _, _, seen30 = _run_main(tmp_path / "a", _segs("regular"), _clips(STARTS))
    rc60, _, _, seen60 = _run_main(tmp_path / "b", _segs("sixty"), _clips(STARTS, "60/1"))
    assert rc30 == rc60 == 0
    assert [c["start_mono_ms"] for c in seen60[0]] == [c["start_mono_ms"] for c in seen30[0]]
    assert [c["start_mono_ms"] for c in seen60[0]] == [5000, 5500, 6234, 17000, 30333, 44891, 58111, 76000]
    # the pre-fix code (30 assumed, and a 60 fps counter rejected by the 5 % check) put them elsewhere
    intercept, slope = capture_clock([s for s in _segs("sixty") if s.get("type") == "segment"])
    assert (intercept, slope) == (0.0, 1000.0 / 30.0), "pre-fix fit falls back to nominal 30 on a 60 fps counter"
    assert round(intercept + slope * 12.0 * 30.0) == 12000 != 17000


def test_capture_clock_at_sixty_fits_the_sixty_counter():
    segs = [s for s in _segs("sixty") if s.get("type") == "segment"]
    intercept, slope = capture_clock(segs, 60.0)
    assert intercept == pytest.approx(5000.0) and slope == pytest.approx(1000.0 / 60.0)
    assert capture_clock(segs) == capture_clock(segs, 30.0)
    short = [s for s in _segs("short") if s.get("type") == "segment"]
    assert capture_clock(short, 60.0) == (0.0, 1000.0 / 60.0), "fallback is nominal at the clip's rate"
    for bad in (0, -60.0, float("nan"), float("inf"), 5000.0):
        with pytest.raises(ValueError):
            capture_clock(segs, bad)


def test_ntsc_rate_is_used_as_written():
    c = _clips([10.0], "60000/1001")[0]
    assert S.clip_fps(c) == 60000 / 1001
    segs = [s for s in _segs("sixty") if s.get("type") == "segment"]
    intercept, slope = capture_clock(segs, 60000 / 1001)
    assert S.clip_start_mono_ms(c, segs) == intercept + slope * 10.0 * (60000 / 1001)


def test_missing_frame_rate_means_thirty():
    c = _clips([12.0])[0]
    assert "frame_rate" not in c["source"] and S.clip_fps(c) == 30.0
    segs = [s for s in _segs("regular") if s.get("type") == "segment"]
    assert S.clip_start_mono_ms(c, segs) == 5000.0 + (1000.0 / 30.0) * 12.0 * 30.0
    assert S.clip_fps({"id": "x", "source": {"start_s": 1.0}}) == 30.0


@pytest.mark.parametrize("bad", ["garbage", "", "60", "0/1", "-60/1", "60/0", "60.0/1", "6_0/1", "nan/1", "１２/1",
                                 "5000/1", "1/2", "1" * 400 + "/1", 60, None, ["60/1"]])
def test_garbage_frame_rate_fails_closed(tmp_path, bad):
    c = _clips([12.0], bad)[0]
    with pytest.raises(S.ClipRateError, match="fail closed"):
        S.clip_fps(c)
    rc, out, err, seen = _run_main(tmp_path, _segs("regular"), _clips([0.5, 12.0]) + [dict(c, id="bad", file="bad.mp4")])
    assert rc == 2 and out == "" and seen == [], "never guessed: no hands document is built"
    assert "source.frame_rate" in err and "fail closed" in err


def test_huge_numerator_is_a_clean_refusal_not_a_traceback(tmp_path):
    """QorProof INFO Q1: a 400-digit numerator must exit 2 with ClipRateError, not OverflowError.

    QorProof INFO QT2: the previous '"Traceback" not in err' check was vacuous because
    ``_run_main`` calls ``main()`` in-process, so an OverflowError escaped the test instead
    of landing in ``err``. This run is a child process, so a traceback would fail the assert.
    """
    bad = "1" * 400 + "/1"
    c = _clips([12.0], bad)[0]
    with pytest.raises(S.ClipRateError, match="fail closed") as ei:
        S.clip_fps(c)
    assert "source.frame_rate" in str(ei.value)
    rc, out, err = run_main_subprocess(
        tmp_path, _segs("regular"), _clips([0.5, 12.0]) + [dict(c, id="bad", file="bad.mp4")]
    )
    assert rc == 2 and out == "", "never guessed: no hands document is built"
    assert "source.frame_rate" in err and "fail closed" in err
    assert "Traceback" not in err and "OverflowError" not in err
