# QorTime receipt v0 (`qor-receipt/v0`): match receipt specification

Status: draft v0. Schema: [`schema/receipt.v0.json`](../schema/receipt.v0.json). Reference implementations:
`qor_receipt/receipt.py` (Python) and `verify/verify.js` (browser). Both must give identical verdicts.

## 1. Canonical form and signatures

* Signed bytes are **RFC 8785 JCS** of the receipt with the top-level `signatures` member removed.
* Restricted value profile: strings, integers with |n| ≤ 2^53−1, booleans, null, arrays, objects. **No floats.**
  Ratios are integers per mille. Producers must refuse floats.
* **Ed25519** (RFC 8032, strict / non-ZIP215). `key_id` = first 16 hex chars of SHA-256(raw 32-byte pubkey).
* Every entry in `players[]` and the `referee` signs the same bytes:
  `signatures.players[role] = {key_id, sig}` and `signatures.referee = {key_id, sig}`.
* `tests/vectors/jcs_vector.json` is the cross-language parity vector.

## 2. Objects

* `match`: `match_id` (issued by the referee), `game` (`madden27`), `platform` (`ps5`),
  `scorebug_profile` (`madden27_standard`, the vendored Qoresence profile), `mode` (`dual` or `single`),
  `started_ms` and `ended_ms` (referee clock, Unix ms).
* `result`: `home_score`, `away_score`, `quarter` (≥4; 5 = OT), `final_t_ms`, `winner`, `method` = `agreed_scorebug_final`, optional `final_basis` (§5).
  `winner` follows the score: `home`, `away`, or `tie` on equal scores (a legacy `away` on a tie is still accepted).
  With `method` = `last_scorebug_read_unconfirmed` (no final was detected) `quarter` is the quarter the last read showed
  (≥1) or `null`, and when nothing was read at all `home_score`/`away_score` are `null` with `winner` = `unknown`;
  such receipts are never a final, and nothing is invented (2026-10-17: previously a 0-0 "away" win in Q4).
* `players[]`: `role` (`home` or `away`), `player_id` (e.g. `psn:<id>`), `pubkey`, `key_kind`
  (`software`, `software-file` or `tpm`).
* `referee`: `pubkey`, `key_id`, `key_kind`.
* `challenges[]`: one per (role, phase), phases `kickoff`, `halftime` and `end`. Fields: `nonce` (16 random bytes, hex),
  `pattern`, `issued_ms`, `deadline_ms`, `result` (referee verdict: `ok`, `reason`, `pause_press_ms`, `done_ms`,
  `latency_ms`, `hid_sha256`).
* `checkpoints[role][]`: `{seq, t_ms, frames_hash, input_hash, prev, hash, referee_ts_ms, referee_sig}`.
* `agreement`: `timelines_agree` (bool; null in single mode), `mismatches[]`.
* `signals.motion_permille[role]`: input/video motion correlation ×1000, or null.
* `trust_tier`: `dual-attested` or `single-attested`. Verifiers **recompute** it and FAIL on a wrong claim.

## 3. Nonce challenge

`pattern_for(nonce)`: `d = SHA-256(nonce bytes)`. For i = 0..3, `b = ALPHABET[d[i] mod 8]` where
ALPHABET = `CROSS CIRCLE SQUARE TRIANGLE UP DOWN LEFT RIGHT`. If `b` equals the previous button, use the next one in
ALPHABET. The pattern is `["OPTIONS", b0..b3]`. Verifiers recompute this and reject a pattern that does not match.
The alphabet and the derivation do not change. A referee issuing a new nonce redraws until CIRCLE and CROSS are only
in the last slot (or absent): in the Madden pause menu those buttons back out or select, so an earlier one lets the
rest of the pattern into live play. A nonce that was already issued still verifies.

The referee accepts the response when, in the agent's HID log (DualSense Edge over USB):
1. an `OPTIONS` press in `[issued_ms, deadline_ms]` is followed **immediately** by the four buttons, in order
   (key-downs only, other presses not allowed in between). The answering press is the first OPTIONS press that is
   followed by the pattern (v0.3.11: an OPTIONS press during a live play does nothing in online Madden and does not
   void the challenge); and
2. the pause menu was detected on the capture feed at `pause_seen_ms`, with 0 ≤ `pause_seen_ms − pause_press_ms` ≤ 600 ms
   when `result.rule` is absent or 1. Rules 2 and 3 use the same 0..2500 ms ceiling and require the successful latencies
   within ±300 ms of their median (see [docs/REFEREE_SERVICE.md](../docs/REFEREE_SERVICE.md)). Rule 3 is the only one
   whose dark-menu luma caps are the capture-path pair (26/16); rules 1 and 2 keep 16/8. An unknown rule id does not
   widen the 600 ms ceiling.
   A negative latency means the video isn't from this console or isn't live. Too large a latency suggests a relay.

