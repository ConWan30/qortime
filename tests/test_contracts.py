"""QorCap <-> QorTime contract tests: schema/contracts/compat.json is the single source of truth.

Every constant duplicated in code (Python verifier, JS verifier, review bundle loader, allowlists) must match it,
and the golden fixtures (tests/fixtures/contract) must give the recorded verdicts. verify/contract.test.mjs
asserts the browser verifier gives the same verdicts on the same files.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from qor_receipt import SCHEMA
from qor_receipt import checkpoints as C
from qor_receipt import receipt as R
from qor_receipt.review import bundle as B
from qor_receipt.review import policy as P

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "schema" / "contracts"
COMPAT = json.loads((CONTRACTS / "compat.json").read_text(encoding="utf-8"))
FIX = sorted((ROOT / "tests" / "fixtures" / "contract").glob("*.json"))


def test_python_constants_match_compat():
    assert SCHEMA in COMPAT["receipt_schemas"]
    assert C.DEFAULT_MAX_GAP_MS == COMPAT["checkpoint_timing"]["max_gap_ms"]
    assert C.DEFAULT_MAX_SKEW_MS == COMPAT["checkpoint_timing"]["max_skew_ms"]
    assert C.GENESIS == B.GENESIS == COMPAT["hash_chain"]["genesis"]
    assert set(R.RESULT_METHODS) == set(COMPAT["result_methods"])
    assert list(B.META_FILES) == COMPAT["bundle_files"]["meta"]
    assert list(B.REQUIRED_FILES) == COMPAT["bundle_files"]["required"]
    assert {R.VERIFIED, R.PARTIAL, R.SELF_ATTESTED, R.FAIL} == set(COMPAT["verdicts"])
    assert P.REVIEW_TYPE == COMPAT["review_type"] and P.CHALLENGE_TOKEN_TYPE == COMPAT["challenge_token_type"]
    assert P.PAIRING_TYPE == COMPAT["pairing"]["type"]
    assert "score_timeline.json" in COMPAT["bundle_files"]["meta"]
    assert COMPAT["score_timeline"]["schema"] == "qorcap-score-timeline/v0"


def test_receipt_schema_enums_match_compat():
    s = json.loads((ROOT / "schema" / "receipt.v0.json").read_text(encoding="utf-8"))
    res = s["properties"]["result"]["properties"]
    assert set(res["method"]["enum"]) == set(COMPAT["result_methods"])
    assert set(res["final_basis"]["enum"]) == set(COMPAT["final_basis"])
    assert s["properties"]["schema"]["const"] in COMPAT["receipt_schemas"]
    assert s["additionalProperties"] is False
    assert "score_timeline" not in s["properties"]
    signals = s["properties"]["signals"]["properties"]
    assert "score_timeline" not in signals


def test_js_constants_match_compat():
    js = (ROOT / "verify" / "verify.js").read_text(encoding="utf-8")
    m = re.search(r"MAX_GAP_MS = (\d+), MAX_SKEW_MS = (\d+)", js)
    assert m and int(m[1]) == COMPAT["checkpoint_timing"]["max_gap_ms"] and int(m[2]) == COMPAT["checkpoint_timing"]["max_skew_ms"]
    assert f'SCHEMA = "{COMPAT["receipt_schemas"][0]}"' in js
    assert f'REVIEW_TYPE = "{COMPAT["review_type"]}"' in js


def test_allowlists_identical_and_official_key():
    a = json.loads((ROOT / "qor_receipt" / "data" / "trusted_referees.json").read_text(encoding="utf-8"))
    b = json.loads((ROOT / "verify" / "trusted_referees.json").read_text(encoding="utf-8"))
    assert a == b
    inc = (ROOT / "verify" / "trusted_referees.inc.js").read_text(encoding="utf-8")
    off = COMPAT["official_referee"]
    assert off["pubkey"] in inc
    assert a["schema"] in COMPAT["trusted_referees_schemas"]
    row = next(r for r in a["referees"] if r["id"] == off["id"])
    assert row["pubkey"] == off["pubkey"] and row["key_id"] == off["key_id"] and row["status"] == "active"
    from qor_receipt.keys import kid
    assert kid(off["pubkey"]) == off["key_id"]


def test_contract_json_schemas_valid_and_allowlist_conforms():
    js = pytest.importorskip("jsonschema")
    for p in CONTRACTS.glob("*.v*.json"):
        js.Draft202012Validator.check_schema(json.loads(p.read_text(encoding="utf-8")))
    js.validate(json.loads((ROOT / "qor_receipt" / "data" / "trusted_referees.json").read_text(encoding="utf-8")),
                json.loads((CONTRACTS / "trusted-referees.v1.json").read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", FIX, ids=[p.stem for p in FIX])
def test_golden_fixture_verdicts(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    v = R.verify_receipt(doc["receipt"], referee_pub=doc["referee_pub"])
    assert v["status"] == doc["expect"]["status"]
    assert v["errors"] == doc["expect"]["errors"]
    assert v["partial"] == doc["expect"]["partial"]
    js = pytest.importorskip("jsonschema")
    js.validate(doc["receipt"], json.loads((ROOT / "schema" / "receipt.v0.json").read_text(encoding="utf-8")))


def test_golden_fixtures_cover_required_cases_and_are_reproducible():
    spec = importlib.util.spec_from_file_location("mcf", ROOT / "scripts" / "make_contract_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cases = mod.cases()
    assert {p.stem for p in FIX} == set(cases)
    for name, (rec, _pub) in cases.items():
        assert json.loads((ROOT / "tests" / "fixtures" / "contract" / f"{name}.json").read_text())["receipt"] == rec, name
    want = {"good": "SELF_ATTESTED", "tampered_score": "FAIL", "foreign_key_pinned_mismatch": "FAIL",
            "foreign_key_claims_official": "FAIL", "missing_checkpoint": "FAIL", "v032_late_stop_tail": "FAIL",
            "v033_sealed_stop_tail": "SELF_ATTESTED", "slow_genesis": "FAIL", "finalize_offline": "SELF_ATTESTED",
            "v0311_challenges_answered": "SELF_ATTESTED", "v0310_challenge_result_null": "SELF_ATTESTED",
            "v0310_legacy_20s_window": "SELF_ATTESTED", "v0311_challenge_window_too_long": "SELF_ATTESTED"}
    for n, s in want.items():
        assert json.loads((ROOT / "tests" / "fixtures" / "contract" / f"{n}.json").read_text())["expect"]["status"] == s, n


def test_v0311_challenge_fixtures_partials():
    """Answered pause-capable challenges add no challenge partial; the v0.3.10 null result is exactly what QorTime
    listed for match 4; an over-long window is flagged."""
    fx = lambda n: json.loads((ROOT / "tests/fixtures/contract" / f"{n}.json").read_text())["expect"]["partial"]  # noqa: E731
    assert not [x for x in fx("v0311_challenges_answered") if "challenge" in x]
    assert not [x for x in fx("v0310_legacy_20s_window") if "challenge" in x]
    assert [x for x in fx("v0310_challenge_result_null") if "challenge" in x] == ["home:challenge_unanswered:kickoff"]
    assert {x for x in fx("v0311_challenge_window_too_long") if "challenge" in x} == {
        f"home:{ph}:challenge_window_too_long" for ph in ("kickoff", "halftime", "end")}
    rules = COMPAT["challenge_rules"]
    from qor_receipt import challenge as CH
    assert rules["window_ms"] == CH.DEFAULT_DEADLINE_MS and rules["max_window_ms"] == CH.MAX_WINDOW_MS
    assert rules["legacy_window_ms"] == CH.LEGACY_DEADLINE_MS and rules["phases"] == list(CH.PHASES)
    assert rules["pause_latency_ms"] == [CH.MIN_LATENCY_MS, CH.MAX_LATENCY_MS]
    versions = rules["rule_versions"]
    assert versions["1"]["pause_latency_ms"] == [0, 600] and versions["1"]["clock"] == "segment_ranges"
    assert versions["2"]["id"] == 2 and versions["2"]["pause_latency_ms"] == [0, CH.RULE2_MAX_LATENCY_MS]
    assert versions["2"]["consistency_ms"] == CH.RULE2_CONSISTENCY_MS and versions["2"]["clock"] == "fragment_pts"
    from qor_receipt.agent.pause_detect import CAPTURE_DARK_MENU_MAX_LOWER, CAPTURE_DARK_MENU_MAX_LUMA, PauseThresholds
    th = PauseThresholds()
    assert versions["1"]["dark_menu_max_luma"] == th.dark_menu_max_luma
    assert versions["1"]["dark_menu_max_lower"] == th.dark_menu_max_lower
    assert versions["2"]["dark_menu_max_luma"] == CAPTURE_DARK_MENU_MAX_LUMA
    assert versions["2"]["dark_menu_max_lower"] == CAPTURE_DARK_MENU_MAX_LOWER
    assert versions["1"]["dark_menu_bright_frac"] == versions["2"]["dark_menu_bright_frac"]
    assert versions["1"]["dark_menu_bright_frac"] == [th.dark_menu_bright_min, th.dark_menu_bright_max]
    assert set(versions) == {"1", "2"}
    motion = COMPAT["motion_rules"]
    from qor_receipt.review.motion import (
        MOTION_BASIS_V3,
        MOTION_BIN_MS,
        MOTION_HZ,
        MOTION_MAX_LAG,
        MOTION_MIN_BINS,
        MOTION_RULE,
        MOTION_SAMPLE_MAX_SEGMENTS,
        MOTION_SIZE,
    )
    assert motion["current"] == MOTION_RULE == 3
    assert motion["floor_permille"] == R.MOTION_MIN_PERMILLE == 300
    assert motion["min_bins"] == MOTION_MIN_BINS == 20
    assert motion["max_lag_bins"] == MOTION_MAX_LAG == 3
    assert motion["bin_ms"] == MOTION_BIN_MS == 1000
    assert motion["sample_max_segments"] == MOTION_SAMPLE_MAX_SEGMENTS == 24
    assert motion["sample_hz"] == MOTION_HZ == 10
    assert motion["sample_size"] == list(MOTION_SIZE) == [640, 360]
    assert motion["clock"] == "fragment_pts"
    assert motion["rule_versions"]["3"]["basis"] == MOTION_BASIS_V3
    assert "stratified random spread of at most 24 media segments drawn after commit" in MOTION_BASIS_V3
    assert set(motion["rule_versions"]["3"]["excluded_keys"]) == {
        "scorebug_absent", "end_menu", "final_screen", "challenge_pause", "total"}
    js = (ROOT / "verify" / "verify.js").read_text(encoding="utf-8")
    assert "MOTION_MIN = 300" in js
    assert "motion-rule/v3" in js
    assert rules["menu_leave_buttons"] == [b for b in CH.ALPHABET if b in CH.MENU_LEAVE]
    receipt = json.loads((ROOT / "schema" / "receipt.v0.json").read_text(encoding="utf-8"))
    rule_enum = receipt["properties"]["challenges"]["items"]["properties"]["result"]["oneOf"][1]["properties"]["rule"]["enum"]
    assert rule_enum == [1, 2]
    bundle = json.loads((CONTRACTS / "qorcap-bundle.v0.json").read_text(encoding="utf-8"))
    assert bundle["properties"]["challenge_rule"]["enum"] == [1, 2]
    js = (ROOT / "verify" / "verify.js").read_text(encoding="utf-8")
    assert f"MAX_WINDOW_MS = {CH.MAX_WINDOW_MS};" in js


def test_pattern_mismatch_stored_from_does_not_bump_current():
    """Informational: recorder 0.3.34 stores a pattern_mismatch. current stays the checked release."""
    versions = COMPAT["recorder_versions"]
    assert versions["current"] == "0.3.33"
    assert versions["challenge_mismatch_stored_from"] == "0.3.34"
    text = COMPAT["challenge_rules"]["recorder_result"]
    assert "From recorder 0.3.34 a pattern_mismatch is stored as ok:false" in text
    assert "parse as a JSON object" in text
    assert "such as 42 or []" in text
    assert "second header row" in text
    assert "no gap larger than 3000 ms" in text
    assert "latest chunk at or before issued_ms" in text
    assert "are not gap-checked" in text
    assert "chunk at or after deadline_ms" in text
    assert "A last chunk before the deadline does not cover it" in text
    assert "deadline_ms minus that chunk is <= 3000" in text
    assert "wall_anchor_ms + t_us//1000" in text
    assert "deadline_ms+2500" in text
    assert "Any status other than connected" in text
    assert "including stalled, unplugged, and missing" in text
    assert "t_us is not an integer, or whose state is not a string" in text
    assert "The official referee applies the same gate" in text
    assert "Only a gate-passing mismatch is challenge_failed" in text
    assert "OPTIONS alone" in text
    assert "Every other non-ok reason stays null" in text
    assert "withheld controller_log" in text


def test_stop_tail_rule_is_the_verifier_rule():
    """The real-match failure signature (cp[n-2] gap+skew, cp[n-1] skew) is exactly a late-stamped stop tail."""
    e = json.loads((ROOT / "tests/fixtures/contract/v032_late_stop_tail.json").read_text())["expect"]["errors"]
    assert e == ["home:cp[6]:gap_too_long", "home:cp[6]:clock_skew", "home:cp[7]:clock_skew"]


def test_js_verifier_same_verdicts_on_golden_fixtures():
    """Runs verify/contract.test.mjs (browser verifier) over the same fixtures. Node is on GitHub's ubuntu runners."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    p = subprocess.run([node, str(ROOT / "verify" / "contract.test.mjs")], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr


