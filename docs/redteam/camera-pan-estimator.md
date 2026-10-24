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

Every bin is `no_claim` with reason `unknown_estimator_hash` unless `estimator_known()` is True (see below).

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
- `cv2.useOptimized()`;
- `numpy.__version__`;
- the module source.

Different OpenCV builds or numpy versions therefore report different hashes. The same hash does **not** guarantee the same output on every host, because OpenCV picks SIMD code paths at runtime from the host CPU. `identity()["opencv"]` records the identity fields, and `identity()["opencv_pinned"]` reports `opencv_matches_pin()`. Tests check that:

- two runs give byte-identical JSONL;
- changing any identity field changes the hash;
- the installed OpenCV matches the pin (it fails on any other build);
- golden output hashes on fixed synthetic frames hold, so output drift fails CI even when the hash inputs look the same.

### Pinned OpenCV

The tested wheel is **`opencv-python-headless==5.0.0.93`** (OpenCV 5.0.0, `cp37-abi3-manylinux_2_28_x86_64`). Its `sha256(getBuildInformation())` is `0ef08003c31127b3b5d34410ab13483c511e73b99e278b1a8abd6ddcb265699a`.

- The pin lives in `tests/constraints-pan-estimator.txt` and in the `dev` extra of `pyproject.toml`. CI already installs `.[dev,vision]`, so it gets the pin, and a test checks that the two places agree.
- To install it locally: `pip install -e ".[dev,vision]" -c tests/constraints-pan-estimator.txt`.
- The live referee image (`services/referee/Dockerfile`) installs `.[referee,vision]`. Neither extra carries the numpy pin or the OpenCV pin. On that image `estimator_known()` is false: the process reports an unknown hash, and `estimate_frames` is `no_claim`.
- numpy is pinned to **`numpy==2.4.6`** in the dev extra and `tests/constraints-pan-estimator.txt`. 2.4.6 has wheels for both CI interpreters (CPython 3.11 and 3.13), so both report one estimator hash. Before the pin, CI resolved numpy 2.4.6 on 3.11 and 2.5.4 on 3.13 (numpy 2.5 dropped 3.11), and the hashes differed. The vision and referee extras keep `numpy>=1.26`.

### Known-good hash allowlist (fail closed)

`qor_receipt/vision/estimator_allowlist.py` holds `KNOWN_ESTIMATOR_SHA256`, a fixed `frozenset` of exact 64-character lowercase hex digests. It is a separate file because the hash covers `pan_estimator.py`'s own source. It currently holds one entry, recorded on CPython 3.11 and 3.13 with numpy 2.4.6, `opencv-python-headless==5.0.0.93`, and `cv2.useOptimized()` True:

`c33339f29b1c8de21439c4d51c7b1cbf93ffee21062e1e0a88f25bd073d321f1`

How the check works:

- `estimator_known()` recomputes the hash in-process. It never takes a hash from the caller.
- It is True only when that hash is in the set **and** `opencv_matches_pin()` is True. It is False on any exception, or if the allowlist is not a frozenset of lowercase hex.
- `identity()` reports `estimator_known` separately from `opencv_pinned`.
- Any other numpy, OpenCV version or build, `useOptimized` setting, parameters, or edited source makes `estimate_frames` return only `no_claim` bins with reason `unknown_estimator_hash`.
- Being known unlocks nothing: the mismatch gate still has no allow path.
- Same-process patching cannot be fully blocked. The check recomputes the hash when it is called. Patching numpy, OpenCV, or the allowlist in the same process, or assigning `__class__` on a gate result, is outside what this check seals. A `__class__` swap is not treated as impossible.

Tests check that:

- the running hash is listed (this runs in both CI jobs);
- the set is a frozenset of lowercase hex;
- patching the numpy version, OpenCV version, OpenCV build, or `useOptimized` gives False and all `no_claim`;
- a tampered allowlist or an exception gives False.

The current hash is the allowlist entry above. Earlier revisions reported:

