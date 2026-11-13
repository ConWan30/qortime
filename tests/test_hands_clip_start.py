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
import errno
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_hands_frame_rate import _REPO_ROOT, _clips, _segs, run_main_subprocess, write_sidecar_bundle

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


# ---------------------------------------------------------------- L1 / L2 / INFO (QorProof qt131)
def _set_duration(kind: str):
    c = _clip()
    if kind == "missing duration_s":
        del c["duration_s"]
    elif kind == "duration_s abc":
        c["duration_s"] = "abc"
    elif kind == "duration_s 12.0 str":
        c["duration_s"] = "12.0"
    elif kind == "duration_s None":
        c["duration_s"] = None
    elif kind == "duration_s list":
        c["duration_s"] = [12]
    elif kind == "duration_s True":
        c["duration_s"] = True
    elif kind == "duration_s False":
        c["duration_s"] = False
    elif kind == "duration_s zero":
        c["duration_s"] = 0
    elif kind == "duration_s negative":
        c["duration_s"] = -1.0
    elif kind == "duration_s nan":
        c["duration_s"] = float("nan")
    elif kind == "duration_s inf":
        c["duration_s"] = float("inf")
    elif kind == "duration_s -inf":
        c["duration_s"] = float("-inf")
    elif kind == "duration_s huge int":
        c["duration_s"] = 10**400
    elif kind == "duration_s 1e16":
        c["duration_s"] = 1e16
    elif kind == "duration_s over max":
        c["duration_s"] = S._MAX_DURATION_S + 1
    else:
        raise AssertionError(kind)
    return c


BAD_DURATION_KINDS = [
    "duration_s abc",
    "duration_s 12.0 str",
    "duration_s None",
    "duration_s list",
    "duration_s True",
    "duration_s False",
    "duration_s zero",
    "duration_s negative",
    "duration_s nan",
    "duration_s inf",
    "duration_s -inf",
    "duration_s huge int",
    "duration_s 1e16",
    "duration_s over max",
]


@pytest.mark.parametrize("kind", BAD_DURATION_KINDS)
def test_bad_duration_s_raises_clip_input_error(kind):
    with pytest.raises(S.ClipInputError, match="fail closed") as ei:
        S.clip_duration_s(_set_duration(kind))
    assert "duration_s" in str(ei.value)


@pytest.mark.parametrize("kind", BAD_DURATION_KINDS)
def test_bad_duration_s_is_a_clean_refusal_from_main(tmp_path, kind):
    good = _clips([0.5, 12.0])
    bad = _set_duration(kind)
    bad["id"], bad["file"] = "bad", "bad.mp4"
    rc, out, err = run_main_subprocess(tmp_path, _segs("regular"), good + [bad])
    assert rc == 2 and out == "", "never guessed: no hands document is built"
    assert "fail closed" in err and "duration_s" in err and "error:" in err
    assert "Traceback" not in err


def test_absent_duration_s_defaults_to_twelve():
    c = _clip()
    del c["duration_s"]
    assert S.clip_duration_s(c) == 12.0


def test_duration_s_int_and_max_are_accepted(tmp_path):
    c = _clip()
    c["duration_s"] = 12
    assert S.clip_duration_s(c) == 12.0
    c["duration_s"] = S._MAX_DURATION_S
    assert S.clip_duration_s(c) == S._MAX_DURATION_S
    c["duration_s"] = 0.001
    assert S.clip_duration_s(c) == 0.001
    # default-12 still lands in the built document when the key is absent
    from test_hands_frame_rate import _run_main

    clip = _clip()
    del clip["duration_s"]
    rc, out, err, seen = _run_main(tmp_path, _segs("regular"), [clip])
    assert rc == 0 and err == ""
    assert seen[0][0]["duration_s"] == 12.0


