"""site/record-pack/: the match record pack page states its limits plainly, uses gold only on the referee-VERIFIED example pack, and has no banned claims."""
import json
import re
from pathlib import Path

from qor_receipt.ai_ruling.guardrail import BANNED

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "site/record-pack/index.html"


def _text():
    return PAGE.read_text(encoding="utf-8")


def test_page_uses_unified_shell_and_is_linked():
    html = _text()
    assert 'data-qor-site="receipts"' in html
    assert 'data-watch-href="#watch-coming-soon"' in html and 'id="watch-coming-soon"' in html
    assert 'classList.add("js")' in html
    assert '<link rel="stylesheet" href="../qor-site.css">' in html
    assert 'href="record-pack/"' in (ROOT / "site/index.html").read_text(encoding="utf-8")
    assert "<loc>https://qortime.com/record-pack/</loc>" in (ROOT / "site/sitemap.xml").read_text(encoding="utf-8")


def test_limits_are_stated_plainly():
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _text()))
    for phrase in (
        "it shows the referee's status and every check behind it",
        "See inside a console or a separate gaming PC.",
        "Show who was holding the controller.",
        "We don't promise that any publisher will review a pack, or that any ban will be reversed.",
        "A per-moment input-to-screen timeline is not in packs yet.",
        "It does not show the footage wasn't faked or edited before it was hashed.",
        "QorTime does not detect cheats.",
        "Our note, not Activision's: a pack is not part of that process, and as far as we know Activision has not reviewed or accepted QorTime packs.",
    ):
        assert phrase in text, phrase
    assert 'href="https://support.activision.com/ban-appeal"' in _text()


def test_no_banned_claims():
    html = _text()
    assert [why for rx, why in BANNED if rx.search(html)] == []
    low = html.lower()
    for word in ("anti-cheat", "anticheat", "proof-of-human", "proof of human", "attested", "verified skill",
                 "proven", " proves", "guarantee", "endorsed by", "skill-proof", "not edited after", "disconnect",
                 "will never", "controller input, game clock and screen agree"):
        assert word not in low, word


def test_timeline_and_setup_cards_are_coming_later():
    html = _text()
    cards = re.findall(r"<article.*?</article>", html, re.S)
    timeline = next(c for c in cards if "<h3>Input-to-screen timeline</h3>" in c)
    setup = next(c for c in cards if "<h3>Setup snapshot</h3>" in c)
    for card in (timeline, setup):
        assert "coming later" in card
        assert "· today" not in card.lower()


def test_sample_figures_match_the_public_verified_match():
    html = _text()
    match = json.loads((ROOT / "site/matches/220105/match.json").read_text(encoding="utf-8"))
    assert match["receipt_status"] == "VERIFIED"
    assert match["final"] == {"away": 7, "home": 21}
    assert match["match_id"] in html and "1d424dc2836f1a8c" in html
    assert 'href="../matches/220105/"' in html
    sample = html.split('id="sample"', 1)[1].split('id="limits"', 1)[0]
    assert "end-menu fallback" in sample and "alignment_not_above_chance" in sample
    assert "270 of 878 segments" in sample and "segments 1–847" in sample
    manifest = json.loads((ROOT / "site/matches/220105/clips/manifest.json").read_text(encoding="utf-8"))
    for clip in manifest["clips"]:
        s = int(clip["source"]["start_s"] + clip["lock_at_s"])  # truncated, like the match page
        after = clip["lock"]["score_after"]
        assert f'{s // 60}:{s % 60:02d}</span><b>{after["away"]} – {after["home"]}</b>' in html, clip["id"]


def test_gold_only_on_the_verified_sample():
    html = _text()
    sample = html.split('id="sample"', 1)[1].split('id="limits"', 1)[0]
    rest = html.replace(sample, "")
    assert "rp-verified" in sample and "rp-gold" in sample
    assert "rp-gold" not in rest and "rp-verified" not in rest
    assert "e4c56d" not in html.lower()


def test_appeal_only_in_the_activision_citation():
    html = _text()
    bans = html.split('id="bans"', 1)[1].split('id="where"', 1)[0]
    citation = [p for p in re.findall(r"<p>.*?</p>", bans, re.S) if "Activision" in p]
    rest = html
    for p in citation:
        rest = rest.replace(p, "")
    assert "appeal" not in rest.lower()
    assert any("support.activision.com/ban-appeal" in p for p in citation)
