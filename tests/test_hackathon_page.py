"""site/hackathon/: hackathon entry page is indexable (demo video live), honest and free of banned claims."""
import re
from pathlib import Path

from qor_receipt.ai_ruling.guardrail import BANNED

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "site/hackathon/index.html"


def _text():
    return PAGE.read_text(encoding="utf-8")


def test_page_exists_and_is_indexable():
    html = _text()
    assert 'content="noindex"' not in html
    assert '<link rel="stylesheet" href="../style.css">' in html


def test_page_is_in_sitemap_but_not_in_site_nav():
    assert "<loc>https://qortime.com/hackathon/</loc>" in (ROOT / "site/sitemap.xml").read_text(encoding="utf-8")
    for page in (ROOT / "site").rglob("*.html"):
        if page == PAGE:
            continue
        nav = re.findall(r"<nav.*?</nav>", page.read_text(encoding="utf-8"), re.S)
        assert not any("hackathon" in n for n in nav), page


def test_page_links_and_demo_slot():
    html = _text()
    for href in ('href="../matches/170123/"', 'href="../matches/220105/"', 'href="../verify/"',
                 'href="https://github.com/ConWan30/qortime-ai"'):
        assert href in html, href
    block = html.split("<!-- DEMO_VIDEO -->", 1)[1].split("<!-- /DEMO_VIDEO -->", 1)[0]
    assert "Demo video coming soon" not in block
    assert '<iframe src="https://www.youtube-nocookie.com/embed/vlGJ1LXxumE"' in block
    assert 'href="https://youtu.be/vlGJ1LXxumE"' in block
    assert "AI explains, it never decides" in html and "PARTIAL" in html


def test_page_has_no_banned_claims():
    html = _text()
    assert [why for rx, why in BANNED if rx.search(html)] == []
    low = html.lower()
    for word in ("qorcap box", "call of duty", "activision", "endorsed by", "partnered with meta",
                 "certified by", "mailto:", "customers"):
        assert word not in low, word
    # gold styling only on the referee-VERIFIED match cards
    golds = re.findall(r'class="[^"]*\bgold\b[^"]*"', html)
    assert len(golds) == 2


def test_hackathon_is_not_claimed_as_endorsement():
    html = _text()
    assert "not endorsed, certified or partnered with by Meta or Cerebral Valley" in html
