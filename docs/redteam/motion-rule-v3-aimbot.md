# Motion rule v3 red team: passthrough aim and log tamper

Test-only harness under `tests/redteam/`. It scores controller logs by calling `input_energy_series`, `score_motion`, `check_inputs`, `check_checkpoints`, and `check_input_files` in process. It does not sign a receipt, does not hold a referee key, and does not open a network connection. Nothing here is a submission.

Harness `redteam-aimbot/v2` for package `qor-receipt==0.1.0`. Harness sha256 `eddafd17630910abfe52e386ffdfa90438497168ede2704d4b0f9da1552cf233` over 25 files.

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

Each scorer sha256 is the hash of `qor-receipt==0.1.0`, the scorer and estimator version strings, and the sorted repo-relative path plus bytes of the scorer module and its transitive in-repo imports.

| scorer | version | package | estimator | file set | sha256 | headline catch rate | headline false-alarm rate | abstained |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| motion-rule-v3 | motion-rule/v3 | 0.1.0 | — | 24 files | 1bc0c4f7bf12798ceeab5d59ef8f04ff3633795113f5cf2523a379eb40bc349b | 1/6 (0.17) | 0/1 (0.00) | 0/7 |
| pan-agreement | pan-agreement/v0 | 0.1.0 | example-no-claim/v0 | 24 files | 8b79baaaec5b07275bf2fdd88d72cf0f0a04f0a523b58946bcc6b58f4fa5c2e5 | n/a (0/6 decided) | n/a (0/1 decided) | 7/7 |
| event-press-agreement | event-press-agreement/v0 | 0.1.0 | example-no-claim-events/v0 | 24 files | e9eea631f81ac8d0fb7fce9a692db5b74f3e54595bd372955132b6a2b1a9e869 | n/a (0/6 decided) | n/a (0/1 decided) | 7/7 |

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
      "qor_receipt/_vendor/qoresence/truth/signing.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
      "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
      "qor_receipt/agent/pause_detect.py",
      "qor_receipt/challenge.py",
      "qor_receipt/checkpoints.py",
      "qor_receipt/delegation.py",
      "qor_receipt/final_detect.py",
      "qor_receipt/jcs.py",
      "qor_receipt/keys.py",
      "qor_receipt/receipt.py",
      "qor_receipt/review/bundle.py",
      "qor_receipt/review/corroboration.py",
      "qor_receipt/review/device_attest.py",
      "qor_receipt/review/media.py",
      "qor_receipt/review/motion.py",
      "qor_receipt/review/policy.py",
      "qor_receipt/review/score_timeline.py",
      "qor_receipt/trusted_referees.py",
      "tests/redteam/aimbot_harness.py",
      "tests/redteam/scorers.py"
    ],
    "package_version": "0.1.0",
    "sha256": "eddafd17630910abfe52e386ffdfa90438497168ede2704d4b0f9da1552cf233",
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
        "qor_receipt/_vendor/qoresence/truth/signing.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
        "qor_receipt/agent/pause_detect.py",
        "qor_receipt/challenge.py",
        "qor_receipt/checkpoints.py",
        "qor_receipt/delegation.py",
        "qor_receipt/final_detect.py",
        "qor_receipt/jcs.py",
        "qor_receipt/keys.py",
        "qor_receipt/receipt.py",
        "qor_receipt/review/bundle.py",
        "qor_receipt/review/corroboration.py",
        "qor_receipt/review/device_attest.py",
        "qor_receipt/review/media.py",
        "qor_receipt/review/motion.py",
        "qor_receipt/review/policy.py",
        "qor_receipt/review/score_timeline.py",
        "qor_receipt/trusted_referees.py",
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
      "sha256": "1bc0c4f7bf12798ceeab5d59ef8f04ff3633795113f5cf2523a379eb40bc349b",
      "version": "motion-rule/v3"
    },
    {
      "estimator": "example-no-claim",
      "estimator_version": "example-no-claim/v0",
      "files": [
        "qor_receipt/__init__.py",
        "qor_receipt/_vendor/qoresence/sync/digit_integrity.py",
        "qor_receipt/_vendor/qoresence/truth/signing.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
        "qor_receipt/agent/pause_detect.py",
        "qor_receipt/challenge.py",
        "qor_receipt/checkpoints.py",
        "qor_receipt/delegation.py",
        "qor_receipt/final_detect.py",
        "qor_receipt/jcs.py",
        "qor_receipt/keys.py",
        "qor_receipt/receipt.py",
        "qor_receipt/review/bundle.py",
        "qor_receipt/review/corroboration.py",
        "qor_receipt/review/device_attest.py",
        "qor_receipt/review/media.py",
        "qor_receipt/review/motion.py",
        "qor_receipt/review/policy.py",
        "qor_receipt/review/score_timeline.py",
        "qor_receipt/trusted_referees.py",
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
      "sha256": "8b79baaaec5b07275bf2fdd88d72cf0f0a04f0a523b58946bcc6b58f4fa5c2e5",
      "version": "pan-agreement/v0"
    },
    {
      "estimator": "example-no-claim-events",
      "estimator_version": "example-no-claim-events/v0",
      "files": [
        "qor_receipt/__init__.py",
        "qor_receipt/_vendor/qoresence/sync/digit_integrity.py",
        "qor_receipt/_vendor/qoresence/truth/signing.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/agreement.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/glyphs.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/profile.py",
        "qor_receipt/_vendor/qoresence/vision/local_scorebug/reader.py",
        "qor_receipt/agent/pause_detect.py",
        "qor_receipt/challenge.py",
        "qor_receipt/checkpoints.py",
        "qor_receipt/delegation.py",
        "qor_receipt/final_detect.py",
        "qor_receipt/jcs.py",
        "qor_receipt/keys.py",
        "qor_receipt/receipt.py",
        "qor_receipt/review/bundle.py",
        "qor_receipt/review/corroboration.py",
        "qor_receipt/review/device_attest.py",
        "qor_receipt/review/media.py",
        "qor_receipt/review/motion.py",
        "qor_receipt/review/policy.py",
        "qor_receipt/review/score_timeline.py",
        "qor_receipt/trusted_referees.py",
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
      "sha256": "e9eea631f81ac8d0fb7fce9a692db5b74f3e54595bd372955132b6a2b1a9e869",
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
