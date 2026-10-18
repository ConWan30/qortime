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


def test_default_slug():
    assert P.default_slug("local-20261017-021003") == "021003"
    assert P.default_slug("Weird ID/../x") == "weird-id-x"
