# Camera-pan estimator (`qorai-optical-flow-pan/v1`)

`qor_receipt/vision/pan_estimator.py`. **Advisory only.** No code on the referee, review, or verifier path imports it, and `tests/test_pan_estimator.py` checks that. A signal built on it can never raise a verdict above PARTIAL and never overrides deterministic checks (hash chains, input proof, motion rule v3, scorebug locks).

## Contract

One record per 100 ms bin on the capture clock: `{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}`. Unreadable bins set `no_claim` true and leave the three measurements null. `identity()` returns the name, version, `sha256`, units, bin size, and parameters. A harness records the version and `sha256`.

## Method

1. Grayscale, downscaled with `INTER_AREA` to 320 px wide.
2. Masked regions, as full-frame fractions. Features are not detected there, and a tracked point that lands there is dropped.
   - The scorebug band comes from the vendored `local_scorebug` profile (`MADDEN27_STANDARD.band_y0..band_y1`, the bottom 8.3 %).
   - The qorcap-highlights burned caption box is `x 0–0.56, y 0–0.21`.
   - The broadcaster bug is `x 0.88–1.0, y 0–0.10`.
   - A 3 % border is also masked.
   - The last two boxes were measured on the 1280x720 highlight clips of match 170123.
3. Shi-Tomasi corners: up to 300, quality 0.01, minimum distance 7 px.
4. Pyramidal Lucas-Kanade tracking: window 21, 3 levels. A forward-backward check drops a point that misses by more than 1 px.
5. Global motion is the per-axis **median** of the surviving flow vectors. There is no RANSAC and no random sampling. Inliers lie within 1.5 px of the median. The residual is the median inlier distance.
6. Confidence = inlier ratio × min(1, tracked / 120) × max(0, 1 − residual / 1 px).
7. Pairs of consecutive frames are attributed to the later frame's 100 ms bin. A bin reports the median rate and the minimum confidence of its pairs.

## Units and signs

The rates are in **percent of frame width per second (%W/s)**. Pitch is normalised by width too, so a pixel means the same on both axes. No field-of-view assumption is made, so these are not degrees.

- `yaw_rate > 0`: the camera pans right, so content moves left.
- `pitch_rate > 0`: the camera tilts up, so content moves down.

## Fail-closed (`no_claim`)

A bin is `no_claim` when **any** frame pair in it is unreadable, or when the bin has no pair at all. Values are never interpolated. A pair is unreadable for any of these reasons:

- The frame failed to decode (`None`), or has a bad shape or a changed size.
- The frame is black (masked mean < 18) or flat (masked std < 6).
- Scene cut: the Bhattacharyya distance between masked 32-bin histograms is > 0.45.
- Timing gap: more than 250 ms, or a time that does not increase.
- Fewer than 30 features detected or tracked.
- Inlier ratio < 0.5, residual > 1 px, or confidence < 0.35.
- Any exception.

## Determinism

- `cv2.setNumThreads(1)` holds during a run, and the previous value is restored afterwards.
- Parameters are fixed in a frozen dataclass.
- Estimation is median-based.
- Every float in the output is rounded to 3 decimals.

`estimator_sha256()` is SHA-256 over:

- the version;
- the canonical parameter JSON;
- `cv2.__version__`;
- the SHA-256 of `cv2.getBuildInformation()`, which is compile-time and stable per wheel;
- the module source.

Two OpenCV builds therefore never share a hash. `identity()["opencv"]` records the version and the build digest. A test checks that two runs give byte-identical JSONL, and another checks that changing the OpenCV version or build string changes the hash.

### Pinned OpenCV

The tested wheel is **`opencv-python-headless==5.0.0.93`** (OpenCV 5.0.0, `cp37-abi3-manylinux_2_28_x86_64`). Its `sha256(getBuildInformation())` is `0ef08003c31127b3b5d34410ab13483c511e73b99e278b1a8abd6ddcb265699a`.

- The pin lives in `tests/constraints-pan-estimator.txt` and in the `dev` extra of `pyproject.toml`. CI already installs `.[dev,vision]`, so it gets the pin, and a test checks that the two places agree.
- To install it locally: `pip install -e ".[dev,vision]" -c tests/constraints-pan-estimator.txt`.
- The live referee image (`services/referee/Dockerfile`) installs `.[referee,vision]`, so it is unchanged. Neither of those extras carries the exact pin.

## Harness plug-in

`tests/redteam/optical_flow.py` adapts the estimator to #67's `CameraPanEstimator` protocol. The harness's `VariantSample` carries no pixels, so the adapter takes a frame source. When the estimator has a `sha256`, `source_identity` folds it in and records it as `estimator_sha256`. That is how the OpenCV build reaches the scorer hash.

The committed red-team report (`motion-rule-v3-aimbot.md`) still uses the no_claim stubs. A test plugs this estimator in on synthetic frames and checks only the contract, the identity, and determinism. No catch rate is asserted or published, because pan versus right stick is not a valid Madden aimbot signal (see below).

