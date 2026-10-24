# Motion rule v3 red team: aimbot-style controller logs

Test-only harness under `tests/redteam/`. It scores rewritten controller logs by calling `input_energy_series`, `score_motion`, `check_inputs`, `check_checkpoints`, and `check_input_files` in process. It does not sign a receipt, does not hold a referee key, and does not open a network connection. Nothing in this directory is a submission.

Harness `redteam-aimbot/v1` sha256 `1261f00ca80d24955aea782858a64857df9e8c6b4bb8586e84e8ecf05414150d`.

## Fixture

`tests/review_fixture.py` is the repo's controller-plus-video bundle. Every stick in that log is centered at 128, so the log cannot express a right-stick aimbot. This harness builds one deterministic synthetic base instead: 36 one-second bins of frame-difference energy, plus HID-like `inputs.jsonl` rows (`t_us`, `b`, `lx`, `ly`, `rx`, `ry`, `l2`, `r2`). The series is a formula. It is not a decoded capture and not a recording of a player.

Camera-pan seconds are 2–13 and 22–33. Those seconds contribute 40 controller rows and frame-difference energy 800. The other seconds contribute 3 rows and energy 20. Motion rule v3 correlates the row-count series with that energy. The floor is the shipped `MOTION_MIN_PERMILLE` (300). A score at or above the floor is `pass`. Anything else, including a flat series at −1000, is `partial`. A partial motion check does not become FAIL.

## Variants

| variant | rewrite |
| --- | --- |
| control | unmodified synthetic log |
| idle-right-stick | right stick centered; left stick, buttons, and row times kept |
| blended-assist | right stick is 40% human plus 60% smooth target; row times kept |
| full-idle | every controller row removed; camera series kept |
| snap-flicks | right stick centered except a 200 ms jump on each pan second |

`snap-flicks` keeps the human row times. The right stick leaves center only inside a 200 ms window on each pan second, which is under the 300 ms bound in the review request. `blended-assist` writes `0.4 * human + 0.6 * target` into the same rows. The target is a smooth sine.

## Motion rule v3 on each variant

| variant | ground truth | input rows | right-stick run (ms) | motion_permille | motion check | input proof |
| --- | --- | --- | --- | --- | --- | --- |
| control | benign | 996 | 11975 | 1000 | pass | pass |
| idle-right-stick | attack | 996 | 0 | 1000 | pass | fail |
| blended-assist | attack | 996 | 19666 | 1000 | pass | fail |
| full-idle | attack | 0 | 0 | -1000 | partial | fail |
| snap-flicks | attack | 996 | 175 | 1000 | pass | fail |

Input proof is the direct result of `check_inputs`, `check_checkpoints`, and `check_input_files`. Control is sealed, so the chunk hashes and checkpoint `input_hash` values match the rows. Each other variant keeps those seals and changes the rows. That is the rewrite a live precheck rejects before motion is scored. The permille column is `score_motion` on the rewritten rows anyway.

## Pluggable scorers

A scorer implements `name`, `version`, `score(sample) -> Score`, and `identity()`. `Score.flagged` is true, false, or null (abstain). `identity().sha256` is recorded below so a re-run of the same tree matches and an edit to the scorer does not.

Catch rate is flagged attacks divided by decided attacks. False-alarm rate is flagged benign divided by decided benign. An abstention is neither a catch nor a false alarm. A rate is `n/a` when that class has no decision. Ground truth for this fixture: `control` is benign, and the other four variants are attacks.

`motion-rule-v3` calls the shipped rule. It flags a variant when the motion check is `partial`. `pan-agreement/v0` is an example decision over 100 ms camera bins. It is not a camera estimator. The estimator plug-in is `CameraPanEstimator.estimate(sample) -> list[PanBin]`. Each bin is `{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}`. Unreadable frames set `no_claim` true and leave yaw, pitch, and confidence null. The estimator plugged into this report is `example-no-claim/v0`, a stub that marks every frame `no_claim`.

| scorer | version | estimator | sha256 | catch rate | false-alarm rate | abstained |
| --- | --- | --- | --- | --- | --- | --- |
| motion-rule-v3 | motion-rule/v3 | — | a6bc018e86e44be84c5ec6408a26ea2cbf91b7bc9a9c063370f32bf3b5f02915 | 1/4 (0.25) | 0/1 (0.00) | 0/5 |
| pan-agreement | pan-agreement/v0 | example-no-claim/v0 | 6f5fd5d82ed637532c45ffcc373e664e918aa027d388d9c271c6c3dc1b0d924b | n/a (0/4 decided) | n/a (0/1 decided) | 5/5 |

