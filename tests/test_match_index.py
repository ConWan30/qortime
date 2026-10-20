import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from qor_receipt.jcs import sha256_hex
from qor_receipt.match_index import build_index
from qor_receipt.match_index.build import bitnames, detect_events

W0 = 1_800_000_000_000


def _read(t, a, h, q=1, c=100):
    return {"t_ms": W0 + t, "away": a, "home": h, "quarter": q, "clock_s": c, "clock_red": False}


def _bundle(d, final=None):
    d.mkdir(parents=True, exist_ok=True)
    tl = [_read(i * 1000, 0, 0) for i in range(5)] + [_read(5000, 0, 9)]  # one-read flicker, must be ignored
    tl += [_read(i * 1000, 0, 0) for i in range(6, 10)] + [_read(i * 1000, 0, 7) for i in range(10, 20)]
    tl += [_read(i * 1000, 0, 7, q=2) for i in range(20, 25)] + [{"obs": "scorebug_absent", "n": 3, "t_ms": W0 + 25000, "until_ms": W0 + 28000}]
    (d / "timeline.jsonl").write_text("\n".join(json.dumps(x) for x in tl))
    cps = []
    for i in range(0, 30, 2):
        q = {"trigger": "segment", "segment_seq": i, "segment_hash": "ab" * 32, "chain_head": "cd" * 32, "score": None}
        if i == 10:
            q.update(trigger="score_change", score={"away": 0, "home": 7, "quarter": 1})
        cps.append({"seq": len(cps), "t_ms": W0 + i * 1000 + 100, "hash": f"{i:064x}", "prev": "0" * 64, "frames_hash": sha256_hex(q), "qorcap": q})
    (d / "checkpoints.jsonl").write_text("\n".join(json.dumps(x) for x in cps))
    man = [{"type": "segment", "seq": i, "mono_ms": i * 1000, "wall_ms": W0 + i * 1000, "frames": i * 30} for i in range(31)]
    (d / "manifest.jsonl").write_text("\n".join(json.dumps(x) for x in man))
    ins = [{"type": "header", "wall_anchor_ms": W0, "t_us": 0}]
    ins += [{"type": "in", "t_us": t * 1000, "b": b} for t, b in [(9000, 0), (9500, 1), (9600, 1), (9700, 0), (9800, 1 << 12)]]
    (d / "inputs.jsonl").write_text("\n".join(json.dumps(x) for x in ins))
    (d / "haptics.jsonl").write_text(json.dumps({"type": "haptic", "t_wall_ms": W0 + 10200, "energy": 500, "duration_ms": 90, "confidence": 800}))
    (d / "bundle.json").write_text(json.dumps({"schema": "qorcap-bundle/v0", "match_id": "t1", "final": final, "final_confirmed": bool(final),
                                               "chain_head": "cd" * 32, "clips": [{"name": "score-a0-h7-q1", "sha256": "ee" * 32}]}))
    (d / "receipt.json").write_text(json.dumps({"result": {"away_score": 0, "home_score": 7, "final_confirmed": False,
                                                           "method": "last_scorebug_read_unconfirmed", "final_t_ms": W0 + 29000}}))
    return d


def test_events_flicker_binding_inputs(tmp_path):
    idx = build_index(_bundle(tmp_path / "b"), tmp_path / "o", frames=False)
    kinds = [e["kind"] for e in idx["events"]]
    assert kinds == ["kickoff", "score_change", "quarter_change", "end_of_game"]
    sc = idx["events"][1]
    assert sc["score_before"] == {"away": 0, "home": 0} and sc["score_after"] == {"away": 0, "home": 7}
    assert sc["t_ms"] == W0 + 10000 and sc["legal_step"] and sc["locked"]
    b = sc["checkpoints"]
    assert b["trigger_match"]["seq"] == 5 and b["trigger_match"]["frames_hash_matches_evidence"] is True
    assert b["clip"]["name"] == "score-a0-h7-q1" and b["before"]["t_ms"] <= sc["t_ms"] <= b["after"]["t_ms"]
    pr = sc["inputs"]["presses"]
    assert [p["buttons"] for p in pr] == [["CROSS"], ["UP"]] and pr[0]["dt_ms"] == -500
    assert sc["inputs"]["haptics"][0]["dt_ms"] == 200 and "Advisory" in sc["inputs"]["advisory"]
    assert sc["recording_s"] == 10.0 and sc["frames"] == [] and sc["frames_missing_reason"] == "no video supplied"
    end = idx["events"][-1]
    assert end["score_after"] is None and end["final_confirmed"] is False and not end["locked"]
    assert "bundle.final (end-of-game menu read)" in idx["missing"]
    html = (tmp_path / "o" / "viewer.html").read_text()
    assert "advisory" in html and "missing" in html and '"match_id": "t1"' in html


