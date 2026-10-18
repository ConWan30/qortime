# QorTime

**One app. One recording. One receipt.**

- **Capture.** [QorCap](https://github.com/ConWan30/qorcap) is the single lightweight Windows app you run during the match. It records the capture card and fingerprints the video (a hash chain plus timestamped checkpoints). It also logs the controller's button presses from the same session (since v0.3.0).
- **Prove.** After the match, QorTime works on the finished recording, not during play. It produces the signed receipt, then the [AI referee](docs/REFEREE_AI.md) (`qortime referee-ai <bundle>`) signs an **accept / dispute / abstain** verdict with plain reasons and evidence frames.
- **Verify (official).** `qorcap submit` / "Submit for Verification" uploads the bundle to the [QorTime official referee](docs/REFEREE_SERVICE.md), which re-checks hash chains, video bytes (spot-check or full), the end-of-game score and challenges on its own, and only then countersigns with the official key, so the receipt can be **VERIFIED** instead of SELF_ATTESTED.
- **Share.** Anyone can check the receipt in the [browser verifier](https://conwan30.github.io/qortime/verify/) or with `qortime verify`, and the referee verdict with `qortime referee-ai-verify`.

Honest limit: QorTime proves the footage's integrity and timing (and, from v0.3, the controller inputs). It does not prove who held the controller.

**Signed, verified match-result receipts for console money-match / escrow platforms**
(Locked, TrustBridge, ArcadeBet, Wagr, and others). First title: **Madden 27 on PS5**.

QorTime (formerly qor-receipt) seals the moment a match ends. Site: https://conwan30.github.io/qortime/ · Verifier: https://conwan30.github.io/qortime/verify/ . The Python package stays `qor_receipt`; both `qortime` and `qor-receipt` commands work.

## Try it

- Site: https://conwan30.github.io/qortime/
- Verify in the browser: https://conwan30.github.io/qortime/verify/
- How it works: https://conwan30.github.io/qortime/how-it-works/
- Pricing: https://conwan30.github.io/qortime/pricing/
- **Verified Match** ($9.99 hosted-referee countersign): [early access](https://conwan30.github.io/qortime/early-access/) / Stripe **test** mode until business KYC. This is not a live paid product for real money. Test Payment Link: https://buy.stripe.com/test_3cI4gy7he9Uuarqczl00000

A platform holding escrow gets a small JSON receipt saying *who played, the final score, how it
was observed*, signed by each player's agent and countersigned by an independent **referee**
service. Anyone can check it offline (CLI) or in a browser ([verifier page](https://conwan30.github.io/qortime/verify/)).
The verdict is **VERIFIED / PARTIAL / FAIL** plus a trust tier: **dual-attested** (both players ran
agents and their timelines agree) or **single-attested**.

## Rig (per player)

```
PS5 --HDMI--> USB capture card --> Windows PC (qor agent: scorebug reader + HID log)
DualSense Edge --Bluetooth--> PS5      (plays the game)
DualSense Edge --USB-------> PC        (same controller, also cabled to the PC: the agent logs its HID reports)
```

Both links are intended. The USB link is what lets the agent prove the inputs, so don't unplug it.

## How a receipt is made trustworthy

| Weak spot | Mitigation in v0 |
|---|---|
| Replay / wrong console / pre-recorded video | Referee **nonce challenges** at game start, second half and Q4, each opened only once Madden allows a pause (after the kickoff and a play call; 90 s window). The nonce maps to `OPTIONS` + 4 buttons. Verifier needs the pattern in the HID log **and** the pause menu on video 0–600 ms after the press. Input-to-video **motion correlation** is an extra signal. |
| Signing key sits on the player's PC | Agent sends **rolling hash checkpoints** (frames + input) every ~4 s. The referee timestamps and countersigns each one. A receipt fabricated afterwards can't carry referee signatures from the past. Keys go through `KeyProvider`, so a TPM can drop in later ([PHASE2](docs/PHASE2.md)). |
| Fake video source | The same live challenge, plus **dual mode**: both agents submit, and score/quarter timelines must agree. Single-agent matches get a lower tier. |

Residual risks are written down plainly in [docs/RISKS.md](docs/RISKS.md) (account sharing, both players colluding, and others).

## Layout

```
spec/match-receipt-v0.md      the format and verification rules
schema/receipt.v0.json        JSON Schema (JCS canonical, Ed25519)
qor_receipt/receipt.py        build / sign / verify
qor_receipt/challenge.py      nonce -> button pattern, HID + vision verifier, motion correlation
qor_receipt/checkpoints.py    hash chain + referee countersign
qor_receipt/referee/app.py    FastAPI referee: start, challenge, checkpoint, timeline, finalize, sign
qor_receipt/final_detect.py   end-of-game from agreed scorebug reads; dual timeline agreement
qor_receipt/cli.py            qortime emit | verify | pattern
qor_receipt/_vendor/          vendored Qoresence code (scorebug reader/agreement, signing, chain), see VENDORED.md
verify/                       static verifier (index.html + verify.js + vendored @noble/ed25519)
```

## Quick start

```bash
pip install -e ".[dev,vision]"
pytest -q && node verify/test.mjs
qortime emit --demo r.json            # synthetic match through the real referee code
qortime verify r.json --referee-pub <hex>
QOR_REFEREE_SEED=<64 hex> uvicorn qor_receipt.referee.app:app   # run a referee
```

## Status

**Real:** receipt format, JCS (Python and JS parity vector), Ed25519 signing and verification (Python
and browser), the checkpoint chain with referee countersign, the challenge pattern and HID/latency verifier,
motion correlation, final detection, dual timeline agreement, the referee API (in-memory), the CLI, the
static verifier, and the tests and CI.

**Windows capture agent: built** (PR #1, `scripts/qor-agent.cmd`): DualSense HID capture, a scorebug
sampler that requires 3-read agreement, and nonce challenges. The reader fix landed in PR #2; in a live
Madden test, of 233 sampled frames 143 were read correctly, 90 were refused, and 0 were read wrong.

**Live:** site at <https://conwan30.github.io/qortime/>, verifier at <https://conwan30.github.io/qortime/verify/>.

**Verified Match (early access / test):** $9.99 list price for a hosted-referee countersign. Stripe checkout is test mode until business KYC. It is not a live paid product for real money. See [docs/STRIPE_VERIFIED_MATCH_TEST.md](docs/STRIPE_VERIFIED_MATCH_TEST.md).

**Known gaps:**
- End-of-game: red final-seconds clock is now read (fail-closed), with `scorebug_gone` / `final_screen` fallbacks
  and `result.final_basis`. A Jev final-screen detector runs in shadow mode only (see above). Not yet validated on a real Q4 ending (none captured). Reader eval: `eval/`.
- Halftime challenge and real pause timing are not yet confirmed in a full live match.
- The referee is in-memory only (no persistence or auth beyond player signatures).
- Phase 2 items (TPM, transparency log) are not started ([PHASE2](docs/PHASE2.md)). No platform integration.

## Jev final-screen detector (shadow mode)

`qor_receipt/agent/final_screen_jev.py` asks TypeSafe Jev 1.14 (model pinned to `jev-1.14.0`; `jev-latest`
is still 1.13) one yes/no (Noul) question while the scorebug is absent: is this Madden's
**post-game options menu with the final score**? Per the player (Madden 27): as soon as the game clock hits
0:00, Madden goes straight to an end-of-game options menu (a list of menu options) that shows the final score
(both team names and scores); the bottom scorebug bar is not shown. That menu is the final frame. The criteria
explicitly reject the mid-game pause menu (the most likely false positive: it looks similar, so the criteria
key on FINAL/post-game wording and a 4th-quarter 0:00 clock), halftime screens, touchdown/score graphics and
broadcast scoreboard overlays. It runs in a background worker (at most one call per 5 s, 15 s timeout; a busy worker
or the rate limit drops the frame), resizes frames to the image limits, and treats any error as `unknown`.

**Shadow only.** Results go to the session log as `jev_final_screen` records (model, frame sha256, answer,
p_yes, confidence, latency, plus `last_scorebug` = the last agreed scorebug `{t_ms, quarter, clock_s}` when
one exists, so a later deterministic rule can require "last clock seen was Q4 near 0:00" + "menu with final
score"). They are wired through `AgentCore.final_screen_shadow`, whose return value is
ignored, so they never open a `final_screen` span, never set `final_basis`, and never feed the trust tier,
the score digits, or the signed receipt.

- `QORTIME_JEV_FINAL_SCREEN=shadow|off`. When it is unset: shadow if `TYPESAFE_API_KEY` is set, otherwise off.
- `export_labels(session_log, out)` turns shadow records into weak labels for a future local detector.
- Live smoke: `python scripts/jev_final_screen_smoke.py --root /workspace` (needs the key and the eval images).
  First run (2026-10-14, 13 real negatives: gameplay, absent strip, pause, touchdown, broadcast overlay,
  halftime report/stats): 0 false yes, p_yes 0.01-0.02, median latency about 200 ms. Re-run 2026-10-15 with
the post-game-options-menu criteria on the same 13 frames: 0 false yes; both pause-menu frames `no`
(p_yes 0.02); all others p_yes 0.01-0.02; median 194 ms. **No real final screen has
  been captured yet**, so recall is untested.

## Credits

Vendored from [ConWan30/Qoresence](https://github.com/ConWan30/Qoresence) (MIT). Design ideas (certificate
shape, PORT-CERT VERIFIED/PARTIAL/FAIL honesty, clock-notary io ledger) come from
[ConWan30/QorTroller](https://github.com/ConWan30/QorTroller) at pinned commit `51d1ab3`. It's referenced, not depended on.
MIT licensed.

## AI referee

`qortime referee-ai <qorcap-bundle>` writes a signed accept / dispute / abstain verdict. It checks integrity in code, reads frames with the scorebug reader (a Meta Muse Spark stub is ready to wire up), and uses TypeSafe Jev to judge frames. See [docs/REFEREE_AI.md](docs/REFEREE_AI.md).

## Match index ("what the AI saw")

`qortime index BUNDLE --video match.mp4 --out qt-index/` writes `match_index.json` (kickoff, quarter changes, every
score change with before/after score, end of game; each with nearby button presses/haptics, the checkpoints and
clip hash that bind it to the video chain, and a heuristic confidence), ~10 burst frames + a contact sheet per event,
and a self-contained `viewer.html`. Inputs/haptics are advisory evidence, not proof; fields the bundle does not carry
are shown as missing. Tail clips (e.g. `last150s.mp4`) are aligned to the end of the recording.

## Match pages (`/matches/<id>/`)

`qortime publish BUNDLE --site site --video match.mp4 [--highlights BUNDLE/highlights]` turns a finished bundle into
`site/matches/<id>/` (score timeline, motion playback, touchdown highlights, controller advisory data) and rebuilds the
`/matches/` index (`qortime matches-index --site site` rebuilds the index alone). It publishes only when the bundle has
a `receipt.json`, verifies it, and labels the page with the status it gets: a FAIL receipt is refused unless
`--allow-fail`, and then every page element says the result is not verified. A clip is tagged "locked" only when its
score lock is in a receipt that does not FAIL. Raw `inputs.jsonl`, haptics, device ids and local paths are never copied;
the page carries only the summarised advisory data, and publish refuses if a private field leaks into the output.
Highlight clips come from `qorcap highlights BUNDLE` (QorCap repo); their manifest must name the same receipt.

## Head-to-head challenges (`/challenge/`)

Free links to settle bragging rights: no money, wagering, entry fees or Stripe. The creator makes a link
(`POST /v1/h2h` on the referee service), the opponent opens it and accepts, nothing to install. Only one player needs
QorCap: the creator's receipt alone settles it ("recorded by @handle"), because online Madden shows both players the same
scoreboard. If both players submit receipts that agree it says "confirmed by both players"; if they disagree it says
"disputed" and shows no winner. Only receipts the verifier computes as VERIFIED count (match started after the challenge
was made, final score confirmed); a SELF_ATTESTED, FAIL or missing receipt shows "unverified", never a winner. Handles
are typed by players and are not verified; quarter length and other rules are agreed, not checked. The challenge record
is not signed with the referee key. API details: `docs/REFEREE_SERVICE.md`.

**PSN name check (on-screen evidence, not account proof).** Sony and EA offer no public login or API, so QorTime never
sees a PSN account. Instead the creator enters both players' PSN IDs, and QorCap (`qorcap finalize --psn-names` or
`qorcap psn-names`) reads the names Madden draws on screen in online head-to-head games (the player banners at the top
of the play-call screen and the pause/end-of-game menu cards) from the recorded video. The consensus read goes into the
signed receipt as `match.observed_handles` (away/home, with the pattern, confidence in permille and how many frames
agreed). The challenge counts a receipt only if those names match the challenge's PSN IDs, ignoring case and look-alike
characters such as 0/O, 1/l and g/y. A reliable read of a different name shows **players don't match**; a missing,
partial or low-confidence read, or a receipt side that contradicts the on-screen side, shows **players unverified**.
Neither shows a winner. Because a signed receipt can't be redacted, `qortime publish` refuses a receipt that carries
both players' names unless `--include-names` is passed.
