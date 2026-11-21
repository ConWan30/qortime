# Verifier auto-pin

> **Hackathon work.** Built during the Meta AI Developer Hackathon, Oct 26 – Nov 4 2026. Everything else in `verify/`
> existed before the hackathon.

## What it does
A shared receipt link with no `?referee=` used to show **PARTIAL** (`referee_key_not_pinned`) even when the receipt
was signed by a QorTime referee on the built-in trusted list. Now, when the pin box is empty, the page pins the key
automatically **only** if it qualifies, and shows: "Referee key pinned automatically from QorTime's built-in trusted list."

A key qualifies only if it is:
1. an **active** row in `trusted_referees.json`, or
2. an online key (`key_kind` `qortime-official-review`) with a **valid, unrevoked delegation** from an allowlisted
   root, checked with the same `checkDelegation()` that `verifyReceipt()` uses.

## What it does not do
- `verify.js` / `verifyReceipt()` are **unchanged**. Auto-pin only chooses the `refereePub` argument that a person
  could already type by hand. It never yields a result a manual pin of the same key would not.
- Unknown, revoked, expired, local self-attested, referee-equals-player and malformed keys get **no pin**, so the
  verifier call is exactly what it was before.
- It never removes errors: a tampered receipt from a trusted referee still FAILs.
- A key typed by the user (or filled from `?referee=`) always wins; auto-pin never runs then.
- Gold is still only shown for VERIFIED (`goldAllowed`), unchanged.
- The Python library (`qor_receipt/receipt.py`) is unchanged; bare library calls still report `referee_key_not_pinned`.

## Files
- `verify/auto-pin.js`: `autoPinFor(receipt, { allowlist, revocations, nowMs })` → `{ pin, source, reason }`.
- `verify/index.html`: calls it in `check()` only when the pin box is empty.
- `verify/auto-pin.test.mjs`: 30 checks, also run at the end of `node verify/test.mjs` (so the existing CI job runs them) (170123 auto-pinned = manual pin = VERIFIED, tampered still FAIL, unknown and
  forged keys unchanged, revoked/expired/out-of-scope delegations never pinned). Run: `node verify/auto-pin.test.mjs`.