The deadline is issue + 90 s (v0.3.11; it was 20 s), and submissions are refused more than 5 s after the deadline.
Verifiers flag `challenge_window_too_long` (partial) when `deadline_ms − issued_ms` is not in (0, 120 s].

**When a challenge opens (v0.3.11).** Online Madden 27 does not let a player pause during a kickoff or a kickoff
return; OPTIONS opens the pause menu only after a play has been called (pre-snap). So the recorder asks the referee
for a phase's nonce only at a pause-capable moment: `kickoff` = after the opening kickoff (game clock seen running in
Q1+) and the first play-call screen that follows; `halftime` = the same after the second-half kickoff (Q3+); `end` =
the same in Q4+. If no play-call screen is detected, the window opens 45 s after the clock started running in that
quarter (`clock_proxy`). Each opening is logged in `challenge_gates.jsonl`. The pattern is only shown after issue, so
the security argument (no pre-knowledge, HID + video pause 0–600 ms after the press) is unchanged.

**Recorder result (QorCap ≥ 0.3.11).** At finalize QorCap re-runs each challenge on its own bundle
(`qor_receipt.review.local`) and writes an ok `result` (`reverified_by: "recorder"`) into the receipt; a miss stays
`null` with the reason in `challenge_results.jsonl`. The official referee re-runs everything itself.

## 4. Checkpoint chain

The agent sends `cp = {seq, t_ms, frames_hash, input_hash, prev}` every ~4 s, with `hash = SHA-256(JCS(cp))`. It is
chained from `prev = 64×"0"`, and the request is signed by the player key. `frames_hash` is the SHA-256 over the
SHA-256 values of the window's frames. `input_hash` is the SHA-256 of the JCS of the window's HID events.
The referee checks seq/prev/hash, then signs
`JCS({type:"checkpoint", match_id, role, hash, referee_ts_ms})`.

The verifier requires: contiguous seq, correct links, valid hashes and referee signatures, referee time never going
backwards, gaps ≤ 10 s (including start→first and last→`ended_ms`), and |`referee_ts_ms` − `t_ms`| ≤ 3 s.

## 5. Final detection and dual mode

Madden draws the AWAY team on the left of the scorebug strip and HOME on the right; agents map left → `away`,
right → `home`. Agents report **agreed** scorebug reads (vendored Qoresence `AgreementWindow`: the same reading on ≥3 frames over
≥400 ms, with stricter re-reads for implausible jumps). A game is final when the last reads show quarter ≥4 and
clock 0:00, with the same non-tied score on ≥3 agreed reads spanning ≥3 s (≥5 if any of those 0:00 reads were
Madden's red final-seconds clock). Two fallbacks apply only after a stable Q4 score whose last readable clock was
≤0:05: the scorebug strip continuously absent (no pause menu) for ≥30 s starting ≤15 s after the last read, or a
final-score screen on ≥3 frames over ≥3 s. Agents send these as `{"obs": "scorebug_absent"|"final_screen", "t_ms",
"until_ms", "n"}` spans inside the timeline; any later score read cancels them. `result.final_basis` (optional,
`clock_zero` | `scorebug_gone` | `final_screen`) records which rule fired; in dual mode it is the weaker of the two
agents' bases (clock_zero > final_screen > scorebug_gone). Ambiguous evidence means no final. In dual mode the referee finalizes only if
both finals are equal and both timelines have the same ordered (home, away, quarter) state changes within 5 s of
each other. Otherwise there's no receipt (HTTP 409, dispute).

## 6. Verdict (following QorTroller PORT-CERT)

* **FAIL** if any of these hold: wrong schema; bad or missing signature; pinned referee key mismatch; any checkpoint
  error; a challenge pattern not derived from its nonce; a failed challenge; a dual receipt whose timelines don't
  agree; `winner` inconsistent with the score; a wrong `trust_tier` claim; a malformed receipt.
* **PARTIAL** when nothing is broken but evidence is missing: referee key not pinned by the verifier, a missing or
  unanswered challenge phase, or a motion signal that is missing or under 300‰.
* **VERIFIED** only otherwise. Missing evidence never yields VERIFIED.
* The trust tier is reported separately. Platforms should set their own policy (e.g. stakes over $X need
  VERIFIED + dual-attested).
