"""The public AI explainer's system prompt uses fair-proof wording, never banned claim words."""
from qor_receipt.ai_ruling.explainer import SYSTEM_PROMPT

BANNED = ("anti-cheat", "anticheat", "verified skill", "proof-of-human", "proof of human", "attested", "proven")


def _identity():
    # Everything before the rules: how the prompt describes QorTime.
    return SYSTEM_PROMPT.split("Rules you must follow:", 1)[0].lower()


def test_identity_has_no_banned_words():
    text = _identity()
    assert "fair match records" in text and "does not detect cheats" in text
    for word in BANNED:
        assert word not in text, word


def test_prompt_never_claims_anti_cheat_or_verified_skill():
    low = SYSTEM_PROMPT.lower()
    for word in ("anti-cheat", "anticheat", "verified skill", "proof-of-human", "proof of human"):
        assert word not in low, word


def test_attested_and_proven_only_appear_as_rules_against_them():
    rules = SYSTEM_PROMPT.split("Rules you must follow:", 1)[1]
    for line in rules.splitlines():
        low = line.lower()
        if "attested" in low:
            assert low.lstrip().startswith("3. never use the words"), line
        if "proven" in low:
            assert "not proven" in low, line