With that wheel the estimator hash is `2d2366343bd925fe201f6dc66c49c84e0b21f8911067e720d3ea934c2720bcb4`. Earlier revisions reported `e225c50d…` (before the OpenCV identity was hashed) and `dcb9d149…` (before the clock-binding gate). The per-bin outputs on the three clips are byte-identical across all three.

## Clock binding gate for video-vs-log detectors

Any video-vs-log mismatch detector must call `mismatch_claim_allowed(per_frame_binding, video_ms, log_ms)` before it claims anything. Examples of such a detector are a skill move, a user catch, or controlled-player motion with no matching logged press.

- **Without per-frame clock binding, the gate always returns False, which means `no_claim`.** That holds whatever the spacing between the events. Spacing alone never permits a claim, and tests fail if a spacing-only allow path comes back.
- With binding, the gate defers timing tolerance to the bound clock. It has no path that relies on the numbers below.

**Observation only, not a limit or threshold:**

- On these clips (one session, 3 clips), two independent ways of aligning clip time to the capture clock disagree by 120–719 ms. The two methods are segment-manifest interpolation and the checkpoint lock time.
- `OBSERVED_MAX_CLIP_CLOCK_SKEW_MS = 719` is the observed max skew (3 clips). `OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS = (120, 719)` is the observed range.
- `SKEW_MARGIN_FACTOR = 1.5` is a guess, not a bound.
- The gate reads none of these values. All of them get replaced once per-frame binding lands.

## Real-data check: match 170123 highlight clips (not committed)

The full 170123 recording is not off the capture PC. The only real frames used were the three public, hash-locked highlight clips (`site/matches/170123/clips/*.mp4`, 12 s each, 1280x720, 30 fps, re-encoded, with a burned caption). The sha256 of all three matches `clips/manifest.json`. The clips cover a field goal (away 3), a touchdown (away 9) and the extra point (away 10), so they are **not** a representative sample of play. The controller log is the `inputs.jsonl` from the match meta archive. The clips are aligned to the capture clock by interpolating `video.mp4` time onto the manifest segments' `mono_ms`. A second alignment derived from the lock disagrees by 120–719 ms (observed max skew, 3 clips), so per-bin stick alignment is approximate.

Units in the rate columns are %W/s.

| clip | bins | claimed | no_claim | no_claim reasons | \|yaw\| p50 / p90 / max | \|pitch\| p50 / p90 / max | est. ms/frame |
| --- | --- | --- | --- | --- | --- | --- | --- |
| hl00-q3-a3-h0 | 119 | 102 | 17 (14.3 %) | low_confidence 14, low_inlier_ratio 2, scene_cut 1 | 0.079 / 1.83 / 2.62 | 0.054 / 2.84 / 29.7 | 5.49 |
| hl01-q4-a9-h0 | 120 | 80 | 40 (33.3 %) | low_confidence 28, low_inlier_ratio 12 | 2.76 / 8.41 / 12.1 | 0.137 / 0.815 / 4.93 | 6.33 |
| hl02-q4-a10-h0 | 119 | 76 | 43 (36.1 %) | low_inlier_ratio 35, low_confidence 7, scene_cut 1 | 0.013 / 1.16 / 2.80 | 0.009 / 12.3 / 30.3 | 6.02 |

Totals: 358 bins, 100 `no_claim` (27.9 %). Every re-run was byte-identical.

The ms/frame column is estimation time on the box CPU, single thread. It includes the downscale from 720p and excludes decode, which took a further 1.3–4.4 ms/frame.

Spot checks of the largest bins match what is on screen:

- About +30 %W/s pitch on the kick-cam tilt following the ball up (hl00 at 1.9–2.5 s, hl02 at 5.4–6.1 s).
- About −12 %W/s yaw on a slow left pan in the end-zone celebration (hl01 at 6.6–7.7 s).

Running `pan-agreement/v0` against the real right stick over these windows:

- Confident moving bins (≥10 %W/s, conf ≥ 0.5): 1, 1, 4.
- Those bins with an idle logged right stick: 1, 1, 4.
- Flags: none, because each clip is under the 10-bin minimum.
- Right-stick-active rows in each window: 0/50, 45/615, 0/12.

This is a benign, referee-verified match, so a flag would have been a false alarm. Three clips is not a false-alarm rate. In these clips, camera motion came from the game's automatic cameras, not from the right stick.

## Limitations

- In Madden NFL 27 the right stick is not the camera control during normal play (it drives ball-carrier and defender moves). Camera pan does not imply right-stick input, so pan agreement is not a sound aimbot signal for Madden. An adapter's injected input never shows up in the USB HID log. A useful detector compares video against the log: an on-screen action (skill move, user catch, controlled-player motion) with no matching logged press.
- Global median translation does not separate camera motion from large moving foregrounds such as crowded player scrums. Those mostly end up as `no_claim` through the inlier and confidence gates. Zoom and roll are not modelled.
- The masks are static and measured on one scorebug skin plus the highlight caption. Other skins (primetime), replay wipes, and in-game banners are not masked.
- Thresholds were tuned on synthetic frames and the three clips above. They have not been validated on a labelled set.
