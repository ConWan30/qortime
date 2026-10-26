# Show Your Hands (hackathon work, Meta AI Developer Hackathon, Oct 26 - Nov 4 2026)

"Here are the hands that made this play." It overlays the **verified** DualSense inputs on a highlight clip and
checks that each on-screen action has a press behind it.

**Advisory, lower-only.** The most it can ever do is lower VERIFIED to PARTIAL (`show_your_hands_flags`). It never
FAILs a match and never raises a status. `verify_receipt` and `verify.js` don't import it. The verifier page shows a
signed `hands.json` (AI advisory key, bound by `receipt_jcs_sha256`) through `verify/hands-view.js` and the same
lower-only guard as the score cross-check. No valid file means no panel and no change.

## Steps
1. **Integrity** (`inputs.py`): re-hash `inputs.jsonl` chunks (SHA-256 of JCS of each segment's records), check the
   chain (`head = SHA-256(JCS({prev, seq, sha256}))`), and require that every non-empty chunk is committed by a receipt
   checkpoint `input_hash` (and every `input_hash` is a chunk or the empty-chunk hash `4f53cda1…b945`). Any mismatch
   means **no overlay and no flags**.
2. **Overlay track** (`track.py`): the input state at each 30 fps frame (buttons, sticks -1..1 assuming centre 128,
   triggers 0..1). The renderer is left to the demo; rendered footage is never committed.
3. **Press-to-pixel** (`press_to_pixel.py`), on the capture clock:
   - Clip alignment: the median press→action lag must be within 0-300 ms, with ≥ 3 anchors and a median residual ≤ 100 ms.
     Otherwise the clip is `unscorable` and its flags never count.
   - `no_press`: an action with no input change in the lag + 700 ms before it.
   - `too_fast`: a reaction under 120 ms after a stimulus (needs supplied stimulus times).
   - `machine_regular`: ≥ 8 presses whose hardware-timestamp intervals have a CV under 3.5 %.
   - A cap needs ≥ 2 flags in scorable clips.
4. **Sidecar** (`sidecar.py`): `hands.json`, schema `qortime.hands.v0`, `affects_verdict: "lower_only"`,
   `max_cap: "PARTIAL"`, bound by `receipt_jcs_sha256`. `sanitize` drops device paths, VID/PID, serials and gamertags.

## Video actions (`video_events.py`, `muse_context.py`)
- Local detector: 160x90 grey frames with the burn-in label and scorebug masked, mean frame difference. An
  **onset** is motion after ≥ 0.5 s of stillness. A **cut** (histogram correlation < 0.7) is never an action.
- Muse Spark (optional, `--muse`, max 12 calls per match): two frames (−0.4 s / +0.6 s) per onset, asking "is this a
  play start". Only confident `player_action` keeps a candidate; errors or no key keep nothing. A second start
  within 6 s in the same clip counts as the same play. Muse can only remove candidates.
- Measured on the public 170123 clips: the local detector found 4 onsets + 1 cut. By eye, 3 onsets were play starts and
  1 was a camera pan over the goalposts.
  - With the final prompt Muse got 9/9 right in one run (4 onsets + 5 non-play frames). The prompt was tuned on
    these same frames, so treat that as in-sample.
  - In the real run, Muse kept 1 of the 3 real play starts and dropped the other 2. Its labels vary run to run, but
    always in the safe direction.

## Match 170123 (real inputs, run Oct 26)
- `inputs.jsonl` re-hashes into 471 chunks, all committed by the receipt's checkpoints (31,184 reports).
- Press-to-pixel: **not scored**. The only anchor was hl01, with a lag of 430 ms. hl00's kick swing came 1.56 s
  before the on-screen onset and hl02's last press 2.29 s before it. Madden play starts aren't single press-to-pixel
  events, and the capture clock fit has about ±0.7 s of jitter.
  - Without the alignment gate, those two legit plays would have been `no_press` false flags.
- `site/matches/170123/hands.json` is the signed result (no per-frame track, no device data). The verifier shows it
  as an info panel; status stays VERIFIED.

## Data
Tests use **SYNTHETIC** inputs (`synthetic.py`). Real match inputs (`inputs.jsonl`, `manifest.jsonl`) stay
private: they are never committed, and the published `hands.json` carries only counts, timings and flags.

CLI: `python -m qor_receipt.hands.sidecar RECEIPT inputs.jsonl clips/manifest.json manifest.jsonl --clips DIR --muse
--no-track --sign-key KEY --out hands.json`. Keep inputs local; use `--no-track` for anything published.
