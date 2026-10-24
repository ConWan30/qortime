import pytest

from qor_receipt.challenge import ALPHABET, PAUSE, motion_correlation_permille, new_nonce, pattern_for, verify_response
from qor_receipt.demo import perform

NONCE = "00112233445566778899aabbccddeeff"


def test_pattern_deterministic_and_shape():
    p = pattern_for(NONCE)
    assert p == pattern_for(NONCE) and p[0] == PAUSE and len(p) == 5
    assert all(b in ALPHABET for b in p[1:]) and all(a != b for a, b in zip(p[1:], p[2:]))


def test_patterns_vary():
    assert len({tuple(pattern_for(new_nonce())) for _ in range(50)}) > 40


def _v(events, seen=1_120, **kw):
    return verify_response(pattern_for(NONCE), events, issued_ms=0, deadline_ms=20_000, pause_seen_ms=seen, **kw)


def test_good_response():
    r = _v(perform(pattern_for(NONCE), 1_000))
    assert r.ok and r.latency_ms == 120


def test_noise_before_pause_ok():
    ev = [{"t_ms": 500, "button": "CROSS", "down": True}] + perform(pattern_for(NONCE), 1_000)
    assert _v(ev).ok


@pytest.mark.parametrize("case,reason", [
    ("wrong", "pattern_mismatch"), ("nopause", "no_pause_press"), ("late", "no_pause_press"),
    ("before_issue", "no_pause_press"), ("extra", "pattern_mismatch"),
])
def test_bad_hid(case, reason):
    p = pattern_for(NONCE)
    if case == "wrong":
        ev = perform([PAUSE] + list(reversed(p[1:])) if p[1:] != p[:0:-1] else [PAUSE] + ["CROSS"] * 4, 1_000)
    elif case == "nopause":
        ev = perform(p[1:], 1_000)
    elif case == "late":
        ev = perform(p, 25_000)
    elif case == "before_issue":
        ev = perform(p, -5_000)
    else:
        ev = perform(p[:2] + ["TRIANGLE" if p[2] != "TRIANGLE" else "CROSS"] + p[2:], 1_000)
    assert _v(ev).reason == reason


@pytest.mark.parametrize("seen,reason", [(None, "pause_not_seen_on_video"), (900, "video_before_input"),
                                         (2_000, "video_latency_too_high")])
def test_bad_video(seen, reason):
    r = _v(perform(pattern_for(NONCE), 1_000), seen=seen)
    assert not r.ok and r.reason == reason


def test_motion_correlation():
    x = [((i * 7) % 11) * 10 for i in range(40)]
    assert motion_correlation_permille(x, [0, 0] + x[:-2]) > 900
    assert motion_correlation_permille(x, [50] * 40) == -1000
    assert motion_correlation_permille(x, [((i * 5) % 3) * 9 for i in range(40)]) < 500


# ---------------------------------------------------------------- v0.3.11: pause-capable windows
def test_v0311_window_is_90s_and_capped():
    from qor_receipt import challenge as CH
    assert CH.DEFAULT_DEADLINE_MS == 90_000 and CH.LEGACY_DEADLINE_MS == 20_000 and CH.MAX_WINDOW_MS == 120_000


def test_dead_options_press_before_the_real_one_does_not_void_the_challenge():
    """Online Madden: OPTIONS during a live play / kickoff return does nothing. The answering press is the first
    OPTIONS followed by exactly the pattern, and the pause frame is judged against THAT press."""
    p = pattern_for(NONCE)
    ev = perform([PAUSE, "CROSS"], 2_000) + perform(p, 30_000)
    r = verify_response(p, ev, issued_ms=0, deadline_ms=90_000, pause_seen_ms=30_150)
    assert r.ok and r.pause_press_ms == 30_000 and r.latency_ms == 150
    # the pause seen after the dead press is not accepted for the real one
    r2 = verify_response(p, ev, issued_ms=0, deadline_ms=90_000, pause_seen_ms=2_150)
    assert not r2.ok and r2.reason == "video_before_input"


