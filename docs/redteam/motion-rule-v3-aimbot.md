# Motion rule v3 red team: passthrough aim and log tamper

Test-only harness under `tests/redteam/`. It scores controller logs by calling `input_energy_series`, `score_motion`, `check_inputs`, `check_checkpoints`, and `check_input_files` in process. It does not sign a receipt, does not hold a referee key, and does not open a network connection. Nothing here is a submission.

The network guard a test can install around a run is a tripwire. It refuses network modules and a few process calls (socket, subprocess.Popen, os.system, os.spawn, ctypes) for the rest of that process.

Harness `redteam-aimbot/v2` for package `qor-receipt==0.1.0`. Harness sha256 `719a4d64de3516f893eb3f21daadfa1ee44ed756e8968414742455d0473f0a86` over 16 files.

The harness sha256 moved because `tests/redteam/scorers.py` and `tests/redteam/aimbot_harness.py` changed. It is still the package version plus the sorted in-repo Python files reachable from the harness. It does not include scorer settings.

## Headline: honest USB log, assisted screen

A passthrough adapter injects aim into the console. QorCap's USB log records the human and is sealed at capture, so chunk hashes and checkpoint `input_hash` match the logged rows. Input proof passes. The screen shows the injected aim. Motion rule v3 correlates row counts with frame-difference energy and does not read stick axes.

Stick-aimbot result for motion rule v3: **0/3** flagged (`honest-passthrough-aim`, `honest-blended-aim`, `honest-snap-aim`). The expected result on this rule is 0/3.

The captured game in view is Madden, where the right stick performs skill moves and hit-stick tackles rather than steering the camera. The two extra honest-seal rows use game-agnostic event kinds: `stick_action` (a right-stick gesture the log does not contain) and `button_action` (a button macro, such as a frame-perfect user catch, with no logged button).

| variant | ground truth | input rows | right-stick run (ms) | screen events | motion_permille | motion check | input proof |
| --- | --- | --- | --- | --- | --- | --- | --- |
| control | benign | 1559 | 191 | 0 | 682 | pass | pass |
| honest-passthrough-aim | attack | 1559 | 191 | 0 | 680 | pass | pass |
| honest-blended-aim | attack | 1559 | 191 | 0 | 661 | pass | pass |
| honest-snap-aim | attack | 1559 | 191 | 0 | 681 | pass | pass |
| honest-stick-action | attack | 1559 | 191 | 4 | 674 | pass | pass |
| honest-button-macro | attack | 1559 | 191 | 4 | 671 | pass | pass |
| honest-full-idle | attack | 0 | 0 | 0 | -1000 | partial | pass |

`honest-full-idle` is the row-count case the rule does catch: an honestly sealed empty log while the control screen still moves. Its check is `partial`, which is not a FAIL. The stick-aimbot and macro rows stay `pass`.

## Fixture parameters

`tests/review_fixture.py` centers every stick, so this base is synthetic. It is 60 one-second bins, not a decoded capture and not a recording of a player. `mix(sec, salt)` is a 16-bit mix: start from `((sec + 1) * 2246822519 + salt * 3266489917)`, xor-shift by 16, multiply by `0x85EBCA6B`, xor-shift by 13, keep 16 bits.

Idle seconds 0–7, 46–53: 1–4 rows, frame-difference energy 4–28. Menu seconds 20–31, 54–59: 22–41 rows (button navigation) and energy 5–34, so the picture is quiet while the log is busy. Play seconds are the rest: latent `15 + mix(sec, 1) % 55`, rows `12 + latent // 2 + mix(sec, 6) % 9`, energy `180 + latent * 7 + mix(sec, 7) % 160 - mix(sec, 8) % 80` with a floor of 40. Play shares the latent across rows and energy. Menu and idle do not. The control permille is above the shipped floor of 300 and below 1000.

Honest right-stick skill gestures last 200 ms starting at offset 400 on play seconds divisible by 5. Buttons are logged on menu rows (`k % 3 == 0`) and on play seconds divisible by 4 (`k % 8 == 0`). `honest-stick-action` places 4 `stick_action` events at offset 500 where the ±150 ms window has no off-center right stick, and adds 150 energy in those seconds. `honest-button-macro` does the same for `button_action` with no logged button and adds 140 energy.

Honest aim screens, still on the human log: passthrough adds `30 + mix(sec, 9) % 50` energy on play seconds; blended replaces energy with `0.80 * human + 0.20 * max(0, 200 + 100 * sin(sec / 7))`; snap adds 130 energy on play seconds divisible by 4.

## Log-tamper test