UNSAFE_NAME_CASES = [
    ("file parent", "file", ".."),
    ("file dot", "file", "."),
    ("file traversal", "file", "../outside.mp4"),
    ("file nested traversal", "file", "foo/../../outside.mp4"),
    ("file slash", "file", "sub/hl00.mp4"),
    ("file backslash", "file", "sub\\hl00.mp4"),
    ("file absolute posix", "file", "/tmp/outside.mp4"),
    ("file drive", "file", "C:\\outside.mp4"),
    ("file colon", "file", "hl00:stream.mp4"),
    ("file newline", "file", "hl00.mp4\nOK: forged"),
    ("file cr", "file", "hl00.mp4\rOK"),
    ("file nul", "file", "hl00.mp4\x00"),
    ("file line sep", "file", "hl00.mp4\u2028"),
    ("file whitespace", "file", " "),
    ("file padded", "file", " hl00.mp4"),
    ("id parent", "id", ".."),
    ("id slash", "id", "a/b"),
    ("id backslash", "id", "a\\b"),
    ("id newline", "id", "hl00\nOK: forged"),
    ("id cr", "id", "hl00\rOK"),
    ("id nul", "id", "hl00\x00"),
    ("id line sep", "id", "hl00\u2028"),
    ("id whitespace", "id", " "),
    ("id padded", "id", " hl00"),
]


@pytest.mark.parametrize("label,field,value", UNSAFE_NAME_CASES)
def test_unsafe_file_or_id_raises_clip_input_error(label, field, value):
    c = _clip()
    c[field] = value
    with pytest.raises(S.ClipInputError, match="fail closed") as ei:
        S.clip_id_and_file(c)
    msg = str(ei.value)
    assert field in msg
    # no raw control / newline injection in the error text
    assert "\nOK:" not in msg and "\rOK" not in msg and "\x00" not in msg and "\u2028" not in msg


@pytest.mark.parametrize("label,field,value", UNSAFE_NAME_CASES)
def test_unsafe_file_or_id_is_a_clean_refusal_from_main(tmp_path, label, field, value):
    c = _clip()
    c[field] = value
    rc, out, err = run_main_subprocess(tmp_path, _segs("regular"), [c])
    assert rc == 2 and out == "", "never guessed: no hands document is built"
    assert "fail closed" in err and "error:" in err
    assert "Traceback" not in err
    assert "\nOK:" not in err and "\rOK" not in err
    assert "\x00" not in err and "\u2028" not in err


def test_file_outside_clips_dir_is_refused_even_if_it_exists(tmp_path):
    """Traversal is refused even when the escaped path exists on disk."""
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"SYNTHETIC")
    c = _clip()
    c["file"] = "../outside.mp4"
    with pytest.raises(S.ClipInputError, match="fail closed"):
        S.clip_id_and_file(c)
    rc, out, err = run_main_subprocess(tmp_path, _segs("regular"), [c])
    assert rc == 2 and out == ""
    assert "fail closed" in err and "Traceback" not in err
    # the outside file must not have been treated as a clip (no document)
    assert outside.exists()


def test_resolved_clip_path_stays_inside_the_clips_dir(tmp_path):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    (clips_dir / "hl00.mp4").write_bytes(b"SYNTHETIC")
    got = S.clip_file_in_dir(clips_dir, "hl00.mp4", clip_id="hl00")
    assert got == (clips_dir / "hl00.mp4").resolve()
    assert clips_dir.resolve() in got.parents


def test_duplicate_ids_are_a_clean_refusal(tmp_path):
    a, b = _clip(), _clip()
    b["file"] = "hl01.mp4"
    # same id, two files
    rc, out, err = run_main_subprocess(tmp_path, _segs("regular"), [a, b])
    assert rc == 2 and out == ""
    assert "duplicate id" in err and "fail closed" in err
    assert "Traceback" not in err