def test_referee_image_pins_sidecar_with_every_scorebug_profile():
    """The referee image's pinned QorCap sidecar must carry every registry scorebug profile (Monday Night included)."""
    img = COMPAT["referee_image"]
    df = (ROOT / "services" / "referee" / "Dockerfile").read_text()
    m = re.search(r"ARG QORCAP_SHA=([0-9a-f]{40})", df)
    assert m and m[1] == img["qorcap_sidecar_sha"]
    assert set(img["scorebug_profiles"]) == set(COMPAT["scorebug_profiles"])
    assert "madden27_monday_night_v1.npz" in df
    assert "qorcap_snp.py" in df and "madden27_sunday_night_v1.npz" in df
    assert "qorcap_tnp.py" in df and "madden27_thursday_night_v1.npz" in df


def test_sidecar_profiles_lists_primetime_profiles(tmp_path):
    from qor_receipt.review import media as M
    (tmp_path / "packs").mkdir()
    assert M.sidecar_profiles(str(tmp_path)) == ["madden27_standard"]
    for f in ["qorcap_mnp.py", "packs/madden27_monday_night_v1.npz", "qorcap_snp.py", "packs/madden27_sunday_night_v1.npz",
              "qorcap_tnp.py", "packs/madden27_thursday_night_v1.npz"]:
        (tmp_path / f).write_bytes(b"")
    assert M.sidecar_profiles(str(tmp_path)) == ["madden27_standard", "madden27_monday_night", "madden27_sunday_night",
                                                 "madden27_thursday_night"]


