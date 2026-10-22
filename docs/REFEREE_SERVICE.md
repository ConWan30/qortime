# QorTime official referee service — "verified uploads"

QorCap records on the player's own laptop, so everything it signs locally is **SELF_ATTESTED**: the laptop's
"referee" key sits next to the player. Verified uploads let that match earn **VERIFIED** from the official
QorTime referee (`qortime-v1`, pubkey `a5aa0c91…feea`) — but only after the referee has re-checked the bundle
itself.

Service: `https://qortime-referee-production.up.railway.app` (Railway, project `zesty-mindfulness`, service
`qortime-referee`, Dockerfile `services/referee/Dockerfile`). Code: `qor_receipt/review/` (engine, checks) and
`qor_receipt/referee_service/app.py` (HTTP). Client: `qorcap submit` / "Submit for Verification" in QorCap ≥ 0.3.9.

## Where the signing key lives (since 2026-10-16, see `docs/REFEREE_KEYS.md`)

- **Root `qortime-v1`** (`a5aa0c91…feea`, allowlist `can_delegate`) is **offline**. The only copy is
  `/workspace/qortime-keys/qortime-referee-v1.key` (0600) on the operator box. It is **not** on Railway. Its only jobs
  are to sign delegation certificates and the revocation list.
- **The online service key** (`qortime-online-sN`) is the Railway variable `QORTIME_SERVICE_SEED`, and its root-signed
  cert is `QORTIME_DELEGATION`. The cert is valid for at most 90 days, is limited to scope `official-review`, and caps the
  verdict at VERIFIED/PARTIAL. Anyone with Railway write access can still sign *as that delegate*. They cannot mint new
  delegations, cannot extend the window, and can be cut off with one root-signed revocation. Every honest signature also
  lands in the public transparency log (`/v1/log`), which is anchored in this repo.
- **Not** on any player laptop. The laptop keeps only its own device key (which now also signs submit requests) and its
  local capture-referee key.

## Flow

```
laptop (QorCap)                                   referee service (official key)
1 POST /v1/submissions  zip of small files ─────▶ recompute manifest chain, checkpoint chain (frames_hash ==
  (manifest, checkpoints, countersigned,           JCS(qorcap)), inputs chunk chain + checkpoint binding, input
   inputs, haptics, input_proof, receipt.json,     proof file hashes, receipt.json signatures/timing under the
   official_challenges, anchors)                   laptop's own keys.  Reject on any error (HTTP 422).
                                    ◀──────────── commit = H(all of the above); THEN draw the plan:
                                                   init segment(s), recorded tail (last 240 s), segments around
                                                   each challenge's OPTIONS press, 16 random media segments
2 PUT /v1/submissions/{id}/segments/{seq} ─────▶ stream-hash each fMP4 segment; must equal the committed
  (raw bytes from video.mp4 at manifest offset)    sha256 + length (stored only if needed for decoding)
3 POST …/review ───────────────────────────────▶ decode tail with ffmpeg → QorCap's own ReaderCore (pinned
                                                   sidecar: scorebug + end-of-game menu) from scratch, no laptop
                                                   timeline; compare with the claimed final; re-run challenge
                                                   patterns from raw inputs.jsonl + pause seen on its own decode;
                                                   motion correlation over decoded seconds; then sign (or refuse)
4 POST …/player-signature (device key) ────────▶ receipt complete; segments deleted
```

`--full` (or `mode=full`) requires every segment (1–3 GB). Same checks; the plan's decode set is unchanged, the
rest is hashed in-stream and discarded.

## Receipt format

Same `qor-receipt/v0`, with:

- `referee = {pubkey: official, key_kind: "qortime-official-review"}` — the official signature covers everything,
  including the new `review` block.
- `review = {type: "qortime-bundle-review/v1", capture_referee, checks, scope, not_proven, final_reread, anchors, …}`.
  `checks.*.state ∈ pass | partial | fail`.
- `checkpoints` are the laptop's live countersigned chain; in review mode both verifiers check them under
  `review.capture_referee.pubkey` (still gap ≤ 10 s, skew ≤ 3 s).