def test_all_clips_are_validated_before_any_decode(tmp_path):
    """A later refusal must not follow energy_curve / detect on an earlier well-formed clip."""
    from test_hands_frame_rate import write_sidecar_bundle

    good, bad = _clip(), _set_duration("duration_s abc")
    bad["id"], bad["file"] = "bad", "bad.mp4"
    argv = write_sidecar_bundle(tmp_path, _segs("regular"), [good, bad])
    calls: list = []
    saved = (V.energy_curve, V.detect, S.build)
    V.energy_curve = lambda path: (calls.append(path), ([], [], 30.0))[1]
    V.detect = lambda e, c, fps: (_ for _ in ()).throw(AssertionError("detect called before refusal"))
    S.build = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("build called before refusal"))
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = S.main(argv)
    finally:
        V.energy_curve, V.detect, S.build = saved
    assert rc == 2 and out.getvalue() == ""
    assert calls == [], "decode must not run before every clip has been validated"
    assert "duration_s" in err.getvalue() and "fail closed" in err.getvalue()


# ---------------------------------------------------------------- long names / OSError / symlink (QorProof qt131 r2 INFO)
def _rewrite_manifest(tmp_path, clips: list) -> None:
    """Overwrite the manifest without trying to materialize overlong or symlink names."""
    (tmp_path / "clips_manifest.json").write_text(
        json.dumps({"schema": "qorcap-highlights/v0", "clips": clips}), encoding="utf-8"
    )


def _run_prepared_subprocess(argv: list[str]) -> tuple[int, str, str]:
    """Child-process run on an already-written bundle (QorProof INFO QT2)."""
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


def _refuse_overlong_before_decode(tmp_path, field: str, value: str) -> None:
    """A later overlong file/id must exit 2 with no traceback and no energy_curve call."""
    good, bad = _clip(), _clip()
    bad["id"], bad["file"] = "bad", "bad.mp4"
    bad[field] = value
    with pytest.raises(S.ClipInputError, match="fail closed") as ei:
        S.clip_id_and_file(bad)
    assert "UTF-8 bytes" in str(ei.value)
    assert "\n" not in str(ei.value)

    argv = write_sidecar_bundle(tmp_path, _segs("regular"), [good])
    _rewrite_manifest(tmp_path, [good, bad])

    calls: list = []
    saved = (V.energy_curve, V.detect, S.build)
    V.energy_curve = lambda path: (calls.append(path), ([], [], 30.0))[1]
    V.detect = lambda e, c, fps: (_ for _ in ()).throw(AssertionError("detect called before refusal"))
    S.build = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("build called before refusal"))
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = S.main(argv)
    finally:
        V.energy_curve, V.detect, S.build = saved
    assert rc == 2 and out.getvalue() == ""
    assert calls == [], "decode must not run before every clip has been validated"
    assert "fail closed" in err.getvalue() and "UTF-8 bytes" in err.getvalue()
    assert "Traceback" not in err.getvalue()

    rc, out, err = _run_prepared_subprocess(argv)
    assert rc == 2 and out == "", "never guessed: no hands document is built"
    assert "fail closed" in err and "error:" in err and "UTF-8 bytes" in err
    assert "Traceback" not in err


def test_255_byte_name_passes_the_grammar():
    """A 255-byte file name is a legal path component; it is not refused for length."""
    c = _clip()
    c["file"] = "a" * (S._MAX_CLIP_COMPONENT_BYTES - 4) + ".mp4"
    assert len(c["file"].encode("utf-8")) == S._MAX_CLIP_COMPONENT_BYTES
    cid, fname = S.clip_id_and_file(c)
    assert fname == c["file"] and cid == c["id"]


def test_128_byte_id_passes_the_grammar():
    c = _clip()
    c["id"] = "b" * S._MAX_CLIP_ID_BYTES
    assert len(c["id"].encode("utf-8")) == S._MAX_CLIP_ID_BYTES
    cid, fname = S.clip_id_and_file(c)
    assert cid == c["id"] and fname == c["file"]


def test_256_char_name_is_a_clean_refusal_before_any_decode(tmp_path):
    name = "a" * 256
    assert len(name) == 256 and len(name.encode("utf-8")) == 256
    _refuse_overlong_before_decode(tmp_path, "file", name)


