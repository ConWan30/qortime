"""Site-wide Trio alignment, /watch/ pillar, unified nav, LF, and wording checks."""
import re
from pathlib import Path

from qor_receipt.ai_ruling.guardrail import BANNED

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
TRIO_PAGES = [
    SITE / "index.html",
    SITE / "cap/index.html",
    SITE / "watch/index.html",
    SITE / "how-it-works/index.html",
    SITE / "pricing/index.html",
]
NEW_ASSETS = [
    SITE / "trio-observatory.css",
    SITE / "trio-stage.js",
    SITE / "gl.js",
    SITE / "qor-ui/nav.js",
    SITE / "qor-ui/footer.js",
]
FORBIDDEN = (
    "anti-cheat",
    "anticheat",
    "anti cheat",
    "attested",
    "proof-of-human",
    "proof of human",
    "who-held",
    "verified skill",
    "qorgrip",
    "qorbox",
    "qoresence",
    "qoract",
)


def test_trio_pages_and_watch_pillar_exist():
    for path in TRIO_PAGES + NEW_ASSETS:
        assert path.is_file(), f"missing {path}"
    sitemap = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://qortime.com/watch/</loc>" in sitemap
    assert "<loc>https://qortime.com/record-pack/</loc>" in sitemap


def test_trio_nav_and_links_are_clean():
    hub_html = (SITE / "index.html").read_text(encoding="utf-8")
    assert 'href="record-pack/"' in hub_html
    assert ".qormoment.tar" in hub_html and "/aux/v1/hash-stamp" in hub_html
    for path in TRIO_PAGES:
        html = path.read_text(encoding="utf-8")
        assert "#watch-coming-soon" not in html, path
        assert "cursor.com/agents" not in html, path
        navs = re.findall(r"<nav.*?</nav>", html, re.S)
        assert navs, path
        assert not any("hackathon" in n.lower() for n in navs), path
        for key in ('data-qor-link="receipts"', 'data-qor-link="capture"', 'data-qor-link="watch"'):
            assert key in html, f"{path}: missing {key}"


def test_strict_lf_wording_and_semantic_gold_bounds():
    for path in TRIO_PAGES + NEW_ASSETS:
        raw = path.read_bytes()
        assert b"\r" not in raw, f"CR found in {path}"
        text = raw.decode("utf-8")
        low = text.lower()
        assert [why for rx, why in BANNED if rx.search(text)] == [], path
        for word in FORBIDDEN:
            assert word not in low, f"{path}: forbidden {word}"
        if path not in (SITE / "index.html", SITE / "trio-observatory.css"):
            assert "e4c56d" not in low and "ref-verified-gold" not in low, path