- `challenges[*].issuer` = signed `qortime-challenge/v1` token; `result` is the referee's own re-run.
- `signals.motion_permille` is computed by the referee (`signals.motion_basis` says how).

## Challenge latency (rule v1 and rule v2)

The bound is a liveness check, not a claim about how fast Madden draws. The HID log must contain
OPTIONS and then the nonce pattern inside the issue window, and the referee's own decode must show
the pause menu open *after that press*. A menu already on the tape (`video_before_input`) means the
video is not this live console. A menu that shows up too late (`video_latency_too_high`) means the
HDMI can be a delayed copy. The four-button pattern is derived from a nonce issued at runtime, so a
pre-recorded or replayed stream does not contain it.

Rule v1 is the historical check and the default. Frames are dated with `segment_ranges` (the
manifest close stamps). The menu must land in 0..600 ms. A receipt with no `result.rule` keeps
that meaning. On a real Madden/PS5 capture the fully drawn menu is about 1.5–1.75 s after OPTIONS
(match 110028: 1556 / 1535 / 1715 ms on the presentation clock), so an honest press cannot pass v1.
The close-stamp clock adds about another 1.6–1.9 s on top of that.

Rule v2 is opt-in: `bundle.json` `challenge_rule` must be the integer 2. Anything else, including a
missing or unknown field, stays v1. The referee then:

- dates frames from the fragment's first-sample PTS (`tfdt`) through the manifest frame fit;
- accepts a menu in 0..2500 ms;
- requires every successful latency to sit within ±300 ms of the median of those latencies
  (a difference of exactly 300 ms still passes; one challenge has no peer and is not compared).

An outlier is flipped to `ok: false`, reason `latency_inconsistent`, before the phase-pass check,
so it does not count toward VERIFIED. The challenges check stays `partial` (not a structural
`fail`). The signed result carries `result.rule`, which is what the verifiers trust. A claimed
success at 1700 ms with no rule is still `challenge_latency_out_of_range`. A claimed rule-2 success
whose latencies disagree is `challenge_latency_inconsistent` (PARTIAL). Mixed rule ids on one role
are `challenge_rule_mixed`.

What v2 still rejects: a pause before the press, a missing pause, a replayed or early pattern, and
a delay past 2500 ms. What a constant-delay relay under 2500 ms can still do: show the menu at the
same offset on every challenge. That offset is inside the ceiling and inside the jitter window.
The pattern is still unpredictable and issued at runtime, so the relay has to be a live delayed
copy, not a recording made earlier.

## Verdict rules (Python `qor_receipt/receipt.py` = JS `verify/verify.js`)

| condition | status |
|---|---|
| any check `fail`, review block missing/forged, chain sig bad under capture key, any normal FAIL rule | FAIL |
| referee key not on the allowlist / revoked (e.g. a forged review by any other key) | SELF_ATTESTED |
| official key, but any check `partial`, or a phase without an **official** passed challenge, or motion < 300‰ | PARTIAL |
| official key + every check `pass` + kickoff/halftime/end official challenges re-verified + motion ok | VERIFIED |

Verifier output adds `verification_basis: "official-post-hoc-review"` and `review.{scope, video_method,
segments_verified, segments_total, not_proven}`; the browser verifier prints the basis line.

## Two-sided corroboration (additive)

`POST /v1/pairings` ignores the body and returns a referee-signed `qortime-pairing/v1` token.
`pairing_id` and `nonce` are 32 hex characters chosen here. Each player stores that exact JSON as
`pairing.json`. A client-chosen id is not a link. This request and token shape are stable.

The cross-check reads each signed score timeline. `signals.score_timeline` inside `receipt.json`
wins when it is present. Otherwise optional `score_timeline.json` is used after its chain and
signature verify. The result is `review.corroboration` (`corroborated`, `disagreed`, or
`single_sided`), including `finals.home` and `finals.away` when those scores were read. It is not
one of `review.checks`. A disagreement is shown and does not flip VERIFIED to FAIL. A pair that
agrees does not flip FAIL to VERIFIED. One submission, a missing timeline, or a forged pairing
token stays on the single-sided rules (`single_sided`, or `disagreed` / `unsigned_score_timeline`
when both players submitted and the timeline does not check out). Detail: [CORROBORATION.md](CORROBORATION.md).