def test_response_press_ms_and_pattern_still_must_be_exact():
    from qor_receipt.challenge import response_press_ms
    p = pattern_for(NONCE)
    ev = perform([PAUSE], 1_000) + perform(p, 5_000)
    assert response_press_ms(p, ev, issued_ms=0, deadline_ms=90_000) == 5_000
    assert response_press_ms(p, perform([PAUSE, "CROSS"], 1_000), issued_ms=0, deadline_ms=90_000) == 1_000
    assert response_press_ms(p, perform(p, 95_000), issued_ms=0, deadline_ms=90_000) is None
    # an extra press inside the pattern is still a mismatch, presses before issue still do not count
    bad = perform(p[:2] + ["TRIANGLE" if p[2] != "TRIANGLE" else "CROSS"] + p[2:], 1_000)
    assert verify_response(p, bad, issued_ms=0, deadline_ms=90_000, pause_seen_ms=1_100).reason == "pattern_mismatch"
    assert verify_response(p, perform(p, -1_000), issued_ms=0, deadline_ms=90_000,
                           pause_seen_ms=-900).reason == "no_pause_press"


def test_recorder_rerun_stores_ok_and_pattern_mismatch():
    """A pass and a wrong-button pattern_mismatch are stored. A glitch or a timeout stays null."""
    from qor_receipt.review.local import rerun
    p = pattern_for(NONCE)
    c = {"id": NONCE[:8], "phase": "kickoff", "pattern": p, "issued_ms": 10_000, "deadline_ms": 100_000}
    presses = [dict(e, mono_ms=e["t_ms"]) for e in perform(p, 40_000)]
    out = rerun(c, presses, lambda lo, hi: [40_200])
    assert out["result"]["ok"] and out["result"]["latency_ms"] == 200 and out["result"]["reverified_by"] == "recorder"
    assert len(out["result"]["hid_sha256"]) == 64
    wrong = [dict(e, mono_ms=e["t_ms"]) for e in perform(
        [PAUSE] + list(reversed(p[1:])) if p[1:] != p[:0:-1] else [PAUSE] + ["CROSS"] * 4, 40_000)]
    mismatch = rerun(c, wrong, lambda lo, hi: [40_200], log_trustworthy=True)
    assert mismatch["result"]["ok"] is False
    assert mismatch["result"]["reason"] == "pattern_mismatch"
    assert mismatch["result"]["reverified_by"] == "recorder"
    assert "rule" not in mismatch["result"]
    closed = rerun(c, wrong, lambda lo, hi: [40_200])
    assert closed["result"] is None and closed["local"]["withheld"] == "controller_log"
    assert closed["local"]["reason"] == "pattern_mismatch"
    miss = rerun(c, presses, lambda lo, hi: [])
    assert miss["result"] is None and miss["local"]["reason"] == "pause_not_seen_on_video"

    def boom(lo, hi):
        raise RuntimeError("ffmpeg not available")
    err = rerun(c, presses, boom)
    assert err["result"] is None and "ffmpeg" in err["local"]["note"]
    none = rerun(c, [], lambda lo, hi: [])
    assert none["result"] is None and none["local"]["reason"] == "no_pause_press"


def test_recorder_challenge_results_on_bundle_dir(tmp_path):
    """Presses come from the committed inputs.jsonl (wall = header anchor + mono), like the referee."""
    import json as _j

    from qor_receipt.review.local import challenge_results
    p = pattern_for(NONCE)
    bits = {"CROSS": 0, "CIRCLE": 1, "SQUARE": 2, "TRIANGLE": 3, "OPTIONS": 9, "UP": 12, "DOWN": 13, "LEFT": 14, "RIGHT": 15}
    rows = [{"type": "header", "schema": "qorcap-inputs/v0", "wall_anchor_ms": 1_000_000}]
    t = 50_000
    for b in p:
        rows += [{"type": "in", "t_us": t * 1000, "b": 1 << bits[b]}, {"type": "in", "t_us": (t + 100) * 1000, "b": 0}]
        t += 600
    (tmp_path / "inputs.jsonl").write_text("\n".join(_j.dumps(r) for r in rows) + "\n")
    c = {"id": "x", "phase": "halftime", "pattern": p, "issued_ms": 1_040_000, "deadline_ms": 1_130_000}
    out = challenge_results(tmp_path, [c], decode=False)
    assert out[0]["result"] is None and out[0]["local"]["pause_press_ms"] == 1_050_000
    assert out[0]["local"]["presses_in_window"] == 5 and "ffmpeg" in out[0]["local"]["note"]


