# Show Your Hands (hackathon work, Meta AI Developer Hackathon, Oct 26 - Nov 4 2026)

"Here are the hands that made this play." It overlays the **verified** DualSense inputs on a highlight clip and
checks that each on-screen action has a press behind it.

**Advisory, lower-only, not wired in.** The most it can ever do is lower VERIFIED to PARTIAL
(`show_your_hands_flags`). It never FAILs a match and never raises a status. In this version it is not connected to
`verify_receipt`, `verify.js` or any page, so no live verdict changes.

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

## Data
Tests use **SYNTHETIC** inputs (`synthetic.py`), not a real controller. Running it on match 170123 needs that match's
`inputs.jsonl` + chunk list from the player's PC; they are not in this repo and won't be committed.

## Next (if the real run looks clean)
- Video action/stimulus times from local frame differencing.
- Sign `hands.json` with the AI advisory key.
- A verifier panel using the same lower-only guard as the score cross-check.