## Device key (informational)

An optional `device_attest.v0.json` sidecar can ride in the bundle. When it is present, the review
records `review.device_key` as `tpm_claimed`, `software`, or `invalid`. It is not a review check.
`tpm_claimed` means the device signature checked and the bundle claims a TPM key. That claim is
unverified. `tpm_attested` is not emitted. An invalid signature is flagged on the verify page and
does not change the verdict. The device-key signed bytes are an assumption documented for the
matching QorCap change: [DEVICE_ATTEST.md](DEVICE_ATTEST.md).

## What VERIFIED means here (honest trust level)

Proven by the referee itself:
- the bundle's hash chains are internally consistent and bind video segment hashes, controller log and checkpoints;
- the laptop holds video bytes matching the committed manifest — **all** of them (`full`), or, in spot-check
  mode, every requested segment; a laptop missing a fraction *f* of the segments passes the random part with
  probability (1−f)^16 (10 % missing → 0.185; 50 % → 1.5e-5). Unsampled content is not inspected;
- the final score comes from the referee's own read of the recorded end-of-game frames;
- with official challenges: the pattern was pressed after the referee issued the nonce (unknowable before), and
  the challenge's chain-head anchor shows the earlier video already existed then. Anchors (`/v1/anchors`, every
  60 s during Start Match) timestamp the chain the same way.

Not proven (also embedded in every receipt as `review.not_proven`):
- who held the controller;
- that the laptop's countersignatures were live (its key is on the player's PC) — official anchors/challenges are
  what tie the chain to real time;
- that footage wasn't fabricated/edited before QorCap hashed it, outside anchored windows;
- QorCap's human-likeness / input-video alignment model is not re-run (hashes and counts are);
- delta corroboration for a one-legal-step end-menu delta still uses QorCap's existing evidence rules
  (haptics / inputs from the committed log; score-clip hashes as claimed in bundle.json — clip bytes not uploaded).

## Operations

```
GET  /health                 {"ok":true,"allowlisted":true,"delegation":{valid,serial,days_left},"log_size":N}
GET  /v1/submit/challenge?pub=<hex>   nonce + pow_bits for a signed submission (docs/REFEREE_KEYS.md §3)
GET  /v1/log/head | /v1/log?start= | /v1/log/inclusion/{body_sha256}   transparency log
GET  /v1/referee             pubkey, key_id, policy, final_reader/ffmpeg availability
POST /v1/pairings            no client id; returns a signed qortime-pairing/v1 token
POST /v1/challenges          {"phase":"kickoff|halftime|end","role":"home","match_id":"…","anchor":{"chain_head":"…"}}
POST /v1/anchors             {"match_id":"…","anchor":{"chain_head":"…","segment_seq":N,"checkpoint_head":"…"}}
POST /v1/submissions?mode=spot-check|full     body: application/zip, header X-Qor-Submit (QorCap >= 0.3.10)
```

Env: `QORTIME_SERVICE_SEED` + `QORTIME_DELEGATION` (secret online key + root cert; legacy `QORTIME_REFEREE_SEED` is
dev-only), `QORTIME_STATE_DIR` (volume `/data`: transparency log + known install keys), `QORTIME_REFEREE_ID`, `QORCAP_SIDECAR` (baked into the image at a pinned
QorCap SHA), `QORTIME_RANDOM_SEGMENTS` (16), `QORTIME_TAIL_MS` (240000), `QORTIME_MAX_ACTIVE` (4),
`QORTIME_SUBMIT_TOKEN` (optional admin override; without it every submission must be install-key signed),
`QORTIME_POW_BITS_*`, `QORTIME_RATE_*`, `QORTIME_REQUIRE_RECORDER_MATCH` (default 1).

In-flight submissions live in `/tmp` and expire after 1 h (a restart drops them; the client just re-submits).
The transparency log and the known-install-key list persist on the Railway volume mounted at `/data`.