- `e225c50d…`, before the OpenCV identity was hashed;
- `dcb9d149…`, before the clock gate;
- `2d236634…`, before the per-bundle frame_bind gate;
- `b0c8f40d…`, before the status, coverage, and encode-latency conditions;
- `b85405bd…`, before the gate moved out of this module and numpy and `useOptimized` joined the hash;
- `c5e8ddfd…` (CPython 3.13, numpy 2.5.4), before the allowlist check and the numpy pin;
- `8f049000…`, before `pairs` / `bin_pairs` fail closed on an unknown hash and `estimator_known` requires an exact `PanParams`.

The per-bin outputs on the three clips are byte-identical across all of them.

## Harness plug-in

`tests/redteam/optical_flow.py` adapts the estimator to #67's `CameraPanEstimator` protocol. The harness's `VariantSample` carries no pixels, so the adapter takes a frame source. When the estimator has a `sha256`, `source_identity` folds it in and records it as `estimator_sha256`.

That identity is caller-reported and is not verified. QorProof's E1–E3 show an estimator can claim any sha256, and D1 shows the adapter cache is shared across frame sources. Those tests are in `tests/test_mismatch_gate_qorproof.py` as `xfail(strict=True)`. QorVolution Bot's follow-up PR fixes that code; this PR does not touch it.

The committed red-team report (`motion-rule-v3-aimbot.md`) still uses the no_claim stubs. A test plugs this estimator in on synthetic frames and checks only the contract, the identity, and determinism. No catch rate is asserted or published, because pan versus right stick is not a valid Madden aimbot signal (see below).

Before the allowlist check, the box (Python 3.13, numpy 2.5.4, the pinned wheel) reported `c5e8ddfdcae2e20a12cd77b87e520abbf73cc20af1b146768eea895b2c5b695f`. That numpy is not the pin, so the hash is unknown. Earlier revisions reported:

- `e225c50d…`, before the OpenCV identity was hashed;
- `dcb9d149…`, before the clock gate;
- `2d236634…`, before the per-bundle frame_bind gate;
- `b0c8f40d…`, before the status, coverage, and encode-latency conditions;
- `b85405bd…`, before the gate moved out of this module and numpy and `useOptimized` joined the hash.

The per-bin outputs on the three clips are byte-identical across all of them.

## Claim gate for video-vs-log detectors: no allow path

`qor_receipt/vision/mismatch_gate.py` holds the gate. It is a separate module, so gate edits do not change the estimator hash.

```python
mismatch_claim_allowed(frame_bind_status, *, coverage, align_gaps, encode_latency, video_ms, log_ms) -> MismatchGateResult
```

Any video-vs-log mismatch detector must call it. Examples are a skill move, a user catch, or controlled-player motion with no matching logged press.

**The result always has `allowed=False` and `ceiling="PARTIAL"`, for every input.**

- `MismatchGateResult` is frozen, its `allowed` is a read-only property, and the object is falsy.
- The class is final. A metaclass rejects a subclass, including a mixin whose `__init_subclass__` does not call `super()`. A same-process `__class__` assignment is not blocked. Equality and hashing require the exact type on both sides, so a plain tuple or a look-alike never compares equal.
- `ceiling`, `repr` and `to_dict()` use the literal `"PARTIAL"`. Patching the module's `CEILING` name changes nothing.
- The reason is the single item of an immutable tuple. There are no slots and no `__dict__`, so `object.__setattr__` cannot change it, and it is re-validated on every read.
- `copy`, `deepcopy` and `pickle` round-trip through `__reduce__`, which rebuilds the object via `__new__` and re-validates the reason, so a forged pickle with an unknown reason is rejected.
- `reason` is diagnostic only. A caller must never branch on it to raise a tier: `no_bundle_verifier` means no verifier exists, not "passed".
- The gate never trusts a caller-built status, coverage, or calibration. Anyone can construct those, so they prove nothing about a bundle.
- The inputs are still validated so that `reason` says which condition a future allow path would have failed first. Reasons are checked in this order: `frame_bind_not_verified`, `encode_latency_calibration_missing` / `_invalid`, `video_time_missing`, `log_time_missing`, `event_time_invalid`, `coverage_missing` / `_invalid`, `align_gaps_invalid`, `event_in_align_gap`, `coverage_overlaps_align_gap`, `event_outside_coverage`. If all inputs look well formed, the reason is `no_bundle_verifier`.