def test_300_byte_multibyte_name_is_a_clean_refusal_before_any_decode(tmp_path):
    """150 x U+00E9 is 300 UTF-8 bytes and only 150 characters: a character cap of 255 would miss it."""
    name = "é" * 150
    assert len(name) == 150 and len(name.encode("utf-8")) == 300
    _refuse_overlong_before_decode(tmp_path, "file", name)


def test_overlong_id_is_a_clean_refusal_before_any_decode(tmp_path):
    name = "c" * (S._MAX_CLIP_ID_BYTES + 1)
    assert len(name.encode("utf-8")) == S._MAX_CLIP_ID_BYTES + 1
    _refuse_overlong_before_decode(tmp_path, "id", name)


def test_oserror_from_exists_is_mapped_and_blocks_decode(tmp_path, monkeypatch):
    """A path that passes the grammar still refuses cleanly when exists() raises OSError."""
    good = _clip()
    argv = write_sidecar_bundle(tmp_path, _segs("regular"), [good])

    def boom(self):
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(Path, "exists", boom)

    calls: list = []
    saved = (V.energy_curve, V.detect, S.build)
    V.energy_curve = lambda path: (calls.append(path), ([], [], 30.0))[1]
    V.detect = lambda e, c, fps: (_ for _ in ()).throw(AssertionError("detect called before refusal"))
    S.build = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("build called before refusal"))
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = S.main(argv)
    finally:
        V.energy_curve, V.detect, S.build = saved
    assert rc == 2 and out.getvalue() == ""
    assert calls == [], "decode must not run after an unusable path"
    text = err.getvalue()
    assert "fail closed" in text and "error:" in text
    assert "ENAMETOOLONG" in text or "not usable" in text
    assert "Traceback" not in text

    # The helper itself maps the same OSError.
    with pytest.raises(S.ClipInputError, match="fail closed") as ei:
        S.clip_file_is_readable(tmp_path / "clips" / good["file"], clip_id=good["id"])
    assert "ENAMETOOLONG" in str(ei.value)


@pytest.mark.parametrize("how", ["absolute", "relative"])
def test_indir_symlink_pointing_outside_is_refused(tmp_path, how):
    """An in-dir symlink whose target is outside --clips is refused (QorProof r2 INFO)."""
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"SYNTHETIC")
    link = clips_dir / "hl00.mp4"
    target = outside if how == "absolute" else Path("..") / "outside.mp4"
    try:
        link.symlink_to(target)
    except OSError as e:
        pytest.skip(f"cannot create symlink: {e}")

    with pytest.raises(S.ClipInputError, match="fail closed") as ei:
        S.clip_file_in_dir(clips_dir, "hl00.mp4", clip_id="hl00")
    assert "not inside" in str(ei.value)

    good, bad = _clip(), _clip()
    good["id"], good["file"] = "good", "good.mp4"
    # write_sidecar_bundle would replace the symlink with a regular stub; put it back.
    argv = write_sidecar_bundle(tmp_path, _segs("regular"), [good, bad])
    dest = tmp_path / "clips" / bad["file"]
    dest.unlink()
    dest.symlink_to(target)

    calls: list = []
    saved = (V.energy_curve, V.detect, S.build)
    V.energy_curve = lambda path: (calls.append(path), ([], [], 30.0))[1]
    V.detect = lambda e, c, fps: (_ for _ in ()).throw(AssertionError("detect called before refusal"))
    S.build = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("build called before refusal"))
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = S.main(argv)
    finally:
        V.energy_curve, V.detect, S.build = saved
    assert rc == 2 and out.getvalue() == ""
    assert calls == [], "decode must not follow an out-of-dir symlink"
    assert "not inside" in err.getvalue() and "fail closed" in err.getvalue()
    assert "Traceback" not in err.getvalue()

    rc, out, err = _run_prepared_subprocess(argv)
    assert rc == 2 and out == ""
    assert "not inside" in err and "fail closed" in err
    assert "Traceback" not in err
    assert outside.exists()

