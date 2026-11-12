"""hands sidecar: a malformed clip start time, file or id is a clean refusal (QorProof LOW QT1).

The bug predates #129: at main fda7b51 (and still at 91b6f67), clip_start_mono_ms
and main() raised KeyError / ValueError / TypeError / OverflowError (exit 1 with a
traceback) when start_s was missing or not a usable number, source was not a dict,
or file / id was missing. Valid 30 fps output stays byte-identical to the hands
golden (sha256 1d47f90d…510c). SYNTHETIC data only.

On the pre-fix code these tests fail: clip_start_mono_ms returns nan/inf or a
guessed time for several of the bad values (bool, negative, "nan", "inf"), or
raises an uncaught exception instead of ClipInputError, and main() exits 1 with
a traceback instead of 2.
"""
from __future__ import annotations

import contextlib
import io

import pytest
from test_hands_frame_rate import _clips, _segs, run_main_subprocess, write_sidecar_bundle

from qor_receipt.hands import sidecar as S
from qor_receipt.hands import video_events as V


def _media():
    return [s for s in _segs("regular") if s.get("type") == "segment"]


def _clip():
    return _clips([12.0])[0]


def _mutate(kind: str):
    c = _clip()
    if kind == "missing start_s":
        del c["source"]["start_s"]
    elif kind == "source missing":
        del c["source"]
    elif kind == "source None":
        c["source"] = None
    elif kind == "source list":
        c["source"] = ["start_s"]
    elif kind == "source string":
        c["source"] = "video.mp4"
    elif kind == "start_s abc":
        c["source"]["start_s"] = "abc"
    elif kind == "start_s nan str":
        c["source"]["start_s"] = "nan"
    elif kind == "start_s inf str":
        c["source"]["start_s"] = "inf"
    elif kind == "start_s 1e999":
        c["source"]["start_s"] = "1e999"
    elif kind == "start_s 12.0 str":
        c["source"]["start_s"] = "12.0"
    elif kind == "start_s nan":
        c["source"]["start_s"] = float("nan")
    elif kind == "start_s inf":
        c["source"]["start_s"] = float("inf")
    elif kind == "start_s -inf":
        c["source"]["start_s"] = float("-inf")
    elif kind == "start_s negative":
        c["source"]["start_s"] = -0.001
    elif kind == "start_s True":
        c["source"]["start_s"] = True
    elif kind == "start_s False":
        c["source"]["start_s"] = False
    elif kind == "start_s None":
        c["source"]["start_s"] = None
    elif kind == "start_s list":
        c["source"]["start_s"] = [0]
    elif kind == "start_s huge int":
        c["source"]["start_s"] = 10**400
    else:
        raise AssertionError(kind)
    return c


BAD_START_KINDS = [
    "missing start_s",
    "source missing",
    "source None",
    "source list",
    "source string",
    "start_s abc",
    "start_s nan str",
    "start_s inf str",
    "start_s 1e999",
    "start_s 12.0 str",
    "start_s nan",
    "start_s inf",
    "start_s -inf",
    "start_s negative",
    "start_s True",
    "start_s False",
    "start_s None",
    "start_s list",
    "start_s huge int",
]


@pytest.mark.parametrize("kind", BAD_START_KINDS)
def test_bad_start_s_raises_clip_input_error(kind):
    with pytest.raises(S.ClipInputError, match="fail closed") as ei:
        S.clip_start_mono_ms(_mutate(kind), _media())
    msg = str(ei.value)
    assert "source" in msg
    if kind.startswith("start_s") or kind == "missing start_s":
        assert "start_s" in msg


@pytest.mark.parametrize("kind", BAD_START_KINDS)
def test_bad_start_s_is_a_clean_refusal_from_main(tmp_path, kind):
    """Child-process run (QorProof INFO QT2): a traceback would fail the Traceback assert."""
    good = _clips([0.5, 12.0])
    bad = _mutate(kind)
    bad["id"], bad["file"] = "bad", "bad.mp4"
    rc, out, err = run_main_subprocess(tmp_path, _segs("regular"), good + [bad])
    assert rc == 2 and out == "", "never guessed: no hands document is built"
    assert "fail closed" in err and "error:" in err
    assert "Traceback" not in err


def test_zero_start_s_is_accepted():
    c = _clip()
    c["source"]["start_s"] = 0
    # intercept 5000, start_s 0 -> 5000 on the regular 30 fps counter
    assert S.clip_start_mono_ms(c, _media()) == 5000.0
    c["source"]["start_s"] = 0.0
    assert S.clip_start_mono_ms(c, _media()) == 5000.0


def test_int_start_s_matches_float():
    a, b = _clip(), _clip()
    a["source"]["start_s"] = 12
    b["source"]["start_s"] = 12.0
    assert S.clip_start_mono_ms(a, _media()) == S.clip_start_mono_ms(b, _media()) == 17000.0


@pytest.mark.parametrize(
    "kind, needle",
    [
        ("missing file", "file"),
        ("file None", "file"),
        ("file empty", "file"),
        ("file int", "file"),
        ("missing id", "id"),
        ("id None", "id"),
        ("id empty", "id"),
        ("id int", "id"),
        ("clip not object", "object"),
    ],
)
def test_missing_file_or_id_is_a_clean_refusal(tmp_path, kind, needle):
    clips = _clips([12.0])
    if kind == "missing file":
        del clips[0]["file"]
    elif kind == "file None":
        clips[0]["file"] = None
    elif kind == "file empty":
        clips[0]["file"] = ""
    elif kind == "file int":
        clips[0]["file"] = 1
    elif kind == "missing id":
        del clips[0]["id"]
    elif kind == "id None":
        clips[0]["id"] = None
    elif kind == "id empty":
        clips[0]["id"] = ""
    elif kind == "id int":
        clips[0]["id"] = 1
    elif kind == "clip not object":
        clips = ["not-a-clip"]
    else:
        raise AssertionError(kind)

    if kind != "clip not object":
        with pytest.raises(S.ClipInputError, match="fail closed") as ei:
            S.clip_id_and_file(clips[0])
        assert needle in str(ei.value)

    rc, out, err = run_main_subprocess(tmp_path, _segs("regular"), clips)
    assert rc == 2 and out == "", "never guessed: no hands document is built"
    assert "fail closed" in err and "error:" in err
    assert needle in err
    assert "Traceback" not in err


def test_absent_clip_file_is_still_skipped_not_refused(tmp_path):
    """A well-formed clip whose media is not on disk is skipped, as before; it is not a refusal."""
    clips = _clips([12.0, 25.333])
    argv = write_sidecar_bundle(tmp_path, _segs("regular"), clips)
    (tmp_path / "clips" / clips[0]["file"]).unlink()
    seen: list = []
    saved = (V.energy_curve, V.detect, S.build)
    V.energy_curve = lambda path: ([], [], 30.0)
    V.detect = lambda e, c, fps: []
    S.build = lambda receipt, data, chunks, cl, **kw: (seen.append(cl), {"clips": cl})[1]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = S.main(argv)
    finally:
        V.energy_curve, V.detect, S.build = saved
    assert rc == 0 and err.getvalue() == ""
    assert [c["id"] for c in seen[0]] == ["hl01"]
    assert seen[0][0]["start_mono_ms"] == 30333