Validation uses exact types:

- **Status.** Only `type(s) is str and s == "verified"` gets past the first check. Enums (the old `FrameBindStatus` was removed), `str` subclasses, bytes, look-alikes, and objects with custom `__eq__` all give `frame_bind_not_verified`.
- **Calibration.** It must be an exact `EncodeLatencyCalibration` with a non-empty `str` source, an exact `int` sample count of at least 30, and exact finite `int`/`float` latencies with 0 < p50 ≤ p99 ≤ 2000 ms. Subclasses, bools, and dicts fail. The 30-sample and 2000 ms bounds are guesses that only pick a reason code.
- **Times.** `video_ms` and `log_ms` must be exact non-negative `int`s. `log_ms=None` is `no_claim`.
- **Coverage and `align_gaps`.** Each must be an exact list or tuple of exact-`int` half-open `[start, end)` pairs, with `0 ≤ start < end`, sorted, and non-overlapping. An event inside an `align_gap` is `no_claim` even when a caller's coverage spans the gap.

Tests:

- one fixture per subclass trick;
- a hand-built object with `status="verified"` and a made-up digest;
- a parametrized sweep of 216,000 input combinations that never yields `allowed=True` (hypothesis is not a dev dependency);
- a check that the gate source has no `return True`;
- an AST scan of every Python file in the repo, except the gate and its tests, for imports, names, attributes, `importlib`/`__import__` calls with a `qor_receipt.vision` string, and strings naming the gate.

**What a real allow path needs. Each piece is missing today:**

1. **The bundle itself.** QorArch's schemas PR (not on main) exposes `verify_frame_bind(bundle)` as the entry point. Its `FrameBindResult` is only a return value and proves nothing on its own. The future gate takes bundle bytes or a path and calls `verify_frame_bind` itself, so the digest is recomputed inside the gate. Nothing here builds against it until it is on main. Frame binding (qorcap PR 75 design: `frames.jsonl` plus a running hash in checkpoints) proves that frames were not altered or reordered after recording. **It does not prove press-to-frame timing or alignment.**
2. **A bundle-bound, verifiable encode-latency calibration.** Frame `mono_ms` is stamped after encoding and pipe buffering, while HID rows are stamped at the USB read. Every press-to-frame gap therefore includes encode latency that has not been measured.
3. **Coverage and align_gaps taken from the verified frame_bind result,** not from the caller.

An allow path may return only in a separate reviewed PR that adds all three. Even then a mismatch signal may lower a verdict to at most PARTIAL (a lower verdict stays as it is); it never raises one.

**Observation only, not a limit or threshold:**

- On these clips (one session, 3 clips), two independent ways of aligning clip time to the capture clock disagree by 120–719 ms. The two methods are segment-manifest interpolation and the checkpoint lock time.
- `OBSERVED_MAX_CLIP_CLOCK_SKEW_MS = 719` is the observed max skew (3 clips). `OBSERVED_CLIP_CLOCK_SKEW_RANGE_MS = (120, 719)` is the observed range.
- `SKEW_MARGIN_FACTOR = 1.5` is a guess, not a bound.
- These constants are in `mismatch_gate.py`, and the gate reads none of them. They get replaced once bundles carry a verified frame_bind result and a verified encode-latency calibration.

## Real-data check: match 170123 highlight clips

The full 170123 recording is not off the capture PC. The only real frames used were the three public, hash-locked highlight clips (`site/matches/170123/clips/*.mp4`, 12 s each, 1280x720, 30 fps, re-encoded, with a burned caption). The sha256 of all three matches `clips/manifest.json`. The clips cover a field goal (away 3), a touchdown (away 9) and the extra point (away 10), so they are **not** a representative sample of play.

