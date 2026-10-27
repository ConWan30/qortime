# Meta AI Developer Hackathon: pre-existing work vs hackathon work

QorTime is entered in the **Startup** track of the Meta AI Developer Hackathon (Oct 26 – Nov 4 2026). The rules let
startups build on an existing codebase if the new work is clearly identified. This file is that list.

QorTime is **anti-cheat and verified gaming skill**: a capture client (QorCap) on the player's PC makes signed match
receipts, and the QorTime referee/verifier checks them. It is not a coaching product.

The capture client is a separate public repo: [`ConWan30/qorcap`](https://github.com/ConWan30/qorcap)
([`HACKATHON.md`](https://github.com/ConWan30/qorcap/blob/main/HACKATHON.md)). Meta models are used in **this**
repo (and the submission demo pipeline), not inside QorCap.

## Pre-existing (before Mon Oct 26 2026, 12:00 PM CT)
Everything in this repo at commit [`0e87ec3`](https://github.com/ConWan30/qortime/commit/0e87ec3) and earlier, including:
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
| Automatic advisory files on the official referee: after each new submission's verdict is final, a background job writes a signed `ai_ruling.json` and (when the referee holds the right video) a signed `score_crosscheck.json`, published at `/v1/advisory/{receipt hash}/…`; the verifier falls back to that copy. Never delays or changes the verdict; failure/timeout/cap = no file ("Muse unchecked"). Per-match cap $0.05, weekly cap $5, kill switch `QORTIME_AI_ADVISORY_ENABLED` | `qor_receipt/ai_ruling/auto.py`, `tests/test_auto_advisory.py`, `verify/advisory-fetch.js`, `verify/advisory-fetch.test.mjs`, small hooks in `qor_receipt/review/engine.py`, `qor_receipt/referee_service/app.py`, `verify/index.html`, `verify/test.mjs`; section in `docs/REFEREE_SERVICE.md` | Muse Spark 1.3 |
| Demo-video captions (keyword-fixed ASR) for the hackathon submission demo | submission demo pipeline (not a product code path in this repo) | Muse Voice Transcribe 1.0 |
| This file | `HACKATHON.md` | |

Measured during the hackathon (box benchmark, not in this repo): Muse Spark 1.3 on the same 134 Madden scorebug crops
used to evaluate the pre-existing reader got 111/111 scores exact and 0 confident-but-wrong answers (the pre-existing
Glimpse reader: 110/111, 6 confident-but-wrong, including 5 on stats-overlay traps).

## Pull requests merged on/after kickoff (Mon Oct 26 2026, 12:00 PM CT)

Boundary commit: [`0e87ec3`](https://github.com/ConWan30/qortime/commit/0e87ec3). Every PR below merged after that boundary.

### Meta-model work built during the hackathon
| PR | One line |
|---|---|
| [#90](https://github.com/ConWan30/qortime/pull/90) | Advisory Muse Spark receipt explainer + this `HACKATHON.md` |
| [#92](https://github.com/ConWan30/qortime/pull/92) | Signed advisory AI explanation + verifier panel |
| [#93](https://github.com/ConWan30/qortime/pull/93) | Muse Spark default score cross-check (lower-only, signed, verifier panel) |
| [#94](https://github.com/ConWan30/qortime/pull/94) | Show Your Hands: verified input overlay + press-to-pixel check (lower-only) |
| [#95](https://github.com/ConWan30/qortime/pull/95) | Show Your Hands: video actions, Muse play-start filter, signed `hands.json` |
| [#96](https://github.com/ConWan30/qortime/pull/96) | Auto Muse explanation + score cross-check on the referee (advisory only) |
| [#97](https://github.com/ConWan30/qortime/pull/97) | Muse errors carry the short API error code |
| [#98](https://github.com/ConWan30/qortime/pull/98) | Operator-only `/v1/advisory/diag` |
| [#99](https://github.com/ConWan30/qortime/pull/99) | Advisory retry: failed Muse calls retryable, max 3 attempts with backoff |
| [#101](https://github.com/ConWan30/qortime/pull/101) | Meta API calls prefer IPv6, IPv4 fallback |

### Other in-window fixes (non-Meta)
| PR | One line |
|---|---|
| [#89](https://github.com/ConWan30/qortime/pull/89) | Verifier auto-pin for trusted referees |
| [#91](https://github.com/ConWan30/qortime/pull/91) | Handoff: Meta hackathon track is Startup |
| [#100](https://github.com/ConWan30/qortime/pull/100) | Site: mobile UI/UX audit fixes |
| [#102](https://github.com/ConWan30/qortime/pull/102) | Site: one QorTime logo lockup on every page |
| [#103](https://github.com/ConWan30/qortime/pull/103) | Site: consistent page nav, tap targets, readable dim text |
| [#104](https://github.com/ConWan30/qortime/pull/104) | Ban CIRCLE/CROSS from challenge body; pin end-menu reader fix (qorcap #116) |
| [#105](https://github.com/ConWan30/qortime/pull/105) | Record QorCap 0.3.38 as the current recorder version |
| [#106](https://github.com/ConWan30/qortime/pull/106) | Motion rule v4: deterministic content-head sampling + median of 3×48 |
| [#107](https://github.com/ConWan30/qortime/pull/107) | This list; removed public match page 021003 (on-screen names in its signed receipt); README Meta-models + demo-video sections |

## Footage policy
Short published **highlight clips** from verified (and labelled FAIL) matches are public under `site/matches/*/clips/`.
**Full match footage** is never committed to this repo. Raw controller logs and private bundles stay offline.

## Rules we keep
- The AI layer is advisory. Verdicts (VERIFIED / PARTIAL / SELF-SIGNED / FAIL) come from the deterministic
  verifier. The one AI-fed signal, the signed Muse score cross-check, can only lower VERIFIED to PARTIAL on the
  verifier page; it can never raise a status or fail a match, and `verify.js` / `verify_receipt` are unchanged. The receipt format is frozen; new data goes in sidecars.
- No secrets, raw full-match footage, raw controller logs or gamertags in the public site tree. Match pages that
  carried on-screen player names were removed from `site/matches/` rather than editing signed receipts.
