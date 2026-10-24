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
    from qor_receipt.canonical import MAX_DEPTH, MAX_RECEIPT_BYTES

    kind = case.get("generate")
    if kind == "depth":
        return "[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1)
    if kind == "size":
        return "[" + " " * MAX_RECEIPT_BYTES + "]"
    text = base
    if "replace" in case:
        old, new = case["replace"]
        found = text.count(old)
        assert found == 1, f"{case['id']}: needle count {found}"
        text = text.replace(old, new, 1)
    if case.get("prefix"):
        text = case["prefix"] + text
    if case.get("suffix"):
        text = text + case["suffix"]
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


def test_limits_match_the_spec_and_do_not_raise(tmp_path):
    import subprocess
    import sys

    from qor_receipt.canonical import MAX_DEPTH, MAX_RECEIPT_BYTES, parse_canonical, structural_errors
    from qor_receipt.h2h import MAX_RECEIPT_BYTES as H2H_CAP

    assert H2H_CAP == MAX_RECEIPT_BYTES

    spec = (ROOT / "spec" / "match-receipt-v0.md").read_text(encoding="utf-8")
    js = (ROOT / "verify" / "canonical.js").read_text(encoding="utf-8")
    assert f"at most {MAX_RECEIPT_BYTES} UTF-8 bytes" in spec
    assert f"at most {MAX_DEPTH} nested" in spec
    assert f"MAX_RECEIPT_BYTES = {MAX_RECEIPT_BYTES}" in js
    assert f"MAX_DEPTH = {MAX_DEPTH}" in js
    exact = "[]" + (" " * (MAX_RECEIPT_BYTES - 2))
    assert len(exact.encode("utf-8")) == MAX_RECEIPT_BYTES
    value, errors = parse_canonical(exact)
    assert errors == [] and value == []
    value, errors = parse_canonical("[" * MAX_DEPTH + "]" * MAX_DEPTH)
    assert errors == [] and isinstance(value, list)
    value, errors = parse_canonical("[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1))
    assert value is None and errors == ["noncanonical_json"]
    # A same-named field outside the schema's hex paths is not a hex failure.
    assert "noncanonical_hex" not in structural_errors({"review": {"bundle": {"hash": "NOT-HEX"}}})
    assert "noncanonical_hex" in structural_errors({"players": [{"role": "home", "pubkey": "ABCD"}]})
    # CLI: a non-ASCII digit is FAIL (exit 2), never PARTIAL (exit 1) and never a traceback.
    target = tmp_path / "digit.json"
    target.write_text('{"quarter": \u00b2}\n', encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "qor_receipt.cli", "verify", str(target)],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2, proc.stderr
    assert "noncanonical_json" in proc.stdout


def test_proto_motion_cannot_upgrade(tmp_path):
    fixture = json.loads((ROOT / "tests" / "fixtures" / "adversarial" / "proto_motion.json").read_text(encoding="utf-8"))
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps(fixture["allowlist"]), encoding="utf-8")
    clean = verify_receipt(fixture["clean"], referee_pub=fixture["pub"], allowlist_path=str(allow))
    injected = verify_receipt(fixture["injected"], referee_pub=fixture["pub"], allowlist_path=str(allow))
    assert clean["status"] == "PARTIAL"
    assert clean["errors"] == []
    assert clean["partial"] == ["home:motion_signal_missing"]
    assert injected["status"] == "FAIL"
    assert injected["errors"] == ["noncanonical_json"]