`python scripts/pan_estimate_clips.py` reproduces this table exactly from the repo, on the pinned wheel. It runs at clip-relative time: offset 0, with the first bin at 0 ms. Units in the rate columns are %W/s.

| clip | bins | claimed | no_claim | no_claim reasons | \|yaw\| p50 / p90 / max | \|pitch\| p50 / p90 / max |
| --- | --- | --- | --- | --- | --- | --- |
| hl00-q3-a3-h0 | 120 | 103 | 17 (14.2 %) | low_confidence 14, low_inlier_ratio 2, scene_cut 1 | 0.075 / 1.83 / 2.62 | 0.046 / 2.84 / 29.7 |
| hl01-q4-a9-h0 | 120 | 80 | 40 (33.3 %) | low_confidence 29, low_inlier_ratio 11 | 2.8 / 8.94 / 12.5 | 0.137 / 0.827 / 4.93 |
| hl02-q4-a10-h0 | 120 | 77 | 43 (35.8 %) | low_inlier_ratio 35, low_confidence 7, scene_cut 1 | 0.013 / 0.615 / 2.8 | 0.006 / 8.19 / 30.3 |

Totals: 360 bins, 100 `no_claim` (27.8 %). The script also prints each clip's bins sha256:

- hl00: `ebffa3da36d196ea3613ef116ac96d6e5133c74fc8360dbca70c813943fab1c0`
- hl01: `73f60d1c47948c8db2ea429f53ae5fa2a70bc2d929bfb98361c269dbd3ca81b6`
- hl02: `d47d2f103d12b13d61700758b6d1a33730961b7f144743924aaaa4fb12f44f4b`

The earlier version of this table had 119/120/119 bins. It ran on capture-clock time instead: each clip was shifted by its interpolated `mono_ms` offset, and the first partial 100 ms bin was dropped. The shift moves every bin boundary, so the counts and percentiles differ slightly from the clip-relative ones above. That alignment needs the match meta archive, which is not in the repo, so that table could not be reproduced from the repo. It was replaced.

Spot checks of the largest bins match what is on screen:

- About +30 %W/s pitch on the kick-cam tilt following the ball up (hl00 at 1.9–2.5 s, hl02 at 5.4–6.1 s).
- About −12 %W/s yaw on a slow left pan in the end-zone celebration (hl01 at 6.6–7.7 s).

### Box-only, not reproducible from the repo

The numbers in this subsection need the match meta archive's `inputs.jsonl` and segment manifest, which are not committed. They are capture-clock aligned (segment interpolation; the lock-derived alignment disagrees by 120–719 ms, observed max skew, 3 clips), so per-bin stick alignment is approximate.

Running `pan-agreement/v0` against the real right stick over these windows:

- Confident moving bins (≥10 %W/s, conf ≥ 0.5): 1, 1, 4.
- Those bins with an idle logged right stick: 1, 1, 4.
- Flags: none, because each clip is under the 10-bin minimum.
- Right-stick-active rows in each window: 0/50, 45/615, 0/12.

Estimation took about 5.5–6.3 ms/frame on the box CPU, single thread, including the downscale from 720p. Decode took a further 1.3–4.4 ms/frame.

This is a benign, referee-verified match, so a flag would have been a false alarm. Three clips is not a false-alarm rate. In these clips, camera motion came from the game's automatic cameras, not from the right stick.

## Limitations

- In Madden NFL 27 the right stick is not the camera control during normal play (it drives ball-carrier and defender moves). Camera pan does not imply right-stick input, so pan agreement is not a sound aimbot signal for Madden. An adapter's injected input never shows up in the USB HID log. A useful detector compares video against the log: an on-screen action (skill move, user catch, controlled-player motion) with no matching logged press.
- Global median translation does not separate camera motion from large moving foregrounds such as crowded player scrums. Those mostly end up as `no_claim` through the inlier and confidence gates. Zoom and roll are not modelled.
- The masks are static and measured on one scorebug skin plus the highlight caption. Other skins (primetime), replay wipes, and in-game banners are not masked.
- Thresholds were tuned on synthetic frames and the three clips above. They have not been validated on a labelled set.