These variants rewrite the human rows and leave the human chunk hashes and checkpoint `input_hash` values in place. Input proof fails. The permille column is `score_motion` on the rewritten rows anyway. Stick rewrites keep the row count, so they score like the control. Deleting the rows is the flat series.

| variant | ground truth | input rows | right-stick run (ms) | screen events | motion_permille | motion check | input proof |
| --- | --- | --- | --- | --- | --- | --- | --- |
| tamper-idle-right-stick | attack | 1559 | 0 | 0 | 682 | pass | fail |
| tamper-blended-assist | attack | 1559 | 11232 | 0 | 682 | pass | fail |
| tamper-snap-flicks | attack | 1559 | 189 | 0 | 682 | pass | fail |
| tamper-full-idle | attack | 0 | 0 | 0 | -1000 | partial | fail |

## Variant index

| variant | cohort | what the fixture does |
| --- | --- | --- |
| control | honest-seal | honest human log and the matching screen |
| honest-passthrough-aim | honest-seal | log is the human USB capture; play seconds gain adapter aim the log does not contain |
| honest-blended-aim | honest-seal | log is the human capture; screen energy is 80% human plus 20% of a smooth aim target |
| honest-snap-aim | honest-seal | log is the human capture; some play seconds absorb a sub-300 ms aim jump |
| honest-stick-action | honest-seal | on-screen stick_action bursts (Madden: skill move / hit stick) with no logged right-stick press |
| honest-button-macro | honest-seal | on-screen button_action events (Madden: user catch or any button macro) with no logged button |
| honest-full-idle | honest-seal | honest empty log, sealed on its own, while the control screen still moves |
| tamper-idle-right-stick | log-tamper | rows rewritten so the right stick is centered; seals left on the human log |
| tamper-blended-assist | log-tamper | right-stick values rewritten to 40% human plus 60% target; seals left in place |
| tamper-snap-flicks | log-tamper | right stick rewritten to a sub-300 ms jump; seals left in place |
| tamper-full-idle | log-tamper | controller rows deleted after sealing; seals still describe the human log |

## Pluggable scorers

A scorer implements `name`, `version`, `score(sample) -> Score`, and `identity()`. `Score.flagged` is true, false, or null (abstain). A scorer may compare 100 ms pan bins with the right stick, or video events with logged presses. Both outputs are plug-ins. This report uses stubs that mark every frame and every second `no_claim`.

Pan bins are `{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}`. Events are `{mono_ms, event_kind, confidence, no_claim}`. `no_claim` leaves the measurements null. Non-finite rates and confidence, and confidence outside 0..1, are rejected.

Catch rate is flagged attacks divided by decided attacks. False-alarm rate is flagged benign divided by decided benign. An abstention is neither. The rates below are the honest-seal cohort, which is the traffic input proof would let through. Log-tamper rates are in the JSON block.

Each scorer sha256 is the hash of `qor-receipt==0.1.0`, the scorer and estimator version strings, the canonical JSON of runtime settings (such as `min_confidence` and any thresholds passed at construction), an estimator sha256 only when the class object is OpticalFlowPanEstimator, recomputed from that module's source (a copied attribute, property, __module__, or __qualname__ is not trusted), the sorted repo-relative path plus bytes of the scorer module and its transitive in-repo imports, the source of an injected estimator or `core` (a hash of that class's own source, and, outside the repo, a path relative to the module root), and declared non-Python weight, model, or data files hashed by bytes. A file that cannot be read is recorded as `unhashed` and is not dropped. An implementation whose file cannot be resolved or read makes the identity `unhashed`, and an unhashed result is not cached. The hashes in the table moved because that coverage is new and because `tests/redteam/scorers.py` changed.

**SYNTHETIC-DATA CAVEAT:** these rates come from synthetic data with only one benign sample (n=1 control) and are NOT real-world or measured detection/false-alarm rates; '0/1 false alarms' is not a false-alarm rate.

