# AI referee: `qortime referee-ai <bundle>`

Takes a **QorCap match bundle** (`bundle.json` with schema `qorcap-bundle/v0`, `manifest.jsonl`, `checkpoints.jsonl`,
`timeline.jsonl`, `video.mp4`, `clips/`, optionally the signed `receipt.json` and `referee.pub`) and writes a signed
verdict to `BUNDLE/referee_verdict.json`: **accept**, **dispute** or **abstain**, with plain reasons and the
evidence frames.

```bash
pip install -e ".[vision]"
export TYPESAFE_API_KEY=...            # optional; without it the Jev layer reports available=false
qortime referee-ai C:\QorCap\match1     # exit 0 accept, 1 abstain, 2 dispute
qortime referee-ai BUNDLE --vision none --no-jev   # integrity only
qortime referee-ai-verify BUNDLE/referee_verdict.json [--pub HEX]
python eval/referee_ai_frames.py FRAMES_DIR [--replay tests/fixtures/referee_ai/jev_answers.json] [--out DIR]
```

## Layers

| Layer | Module | Job | Can it cause dispute? |
|---|---|---|---|
| Integrity (code) | `referee_ai/integrity.py` | Python port of `qorcap verify-bundle` (segment SHA-256 + chain `SHA-256(JCS({prev,seq,sha256}))`, checkpoint hash/`frames_hash` commitment/known chain head), bundle.json heads, clip hashes, `receipt_draft_sha256` (= SHA-256 of the JCS draft, as QorCap's engine.rs computes it), `verify_receipt` on `receipt.json` pinned to `referee.pub`. **Legal score steps** (1, 2, 3, 6, 7, 8; never backwards), quarter never backwards, game clock never runs backwards within a quarter, final screen / bundle final / receipt result == last scorebug score. | Yes. This is the only place that catches tampering. |
| Seeing (pluggable) | `referee_ai/vision.py` | Default `scorebug` = QorTime's vendored Qoresence `ScorebugReader` (640x360, away = left). `meta` = **stub** for Meta Muse Spark (hackathon key due 2026-10-26). It makes no calls and returns `ok=false` (`meta_key_missing` or `meta_adapter_not_implemented`). | Only via agreement: its 2 most recent frames must read the same score. |
| Judgment | `referee_ai/jev.py` | TypeSafe Jev System One (`jev-1.14.0` by default, header `TypeSafe-Danger-Zone-Images: true`, image in `assets`). Asks frame type (live / menu / final / black / cutscene) and score, quarter and game clock as Choices. **A field is accepted only at confidence >= 0.9 on >= 2 frames with no confident disagreement; otherwise it abstains.** No tamper question: Jev reads doctored frames at face value with confidence of about 1.0. | Only when its multi-frame consensus contradicts the claim. |

Verdict: **dispute** on any integrity error or accepted contradiction. **accept** when integrity is clean and at least
one seeing source confirms the claimed final on >= 2 confident frames. **abstain** otherwise.

Single confident per-frame reads (Jev at conf >= 0.9, or an ok scorebug read) also go through the integrity
progression checks, so a doctored frame that a model reads "correctly" is still caught by the rules in code.

## Meta Muse Spark adapter interface (to wire up on 2026-10-26)

`MetaMuseVision.observe(img: np.ndarray BGR) -> Observation(source="meta", ok, reason, frame_type, away, home, quarter, clock_s, conf={field: permille})`.
Key from `META_MUSE_API_KEY` (never logged), model from `META_MUSE_MODEL`. Fail closed (`ok=false` + reason). Report
only what is visible. Do not pass uncalibrated confidences as >= 900.

## Verdict format (`qortime-referee-verdict/v0`)

The format is receipt-friendly. The signed bytes are the JCS of the verdict without `signature` (no floats, so confidences
are permille ints), signed with Ed25519. `key_id` follows the qor-receipt rule. The default key is `~/.qor-receipt/referee-ai.key`.
Top-level fields:

- `verdict`, `reasons`, `claim`
- `subject` (match_id, chain/checkpoint heads, receipt and bundle.json SHA-256)
- `integrity`, `seeing`
- `judgment` (model, threshold, consensus, calls, tokens, median latency)
- `evidence[]` (frame id, video position, PNG SHA-256, every observation)
- `referee_ai` (pubkey), `signature`

The verdict sits next to the bundle. It does not modify `receipt.json`, whose signatures cover its own bytes.

`referee_verdict.json` is never a training label. The accept / dispute / abstain word is a model-bearing judgment. It is not one of `review.checks`. On-device learning consumes `review.checks` only: `bundle_structure`, `capture_chain`, `video_bytes`, `final_reread`, `challenges`, `motion`, and `input_integrity`, plus an owner review when the owner supplies one. The AI explains those checks. It does not decide the receipt verdict.

`final_reread` keeps its `state` and prose `detail`. When the referee's reader already has integer `away` and `home`, the check may also carry an optional `value` object (`away`, `home`, and `quarter` when the reader has one). A pass with no `value` cannot confirm a score, because nothing here parses the prose. The verifier ignores `value`. Adding or omitting it does not change VERIFIED, PARTIAL, SELF_ATTESTED, or FAIL.

## Measured (2026-10-16)

- 22 real Madden capture frames + 3 doctored copies, live `jev-1.14.0`:
  - Frame type: 14/15.
  - Score: 12/13. The one miss abstained: home was "none" at conf 0.48.
  - Median latency: 234 ms, about 6k input tokens per frame (about $0.00025).
  - All 7 scenarios gave the expected verdict, and all 3 doctored scenarios were disputed by integrity rules.
- Real QorCap bundle (`match-20261014-155852`, test-pattern capture): integrity clean, receipt PARTIAL (no challenges), verdict **abstain**, because the video is a test pattern and neither the scorebug reader nor Jev (all conf < 0.9) saw a Madden score. A one-bit flip in `video.mp4` gives **dispute** (`segment 1 SHA-256 mismatch`).

## Gaps

- Bundle frames are mapped to wall time from the manifest (last segment `wall_ms` minus video duration). Positions are approximate (about 1 segment), and `video.partN.mp4` is hashed but not sampled.
- The checks do not cover challenges, `challenges.jsonl` nonce stamps, or player identity. Like QorCap itself, the referee proves footage integrity and timing, not who held the controller.
- Jev images are "Dreamer-ready" (beta). Re-run `eval/referee_ai_frames.py` after model bumps.
