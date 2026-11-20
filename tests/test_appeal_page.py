"""site/appeal/: the appeal-pack page states its limits plainly, uses gold only on the referee-VERIFIED sample, and has no banned claims."""
import json
import re
from pathlib import Path

from qor_receipt.ai_ruling.guardrail import BANNED

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "site/appeal/index.html"


def _text():
    return PAGE.read_text(encoding="utf-8")


def test_page_uses_unified_shell_and_is_linked():
    html = _text()
    assert 'data-qor-site="receipts"' in html
    assert 'data-watch-href="#watch-coming-soon"' in html and 'id="watch-coming-soon"' in html
    assert 'classList.add("js")' in html
    assert '<link rel="stylesheet" href="../qor-site.css">' in html
    assert 'href="appeal/"' in (ROOT / "site/index.html").read_text(encoding="utf-8")
    assert "<loc>https://qortime.com/appeal/</loc>" in (ROOT / "site/sitemap.xml").read_text(encoding="utf-8")


def test_limits_are_stated_plainly():
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _text()))
    for phrase in (
        "controller input, game clock and screen agree",
        "See inside a console or a separate gaming PC.",
        "Show who was holding the controller.",
        "We don't promise that any publisher will review a pack, or that any ban will be reversed.",
        "coming later",
    ):
        assert phrase in text, phrase
    assert 'href="https://support.activision.com/ban-appeal"' in _text()


def test_no_banned_claims():
    html = _text()
    assert [why for rx, why in BANNED if rx.search(html)] == []
    low = html.lower()
    for word in ("anti-cheat", "anticheat", "proof-of-human", "proof of human", "attested", "verified skill",
                 "proven", " proves", "guarantee", "endorsed by"):
        assert word not in low, word


def test_sample_figures_match_the_public_verified_match():
    html = _text()
    match = json.loads((ROOT / "site/matches/220105/match.json").read_text(encoding="utf-8"))
    assert match["receipt_status"] == "VERIFIED"
    assert match["final"] == {"away": 7, "home": 21}
    assert match["match_id"] in html and "1d424dc2836f1a8c" in html
    assert 'href="../matches/220105/"' in html
    manifest = json.loads((ROOT / "site/matches/220105/clips/manifest.json").read_text(encoding="utf-8"))
    for clip in manifest["clips"]:
        s = round(clip["source"]["start_s"] + clip["lock_at_s"])
        after = clip["lock"]["score_after"]
        assert f'{s // 60}:{s % 60:02d}</span><b>{after["away"]} – {after["home"]}</b>' in html, clip["id"]


def test_gold_only_on_the_verified_sample():
    html = _text()
    sample = html.split('id="sample"', 1)[1].split('id="limits"', 1)[0]
    rest = html.replace(sample, "")
    assert "appeal-verified" in sample and "appeal-gold" in sample
    assert "appeal-gold" not in rest and "appeal-verified" not in rest
    assert "e4c56d" not in html.lower()