def _bits():
    from qor_receipt.review.bundle import BUTTON_BITS

    return {name: bit for name, bit in BUTTON_BITS}


def _challenge(nonce: str, phase: str, issued: int) -> dict:
    return {"id": nonce[:8], "role": "home", "phase": phase, "nonce": nonce,
            "pattern": pattern_for(nonce), "issued_ms": issued, "deadline_ms": issued + 90_000, "result": None}


def _header(anchor_ms: int = 0) -> dict:
    return {"type": "header", "schema": "qorcap-inputs/v0", "t_us": 0, "seg": 0, "wall_anchor_ms": anchor_ms}


def _press(t_ms: int, button: str) -> dict:
    return {"type": "in", "t_us": t_ms * 1000, "b": 1 << _bits()[button], "seg": 0}


def _status(t_ms: int, state: str) -> dict:
    return {"type": "status", "t_us": t_ms * 1000, "state": state, "detail": state, "seg": 0}


def _chunk(t_ms: int, seq: int = 1) -> dict:
    return {"type": "chunk", "seq": seq, "seg": seq, "n": 1, "sha256": "ab" * 32,
            "prev": "cd" * 32, "head": "ef" * 32, "t_us": t_ms * 1000}


def _chunk_span(issued_ms: int, deadline_ms: int, step: int = 2_000) -> list[dict]:
    """Chunk rows from one step before the window through the deadline."""
    rows = []
    t = issued_ms - step
    seq = 0
    while t < deadline_ms:
        rows.append(_chunk(t, seq))
        seq += 1
        t += step
    rows.append(_chunk(deadline_ms, seq))
    return rows


def _kickoff():
    return _challenge("00112233445566778899aabbccddeeff", "kickoff", 1_000_000)


def _phases(kick: dict) -> list[dict]:
    return [kick,
            _challenge("ffeeddccbbaa99887766554433221100", "halftime", 2_000_000),
            _challenge("0123456789abcdef0123456789abcdef", "end", 3_000_000)]


