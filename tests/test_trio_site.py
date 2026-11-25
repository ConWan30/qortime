"""Site-wide Trio alignment, /watch/ pillar, /verify/ shell, unified nav, LF, and wording checks."""
import re
import shutil
import subprocess
import tempfile
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


def test_verifier_page_uses_trio_shell_and_valid_js():
    v_html = ROOT / "verify/index.html"
    v_css = ROOT / "verify/viewer.css"
    for path in (v_html, v_css):
        assert path.is_file(), f"missing {path}"
        assert b"\r" not in path.read_bytes(), f"CR found in {path}"

    html = v_html.read_text(encoding="utf-8")
    assert "../trio-observatory.css" in html and 'href="viewer.css"' in html
    assert "#watch-coming-soon" not in html and "cursor.com/agents" not in html
    navs = re.findall(r"<nav.*?</nav>", html, re.S)
    assert navs and not any("hackathon" in n.lower() for n in navs)
    for key in ('data-qor-link="receipts"', 'data-qor-link="capture"', 'data-qor-link="watch"'):
        assert key in html, f"verify/index.html: missing {key}"

    scripts = re.findall(r'<script type="module">(.*?)</script>', html, re.S)
    assert len(scripts) >= 2
    node = shutil.which("node")
    if node:
        for body in scripts:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".mjs", mode="w", encoding="utf-8", delete=False) as tmp:
                    tmp.write(body)
                    tmp_path = Path(tmp.name)
                proc = subprocess.run([node, "--check", str(tmp_path)], capture_output=True, text=True)
                assert proc.returncode == 0, f"verify/index.html module syntax error: {proc.stderr}"
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