def test_tampered_checkpoint_never_locks(tmp_path):
    d = _bundle(tmp_path / "b")
    rows = [json.loads(x) for x in (d / "checkpoints.jsonl").read_text().splitlines()]
    rows[5]["qorcap"]["score"]["home"] = 7  # unchanged score but forge frames_hash
    rows[5]["frames_hash"] = "00" * 32
    (d / "checkpoints.jsonl").write_text("\n".join(json.dumps(x) for x in rows))
    sc = build_index(d, tmp_path / "o", frames=False)["events"][1]
    assert sc["checkpoints"]["trigger_match"]["frames_hash_matches_evidence"] is False and not sc["locked"]


def test_final_and_zip_and_cache(tmp_path):
    d = _bundle(tmp_path / "b", final={"away": 0, "home": 10})
    z = tmp_path / "bundle-meta.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in d.iterdir():
            zf.write(p, p.name)
    idx = build_index(z, tmp_path / "o", frames=False)
    end = idx["events"][-1]
    assert end["score_after"] == {"away": 0, "home": 10} and end["final_confirmed"] and end["locked"]
    assert build_index(z, tmp_path / "o", frames=False)["cache_key"] == idx["cache_key"]


def test_illegal_step_low_confidence():
    tl = [_read(i * 1000, 0, 0) for i in range(5)] + [_read(i * 1000, 0, 5) for i in range(5, 9)]
    ev = detect_events(tl, None, None, False)
    sc = [e for e in ev if e["kind"] == "score_change"][0]
    assert sc["legal_step"] is False and sc["confidence"] < 0.85
    assert bitnames(1 | 1 << 9) == ["CROSS", "OPTIONS"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_burst_frames_from_video(tmp_path):
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=30",
                    "-pix_fmt", "yuv420p", str(v)], check=True)
    idx = build_index(_bundle(tmp_path / "b"), tmp_path / "o", video=v)
    sc = idx["events"][1]
    # +7 = TD + PAT: burst pre-rolls to PLAY_PAD_S before the last read of the old score (9 s) so the play is in frame
    assert sc["play"].startswith("touchdown + extra point") and sc["pre_roll_s"] == 5.0
    assert len(sc["frames"]) == 18 and all(f["ok"] for f in sc["frames"]) and sc["contact_sheet"]
    assert sc["frames"][0]["dt_s"] == -5.0 and sc["frames"][-1]["dt_s"] == 2.0
    assert "data:image/jpeg;base64," in (tmp_path / "o" / "viewer.html").read_text()


def test_viewer_template_renders_self_contained(tmp_path):
    from qor_receipt.match_index.viewer import TEMPLATE, write_viewer
    assert "__DATA__" in TEMPLATE and "__TITLE__" in TEMPLATE
    idx = {"match_id": "m</script><b>", "events": [{"kind": "score_change", "t_ms": W0, "score_before": None, "score_after": {"away": 0, "home": 7},
                                                    "locked": True, "frames": [{"file": "events/x/frame_00.jpg", "dt_s": 0.0}]}],
           "result": {"final": None, "final_confirmed": False}, "chain": {}, "input_summary": {}, "video": {}, "missing": ["bundle.final"]}
    html = write_viewer(idx, tmp_path).read_text()
    assert "__DATA__" not in html and "__TITLE__" not in html
    assert "</script><b>" not in html.split("<script>", 1)[1].rsplit("</script>", 1)[0]  # data blob cannot break out
    assert "m&lt;/script&gt;&lt;b&gt;" in html  # title escaped
    assert "prefers-reduced-motion" in html and "advisory evidence" in html.lower() and "not proof" in html
    assert '"src": null' in html  # missing frame file stays missing, never fabricated
    for ext in ("http://", "https://", "<link", "@import", "src=\"//"):
        assert ext not in html.replace("http://www.w3.org/2000/svg", "")


