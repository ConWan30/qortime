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
| Verifier auto-pin: shared links pin the referee key automatically only when it is on the built-in trusted list  | `verify/auto-pin.js`, `verify/AUTO_PIN.md`, `verify/auto-pin.test.mjs`, a small hook in `verify/index.html` | none |
| Signed advisory explanation (separate AI advisory key, never a referee key) + verifier panel shown only when signature, receipt binding and echoed status check out | `qor_receipt/ai_ruling/signing.py`, `qor_receipt/ai_ruling/advisory_keys.json`, `verify/ai-ruling-view.js`, `verify/ai_advisory_keys.inc.js`, `verify/ai-ruling.test.mjs`, `site/matches/170123/ai_ruling.json`, small hooks in `verify/index.html` and `verify/viewer.css` | Muse Spark 1.3 |
| Score cross-check: Muse Spark is the default second reader of every locked score; agreement keeps the status, a disagreement can only lower VERIFIED to PARTIAL; Muse unavailable = "Muse unchecked", no penalty. Signed sidecar + verifier panel | `qor_receipt/ai_ruling/score_crosscheck.py`, `tests/test_score_crosscheck.py`, `verify/score-crosscheck-view.js`, `verify/lower-only.js`, `verify/score-crosscheck.test.mjs`, `site/matches/170123/score_crosscheck.json`, small hooks in `verify/index.html`, `verify/viewer-ui.js`, `verify/viewer.css` | Muse Spark 1.3 |
| Scorebug locator (info only, not used for any status) | `qor_receipt/ai_ruling/sam_scorebug.py`, `tests/test_sam_scorebug.py` | SAM 3.1 |
| Show Your Hands: verified controller-input overlay track for highlight clips + press-to-pixel check (no_press / too_fast / machine_regular). Lower-only, capped at PARTIAL. Local video action detection, a Muse Spark play-start filter (can only remove candidates), a signed `hands.json` and a verifier panel that is hidden without a valid signed file | `qor_receipt/hands/`, `tests/test_hands.py`, `tests/test_hands_video.py`, `verify/hands-view.js`, `verify/hands.test.mjs`, `site/matches/170123/hands.json`, small hooks in `verify/index.html`, `verify/viewer.css` | Muse Spark 1.3 (play-start filter) |
| This file | `HACKATHON.md` | |

Measured during the hackathon (box benchmark, not in this repo): Muse Spark 1.3 on the same 134 Madden scorebug crops
used to evaluate the pre-existing reader got 111/111 scores exact and 0 confident-but-wrong answers (the pre-existing
Glimpse reader: 110/111, 6 confident-but-wrong, including 5 on stats-overlay traps).

## Rules we keep
- The AI layer is advisory. Verdicts (VERIFIED / PARTIAL / SELF-SIGNED / FAIL) come from the deterministic
  verifier. The one AI-fed signal, the signed Muse score cross-check, can only lower VERIFIED to PARTIAL on the
  verifier page; it can never raise a status or fail a match, and `verify.js` / `verify_receipt` are unchanged. The receipt format is frozen; new data goes in sidecars.
- No secrets, raw footage, raw controller logs or gamertags in this public repo.