| variant | motion-rule-v3 | pan-agreement |
| --- | --- | --- |
| control | clear | abstain |
| idle-right-stick | clear | abstain |
| blended-assist | clear | abstain |
| full-idle | flag | abstain |
| snap-flicks | clear | abstain |

`flag` on `motion-rule-v3` is the `partial` check. `clear` is `pass`. `abstain` means the scorer declined to decide. The stub declines because it never claims a frame, so its catch rate and false-alarm rate are `n/a`.

## What motion rule v3 catches

`full-idle` is `partial` (−1000). The on-screen series still moves and the controller series is flat, which is the row-count failure the rule is built to report.

## What motion rule v3 misses

`idle-right-stick`, `blended-assist`, and `snap-flicks` stay `pass`, with the same permille as `control`. Their row counts are the control row counts. The rule counts rows, so a right-stick rewrite is invisible to it. Those three are the known gap this harness records. The assertions in `tests/test_redteam_aimbot_motion.py` expect that miss, which keeps CI green while the gap is still open.

## Reproducing

```bash
pytest -m known_gap -s tests/test_redteam_aimbot_motion.py
```

The motion-rule hash covers `MotionRuleV3Scorer`, `input_energy_series`, `correlate_keyed`, the text of `qor_receipt/review/motion.py`, and the integer `MOTION_MIN_PERMILLE`. The pan-agreement hash covers `PanAgreementScorer` and the estimator class plugged into it. The harness hash covers `tests/redteam/aimbot_harness.py`.

Another estimator plugs in beside the shipped rule. Put `tests` on `PYTHONPATH` first (the pytest module does this itself):

```python
from redteam.aimbot_harness import evaluate
from redteam.scorers import PanAgreementScorer

report = evaluate(scorers=[PanAgreementScorer(MyEstimator())])
```

`evaluate` keeps `motion-rule-v3` in the list when the caller does not pass it.

```json
{
  "harness": {
    "sha256": "1261f00ca80d24955aea782858a64857df9e8c6b4bb8586e84e8ecf05414150d",
    "version": "redteam-aimbot/v1"
  },
  "scorers": [
    {
      "abstained": 0,
      "catch_rate": 0.25,
      "decided_attacks": 4,
      "decided_benign": 1,
      "estimator": null,
      "estimator_version": null,
      "false_alarm_rate": 0.0,
      "false_positives": 0,
      "name": "motion-rule-v3",
      "sha256": "a6bc018e86e44be84c5ec6408a26ea2cbf91b7bc9a9c063370f32bf3b5f02915",
      "true_positives": 1,
      "version": "motion-rule/v3"
    },
    {
      "abstained": 5,
      "catch_rate": null,
      "decided_attacks": 0,
      "decided_benign": 0,
      "estimator": "example-no-claim",
      "estimator_version": "example-no-claim/v0",
      "false_alarm_rate": null,
      "false_positives": 0,
      "name": "pan-agreement",
      "sha256": "6f5fd5d82ed637532c45ffcc373e664e918aa027d388d9c271c6c3dc1b0d924b",
      "true_positives": 0,
      "version": "pan-agreement/v0"
    }
  ],
  "variants": [
    {
      "decisions": {
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "pass",
      "input_rows": 996,
      "label": "benign",
      "motion_check": "pass",
      "motion_permille": 1000,
      "name": "control",
      "right_stick_run_ms": 11975
    },
    {
      "decisions": {
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 996,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 1000,
      "name": "idle-right-stick",
      "right_stick_run_ms": 0
    },
    {
      "decisions": {
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 996,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 1000,
      "name": "blended-assist",
      "right_stick_run_ms": 19666
    },
    {
      "decisions": {
        "motion-rule-v3": "flag",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 0,
      "label": "attack",
      "motion_check": "partial",
      "motion_permille": -1000,
      "name": "full-idle",
      "right_stick_run_ms": 0
    },
    {
      "decisions": {
        "motion-rule-v3": "clear",
        "pan-agreement": "abstain"
      },
      "input_proof": "fail",
      "input_rows": 996,
      "label": "attack",
      "motion_check": "pass",
      "motion_permille": 1000,
      "name": "snap-flicks",
      "right_stick_run_ms": 175
    }
  ]
}
```