def test_viewer_mobile_contract(tmp_path):
    """Mobile QA (2026-10-17): touch hit areas, page scroll through the timeline, no unused embeds."""
    from qor_receipt.match_index.viewer import TEMPLATE, write_viewer
    (tmp_path / "events" / "x").mkdir(parents=True)
    (tmp_path / "events" / "x" / "frame_00.jpg").write_bytes(b"\xff\xd8frame")
    (tmp_path / "events" / "x" / "contact.jpg").write_bytes(b"\xff\xd8contact-sheet-bytes")
    idx = {"match_id": "m", "events": [{"kind": "score_change", "t_ms": W0, "score_after": {"away": 0, "home": 7},
                                        "contact_sheet": "events/x/contact.jpg",
                                        "frames": [{"file": "events/x/frame_00.jpg", "dt_s": 0.0}]}],
           "result": {}, "chain": {}, "input_summary": {}, "video": {}, "missing": []}
    html = write_viewer(idx, tmp_path).read_text()
    import base64
    assert base64.b64encode(b"\xff\xd8frame").decode() in html
    assert base64.b64encode(b"\xff\xd8contact-sheet-bytes").decode() not in html  # unused by the viewer
    assert "touch-action:pan-y" in TEMPLATE and "touch-action:none" not in TEMPLATE  # vertical swipes still scroll
    assert "pointercancel" in TEMPLATE and 'id="prv"' in TEMPLATE and 'id="nxt"' in TEMPLATE
    assert "width:44px;height:44px" in TEMPLATE  # markers / step buttons meet the 44px touch target
    assert "prefers-reduced-motion" in TEMPLATE and "(pointer:coarse)" in TEMPLATE


def test_viewer_sticky_ticker_and_manual_hash_check():
    """Sticky score ticker, and checkpoint hashes only after a click or tap.

    overflow-x:hidden on html and body is a scroll container, so position:sticky never pins.
    clip does not create one. hidden remains first so browsers without clip still block sideways scroll.
    """
    from qor_receipt.match_index.viewer import TEMPLATE

    html_rule = TEMPLATE.split("html{", 1)[1].split("}", 1)[0]
    body_rule = TEMPLATE.split("body{", 1)[1].split("}", 1)[0]
    assert "overflow-x:hidden;overflow-x:clip" in html_rule
    assert "overflow-x:hidden;overflow-x:clip" in body_rule
    assert "position:sticky;top:0" in TEMPLATE
    assert "new IntersectionObserver" not in TEMPLATE
    assert "io.observe" not in TEMPLATE
    assert 'proofBtn.onclick=()=>startProof()' in TEMPLATE
    assert 'if(proofRunning){showProofBtn("run");return}' in TEMPLATE
    assert 'b.textContent="Checking hashes…"' in TEMPLATE
    assert 'textContent=proofStarted?"Check again"' in TEMPLATE
    assert '.btn[aria-busy="true"]' in TEMPLATE
    assert "b.disabled=true" not in TEMPLATE
    # receipt status and the gold seal path are unchanged
    assert 'STATUS==="VERIFIED"' in TEMPLATE
    assert "Receipt FAIL · not a verified result" in TEMPLATE
    root = Path(__file__).resolve().parents[1]
    for rel in ("site/matches/125430/index.html", "site/matches/021003/index.html"):
        page = (root / rel).read_text()
        assert "overflow-x:hidden;overflow-x:clip" in page
        assert "new IntersectionObserver" not in page
        assert 'proofBtn.onclick=()=>startProof()' in page
        assert 'textContent=proofStarted?"Check again"' in page
        assert "Check hashes in this browser" in page
        assert '"status": "VERIFIED"' not in page