### Deployment record (2026-10-16 CDT)

- Railway service `qortime-referee` `fefa2ecd-b255-4ee8-ba71-6607f24ccc22` runs in project `zesty-mindfulness`
  (`d2fab6f8-…`), environment `production`. Source is `ConWan30/qortime@main`. The Dockerfile path is set to
  `services/referee/Dockerfile` (service config plus `RAILWAY_DOCKERFILE_PATH`). Watch patterns are `qor_receipt/**`,
  `services/referee/**` and `pyproject.toml`. The healthcheck is `/health`.
- `QORTIME_REFEREE_SEED` was set with the Railway MCP `set-variables` call. The value was substituted from
  the operator box file `qortime-keys/qortime-referee-v1.key` at call time, so it was never printed, logged or committed.
  `QORTIME_REFEREE_ID=qortime-v1` and `QORTIME_MAX_ACTIVE=4`. `QORTIME_SUBMIT_TOKEN` is unset, so submissions are open.
  The size caps and the max-active limit bound abuse.
- After deploy, `GET /v1/referee` reported pubkey `a5aa0c91…feea`, `allowlisted: true`, `final_reader: true` and
  `ffmpeg: true`.
- To rotate, set a new seed the same way, add the new allowlist entry and retire the old one (`TRUSTED_REFEREES.md`).

Cost: one small always-on Railway container (≈0.25–0.5 vCPU idle-light, 512 MB–1 GB RAM while decoding).
Ingress is free on Railway; a spot-check is ~150–300 MB per match (mostly the 240 s tail), `--full` 1–3 GB.
Review CPU is ~10–60 s per match. Egress is tiny (JSON). Stripe is not involved.

Local run:

```
pip install -e ".[referee,vision]"
QORTIME_REFEREE_SEED=<hex> QORCAP_SIDECAR=/path/to/qorcap/sidecar qortime referee-service --port 8080
```

### Hardening deployment record (2026-10-16 CDT, PR #27)

- Online key `qortime-online-s1` (pubkey `ab1f6e2c…e567`, key_id `5443cbdc7f94ad26`) was created on the operator box at
  `qortime-keys/online/` (0600). Delegation serial 1 is signed by root `qortime-v1` and published as
  `trust/delegations/serial-1.json`. It expires 2026-12-15, so rotate before then with `scripts/referee_keys.py delegate`.
- These Railway variables were set by substituting file contents at call time, so no value was printed:
  `QORTIME_SERVICE_SEED`, `QORTIME_DELEGATION`, `QORTIME_REFEREE_ID=qortime-online-s1` and `QORTIME_STATE_DIR=/data`.
  `QORTIME_REFEREE_SEED` (the root seed) was overwritten with an empty string. The API token can't delete the variable
  itself, so that still needs to be done in the dashboard. Volume `qortime-referee-state` (68877173…) is mounted at
  `/data`.
- After the deploy, `/health` reported `delegation.valid: true`, serial 1 and 59 days left. `/v1/referee` reported pubkey
  `ab1f6e2c…`. An unsigned `POST /v1/submissions` returned 426 with the upgrade message.
