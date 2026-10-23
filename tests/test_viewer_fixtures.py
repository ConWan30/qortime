"""The verifier-page fixtures are synthetic and trusted only by their own allowlist. Their verdicts come from verify_receipt."""
import json
from pathlib import Path

import pytest

from qor_receipt.receipt import verify_receipt

FIX = Path(__file__).resolve().parent / "vectors" / "viewer"
CASES = sorted(FIX.glob("*.json"))


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_viewer_fixture_verdict(path, tmp_path):
    fx = json.loads(path.read_text())
    assert fx["note"].startswith("SYNTHETIC")
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps(fx["allowlist"]))
    out = verify_receipt(fx["receipt"], referee_pub=fx["referee_pub"], allowlist_path=str(allow))
    assert out["status"] == fx["expect"]
    # without the fixture allowlist none of them can be VERIFIED
    assert verify_receipt(fx["receipt"], referee_pub=fx["referee_pub"])["status"] != "VERIFIED"


def test_viewer_fixtures_cover_each_status():
    assert {json.loads(p.read_text())["expect"] for p in CASES} >= {"VERIFIED", "PARTIAL", "FAIL"}