def test_scoring_play_labels_and_preroll_window():
    """2026-10-17 match 021003: TD bursts showed the PAT setup (scorebug updates ~10-20 s after the TD), PATs looked like FGs."""
    from qor_receipt.match_index.build import MAX_PRE_S, Clock, burst_window
    tl = [_read(i * 1000, 0, 0, c=300 - i) for i in range(0, 10)]           # last 0-0 read at 9 s
    tl += [_read(i * 1000, 0, 6, c=280) for i in range(30, 36)]             # TD shows on the scorebug at 30 s (21 s later)
    tl += [_read(i * 1000, 0, 7, c=280) for i in range(40, 45)]             # PAT
    tl += [_read(i * 1000, 3, 7, c=200) for i in range(100, 105)]           # FG
    tl += [_read(i * 1000, 5, 7, c=150) for i in range(400, 405)]           # +2 long after any TD = safety
    tl += [_read(i * 1000, 5, 13, c=100) for i in range(500, 503)] + [_read(i * 1000, 5, 15, c=100) for i in range(520, 523)]
    tl += [_read(i * 1000, 5, 15, c=100) for i in range(600, 700)]          # 60+ s of reads after the last old read
    tl += [_read(i * 1000, 12, 15, c=90) for i in range(700, 703)]          # +7 with a long gap: capped at MAX_PRE_S
    sc = [e for e in detect_events(tl, None, None, False) if e["kind"] == "score_change"]
    assert [e["play"].split(" (")[0] for e in sc] == ["touchdown", "extra point", "field goal", "safety", "touchdown",
                                                       "two-point conversion", "touchdown + extra point"]
    clock = Clock([{"type": "segment", "mono_ms": i * 1000, "wall_ms": W0 + i * 1000, "frames": i * 30} for i in range(800)])
    a, z, n, pre = burst_window(sc[0], 30.0, clock)
    assert (a, z, n, pre) == (5.0, 32.0, 18, 25.0)                         # TD: from 4 s before the last 0-0 read
    assert burst_window(sc[1], 40.0, clock) == (37.0, 42.0, 10, 3.0)       # PAT keeps the short burst
    assert burst_window(sc[6], 700.0, clock)[3] == 5.0                     # last 5-15 read at 699 s, minus the 4 s pad
    gap = dict(sc[6], last_old_read_t_ms=W0 + 100_000)
    assert burst_window(gap, 700.0, clock)[3] == MAX_PRE_S                 # never more than MAX_PRE_S


def test_viewer_audit_strings_stay_honest_without_network(tmp_path):
    """Audit items that must be in the page source, with no https and no gold seal baked into a FAIL page."""
    from qor_receipt.match_index.viewer import TEMPLATE, og_png, write_viewer

    idx = {"match_id": "local-1", "events": [{"id": "e1", "kind": "score_change", "t_ms": W0, "recording_s": 12,
                                              "score_after": {"away": 0, "home": 7}, "play": "touchdown", "confidence": 0.9,
                                              "frames": []}],
           "result": {"final": None, "final_confirmed": False}, "chain": {}, "input_summary": {}, "video": {},
           "missing": ["bundle.final (end-of-game menu read)"]}
    html = write_viewer(idx, tmp_path).read_text()
    for needle in ("<h1", 'aria-hidden="true"', "No confirmed final.", "qorcap highlights --check",
                   'id="share"', "Check hashes in this browser", "this match only", "Download clip",
                   "How to check", "Tap play to load"):
        assert needle in html
    assert "https://" not in html and "<link" not in html
    assert '"locked": true' not in html
    fail = og_png({"match_id": "m", "result": {"final": None, "final_confirmed": False},
                   "publish": {"slug": "m", "receipt": {"status": "FAIL"}}})
    assert _goldish(fail) == 0
    verified = og_png({"match_id": "m", "result": {"final": {"away": 1, "home": 2}, "final_confirmed": True},
                       "publish": {"slug": "m", "receipt": {"status": "VERIFIED"}}})
    assert _goldish(verified) > 0
    assert "width:44px;height:44px" in TEMPLATE
    assert "min-height:44px" in TEMPLATE


def _goldish(png: bytes) -> int:
    import struct
    import zlib
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    i, w, h, idat = 8, 0, 0, b""
    while i + 8 <= len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        tag, data = png[i + 4:i + 8], png[i + 8:i + 8 + n]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif tag == b"IDAT":
            idat += data
        i += 12 + n
        if tag == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    n = 0
    for y in range(h):
        row = raw[y * stride + 1:(y + 1) * stride]
        for x in range(0, len(row), 3):
            r, g, b = row[x], row[x + 1], row[x + 2]
            if r > 210 and 170 <= g <= 230 and b < 130:
                n += 1
    return n
