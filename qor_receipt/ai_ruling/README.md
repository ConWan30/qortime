# `qor_receipt/ai_ruling/`: advisory receipt explainer (Muse Spark)

> **Built during the Meta AI Developer Hackathon, Oct 26 – Nov 4 2026.** Everything in this folder is new hackathon
> work. See [`HACKATHON.md`](../../HACKATHON.md) for pre-existing vs hackathon work.

Takes a QorTime receipt plus the referee/verifier results and writes a short plain-language explanation with
Meta **Muse Spark** (`muse-spark-1.3`, Standard mode, via the Meta Model API).

## Advisory only
- The verdict comes from `qor_receipt.receipt.verify_receipt` (Python) or `verify/verify.js` (browser). This module
  is handed that result, copies the status, and **never decides, computes, edits or overrides it**.
- It never edits the receipt. `schema/receipt.v0.json` is frozen; output is a sidecar, `ai_ruling.json`
  (`qortime.ai_ruling.v0`, `"affects_verdict": false`, bound by `receipt_sha256`).
- The verdict path (`receipt.py`, `cli.py`, `verify/*.js`) does not import this package (a test checks it).
- Anti-cheat and verified skill only. It is **not coaching**: the prompt forbids gameplay advice and the guardrail
  drops any text that gives it.

## Fail closed
No key, HTTP error, network error, timeout, malformed or empty answer, `finish_reason=length`, or a guardrail hit:
`ok=False`, **no explanation is shown and no sidecar is written**. The verdict is identical in every case.

## Guardrail (deterministic, `guardrail.py`)
The text is dropped if it:
- does not state the verifier's status exactly (VERIFIED / PARTIAL / SELF-SIGNED / FAIL) or names any other status;
- says a non-VERIFIED match "is verified";
- uses "attested" or "hardware-verified" (device keys top out at `tpm_claimed`, which is an unverified claim);
- gives coaching or tips, says "cheat-free" or "guarantee", or claims who was holding the controller;
- is longer than 1,500 characters.

## What the model sees
Only allowlisted facts (`facts_for`): status, game/platform/mode, the claimed result, verifier errors/partials,
referee trust and delegation serial, review check states and details, the `not_proven` list, the controller-input
advisory numbers, and the device-key status capped at `tpm_claimed`. **No** pubkeys, player ids, match ids, device
instance paths or wall-clock times. The word "attested" is rewritten to "signed" before it reaches the model.

## Use
```bash
export META_API_KEY=...        # or MODEL_API_KEY. Never hard-coded, never logged, never written to the sidecar.
python -m qor_receipt.ai_ruling site/matches/170123/receipt.json --pub <referee pubkey> --out /tmp/ai_ruling.json
```
```python
from qor_receipt.receipt import verify_receipt
from qor_receipt.ai_ruling import explain
v = verify_receipt(receipt, referee_pub=pub)    # the verdict
res = explain(receipt, v)                       # advisory; res.ok False => show nothing extra
```
Real Muse Spark output for match 170123 (pinned → VERIFIED, unpinned → PARTIAL):
`docs/hackathon/ai_ruling_170123_*.example.json`. About 700 prompt / 350–600 completion tokens per call
(~$0.003 at Standard prices).

## Tests
`tests/test_ai_ruling.py` (API mocked, no network): verdict and receipt byte-identical whether the AI succeeds,
errors, returns nothing or trips the guardrail; missing key makes no call; key never in the sidecar, repr, request
body or error text; guardrail cases; no PII in facts; sidecar shape; CLI writes nothing on failure.

## Signing (separate AI advisory key)
`signing.py` signs `ai_ruling.json` with a **separate advisory key** (`key_kind` `qortime-ai-advisory`, allowlist
`advisory_keys.json`, mirrored for the browser in `verify/ai_advisory_keys.inc.js`). It is never mistakable for a
referee verdict signature:
- different field (`advisory_signature` + `advisory_key`, never `signature` / `signatures` / `referee`);
- different key kind and allowlist; signing refuses a key on the trusted referee list, and checking rejects an
  advisory key that is a referee key or the receipt's own referee key;
- bound to the receipt by `receipt_jcs_sha256` = SHA-256(JCS(receipt)), the same value in Python and the browser.

`python -m qor_receipt.ai_ruling RECEIPT --pub HEX --sign-key PATH` writes a signed sidecar. The private key stays on
the operator box (never in git).

## Verifier panel
`verify/ai-ruling-view.js` shows an **"AI explanation · advisory"** panel (info tone, never gold) only when the
advisory signature, the advisory key allowlist, the receipt binding and the echoed status (must equal what
`verify.js` just computed) all check out. Otherwise nothing is shown. Sources: a dropped `ai_ruling.json`, or the
`ai_ruling.json` next to a same-origin `?receipt=` URL. Live example: match 170123.

## SAM 3.1 scorebug check (`sam_scorebug.py`, lower-only, NOT wired in)
Asks Meta SAM 3.1 for a "scoreboard" on frames that should be live play and checks the box sits where the game's
scorebug profile says (madden27_standard: full-width bottom strip). Output: `sam_scorebug.json`
(`qortime.sam_scorebug.v0`, `affects_verdict: "lower_only"`, `max_cap: "PARTIAL"`).
- `cap` is only ever `"PARTIAL"` or `null`; `apply_cap` can only turn VERIFIED into PARTIAL. It never raises a
  status and never FAILs a match. API errors or a missing key mark frames `unchecked`, which never count against
  anyone. A cap needs >= 2 bad frames and a strict majority of checked frames.
- Highlight clips carry QorCap's burned-in "SCOREBUG LOCK" label top-left; `mask_burn_in` blanks it first,
  otherwise SAM boxes that label instead of the game's scorebug.
- **Measured (Oct 26, public 170123 clip frames, 1280x720, label masked): SAM found the real scorebug on 4 of 6
  legit frames** (both `hl02` frames missed it with "scoreboard"; "score banner" and "scorebug" were no better).
  That miss rate is too high for a status signal, so this is **not** connected to `verify_receipt`, `verify.js` or
  any page.
