"""Shared adversarial receipts: Python and verify/adversarial.test.mjs read the same manifest.

Each case is a raw-text edit of the published match 170123 receipt. The verifier
must see that text, not a re-serialized object (JSON.parse/json.loads erase the
float/int and duplicate-key distinctions these cases exist to catch).
"""
from __future__ import annotations

import json
from pathlib import Path

from qor_receipt.receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "adversarial" / "manifest.json"
PUBLISHED = ROOT / "tests" / "fixtures" / "adversarial" / "published_verdicts.json"


def _apply(base: str, case: dict) -> str:
    text = base
    if "replace" in case:
        old, new = case["replace"]
        found = text.count(old)
        assert found == 1, f"{case['id']}: needle count {found}"
        text = text.replace(old, new, 1)
    return text


def _load_base() -> tuple[dict, str, str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # Keep the published bytes, including CRLF. Universal-newline reads would
    # hide a parser that only accepts LF.
    base = (ROOT / manifest["base"]).read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    pin = json.loads(base)["referee"]["pubkey"]
    return manifest, base, pin


def test_adversarial_cases_match_manifest():
    manifest, base, pin = _load_base()
    assert manifest["cases"][0]["id"] == "baseline"
    for case in manifest["cases"]:
        text = _apply(base, case)
        verdict = verify_receipt(text, referee_pub=pin)
        assert verdict["status"] == case["expect"], case["id"]
        if case.get("error"):
            assert case["error"] in verdict["errors"], (case["id"], verdict["errors"][:6])


def test_published_match_verdicts_unchanged():
    expected = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    for match_id, modes in expected.items():
        raw = (ROOT / "site" / "matches" / match_id / "receipt.json").read_bytes().decode("utf-8")
        doc = json.loads(raw)
        pin = doc["referee"]["pubkey"]
        for mode, want in modes.items():
            verdict = verify_receipt(raw, referee_pub=pin if mode == "pinned" else None)
            assert verdict["status"] == want["status"], match_id
            assert verdict["errors"] == want["errors"], (match_id, mode)
            assert verdict["partial"] == want["partial"], (match_id, mode)
            assert verdict["trust_tier"] == want["trust_tier"], match_id
            assert verdict.get("referee_trust") == want["referee_trust"], match_id
            assert verdict.get("referee_id") == want["referee_id"], match_id
