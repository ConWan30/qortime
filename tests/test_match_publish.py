"""qortime publish: receipt-gated public match pages, privacy scan, highlights linking, /matches/ index."""
import json
import shutil

import pytest

from qor_receipt.match_index import publish as P
from qor_receipt.match_index.build import _jsonl  # noqa: F401  (import check)


def _bundle(tmp, receipt=True, status_fail=False, private=False):
    from qor_receipt.demo import demo_receipt

    b = tmp / "b"
    b.mkdir()
    rec, _pub = demo_receipt("single")
    if status_fail:
        rec["result"]["home_score"] = rec["result"]["home_score"] + 1  # breaks the signature -> FAIL
    t0 = 1_792_000_000_000
    (b / "bundle.json").write_text(json.dumps({"match_id": rec["match"]["match_id"], "role": "home", "final": None,
                                               **({"device_id": "@device_pnp_\\\\?\\usb#x"} if private else {})}))
    if receipt:
        (b / "receipt.json").write_text(json.dumps(rec))
    tl = [{"away": 0, "home": 0, "quarter": 1, "clock_s": 300, "t_ms": t0 + i * 1000} for i in range(5)]
    tl += [{"away": 0, "home": 6, "quarter": 1, "clock_s": 250, "t_ms": t0 + 20_000 + i * 1000} for i in range(5)]
    (b / "timeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tl))
    (b / "inputs.jsonl").write_text('{"type":"header","wall_anchor_ms":%d}\n{"type":"in","t_us":19000000,"b":1}\n' % t0)
    return b


def test_refuses_without_receipt(tmp_path):
    with pytest.raises(P.PublishRefused, match="receipt"):
        P.publish(_bundle(tmp_path, receipt=False), tmp_path / "site", frames=False, slug="101500")
    assert not (tmp_path / "site" / "matches").exists()


def test_publishes_page_index_and_never_raw_inputs(tmp_path):
    site = tmp_path / "site"
    out = P.publish(_bundle(tmp_path), site, frames=False, slug="101500")
    page = site / "matches" / "101500"
    assert out["slug"] == "101500" and out["receipt_status"] in ("SELF_ATTESTED", "VERIFIED", "PARTIAL")
    assert (page / "index.html").exists() and (page / "receipt.json").exists() and (page / "match.json").exists()
    assert not list(page.rglob("inputs.jsonl")) and not list(page.rglob("haptics.jsonl"))
    html = (page / "index.html").read_text()
    assert '"publish"' in html and "advisory evidence, not proof" in html.lower()
    idx = (site / "matches" / "index.html").read_text()
    assert "101500/" in idx and out["match_id"] in idx
    assert "first" not in idx.lower().replace("first-", "")


def test_fail_receipt_needs_allow_fail_and_is_never_gold(tmp_path):
    b = _bundle(tmp_path, status_fail=True)
    with pytest.raises(P.PublishRefused, match="FAIL"):
        P.publish(b, tmp_path / "site", frames=False, slug="101500")
    out = P.publish(b, tmp_path / "site", frames=False, slug="101500", allow_fail=True)
    assert out["receipt_status"] == "FAIL"
    data = (tmp_path / "site" / "matches" / "101500" / "index.html").read_text()
    assert '"locked": true' not in data


def test_privacy_scan():
    assert P.privacy_problems('{"file": "C:\\\\Users\\\\Contr\\\\x.mp4"}')
    assert P.privacy_problems('"device_id": "x"')
    assert not P.privacy_problems('{"device": "DualSense Edge (USB)"}')


def test_will_not_overwrite_hand_built_or_other_match(tmp_path):
    site = tmp_path / "site"
    (site / "matches" / "101500").mkdir(parents=True)
    (site / "matches" / "101500" / "index.html").write_text("hand built")
    b = _bundle(tmp_path)
    with pytest.raises(P.PublishRefused, match="hand-built"):
        P.publish(b, site, frames=False, slug="101500")
    shutil.rmtree(site / "matches" / "101500")
    (site / "matches" / "101500").mkdir()
    (site / "matches" / "101500" / "index.html").write_text("x")
    (site / "matches" / "101500" / "match.json").write_text(json.dumps({"match_id": "someone-else"}))
    with pytest.raises(P.PublishRefused, match="already holds"):
        P.publish(b, site, frames=False, slug="101500")


def test_poster_skips_when_ffmpeg_is_missing(tmp_path, monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    monkeypatch.setattr(P.subprocess, "run", missing)
    assert P._poster(tmp_path / "clip.mp4", 0.15, tmp_path / "out.jpg") is False
    assert not (tmp_path / "out.jpg").exists()


def test_highlights_link_to_score_events(tmp_path):
    import hashlib

    b = _bundle(tmp_path)
    h = tmp_path / "hl"
    h.mkdir()
    (h / "hl00.mp4").write_bytes(b"\x00" * 64)
    rsha = hashlib.sha256((b / "receipt.json").read_bytes()).hexdigest()
    lock = {"t_ms": 1_792_000_020_000, "score_after": {"away": 0, "home": 6}, "in_receipt": True,
            "frames_hash_matches_evidence": True, "hash": "ab" * 32, "seq": 3, "play": "touchdown"}
    (h / "manifest.json").write_text(json.dumps({"schema": "qorcap-highlights/v0", "receipt": {"sha256": rsha},
                                                 "clips": [{"id": "hl00", "file": "hl00.mp4", "sha256": "00", "lock": lock}]}))
    P.publish(b, tmp_path / "site", highlights=h, frames=False, slug="101500")
    page = tmp_path / "site" / "matches" / "101500"
    assert (page / "clips" / "hl00.mp4").exists() and (page / "clips" / "manifest.json").exists()
    data = (page / "index.html").read_text()
    assert '"highlight": 0' in data and '"url": "clips/hl00.mp4"' in data
    (h / "manifest.json").write_text(json.dumps({"receipt": {"sha256": "ff" * 32}, "clips": []}))
    with pytest.raises(P.PublishRefused, match="different receipt"):
        P.publish(b, tmp_path / "site", highlights=h, frames=False, slug="101500")


def test_chain_rail_cracks_and_gold_seal_only_when_verified():
    rec = {"checkpoints": {"home": [{"seq": i, "t_ms": 1_000 + i * 2_000} for i in range(6)]}}
    events = [{"t_ms": 1_000, "recording_s": 0.0}, {"t_ms": 1_000 + 5 * 2_000, "recording_s": 10.0}]
    rail = P.chain_rail(rec, events, "FAIL", ["home:cp[0]:gap_too_long", "home:cp[4]:clock_skew"])
    assert rail["n"] == 6 and rail["seal"] is False and rail["dt_s"] == 2.0
    assert {c["i"] for c in rail["cracks"]} == {0, 4}
    assert "not a seal" in rail["note"]
    sealed = P.chain_rail(rec, events, "VERIFIED", [])
    assert sealed["seal"] is True and sealed["cracks"] == []
    assert P.chain_rail({}, events, "FAIL", []) is None
    assert P.honest_vtt("LOCKED AWAY 0\nscore locked here\n", "FAIL").startswith("SCOREBUG LOCK")
    assert "scorebug read here" in P.honest_vtt("score locked here\n", "SELF_ATTESTED")
    assert P.honest_vtt("LOCKED AWAY 0\n", "VERIFIED").startswith("LOCKED")


def test_viewer_does_not_inline_frames_or_claim_verified_highlights(tmp_path):
    from qor_receipt.match_index.viewer import write_viewer

    out = tmp_path / "m"
    (out / "events").mkdir(parents=True)
    jpg = out / "events" / "f.jpg"
    jpg.write_bytes(bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffda00080001000100003400ffd9"))
    thumb = out / "events" / "f_t.jpg"
    thumb.write_bytes(jpg.read_bytes())
    idx = {"match_id": "m", "events": [{"id": "e", "kind": "score_change", "locked": False, "recording_s": 1,
                                        "frames": [{"file": "events/f.jpg", "thumb": "events/f_t.jpg", "dt_s": 0, "video_s": 1}]}],
           "publish": {"receipt": {"status": "FAIL", "label": "FAIL", "errors": ["gap_too_long"], "partial": []},
                       "highlights": [], "advisory": "advisory evidence, not proof"}}
    write_viewer(idx, out, embed=False, name="index.html")
    html = (out / "index.html").read_text()
    assert "data:image/jpeg;base64" not in html
    assert "events/f_t.jpg" in html
    assert "Verified highlights" not in html
    assert "advisory evidence, not proof" in html.lower()
    assert "SCOREBUG LOCK" in html
    assert "Proof chain" in html
    assert "Video segment bytes are not on this page" in html
    assert "Highlight clips are re-encoded" in html
    assert "not a check of the picture" in html
    assert "advisory evidence, not proof" in html.lower()


def test_index_gold_only_when_referee_verified(tmp_path):
    root = tmp_path / "site"
    for slug, status, final in (("aa", "SELF_ATTESTED", True), ("bb", "VERIFIED", True), ("cc", "FAIL", False)):
        d = root / "matches" / slug
        d.mkdir(parents=True)
        (d / "index.html").write_text("x")
        (d / "match.json").write_text(json.dumps({
            "url": f"{slug}/", "match_id": slug, "game": "madden27", "started_ms": 1_792_000_000_000,
            "final": {"away": 22, "home": 41} if final else None, "final_confirmed": final,
            "receipt_result": {"away": 7, "home": 14}, "receipt_status": status,
            "events": 1, "touchdowns": 0, "clips": 0}))
    P.write_index(root)
    html = (root / "matches" / "index.html").read_text()
    assert html.count('class="lockg"') == 1
    assert "not referee-signed" in html
    assert "receipt FAILS verification" in html


def test_default_slug():
    assert P.default_slug("local-20261017-021003") == "021003"
    assert P.default_slug("Weird ID/../x") == "weird-id-x"


def test_receipt_with_on_screen_names_needs_explicit_opt_in(tmp_path):
    b = _bundle(tmp_path)
    rec = json.loads((b / "receipt.json").read_text())
    o = {"handle": "Qortrola30", "pattern": "Qortrola30", "confidence_permille": 990, "sightings": 9, "agree": 9}
    rec["match"]["observed_handles"] = {"source": "on_screen", "away": o, "home": dict(o, handle="Rival_Opp_77",
                                                                                     pattern="Rival_Opp_77")}
    (b / "receipt.json").write_text(json.dumps(rec))
    with pytest.raises(P.PublishRefused, match="PSN names"):
        P.publish(b, tmp_path / "site", frames=False, slug="101500")
    # opting in gets past the name guard (this edited receipt then FAILs verification, which is refused on its own)
    with pytest.raises(P.PublishRefused, match="does not verify"):
        P.publish(b, tmp_path / "site", frames=False, slug="101500", include_names=True)
