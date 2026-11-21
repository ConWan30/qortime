"""Site-wide wording gate for public site pages, scripts and styles (verify/ and contracts/ excluded).

'fair-proof' and 'skill-proof' are kept on purpose (Con, Nov 20, 2026). Honest negations such as
'Not proven' and 'does not prove' are allowed; only the claims below are banned.
"""
import re
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "site"
EXCLUDED = {"verify", "contracts"}
BANNED = {
    "anti-cheat": re.compile(r"\banti[- ]?cheat", re.IGNORECASE),
    "verified skill": re.compile(r"\bverified\s+skill", re.IGNORECASE),
    "proof-of-human": re.compile(r"\bproof[- ]of[- ]human", re.IGNORECASE),
    "QorTime proves": re.compile(r"\bqortime\s+proves\b", re.IGNORECASE),
}


def _files():
    for pattern in ("*.html", "*.js", "*.css"):
        for path in SITE.rglob(pattern):
            if not EXCLUDED.intersection(path.relative_to(SITE).parts):
                yield path


def _hits(text):
    return [name for name, rx in BANNED.items() if rx.search(text)]


def test_site_has_files_to_check():
    names = {p.relative_to(SITE).as_posix() for p in _files()}
    assert "index.html" in names and "cap/index.html" in names and "record-pack/index.html" in names


def test_no_banned_claims_on_the_site():
    hits = []
    for path in _files():
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for name in _hits(line):
                hits.append(f"{path.relative_to(SITE)}:{n} {name}")
    assert hits == []


def test_gate_catches_claims_but_keeps_honest_negations():
    assert _hits("QorTime is anti-cheat") == ["anti-cheat"]
    assert _hits("an anticheat tool") == ["anti-cheat"]
    assert _hits("anti cheat") == ["anti-cheat"]
    assert _hits("Toward verified skill") == ["verified skill"]
    assert _hits("a proof-of-human badge") == ["proof-of-human"]
    assert _hits("qortime proves · anyone verifies") == ["QorTime proves"]
    for ok in ("Not proven: who was holding the controller", "It does not prove who held the controller",
               "A fair-proof of the recording and a skill-proof of what the card saw"):
        assert _hits(ok) == [], ok