def _wrong_button(pattern: list[str]) -> str:
    return next(b for b in ("CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "UP", "DOWN", "LEFT", "RIGHT") if b != pattern[1])


def _fill(bundle, rows, challenges, *, chop: int = 0, tear_after: int | None = None,
          inject: tuple[int, str] | None = None):
    import json as _j

    from qor_receipt.review.local import challenge_results

    bundle.mkdir(parents=True)
    lines = [_j.dumps(r) for r in rows]
    if tear_after is not None:
        lines.insert(tear_after, '{"type": "in", "t_us":')
    if inject is not None:
        lines.insert(inject[0], inject[1])
    raw = ("\n".join(lines) + "\n").encode()
    if chop:
        raw = raw[:-chop]
    (bundle / "inputs.jsonl").write_bytes(raw)
    return challenge_results(bundle, challenges, decode=False)


def _boundary(tmp_path, name, rows):
    out = _fill(tmp_path / name, rows, _phases(_kickoff()))
    return out[0]["result"], out[0]["local"]


def test_pattern_mismatch_is_stored_when_the_log_can_support_it(tmp_path):
    """A wrong button on a connected pad that covers the deadline is a stored mismatch."""
    from qor_receipt.review.local import STALL_LAG_MS

    assert STALL_LAG_MS == 2_500
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    out = _fill(tmp_path / "bundle", [
        _header(), _status(0, "connected"), _press(t0, "OPTIONS"),
        _press(t0 + 400, _wrong_button(kick["pattern"])),
        *_chunk_span(kick["issued_ms"], kick["deadline_ms"]),
    ], _phases(kick))
    stored = out[0]["result"]
    assert stored["ok"] is False and stored["reason"] == "pattern_mismatch"
    assert stored["reverified_by"] == "recorder" and "rule" not in stored
    assert isinstance(stored["hid_sha256"], str) and len(stored["hid_sha256"]) == 64
    assert stored["done_ms"] is None and stored["latency_ms"] is None
    assert isinstance(stored["pause_press_ms"], int)
    assert "withheld" not in out[0]["local"]
    assert out[1]["result"] is None and out[1]["local"]["reason"] == "no_pause_press"
    assert out[2]["result"] is None and "withheld" not in out[2]["local"]


def test_options_alone_and_late_rest_are_stored_mismatches(tmp_path):
    """OPTIONS alone, and OPTIONS in the window with the rest after the deadline, are pattern_mismatch."""
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    header = _header()
    span = _chunk_span(kick["issued_ms"], kick["deadline_ms"])
    alone = _fill(tmp_path / "alone", [header, _status(0, "connected"), _press(t0, "OPTIONS"), *span], _phases(kick))
    assert alone[0]["result"]["reason"] == "pattern_mismatch" and "withheld" not in alone[0]["local"]
    late_rows = [header, _status(0, "connected"), _press(t0, "OPTIONS")]
    for i, button in enumerate(kick["pattern"][1:]):
        late_rows.append(_press(kick["deadline_ms"] + 1_000 + i * 400, button))
    late_rows.extend(span)
    late = _fill(tmp_path / "late", late_rows, _phases(kick))
    assert late[0]["result"]["reason"] == "pattern_mismatch" and "withheld" not in late[0]["local"]


def _glitch_rows(kind: str):
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    header, connected = _header(), _status(0, "connected")
    options = _press(t0, "OPTIONS")
    wrong = _press(t0 + 400, _wrong_button(kick["pattern"]))
    span = _chunk_span(kick["issued_ms"], kick["deadline_ms"])
    if kind == "stall":
        return [header, connected, options, _press(t0 + 400, kick["pattern"][1]),
                _status(t0 + 800, "stalled"), *span], 0, None
    if kind == "unplug":
        return [header, connected, options, _status(t0 + 200, "unplugged"), *span], 0, None
    if kind == "missing":
        return [header, connected, options, _status(t0 + 200, "missing"), *span], 0, None
    if kind == "reconnect":
        return [header, connected, options, _status(t0 + 200, "unplugged"),
                _status(t0 + 800, "connected"), _press(t0 + 1_200, _wrong_button(kick["pattern"])), *span], 0, None
    if kind == "truncated":
        return [header, connected, options, *span, wrong], 12, None
    if kind == "stall_after":
        return [header, connected, options, wrong, _status(kick["deadline_ms"] + 500, "stalled"), *span], 0, None
    if kind == "clean_cut":
        return [header, connected, options, wrong], 0, None
    if kind == "torn":
        return [header, connected, options, wrong, *span], 0, 3
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["stall", "unplug", "missing", "reconnect", "truncated", "stall_after", "clean_cut", "torn"])
def test_controller_glitch_withholds_the_mismatch(tmp_path, kind):
    """A recorder or controller gap is not stored as a player failure."""
    import json as _j

    from qor_receipt.review.local import inputs_log_intact

    rows, chop, tear_after = _glitch_rows(kind)
    out = _fill(tmp_path / "bundle", rows, _phases(_kickoff()), chop=chop, tear_after=tear_after)
    assert out[0]["result"] is None
    assert out[0]["local"]["reason"] == "pattern_mismatch" and out[0]["local"]["withheld"] == "controller_log"
    path = tmp_path / "bundle" / "inputs.jsonl"
    if kind in {"truncated", "torn"}:
        assert inputs_log_intact(path) is False
    if kind == "torn":
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        _j.loads(lines[-1])
        with pytest.raises(_j.JSONDecodeError):
            _j.loads(lines[3])
    if kind == "clean_cut":
        assert inputs_log_intact(path) is True


def test_ignoring_the_controller_log_stores_the_glitch(tmp_path, monkeypatch):
    """Trusting every pattern_mismatch would store a stall as a player failure."""
    from qor_receipt.review import local

    monkeypatch.setattr(local, "mismatch_log_trustworthy", lambda *a, **k: True)
    rows, chop, tear_after = _glitch_rows("stall")
    out = _fill(tmp_path / "bundle", rows, _phases(_kickoff()), chop=chop, tear_after=tear_after)
    assert out[0]["result"]["reason"] == "pattern_mismatch"
    assert "withheld" not in out[0]["local"]


def test_window_boundaries_and_the_stall_lag(tmp_path):
    from qor_receipt.review.local import STALL_LAG_MS

    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    t0 = issued + 1_000
    presses = [_press(t0, "OPTIONS"), _press(t0 + 400, _wrong_button(kick["pattern"]))]
    span = _chunk_span(issued, deadline)
    stored, local = _boundary(tmp_path, "at-issued", [_header(), _status(issued, "connected"), *presses, *span])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local
    for name, stall_at in (("stall-issued", issued), ("stall-deadline", deadline), ("stall-lag", deadline + STALL_LAG_MS)):
        stored, local = _boundary(
            tmp_path, name,
            [_header(), _status(0, "connected"), *presses, _status(stall_at, "stalled"), *span])
        assert stored is None and local["withheld"] == "controller_log", name
    stored, local = _boundary(
        tmp_path, "after-lag",
        [_header(), _status(0, "connected"), *presses, _status(deadline + STALL_LAG_MS + 1, "stalled"), *span])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local


def test_no_status_rows_withhold_the_mismatch(tmp_path):
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    stored, local = _boundary(tmp_path, "bundle", [
        _header(), _press(t0, "OPTIONS"), _press(t0 + 400, _wrong_button(kick["pattern"])),
        *_chunk_span(kick["issued_ms"], kick["deadline_ms"])])
    assert stored is None and local["reason"] == "pattern_mismatch" and local["withheld"] == "controller_log"


def test_status_tie_at_issue_fails_closed(tmp_path):
    """Every status row at the last wall <= issued_ms must be connected, in either order."""
    kick = _kickoff()
    issued = kick["issued_ms"]
    t0 = issued + 1_000
    presses = [_press(t0, "OPTIONS"), _press(t0 + 400, _wrong_button(kick["pattern"]))]
    span = _chunk_span(issued, kick["deadline_ms"])
    stored, local = _boundary(tmp_path, "connected", [
        _header(), _status(issued, "connected"), *presses, *span])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local
    for name, first, second in (
        ("asleep-then-connected", "asleep", "connected"),
        ("connected-then-asleep", "connected", "asleep"),
    ):
        stored, local = _boundary(tmp_path, name, [
            _header(), _status(issued, first), _status(issued, second), *presses, *span])
        assert stored is None and local["withheld"] == "controller_log", name


def test_status_rows_are_sorted_before_the_window_check(tmp_path):
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    t0 = issued + 1_000
    presses = [_press(t0, "OPTIONS"), _press(t0 + 400, _wrong_button(kick["pattern"]))]
    span = _chunk_span(issued, deadline)
    stored, local = _boundary(tmp_path, "late-first", [
        _header(), _status(deadline + 10_000, "stalled"), _status(issued, "connected"), *presses, *span])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local
    stored, local = _boundary(tmp_path, "stall-first", [
        _header(), _status(t0 + 800, "stalled"), _status(0, "connected"), *presses, *span])
    assert stored is None and local["withheld"] == "controller_log"


def test_nonzero_wall_anchor_is_applied(tmp_path):
    anchor = 5_000_000
    kick = _challenge("00112233445566778899aabbccddeeff", "kickoff", anchor)
    wrong = _wrong_button(kick["pattern"])
    stored = _fill(tmp_path / "store", [
        _header(anchor), _status(0, "connected"), _press(1_000, "OPTIONS"), _press(1_400, wrong),
        *_chunk_span(0, 90_000),
    ], [kick])
    assert stored[0]["result"]["reason"] == "pattern_mismatch"
    withheld = _fill(tmp_path / "stall", [
        _header(anchor), _status(0, "connected"), _press(1_000, "OPTIONS"), _press(1_400, wrong),
        _status(1_000, "stalled"), *_chunk_span(0, 90_000),
    ], [kick])
    assert withheld[0]["result"] is None and withheld[0]["local"]["withheld"] == "controller_log"


def test_malformed_status_withholds_the_mismatch(tmp_path):
    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    span = _chunk_span(kick["issued_ms"], kick["deadline_ms"])
    bad_time = {"type": "status", "t_us": float((t0 + 800) * 1000), "state": "stalled", "detail": "stalled", "seg": 0}
    stored, local = _boundary(tmp_path, "time", [
        _header(), _status(0, "connected"), _press(t0, "OPTIONS"),
        _press(t0 + 400, _wrong_button(kick["pattern"])), bad_time, *span])
    assert stored is None and local["withheld"] == "controller_log"
    bad_state = {"type": "status", "t_us": (t0 + 800) * 1000, "state": 1, "seg": 0}
    stored, local = _boundary(tmp_path, "state", [
        _header(), _status(0, "connected"), _press(t0, "OPTIONS"),
        _press(t0 + 400, _wrong_button(kick["pattern"])), bad_state, *span])
    assert stored is None and local["withheld"] == "controller_log"
    bad_bool = {"type": "status", "t_us": True, "state": "connected", "seg": 0}
    stored, local = _boundary(tmp_path, "bool", [
        _header(), _status(0, "connected"), _press(t0, "OPTIONS"),
        _press(t0 + 400, _wrong_button(kick["pattern"])), bad_bool, *span])
    assert stored is None and local["withheld"] == "controller_log"


def test_missing_inputs_jsonl_withholds_the_mismatch(tmp_path):
    """A bundle with no inputs.jsonl cannot support a stored mismatch."""
    from qor_receipt.review.local import inputs_log_intact, mismatch_log_trustworthy

    missing = tmp_path / "empty" / "inputs.jsonl"
    assert inputs_log_intact(missing) is False
    kick = _kickoff()
    records = [_header(), _status(0, "connected"), *_chunk_span(kick["issued_ms"], kick["deadline_ms"])]
    assert mismatch_log_trustworthy(
        tmp_path / "empty", records, issued_ms=kick["issued_ms"], deadline_ms=kick["deadline_ms"],
        anchor_ms=0) is False


def test_connected_after_a_stall_needs_the_wall_anchor(tmp_path):
    """Dropping wall_anchor_ms hides an in-window stall behind the later connected row."""
    anchor = 400_000
    kick = _challenge("00112233445566778899aabbccddeeff", "kickoff", anchor)
    wrong = _wrong_button(kick["pattern"])
    rows = [
        _header(anchor), _status(0, "connected"), _press(500, "OPTIONS"), _press(900, wrong),
        _status(1_000, "stalled"), _status(2_000, "connected"),
        *_chunk_span(0, kick["deadline_ms"]),
    ]
    filled = _fill(tmp_path / "bundle", rows, [kick])
    assert filled[0]["result"] is None
    assert filled[0]["local"]["reason"] == "pattern_mismatch"
    assert filled[0]["local"]["withheld"] == "controller_log"


@pytest.mark.parametrize("literal", ["42", "[]"])
def test_non_object_jsonl_line_withholds_the_mismatch(tmp_path, literal):
    """A line that parses but is not an object is malformed. It is not a player FAIL."""
    from qor_receipt.review.local import inputs_log_intact

    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    rows = [_header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"])),
            *_chunk_span(kick["issued_ms"], kick["deadline_ms"])]
    out = _fill(tmp_path / "bundle", rows, _phases(kick), inject=(2, literal))
    assert inputs_log_intact(tmp_path / "bundle" / "inputs.jsonl") is False
    assert out[0]["result"] is None
    assert out[0]["local"]["reason"] == "pattern_mismatch" and out[0]["local"]["withheld"] == "controller_log"


def test_second_header_withholds_the_mismatch(tmp_path):
    """Two header rows make the log malformed. The mismatch is not stored."""
    from qor_receipt.review.local import inputs_log_intact

    kick = _kickoff()
    t0 = kick["issued_ms"] + 1_000
    rows = [_header(), _header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"])),
            *_chunk_span(kick["issued_ms"], kick["deadline_ms"])]
    out = _fill(tmp_path / "bundle", rows, _phases(kick))
    assert inputs_log_intact(tmp_path / "bundle" / "inputs.jsonl") is False
    assert out[0]["result"] is None
    assert out[0]["local"]["reason"] == "pattern_mismatch" and out[0]["local"]["withheld"] == "controller_log"


def test_chunk_coverage_must_be_continuous(tmp_path):
    """One far-future chunk, or a crash gap before post-deadline rows, does not cover the window.

    The open is the latest chunk at or before issued_ms, within 3000 ms. The chain must then reach
    a chunk at or after deadline_ms. A chunk 1 ms after the open does not close it, and a last chunk
    1 ms before the deadline does not cover it.
    """
    from qor_receipt.review.local import CHUNK_GAP_MAX_MS, CHUNK_GAP_SLACK_MS, CHUNK_INTERVAL_MS, chunks_cover

    assert CHUNK_INTERVAL_MS == 2_000 and CHUNK_GAP_SLACK_MS == 1_000 and CHUNK_GAP_MAX_MS == 3_000
    kick = _kickoff()
    issued, deadline = kick["issued_ms"], kick["deadline_ms"]
    t0 = issued + 1_000
    head = [_header(), _status(0, "connected"), _press(t0, "OPTIONS"),
            _press(t0 + 400, _wrong_button(kick["pattern"]))]

    far = _fill(tmp_path / "far", [*head, _chunk(deadline + 60_000)], _phases(kick))
    assert far[0]["result"] is None and far[0]["local"]["withheld"] == "controller_log"
    gap = _fill(
        tmp_path / "gap",
        [*head, *_chunk_span(issued, issued + 10_000), _chunk(deadline), _chunk(deadline + 2_000, seq=99)],
        _phases(kick))
    assert gap[0]["result"] is None
    assert gap[0]["local"]["reason"] == "pattern_mismatch" and gap[0]["local"]["withheld"] == "controller_log"

    stored, local = _boundary(tmp_path, "at-bound", [*head, *_chunk_span(issued, deadline, step=CHUNK_GAP_MAX_MS)])
    assert stored["reason"] == "pattern_mismatch" and "withheld" not in local
    stored, local = _boundary(
        tmp_path, "over-bound", [*head, *_chunk_span(issued, deadline, step=CHUNK_GAP_MAX_MS + 1)])
    assert stored is None and local["withheld"] == "controller_log"

    def cover(times: list[int]) -> bool:
        return chunks_cover([_chunk(t, i) for i, t in enumerate(times)],
                            issued_ms=issued, deadline_ms=deadline, anchor_ms=0)

    def span(step: int, *, start: int, end: int) -> list[int]:
        times, t = [], start
        while t < end:
            times.append(t)
            t += step
        times.append(end)
        return times

    chain = span(CHUNK_GAP_MAX_MS, start=issued + 2_000, end=deadline)
    assert cover([issued - 1_000, *chain]) is True
    assert cover([issued - 60_000, issued - 1_000, *chain]) is True
    assert cover([issued - CHUNK_GAP_MAX_MS - 1, issued + 1, deadline]) is False
    assert cover(span(2_000, start=issued + 1, end=deadline)) is False
    short = span(2_000, start=issued - 2_000, end=deadline)
    assert cover(short[:-1] + [deadline - 1]) is False
    assert cover(span(CHUNK_GAP_MAX_MS, start=issued, end=deadline) + [deadline + 60_000]) is True
    assert cover([issued, issued + CHUNK_GAP_MAX_MS + 1, deadline]) is False
