"""Site-wide wording gate: no 'fair-proof' or 'skill-proof' in public site pages, scripts or styles."""
import re
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "site"
EXCLUDED = {"verify", "contracts"}
BANNED = re.compile(r"fair-proof|skill-proof", re.IGNORECASE)


def _files():
    for pattern in ("*.html", "*.js", "*.css"):
        for path in SITE.rglob(pattern):
            if not EXCLUDED.intersection(path.relative_to(SITE).parts):
                yield path


def test_site_has_files_to_check():
    assert any(p.name == "index.html" for p in _files())


def test_no_fair_proof_or_skill_proof_on_the_site():
    hits = []
    for path in _files():
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if BANNED.search(line):
                hits.append(f"{path.relative_to(SITE)}:{n}")
    assert hits == []
