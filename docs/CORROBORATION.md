# Two-sided corroboration

Status: additive. It does not change `VERIFIED`, `PARTIAL`, `SELF_ATTESTED`, or `FAIL`.

QorCap records one console. When both players record the same match and both submit, the referee can say
whether those two signed recordings agree. That sentence is a separate status. It is not a review check.
A match that fails its own checks stays failed. A single-sided receipt that would be `VERIFIED` stays
`VERIFIED`, with corroboration `single_sided`. A disagreement is stored and shown. It is not folded into
the verdict.

## Pairing

`POST /v1/pairings` returns a referee-signed token:

```json
{"type": "qortime-pairing/v1", "pairing_id": "<32 hex>", "nonce": "<32 hex>", "issued_ms": 0, "key_id": "<16 hex>", "sig": "<128 hex>"}
```

The referee chooses `pairing_id` and `nonce`. The signature covers every field except `sig` (RFC 8785 JCS).
Each player puts that token, unchanged, in the bundle as `pairing.json`.

The referee links two submissions only when `accept_pairing_token` verifies the signature under the
referee key and reads the id and nonce from that signed body. An edited id, a missing signature, or a
token signed by another key is `pairing_unverified`: the submission stays `single_sided`. The role
(`home` or `away`) comes from the bundle, not from the token. Two submissions that claim the same role
are `role_conflict` and `disagreed`.

## Pairing contract (stable)

QorCap calls this. Do not change the token shape or the endpoint.

`POST /v1/pairings` takes no meaningful body. A client-supplied id or nonce is ignored. The response
is a referee signature over RFC 8785 JCS of every field except `sig`:

```json
{"type": "qortime-pairing/v1", "pairing_id": "<32 hex>", "nonce": "<32 hex>", "issued_ms": 0, "key_id": "<16 hex>", "sig": "<128 hex>"}
```

`pairing_id` and `nonce` are 32 lowercase hex characters from the referee. Each player stores that
JSON object, unchanged, as `pairing.json` in the bundle. The referee links the two submissions only
when that signature verifies under the referee key. The role comes from the bundle, not the token.

## Signed score timeline

When `receipt.json` contains `signals.score_timeline.events`, that player-signed field is the
timeline. The root of `receipt.v0.json` has `additionalProperties: false` and does not list
`score_timeline`, so the events are not a root field. QorCap PR #59 does not write them into
`receipt.json`. `signals` does not set `additionalProperties: false`, so a receipt that does
include the field is still schema-legal, and that field wins over the sidecar.

When the field is absent, the referee reads optional `score_timeline.json`
(`qorcap-score-timeline/v0`), the sidecar from QorCap PR #59. Each event is
`{t_ms, home, away, quarter}`.

The signed bytes are RFC 8785 JCS of:

```json
{"type": "qorcap-score-timeline/v0", "signals": {"score_timeline": {"events": []}}, "chain": [], "chain_head": "<64 hex>", "checkpoint_head": "<64 hex>"}
```

The `signature` object is not part of those bytes. Each chain link is
`sha256(JCS({t_ms, home, away, quarter, prev}))`. The first `prev` is 64 `0` characters.
`chain_head` is the last link. `checkpoint_head` is `receipt.checkpoints[<role>][-1].hash`.

Two signatures over those bytes are accepted:

1. Ed25519. `signature.pub` and `signature.sig` are hex. `pub` is the receipt player public key.
   This is what QorCap `write_score_timeline(bundle, device, rec)` writes: `device` is the key
   that also signs the receipt. It is not the optional TPM key.
2. The public key in `device_attest.v0.json`, when that file is in the bundle and its own
   signature over the same checkpoint head verifies (`software` or `tpm_claimed`).
   `signature.pub` is `public_key_spki_b64` or the hex of that SPKI DER. `signature.sig` is hex
   or standard base64. ECDSA P-256 is DER over SHA-256. RSA-2048 is RSASSA-PKCS1-v1_5 over
   SHA-256, the same bytes `src/device_attest.rs` verifies. PSS is not accepted. A key pasted
   into the sidecar without a valid device signature does not count. The status of that key is
   still at most `tpm_claimed`.

A missing file, a bad chain, a checkpoint head that is not the receipt head, or a signature that
does not verify is not a timeline. Both players present and no timeline is `disagreed` with
`unsigned_score_timeline`. One player is still `single_sided`. The sidecar never changes
`VERIFIED` or `FAIL`.

If `timeline.jsonl` is also in the bundle, its score reads (observation spans ignored) must collapse to
the same state changes at the same timestamps. A mismatch is `timeline_file_mismatch`. The file does not
replace the signed events.

## What "agree" means

`SCORE_EVENT_TOLERANCE_MS` is **5000**. This is the same absolute window as
`qor_receipt.final_detect.timelines_agree`.

- The final scores (`result.home_score`, `result.away_score`) must be equal, and the last signed event's
  score must be that final.
- Consecutive identical `(home, away, quarter)` states count as one change. The two sequences of changes
  must be the same, in the same order.
- For each change, `|t_home - t_away|` must be **≤ 5000** ms. A difference of 5000 ms still agrees. 5001 ms
  is `state_<score>_time_differs`.
- Timestamps are the signed `t_ms` values. A constant clock offset is **not** subtracted. A recording
  shifted by more than five seconds disagrees even when the scores match.

## Challenge independence

Corroboration also requires the responses to be independent:

- player signing keys differ (`same_player_key` otherwise);
- final checkpoint heads (`checkpoints[role][-1].hash`) differ (`same_checkpoint_head`);
- challenge nonces are disjoint (`shared_challenge_nonce`);
- response fingerprints `(nonce, pattern, hid_sha256, pause_press_ms, done_ms)` are disjoint
  (`copied_challenge_response`).

A replay that resubmits the other player's evidence under a new key fails these checks and is
`disagreed`. It is not `corroborated`.

## Status

| status | when |
|---|---|
| `single_sided` | no verified partner for the other role |
| `corroborated` | both roles present, no reasons |
| `disagreed` | both roles present and any reason above, or a `role_conflict` |

`affects_verdict` is always false. The block lives on `review.corroboration` and on the submission
object the referee returns. The verify page prints it under the verdict. It does not use the gold
Verified Match treatment.

The snapshot inside a countersigned receipt is what the referee knew when it signed. If the partner
is still waiting to add their player signature, the referee re-signs that receipt so both copies
carry the same status. A receipt that is already `complete` is not rewritten. The live submission
object still shows the updated status.

`pairing.json` is an optional bundle meta file (`schema/contracts/compat.json`). QorCap vendors that
contract; this filename is the QorTime side of the pairing.