The 150 ms event window is valid only on synthetic data with a shared clock. On real footage, no video-vs-log mismatch claim is ever allowed: the mismatch claim gate has no allow path, so every mismatch claim is no-claim with ceiling PARTIAL, for every bundle, and older footage stays no-claim. A frame_bind chain (qorcap #75, --frame-bind) that verifies proves frames were not altered after recording, not press-to-frame timing. An allow path may come only in a separate reviewed PR that verifies the bundle itself; even then a mismatch signal may lower a verdict to at most PARTIAL and never raises one.

| scorer | version | package | estimator | file set | sha256 | headline catch rate | headline false-alarm rate | abstained |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| motion-rule-v3 | motion-rule/v3 | 0.1.0 | — | 15 files | ff6b686b6b334b4a74b191c7715a6d4dc3d5793ac2421d3ff99baec45b705b18 | 1/6 (0.17) | 0/1 (0.00) | 0/7 |
| pan-agreement | pan-agreement/v0 | 0.1.0 | example-no-claim/v0 | 15 files | 92a7a6ee5bbd6ed0be7b7b62b5235cd6cc657d32ba412d806dd316b9e4332202 | n/a (0/6 decided) | n/a (0/1 decided) | 7/7 |
| event-press-agreement | event-press-agreement/v0 | 0.1.0 | example-no-claim-events/v0 | 15 files | 9df253d5606492378ac5a3aba6884e8f1aae388db75b75a4e1ea2f9ef4aac58a | n/a (0/6 decided) | n/a (0/1 decided) | 7/7 |

| variant | cohort | motion-rule-v3 | pan-agreement | event-press-agreement |
| --- | --- | --- | --- | --- |
| control | honest-seal | clear | abstain | abstain |
| honest-passthrough-aim | honest-seal | clear | abstain | abstain |
| honest-blended-aim | honest-seal | clear | abstain | abstain |
| honest-snap-aim | honest-seal | clear | abstain | abstain |
| honest-stick-action | honest-seal | clear | abstain | abstain |
| honest-button-macro | honest-seal | clear | abstain | abstain |
| honest-full-idle | honest-seal | flag | abstain | abstain |
| tamper-idle-right-stick | log-tamper | clear | abstain | abstain |
| tamper-blended-assist | log-tamper | clear | abstain | abstain |
| tamper-snap-flicks | log-tamper | clear | abstain | abstain |
| tamper-full-idle | log-tamper | flag | abstain | abstain |

`flag` on `motion-rule-v3` is the `partial` check. `clear` is `pass`. `abstain` means the scorer declined. The stubs decline because they never claim a frame or an event.

## Reproducing

```bash
pytest -m known_gap -s tests/test_redteam_aimbot_motion.py
```

Put `tests` on `PYTHONPATH` before importing the harness yourself (the pytest module does this):

```python
from redteam.aimbot_harness import evaluate
from redteam.scorers import EventPressAgreementScorer, PanAgreementScorer

report = evaluate(scorers=[PanAgreementScorer(MyPanEstimator()), EventPressAgreementScorer(MyEventEstimator())])
```

`evaluate` keeps `motion-rule-v3` in the list when the caller does not pass it.

```json
{
  "harness": {
    "files": [
      "qor_receipt/__init__.py",
      "qor_receipt/_vendor/qoresence/sync/digit_integrity.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
      "qor_receipt/agent/pause_detect.py",
      "qor_receipt/challenge.py",
      "qor_receipt/final_detect.py",
      "qor_receipt/jcs.py",
      "qor_receipt/review/bundle.py",
      "qor_receipt/review/media.py",
      "qor_receipt/review/motion.py",
      "qor_receipt/review/policy.py",
      "tests/redteam/aimbot_harness.py",
      "tests/redteam/scorers.py"
    ],
    "package_version": "0.1.0",
    "sha256": "719a4d64de3516f893eb3f21daadfa1ee44ed756e8968414742455d0473f0a86",
    "version": "redteam-aimbot/v2"
  },
  "headline_stick_aimbots": {
    "flagged_by_motion_rule_v3": 0,
    "names": [
      "honest-passthrough-aim",
      "honest-blended-aim",
      "honest-snap-aim"
    ],
    "of": 3
  },
  "scorers": [
    {
      "estimator": null,
      "estimator_version": null,
      "files": [
        "qor_receipt/__init__.py",
        "qor_receipt/_vendor/qoresence/sync/digit_integrity.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
        "qor_receipt/agent/pause_detect.py",
        "qor_receipt/challenge.py",
        "qor_receipt/final_detect.py",
        "qor_receipt/jcs.py",
        "qor_receipt/review/bundle.py",
        "qor_receipt/review/media.py",
        "qor_receipt/review/motion.py",
        "qor_receipt/review/policy.py",
        "tests/redteam/scorers.py"
      ],
      "headline_abstained": 0,
      "headline_catch_rate": 0.16666666666666666,
      "headline_decided_attacks": 6,
      "headline_false_alarm_rate": 0.0,
      "headline_false_positives": 0,
      "headline_true_positives": 1,
      "log_tamper_catch_rate": 0.25,
      "log_tamper_decided_attacks": 4,
      "log_tamper_true_positives": 1,
      "name": "motion-rule-v3",
      "package_version": "0.1.0",
      "sha256": "ff6b686b6b334b4a74b191c7715a6d4dc3d5793ac2421d3ff99baec45b705b18",
      "version": "motion-rule/v3"
    },
    {
      "estimator": "example-no-claim",
      "estimator_version": "example-no-claim/v0",
      "files": [
        "qor_receipt/__init__.py",
        "qor_receipt/_vendor/qoresence/sync/digit_integrity.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
        "qor_receipt/agent/pause_detect.py",
        "qor_receipt/challenge.py",
        "qor_receipt/final_detect.py",
        "qor_receipt/jcs.py",
        "qor_receipt/review/bundle.py",
        "qor_receipt/review/media.py",
        "qor_receipt/review/motion.py",
        "qor_receipt/review/policy.py",
        "tests/redteam/scorers.py"
      ],
      "headline_abstained": 7,
      "headline_catch_rate": null,
      "headline_decided_attacks": 0,
      "headline_false_alarm_rate": null,
      "headline_false_positives": 0,
      "headline_true_positives": 0,
      "log_tamper_catch_rate": null,
      "log_tamper_decided_attacks": 0,
      "log_tamper_true_positives": 0,
      "name": "pan-agreement",
      "package_version": "0.1.0",
      "sha256": "92a7a6ee5bbd6ed0be7b7b62b5235cd6cc657d32ba412d806dd316b9e4332202",
      "version": "pan-agreement/v0"
    },
    {
      "estimator": "example-no-claim-events",
      "estimator_version": "example-no-claim-events/v0",
      "files": [
        "qor_receipt/__init__.py",
        "qor_receipt/_vendor/qoresence/sync/digit_integrity.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
        "qor_receipt/agent/pause_detect.py",
        "qor_receipt/challenge.py",
        "qor_receipt/final_detect.py",
        "qor_receipt/jcs.py",
        "qor_receipt/review/bundle.py",
        "qor_receipt/review/media.py",
        "qor_receipt/review/motion.py",
        "qor_receipt/review/policy.py",
        "tests/redteam/scorers.py"
      ],
      "headline_abstained": 7,
      "headline_catch_rate": null,
      "headline_decided_attacks": 0,
      "headline_false_alarm_rate": null,
      "headline_false_positives": 0,
      "headline_true_positives": 0,
      "log_tamper_catch_rate": null,
      "log_tamper_decided_attacks": 0,
      "log_tamper_true_positives": 0,
      "name": "event-press-agreement",
      "package_version": "0.1.0",
      "sha256": "9df253d5606492378ac5a3aba6884e8f1aae388db75b75a4e1ea2f9ef4aac58a",
      "version": "event-press-agreement/v0"
    }
  ],
  "variants": [
    {
      "cohort": "honest-seal",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 1559,
      "label": "benign",
      "motion_check": "pass",
      "motion_permille": 682,
      "name": "control",
      "right_stick_run_ms": 191,
      "screen_events": 0
    },
    {
      "cohort": "honest-seal",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 680,
      "name": "honest-passthrough-aim",
      "right_stick_run_ms": 191,
      "screen_events": 0
    },
    {
      "cohort": "honest-seal",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 661,
      "name": "honest-blended-aim",
      "right_stick_run_ms": 191,
      "screen_events": 0
    },
    {
      "cohort": "honest-seal",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 681,
      "name": "honest-snap-aim",
      "right_stick_run_ms": 191,
      "screen_events": 0
    },
    {
      "cohort": "honest-seal",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 674,
      "name": "honest-stick-action",
      "right_stick_run_ms": 191,
      "screen_events": 4
    },
    {
      "cohort": "honest-seal",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 671,
      "name": "honest-button-macro",
      "right_stick_run_ms": 191,
      "screen_events": 4
    },
    {
      "cohort": "honest-seal",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "flag",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 0,
      "label": "attack",
      "motion_check": "partial",
      "motion_permille": -1000,
      "name": "honest-full-idle",
      "right_stick_run_ms": 0,
      "screen_events": 0
    },
    {
      "cohort": "log-tamper",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 682,
      "name": "tamper-idle-right-stick",
      "right_stick_run_ms": 0,
      "screen_events": 0
    },
    {
      "cohort": "log-tamper",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 682,
      "name": "tamper-blended-assist",
      "right_stick_run_ms": 11232,
      "screen_events": 0
    },
    {
      "cohort": "log-tamper",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 1559,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 682,
      "name": "tamper-snap-flicks",
      "right_stick_run_ms": 189,
      "screen_events": 0
    },
    {
      "cohort": "log-tamper",
      "decisions": {
        "event-press-agreement": "abstain",
        "motion-rule-v3": "flag",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 0,
      "label": "attack",
      "motion_check": "partial",
      "motion_permille": -1000,
      "name": "tamper-full-idle",
      "right_stick_run_ms": 0,
      "screen_events": 0
    }
  ]
}
```