def test_hardening_contracts_match_code():
    from qor_receipt import delegation as D
    from qor_receipt import translog as T
    from qor_receipt.referee_service import submit_auth as A

    assert COMPAT["referee_delegation"]["type"] == D.DELEGATION_TYPE
    assert COMPAT["referee_delegation"]["revocations_type"] == D.REVOCATIONS_TYPE
    assert COMPAT["referee_delegation"]["max_lifetime_days"] * 86_400_000 == D.MAX_LIFETIME_MS
    assert COMPAT["transparency_log"]["entry_type"] == T.ENTRY_TYPE and COMPAT["transparency_log"]["head_type"] == T.HEAD_TYPE
    sa = COMPAT["submit_auth"]
    assert sa["request_type"] == A.SUBMIT_TYPE and sa["pow_prefix"] == A.POW_PREFIX and sa["header"].lower() == A.HEADER
    assert sa["min_client"] in A.UPGRADE_MSG


# ---- 2026-10-17: result winner "tie"/"unknown" and the real quarter on unconfirmed reads (Py/JS parity)
U, A = "last_scorebug_read_unconfirmed", "agreed_scorebug_final"
RESULT_CASES = [
    ({"home_score": 21, "away_score": 17, "quarter": 4, "winner": "home", "method": A}, []),
    ({"home_score": 17, "away_score": 21, "quarter": 5, "winner": "away", "method": "end_menu_read"}, []),
    ({"home_score": 24, "away_score": 24, "quarter": 5, "winner": "tie", "method": "end_menu_read"}, []),
    ({"home_score": 24, "away_score": 24, "quarter": 4, "winner": "away", "method": A}, []),          # legacy tie
    ({"home_score": 24, "away_score": 24, "quarter": 4, "winner": "home", "method": A}, ["winner_inconsistent"]),
    ({"home_score": 21, "away_score": 17, "quarter": 4, "winner": "tie", "method": A}, ["winner_inconsistent"]),
    ({"home_score": 7, "away_score": 7, "quarter": 1, "winner": "tie", "method": U}, []),              # real Q1 kept
    ({"home_score": 7, "away_score": 7, "quarter": 1, "winner": "tie", "method": A}, ["quarter_not_final"]),
    ({"home_score": None, "away_score": None, "quarter": None, "winner": "unknown", "method": U}, []),  # nothing read
    ({"home_score": None, "away_score": None, "quarter": None, "winner": "away", "method": U}, ["winner_inconsistent"]),
    ({"home_score": None, "away_score": None, "quarter": 4, "winner": "unknown", "method": A},
     ["score_not_nonneg_int"]),
    ({"home_score": 3, "away_score": 0, "quarter": 0, "winner": "home", "method": U}, ["quarter_invalid"]),
    ({"home_score": 3, "away_score": None, "quarter": 2, "winner": "unknown", "method": U}, ["score_not_nonneg_int"]),
]


@pytest.mark.parametrize("res,errs", RESULT_CASES)
def test_result_rules(res, errs):
    assert R.result_errors(dict(res, final_t_ms=1)) == errs


def test_result_rules_js_parity():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    cases = json.dumps([dict(r, final_t_ms=1) for r, _ in RESULT_CASES])
    js = ("import { resultErrors } from './verify/verify.js';"
          f"console.log(JSON.stringify({cases}.map(resultErrors)));")
    p = subprocess.run([node, "--input-type=module", "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == [e for _, e in RESULT_CASES]


def test_schema_allows_tie_unknown_and_null_quarter():
    res = json.loads((ROOT / "schema" / "receipt.v0.json").read_text())["properties"]["result"]["properties"]
    assert res["winner"]["enum"] == ["home", "away", "tie", "unknown"]
    assert res["quarter"] == {"type": ["integer", "null"], "minimum": 1}
