# Meta AI Developer Hackathon: pre-existing work vs hackathon work

QorTime is entered in the **Startup** track of the Meta AI Developer Hackathon (Oct 26 – Nov 4 2026). The rules let
startups build on an existing codebase if the new work is clearly identified. This file is that list.

QorTime is **anti-cheat and verified gaming skill**: a capture client (QorCap) on the player's PC makes signed match
receipts, and the QorTime referee/verifier checks them. It is not a coaching product.

## Pre-existing (before Mon Oct 26 2026, 12:00 PM CT)
Everything in this repo at commit `0e87ec3` and earlier, including:
- the receipt format (`schema/receipt.v0.json`, frozen), signing and verification (`qor_receipt/receipt.py`,
  `verify/verify.js`), referee delegation and revocation, the transparency log;
- the referee service (`services/`, `qor_receipt/referee_service/`) and the official post-hoc review;
- the AI referee (`qor_receipt/referee_ai/`, TypeSafe Jev + local scorebug reader) and its `MetaMuseVision` stub;
- the website and match pages (`site/`), including match 170123 and its highlight clips;
- the QorCap capture client (separate repo `ConWan30/qorcap`).

## Built during the hackathon
Each item lives in its own new path and is labeled in the code.

| What | Where | Meta model |
|---|---|---|
| Advisory receipt explainer: receipt + referee results → plain-language explanation, fail-closed, deterministic guardrail, never changes the verdict | `qor_receipt/ai_ruling/`, `tests/test_ai_ruling.py`, `docs/hackathon/` | Muse Spark 1.3 |
| Verifier auto-pin: shared links pin the referee key automatically only when it is on the built-in trusted list (separate PR) | `verify/auto-pin.js`, `verify/AUTO_PIN.md`, `verify/auto-pin.test.mjs`, a small hook in `verify/index.html` | none |
| This file | `HACKATHON.md` | |

Measured during the hackathon (box benchmark, not in this repo): Muse Spark 1.3 on the same 134 Madden scorebug crops
used to evaluate the pre-existing reader got 111/111 scores exact and 0 confident-but-wrong answers (the pre-existing
Glimpse reader: 110/111, 6 confident-but-wrong, including 5 on stats-overlay traps).

## Rules we keep
- The AI layer is advisory. Verdicts (VERIFIED / PARTIAL / SELF-SIGNED / FAIL) come only from the deterministic
  verifier. The receipt format is frozen; new data goes in sidecars.
- No secrets, raw footage, raw controller logs or gamertags in this public repo.