- The referee image pins the QorCap sidecar at `739efbf`, which includes the Monday Night pack (`madden27_monday_night_v1.npz`).
- Since QorCap `67853bf` (v0.3.11, PR #17), the image also pins Sunday Night (`madden27_sunday_night`, QorCap `qorcap_snp.py` + `madden27_sunday_night_v1.npz`) and Thursday Night
  (`madden27_thursday_night`, `qorcap_tnp.py` + `madden27_thursday_night_v1.npz`); the Dockerfile `test -f`s both so a stale pin fails the build.
  `/v1/referee` now reports `scorebug_profiles`.
- Since QorCap `6f7fd79` (v0.3.23, PR #37), the image pins the sidecar that ships `qorcap_standard.py` with the
  real-glyph standard packs (`madden27_standard_score_110028.npz`, `madden27_standard_score_084523.npz`; the Dockerfile
  `test -f`s them). The referee now builds its final-read `ReaderCore` with `qorcap_standard.standard_reader()`, exactly
  as QorCap does. Before this it used QorTime's bare `ScorebugReader()`, read no scorebug in the tail of
  match-20261018-084523 (the red home "14" scores under the 0.70 gate on the bare pack) and failed `final_reread`.
  The tail is still decoded at 2 Hz: with the standard reader 084523 gets 156 agreed reads at 2 Hz, and 6/15 Hz with the
  bare reader still read nothing, so a higher rate buys nothing here.
- Since QorCap `1680734` (v0.3.24, PR #39), the pinned sidecar's `ReaderCore` also has the guarded touchdown fast path
  (template margin >= 0.15 on every fast-path read, look-alike single-digit jumps held for 2 confirming reads, every
  fast-path lock held 1 s). The referee builds no Glimpse listener and no veto, so its read is local-only, same as
  QorCap's offline finalize. QorCap's 15-match replay gate: 0 new wrong locks vs v0.3.21.
- **End-of-game menu without a tail scorebug (fail closed).** If QorCap's ReaderCore rejects the menu only because no
  Q4 scorebug was read before it, the referee accepts the menu when all of these hold: real templates (not a partly
  covered "corroborated" panel), confidence >= 0.90 over >= 2 agreeing frames, not tied, no locked read (the referee's
  own tail reads, else the bundle's committed `timeline.jsonl` locks) above the menu on either side, and reachable from
  the last locked read (a lone +1 only as a single try). `final_confirmed` is true only when the menu equals the last
  locked read, or is one legal play past a Q4/OT lock; otherwise it is posted unconfirmed and `final_reread` is PARTIAL.
  Clock, illegal-step and tie rejections stay refused, and an unreadable tail still fails. Locks never supply a score;
  they can only refuse one. Challenges and trust tiers are unchanged.

## Head-to-head challenges (`/v1/h2h`, free, not for betting)

| Route | Auth | What it does |
|---|---|---|
| `POST /v1/h2h` | none | `{creator, opponent?, creator_psn, opponent_psn, rules{quarter_min, difficulty, notes}}` → `{challenge, creator_token}`. PSN IDs must match `^[A-Za-z][A-Za-z0-9_-]{2,15}$` and must not be look-alikes of each other (400 otherwise) |
| `GET /v1/h2h/{id}` | none | public challenge + settlement (`open`/`accepted`/`unverified`/`players_unverified`/`players_mismatch`/`recorded`/`confirmed`/`disputed`); each counted receipt carries `observed` and `players_check` |
| `POST /v1/h2h/{id}/accept` | none | once only → `{opponent_token}` |
| `POST /v1/h2h/{id}/receipts` | `Bearer <player token>` | upload a receipt (≤4 MB); verified with the same rules as the verifier |
| `GET /v1/h2h/{id}/receipts/{sha16}` | none | download a submitted receipt |

Tokens are returned once and only their sha256 is stored (`$STATE_DIR/h2h/`). A receipt can settle one challenge only.

PSN check: a VERIFIED receipt counts only when its signed `match.observed_handles` (names QorCap read from Madden's
on-screen banners) match `creator.psn`/`opponent.psn`. Characters are compared one by one, case-insensitive, with
look-alike groups (`0Oo QDd`, `1lIi|j`, `gyq9`, `5Ss`, `2Zz`, `8B`, …). Both side assignments are tried. A mismatch needs
a complete read with confidence ≥ 850‰ and ≥ 3 agreeing frames; otherwise the result is `players_unverified`. A receipt
whose role contradicts the on-screen side is `players_unverified`. Neither status has a winner. This is on-screen
evidence, not account proof: QorTime cannot tell look-alike names apart, and anyone who controls the console account
controls the name shown. Challenges created before this check (no PSN IDs) settle as `players_unverified`.
Money words (bet, wager, $, stake, payout...) are rejected in rules. CORS: `QORTIME_CORS_ORIGINS`
(default `https://conwan30.github.io`). Rate limit: `QORTIME_RATE_H2H_HOUR` (default 300/hour per client).
Settlement never signs anything; it reads receipts and fails closed.
