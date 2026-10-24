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
ALPHABET. The pattern is `["OPTIONS", b0..b3]`.

The referee accepts the response when, in the agent's HID log (DualSense Edge over USB):
1. an `OPTIONS` press in `[issued_ms, deadline_ms]` is followed **immediately** by the four buttons, in order
   (key-downs only, other presses not allowed in between). The answering press is the first OPTIONS press that is
   followed by the pattern (v0.3.11: an OPTIONS press during a live play does nothing in online Madden and does not
   void the challenge); and
2. the pause menu was detected on the capture feed at `pause_seen_ms`, with 0 ≤ `pause_seen_ms − pause_press_ms` ≤ 600 ms.
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
SHA-256 values of the window's frames. A per-frame PTS binding, when a bundle has one, is the optional `frames.jsonl`
sidecar in §7, outside this receipt. `input_hash` is the SHA-256 of the JCS of the window's HID events.
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

## 7. Per-bundle frame binding

`qor-receipt/v0` gains no fields for this. `body_of` still copies only `seq`, `t_ms`, `frames_hash`, `input_hash`,
and `prev`, so a `qorcap` object on a checkpoint line stays off the signed receipt. Old bundles that omit the
sidecars stay valid. `frames.jsonl` and `readers.json` are optional bundle members (`bundle_files.meta`), not
required files.

`frames.jsonl` (`qorcap-frame-bind/v0`) is one RFC 8785 JCS object per line, LF, no CR. The newline is not hashed.
`head_0` is 64 `0` characters. `head_n = SHA-256(JCS({prev, sha256}))`, where `sha256` is the hex SHA-256 of line
n's exact bytes. A bound checkpoint carries `qorcap.frame_bind = {schema, n, head}` for a prefix of that chain.
The last checkpoint's `n` is the line count. Every checkpoint carries it, or none of them do.

The first line is a header. `time_base` is `{num, den}`, both integers ≥ 1. Each frame line carries integer `pts`
in that base. Presentation time in seconds is `pts * num / den`. The base is the encoder's: a 90 kHz MPEG-TS PTS
is `{num: 1, den: 90000}`. The line has no `fps`, `fps_num`, or `fps_den`, and frame time is not a frame index
divided by a rate. `mono_ms` is the capture monotonic millisecond of the packet that starts the frame, which binds
that PTS to the capture clock. `au_sha256` is the SHA-256 of the encoded video access unit.

A segment that is not fully bound is one `align_gap` line: `seg`, `from_frame`, `samples`, `bound`, and `reason`.
Coverage is `bound` of `samples`. The sample span is `[from_frame, from_frame + samples)`. Samples in that span
without a frame row are the gap. `reason` is `pts_regression`, `composition_offset`, `sample_pes_mismatch`,
`clock_lost`, or `unreadable`.

`readers.json` (`qorcap-readers/v0`) is one closed object (`additionalProperties` false): hashes of the scorebug
and vision reader code and packs, plus a `runtimes` array of the builds that produced the reads (ffmpeg and cv2
when those produced them). Checkpoint seq 0 carries `qorcap.readers` as `{schema, sha256}` of the file bytes
(JCS plus one trailing LF) or `{schema, state: "unbound"}` when no file was sealed. Later checkpoints omit it.

`qor_receipt.review.frame_bind.frame_bind_status` returns exactly one status, as a plain `str`:

frame-bind status set: verified, missing, stripped, broken

Code imports `FRAME_BIND_STATUSES` and `FRAME_BIND_VERIFIED` from that module. `is_frame_bind_verified(value)` is
true only when `type(value) is str` and `value == FRAME_BIND_VERIFIED`. The type check runs before equality, so a
`str` subclass (including one whose `__eq__` always returns true) and an Enum equal to `FRAME_BIND_VERIFIED` are
not verified. A value that is not that plain string, or that is not in `FRAME_BIND_STATUSES`, is not a claim.

The primary API is `verify_frame_bind(bundle)`. `bundle` is a directory, the canonical bundle bytes, or a zip of
the sidecar files. The function reads those bytes and recomputes the status, the bundle digest, and coverage.
It returns a frozen `FrameBindResult` with `status`, `bundle_digest`, and `coverage` (one segment per `seg`,
including each `align_gap`). `FrameBindResult` is not a trust token. Anyone can construct one, including a
`status` of `FRAME_BIND_VERIFIED` and a fabricated digest. Downstream gates MUST call `verify_frame_bind`
themselves on the bundle. A gate that is handed a result MUST recompute the bundle digest from bytes and
re-verify if it does not match that result (`frame_bind_result_ok(bundle, result)` does this). Gates must never
trust a caller-supplied result. A copy of a real result compares equal; that still requires the bundle bytes.

* **verified** — every checkpoint has `frame_bind`, seq 0 has `readers`, the files those commitments require are
  present, and the hash chain, PTS order, gap coverage, and schemas check. Successive frame rows have strictly
  increasing `pts`. `readers` `state: "unbound"` with no `readers.json` can still be verified: that is a frame
  binding with no reader-identity claim.
* **missing** — no `frame_bind`, no `readers` on seq 0, and neither sidecar file. missing does not prove binding was never enabled.
  A complete strip and reseal is indistinguishable from never-enabled at this verifier. Only a
  referee signature over a checkpoint hash that still contained the commitment can show that removal, and this
  status does not check that signature.
* **stripped** — a visible leftover: `frame_bind` on some checkpoints but not others; `frame_bind` without
  `readers` on seq 0, or `readers` on seq 0 without `frame_bind` on every checkpoint; `frames.jsonl` or
  `readers.json` present without its commitment; a `frame_bind` or readers-hash commitment whose file is absent;
  or `readers.json` beside `state: "unbound"`.
* **broken** — the commitments and the files they require are present, and the hash chain, PTS monotonicity, gap
  coverage, or a schema check fails. A checkpoints file that cannot be parsed, and any other shape, is broken.

missing and stripped are no claim. broken is no claim. The status is advisory. It is not an input to `VERIFIED`,
`PARTIAL`, `FAIL`, or `SELF_ATTESTED`.

A video-versus-log mismatch claim for a bundle is allowed only when that bundle's own frame-bind status is
verified and the press-to-frame encode latency for that bundle was measured. A recorder self-report does not
count as that measurement. missing, stripped, broken, any other value, or any event inside an align_gap is no
claim. There is no fixed-fps skew formula. 719 ms is an observed maximum across 3 clips in one session. It is
not a limit.

`verify/verify.js` does not walk these files. `verify/frame-bind-status.js` exports the same status set and
`isFrameBindVerified`, which is true only for a string primitive equal to the verified constant. It does not
decide a receipt and does not recompute a bundle. Porting `verify_frame_bind` into the browser verifier is a
follow-up.
