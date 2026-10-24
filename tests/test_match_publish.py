"""qortime publish: receipt-gated public match pages, privacy scan, highlights linking, /matches/ index."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from qor_receipt.match_index import publish as P
from qor_receipt.match_index.build import _jsonl  # noqa: F401  (import check)
from qor_receipt.receipt import verify_receipt


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


def _jsdom_prefix() -> Path:
    """Install the pinned jsdom tree. Skip when npm or the network cannot provide it."""
    import shutil
    import subprocess

    prefix = Path(__file__).resolve().parent / "jsdom"
    if (prefix / "node_modules" / "jsdom").exists():
        return prefix
    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm is not available")
    try:
        subprocess.check_call([npm, "ci", "--ignore-scripts"], cwd=prefix, timeout=180)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("jsdom dependencies could not be installed")
    if not (prefix / "node_modules" / "jsdom").exists():
        pytest.skip("jsdom is not installed")
    return prefix


def _render_page(tmp_path, receipt):
    """Write a page and return the headline and banner the browser actually shows."""
    import os
    import shutil
    import subprocess

    from qor_receipt.match_index.viewer import write_viewer

    if not shutil.which("node"):
        pytest.skip("node is not installed")
    prefix = _jsdom_prefix()
    idx = {"match_id": "m", "events": [
        {"kind": "kickoff", "recording_s": 12, "quarter": 1, "clock_s": 900, "score_after": {"away": 0, "home": 0}}],
           "publish": {"receipt": receipt, "highlights": [], "advisory": "advisory evidence, not proof"}}
    tmp_path.mkdir(parents=True, exist_ok=True)
    page = write_viewer(idx, tmp_path, embed=False, name="index.html")
    env = os.environ.copy()
    proc = subprocess.run(
        ["node", str(Path(__file__).parent / "render_viewer.mjs"), str(page), str(prefix / "package.json")],
        check=True, capture_output=True, text=True, env=env, timeout=30)
    return json.loads(proc.stdout)


def test_refusal_reason_requires_a_fail():
    """A reason on a signed PARTIAL or VERIFIED receipt is not a headline. A fail check names itself."""
    reason = "home:kickoff:challenge_failed:pattern_mismatch"
    stray = {"review": {"reason": reason, "checks": {"challenges": {"state": "partial"}, "motion": {"state": "pass"}}}}
    for status in ("VERIFIED", "PARTIAL", "SELF_ATTESTED"):
        assert P._refusal_reason(stray, {"status": status, "errors": []}) is None
    assert P._refusal_reason(stray, {"status": "FAIL", "errors": ["home:cp[1]:gap_too_long"]}) == reason
    bare = {"review": {"checks": {"input_integrity": {"state": "fail"}}}}
    assert P._refusal_reason(bare, {"status": "PARTIAL", "errors": []}) == "review_check_failed:input_integrity"
    from_errors = {"review": {"checks": {}}}
    err = "home:kickoff:challenge_failed:pattern_mismatch"
    assert P._refusal_reason(from_errors, {"status": "FAIL", "errors": ["home:cp[1]:gap_too_long", err]}) == err
    assert P._refusal_reason(from_errors, {"status": "PARTIAL", "errors": [err]}) is None


def test_public_page_puts_a_challenge_refusal_at_the_top(tmp_path):
    """FAIL is the headline only for a fail check or a blocking reason. A plain partial stays Partial."""
    reason = "home:kickoff:challenge_failed:pattern_mismatch"
    phrase = "Challenge failed at kickoff (home): wrong button pattern"
    failed = _render_page(tmp_path / "fail", {
        "status": "PARTIAL", "label": "PARTIAL · some checks not covered", "errors": [],
        "partial": ["review_partial:motion"], "reason": reason,
        "checks": [{"name": "challenges", "state": "fail", "line": "3 challenges, rule 2"},
                   {"name": "motion", "state": "partial", "line": "low"}]})
    assert failed["role"] == "status"
    assert phrase in failed["statusText"]
    assert "Receipt FAIL" in failed["statusText"]
    assert "fail" in failed["className"]
    assert failed["title"] == (
        "FAIL · Challenge failed at kickoff (home): wrong button pattern. "
        "The referee refused to sign because challenges failed.")
    assert "PARTIAL" not in failed["title"]
    assert phrase in failed["banner"] and "pattern_mismatch" in failed["bannerHtml"]
    assert "referee refused to sign because challenges failed" in failed["banner"]
    assert "PARTIAL" in (tmp_path / "fail" / "index.html").read_text(encoding="utf-8")

    plain = _render_page(tmp_path / "partial", {
        "status": "PARTIAL", "label": "PARTIAL · some checks not covered", "errors": [],
        "partial": ["review_partial:motion", "home:challenge_unanswered:kickoff"],
        "checks": [{"name": "challenges", "state": "partial"}, {"name": "motion", "state": "partial"}]})
    assert plain["statusText"] == "Partial · not a verified result"
    assert "Receipt FAIL" not in plain["statusText"] and plain["banner"] == ""
    assert "fail" not in plain["className"].split()

    local = _render_page(tmp_path / "local", {
        "status": "FAIL", "label": "FAIL · this receipt does not verify", "errors": [reason],
        "partial": [], "reason": reason, "checks": []})
    assert phrase in local["statusText"]
    assert "Recorded on this receipt" in local["banner"]
    assert "referee refused" not in local["banner"]

    unknown = _render_page(tmp_path / "unknown", {
        "status": "FAIL", "label": "FAIL", "errors": [], "partial": [],
        "reason": "home:kickoff:challenge_failed:brand_new_code", "checks": []})
    assert "<code>home:kickoff:challenge_failed:brand_new_code</code>" in unknown["statusHtml"]
    assert "brand_new_code" in unknown["statusText"]

    other = _render_page(tmp_path / "other", {
        "status": "PARTIAL", "label": "PARTIAL · some checks not covered", "errors": [], "partial": [],
        "checks": [{"name": "input_integrity", "state": "fail"}]})
    assert "review_check_failed:input_integrity" in other["statusHtml"]
    assert other["title"] == "FAIL · The referee refused to sign because input integrity failed."
    assert "PARTIAL" not in other["title"]
    assert "input integrity" in other["banner"] and "referee refused to sign" in other["banner"]
    assert "challenges failed" not in other["banner"]

    label = "FAIL · this receipt does not verify; nothing on this page is a verified result"
    card = _render_page(tmp_path / "card", {
        "status": "FAIL", "label": label,
        "errors": ["home:cp[0]:gap_too_long", "home:cp[1]:clock_skew", reason],
        "partial": [], "checks": []})
    assert card["title"] == label
    assert "two checkpoints more than 10 s apart" in card["errsText"]
    assert "checkpoint time more than 3 s from its countersign" in card["errsText"]
    assert "wrong button pattern" in card["errsText"]
    assert reason in card["errsHtml"]


def test_refused_page_keeps_a_partial_label_off_the_tooltip(tmp_path):
    """A refused receipt can still be labelled PARTIAL. The tooltip says FAIL, not that label."""
    reason = "home:kickoff:challenge_failed:pattern_mismatch"
    shown = _render_page(tmp_path, {
        "status": "PARTIAL", "label": "PARTIAL · some checks not covered", "errors": [],
        "partial": ["review_partial:motion"], "reason": reason,
        "checks": [{"name": "challenges", "state": "fail"}]})
    assert "Receipt FAIL" in shown["statusText"]
    assert "wrong button pattern" in shown["statusText"]
    assert shown["title"] == (
        "FAIL · Challenge failed at kickoff (home): wrong button pattern. "
        "The referee refused to sign because challenges failed.")
    assert shown["title"] != "PARTIAL · some checks not covered"
    assert "PARTIAL" not in shown["title"]


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
    assert 'class="tag x"' in html
    assert "not a ranking" in html
    assert "not a verified comparison" not in html  # one card on this index is referee-verified


def test_verifier_paid_tag_is_not_gold_and_can_preload_a_receipt():
    root = Path(__file__).resolve().parents[1]
    page = (root / "verify" / "index.html").read_text()
    assert "paid, test mode" in page
    assert 'q.get("receipt")' in page
    assert "og:image" in page
    assert 'sigStatus === "VERIFIED"' in page
    missing = (root / "site" / "404.html").read_text()
    assert "This page is not on QorTime." in missing
    assert "matches/" in missing


def test_published_original_receipts_keep_their_real_verdicts():
    """The laptop receipts are published byte-for-byte. Verification is whatever verify_receipt returns."""
    root = Path(__file__).resolve().parents[1]
    raw = (root / "site/matches/125430/receipt.json").read_bytes()
    man = json.loads((root / "site/matches/125430/clips/manifest.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == man["receipt"]["sha256"]
    failed = verify_receipt(json.loads(raw))
    assert failed["status"] == "FAIL"
    assert "home:cp[0]:gap_too_long" in failed["errors"]
    assert any("clock_skew" in e for e in failed["errors"])
    page = (root / "site/matches/125430/index.html").read_text()
    assert "Recorded Oct 16, 2026" in page and "12:54" in page
    assert "source videos are not in this repo" not in page
    assert "still burn" not in page
    man_clips = {c["id"]: c for c in man["clips"]}
    for cid, sha in (
        ("hl00-q1-a0-h6", "03c73d30e21aeb0df3ed07212d840d35c3d457bca30de45b6af3c2578f08e941"),
        ("hl01-q1-a0-h7", "f1e3453e32ccc9b770ec560a70898c174f3e684137874e8e95fa11198bcb1f99"),
        ("hl02-q2-a6-h7", "6938b77fc1042dd7717191b73307191cffca5f1c18b713f0535ce9932926ddc7"),
        ("hl03-q2-a7-h7", "de4fc039e6f317072fce502389d8c50fbae55767745b67a9f7733ce227cf2847"),
        ("hl04-q2-a7-h13", "fbbae2546460fe1d51f51fe5b82f08597b96ee233f4e3139332716ca57551e1a"),
        ("hl05-q2-a7-h14", "d4fea146df825ffc18cdb3939cf5b9b2a4fecf84094f0251f8383c99f55c48f6"),
    ):
        raw_clip = (root / "site/matches/125430/clips" / f"{cid}.mp4").read_bytes()
        assert hashlib.sha256(raw_clip).hexdigest() == sha == man_clips[cid]["sha256"]
        assert sha in page
        assert man_clips[cid]["duration_s"] == 12.0
        assert round(man_clips[cid]["source"]["end_s"] - man_clips[cid]["source"]["start_s"], 3) == 12.0
    assert '"status": "FAIL"' in page
    assert "These clips burn SCOREBUG LOCK" in page

    current = (root / "site/matches/021003/receipt.json").read_bytes()
    earlier = (root / "site/matches/021003/receipt-pre-psn-20261018.json").read_bytes()
    assert hashlib.sha256(current).hexdigest().startswith("182d3a02")
    assert hashlib.sha256(earlier).hexdigest().startswith("2ea2645d")
    attested_body = json.loads(current)
    attested = verify_receipt(attested_body)
    assert attested["status"] == "SELF_ATTESTED" and attested["errors"] == []
    # Offline re-sign after the side correction. The official referee 422s because these
    # checkpoint signatures are not the live countersigned.jsonl. That rejection is correct.
    assert attested_body["referee"]["key_kind"] == "local-self-attested-offline"
    assert any("side corrected after recording" in n for n in attested_body["result"]["notes"])
    assert attested["status"] != "VERIFIED"
    page2 = (root / "site/matches/021003/index.html").read_text()
    assert "HTTP 422" in page2 and "receipt-pre-psn-20261018.json" in page2
    assert "not submitted" not in page2.lower()
    assert '"status": "SELF_ATTESTED"' in page2
    assert '"status": "VERIFIED"' not in page2


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


OFFICIAL_170123_SHA256 = "69480a406d1621409cf03dc98eadcb9020c384a368d9fb0fcab63b61c9f12bb0"


def test_published_match_170123_verifies_and_stays_public():
    """The first referee-verified page. The signed receipt is byte-for-byte; the page does not copy instance ids.

    Highlights were approved later: 3 hash-bound clips plus redacted event stills (no hash binding on stills)."""
    root = Path(__file__).resolve().parents[1]
    folder = root / "site/matches/170123"
    raw = (folder / "receipt.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == OFFICIAL_170123_SHA256
    body = json.loads(raw)
    bare = verify_receipt(body)
    assert bare["status"] == "PARTIAL" and bare["errors"] == [] and bare["partial"] == ["referee_key_not_pinned"]
    pinned = verify_receipt(body, referee_pub=body["referee"]["pubkey"])
    assert pinned["status"] == "VERIFIED" and pinned["errors"] == [] and pinned["partial"] == []
    assert pinned["referee_id"] == "qortime-online-s2"
    deleg = pinned["delegation"]
    assert deleg["serial"] == 2 and deleg["delegated_by"] == "qortime-v1"
    checks = body["review"]["checks"]
    video = checks["video_bytes"]["detail"]
    motion = checks["motion"]["detail"]
    challenges = checks["challenges"]["detail"]
    assert video["method"] == "spot-check" and video["segments_verified"] == 166 and video["segments_total"] == 525
    assert motion["rule"] == 3 and motion["motion_permille"] == 521 and motion["bins"] == 63
    assert challenges["count"] == 3 and challenges["rule"] == 2
    assert body["result"]["away_score"] == 10 and body["result"]["home_score"] == 0
    assert body["result"]["method"] == "end_menu_read" and body["result"]["final_confirmed"] is True
    assert all(checks[name]["state"] == "pass" for name in (
        "bundle_structure", "capture_chain", "video_bytes", "final_reread", "challenges", "motion", "input_integrity"))
    page = (folder / "index.html").read_text(encoding="utf-8")
    assert '"status": "VERIFIED"' in page
    for line in body["review"]["not_proven"]:
        assert line in page
    assert "Footage is not published" not in page and "Footage stays on the player's PC." not in page
    assert "#seal{" in page and ".seal{position:absolute" not in page
    assert "Full footage stays on the player's PC." in page
    assert "Some stills are redacted copies to hide account names." in page
    assert "are redacted copies" in page and "not the raw recording pixels" in page
    assert "Recorded Oct 23, 2026, 5:01 PM" in page and "5:18 PM CT." in page
    assert "qortime-online-s2" in page and "delegation serial 2" in page
    assert "hardware-verified" not in page.lower() and "attestation" not in page.lower()
    assert "id=instance:" not in page and "HID#" not in page and "@device" not in page
    assert "DualSense Edge (USB)" in page
    assert P.privacy_problems(page) == []
    man = json.loads((folder / "clips/manifest.json").read_text(encoding="utf-8"))
    assert man["receipt"]["sha256"] == OFFICIAL_170123_SHA256
    assert len(man["clips"]) == 3
    for clip in man["clips"]:
        assert clip["lock"]["in_receipt"] is True
        assert hashlib.sha256((folder / "clips" / clip["file"]).read_bytes()).hexdigest() == clip["sha256"]
    assert sorted(p.name for p in folder.rglob("*.mp4")) == sorted(c["file"] for c in man["clips"])
    mp4s_jpgs = list(folder.rglob("*.mp4")) + list(folder.rglob("*.jpg"))
    assert all(p.relative_to(folder).parts[0] in ("clips", "events") for p in mp4s_jpgs)
    stills = [p for p in (folder / "events").rglob("*.jpg") if not p.name.endswith("_t.jpg")]
    assert len(stills) == 98
    for ev, fr in (("e02_quarter_change", "frame_08"), ("e02_quarter_change", "frame_09"), ("e07_end_of_game", "frame_03"),
                   ("e07_end_of_game", "frame_04"), ("e07_end_of_game", "frame_05")):
        assert f'"src": "events/{ev}/{fr}.jpg", "redacted": true' in page
    assert (folder / "og.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    summary = json.loads((folder / "match.json").read_text(encoding="utf-8"))
    assert summary["receipt_status"] == "VERIFIED" and summary["final"] == {"away": 10, "home": 0}
    assert summary["receipt_result"] == {"away": 10, "home": 0, "method": "end_menu_read"}
    assert summary["clips"] == 3
    index = (root / "site/matches/index.html").read_text(encoding="utf-8")
    assert index.find('href="170123/"') < index.find('href="021003/"') < index.find('href="125430/"')
    assert 'class="lockg"' in index and "n = 1" in index
    assert "SELF-SIGNED" in index and ">FAIL<" in index
    assert "No page here is referee-verified" not in index


def test_publish_uses_no_glibc_only_strftime_flags():
    # '%-d' / '%-I' style no-pad directives are glibc-only; Windows strftime raises ValueError on them.
    src = Path(P.__file__).read_text(encoding="utf-8")
    assert "%-" not in src, "publish.py must not use '%-' strftime directives (crash on Windows)"


def test_recorded_ct_portable_12h_clock(monkeypatch):
    import time as _time

    real = _time.strftime

    def windows_like(fmt, *a):
        if "%-" in fmt:
            raise ValueError("Invalid format string")
        return real(fmt, *a)

    monkeypatch.setattr(P.time, "strftime", windows_like)
    ct = 5 * 3600 * 1000  # UTC ms of 00:00 CT on 1970-01-01, the fixed offset the page uses
    day = 86_400_000 * 20_000
    rec = {"match": {"started_ms": day + ct + 9 * 3600_000 + 5 * 60_000, "ended_ms": day + ct + 12 * 3600_000 + 30 * 60_000}}
    assert P._recorded_ct(rec) == "Recorded Oct 04, 2024, 9:05 AM–12:30 PM CT."
    assert P._recorded_ct({"match": {"started_ms": day + ct + 7 * 60_000}}) == "Recorded Oct 04, 2024, 12:07 AM CT."
