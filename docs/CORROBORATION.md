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

## Signed score timeline

The timeline that is cross-checked is `signals.score_timeline.events` inside the player-signed
`receipt.json`. Each event is `{t_ms, home, away, quarter}`. The player signature is the commitment.
Events that fail that signature are not a timeline (`unsigned_score_timeline`).

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
