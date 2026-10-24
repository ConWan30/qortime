"""Test-only red team for motion rule v3 against aimbot-style controller logs.

The base is a deterministic synthetic series. ``tests/review_fixture.py`` is the
repo's controller-plus-video bundle, and every stick in that log is centered, so
it cannot express these variants. This module builds 36 seconds of frame-difference
energy and HID-like ``inputs.jsonl`` rows, rewrites the rows, and scores them by
calling the motion and input-proof functions directly.

There is no referee client, no network call, and no signing key. A rewritten log
keeps the original chunk hashes and checkpoint ``input_hash`` values, so
``check_inputs`` / ``check_checkpoints`` / ``check_input_files`` fail it. The live
precheck rejects that log before motion is scored. The motion numbers below are
what ``score_motion`` returns when it is called on the rewritten rows anyway.

Plug in another scorer with ``evaluate(scorers=[...])``. Motion rule v3 stays in
the list. A camera-pan estimator implements ``CameraPanEstimator`` and is passed
to ``PanAgreementScorer``.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qor_receipt.jcs import sha256_hex
from qor_receipt.review.bundle import (
    GENESIS,
    check_checkpoints,
    check_input_files,
    check_inputs,
    file_sha,
    link,
    load,
)
from redteam.scorers import (
    MotionRuleV3Scorer,
    RateResult,
    Score,
    Scorer,
    VariantSample,
    default_scorers,
    rates_for,
)

HARNESS_VERSION = "redteam-aimbot/v1"
SECONDS = 36
SEG_S = 2
WALL_ANCHOR_MS = 1_700_000_000_000
FLICK_START_MS = 100
FLICK_MS = 200
PAN_SECONDS = frozenset(range(2, 14)) | frozenset(range(22, 34))
VARIANTS = ("control", "idle-right-stick", "blended-assist", "full-idle", "snap-flicks")
LABELS = {
    "control": "benign",
    "idle-right-stick": "attack",
    "blended-assist": "attack",
    "full-idle": "attack",
    "snap-flicks": "attack",
}
VARIANT_NOTES = {
    "control": "unmodified synthetic log",
    "idle-right-stick": "right stick centered; left stick, buttons, and row times kept",
    "blended-assist": "right stick is 40% human plus 60% smooth target; row times kept",
    "full-idle": "every controller row removed; camera series kept",
    "snap-flicks": "right stick centered except a 200 ms jump on each pan second",
}


def _clamp(value: int) -> int:
    return 0 if value < 0 else 255 if value > 255 else value


def pan(sec: int) -> bool:
    """Seconds whose synthetic camera is moving. The pattern is not a 3-second cycle."""
    return sec in PAN_SECONDS


def frame_diff_energy(sec: int) -> int:
    """Stand-in for ``motion_series``: one frame-difference energy per second."""
    return 800 if pan(sec) else 20


def report_count(sec: int) -> int:
    """Row count per second. This is the signal motion rule v3 correlates."""
    return 40 if pan(sec) else 3


def motion_series() -> dict[int, int]:
    return {sec: frame_diff_energy(sec) for sec in range(SECONDS)}


def _human_axes(sec: int, k: int) -> tuple[int, int, int, int, int]:
    """Left stick, right stick, and the button mask for one human report."""
    if not pan(sec):
        return 128, 128, 128, 128, 0
    sign = 1 if sec % 2 == 0 else -1
    lx = _clamp(128 + sign * (30 + (k % 3)))
    ly = _clamp(128 - sign * 18)
    rx = _clamp(128 + sign * (50 + (k % 5)))
    ry = _clamp(128 + sign * 16)
    buttons = 1 if k % 10 == 0 else 0
    return lx, ly, rx, ry, buttons


def control_rows() -> list[dict[str, Any]]:
    """HID-like ``inputs.jsonl`` records (decoded axes, not a raw 64-byte report)."""
    rows: list[dict[str, Any]] = [{
        "type": "header",
        "schema": "qorcap-inputs/v0",
        "t_us": 0,
        "seg": 0,
        "wall_anchor_ms": WALL_ANCHOR_MS,
    }]
    for sec in range(SECONDS):
        n = report_count(sec)
        for k in range(n):
            t_ms = sec * 1000 + (k * 1000) // n
            lx, ly, rx, ry, buttons = _human_axes(sec, k)
            rows.append({
                "type": "in",
                "t_us": t_ms * 1000,
                "seg": sec // SEG_S + 1,
                "b": buttons,
                "lx": lx,
                "ly": ly,
                "rx": rx,
                "ry": ry,
                "l2": 0,
                "r2": 0,
            })
    return rows


def _blend(row: Mapping[str, Any]) -> tuple[int, int]:
    """Human right stick plus a smooth injected aim point. Row time is unchanged."""
    t = (int(row["t_us"]) // 1000) / 1000.0
    target_rx = int(round(128 + 70 * math.sin(t * 1.1)))
    target_ry = int(round(128 + 45 * math.cos(t * 0.7)))
    rx = int(round(0.4 * int(row["rx"]) + 0.6 * target_rx))
    ry = int(round(0.4 * int(row["ry"]) + 0.6 * target_ry))
    return _clamp(rx), _clamp(ry)


def _snap(row: Mapping[str, Any]) -> tuple[int, int]:
    """One corner jump per pan second. The active window is ``FLICK_MS`` (under 300 ms)."""
    t_ms = int(row["t_us"]) // 1000
    if pan(t_ms // 1000) and FLICK_START_MS <= (t_ms % 1000) < FLICK_START_MS + FLICK_MS:
        return 240, 40
    return 128, 128


def rewrite_rows(rows: Sequence[Mapping[str, Any]], variant: str) -> list[dict[str, Any]]:
    """Return the controller records for ``variant``. Chunk records are not an input here."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    if variant == "full-idle":
        return [dict(row) for row in rows if row.get("type") != "in"]
    out: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        if copied.get("type") == "in" and variant == "idle-right-stick":
            copied["rx"] = 128
            copied["ry"] = 128
        elif copied.get("type") == "in" and variant == "blended-assist":
            copied["rx"], copied["ry"] = _blend(copied)
        elif copied.get("type") == "in" and variant == "snap-flicks":
            copied["rx"], copied["ry"] = _snap(copied)
        out.append(copied)
    return out


def max_right_stick_run_ms(rows: Sequence[Mapping[str, Any]], deadzone: int = 8) -> int:
    """Longest contiguous run of off-center right-stick samples, in milliseconds.

    The length is the span from the first active sample to the last, so a single
    sample is 0 ms. Motion rule v3 does not compute this.
    """
    samples = sorted((row for row in rows if row.get("type") == "in"), key=lambda row: int(row["t_us"]))
    best = 0
    start: int | None = None
    prev: int | None = None
    for row in samples:
        active = abs(int(row["rx"]) - 128) > deadzone or abs(int(row["ry"]) - 128) > deadzone
        t_ms = int(row["t_us"]) // 1000
        if active:
            if start is None:
                start = t_ms
            prev = t_ms
        elif start is not None and prev is not None:
            best = max(best, prev - start)
            start = None
            prev = None
    if start is not None and prev is not None:
        best = max(best, prev - start)
    return best


def _dump_jsonl(records: Sequence[Mapping[str, Any]]) -> bytes:
    lines = [json.dumps(record, sort_keys=True, separators=(",", ":")) for record in records]
    return ("".join(line + "\n" for line in lines)).encode("utf-8")


def _load_jsonl(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def seal_input_log(rows: Sequence[Mapping[str, Any]]) -> dict[str, bytes]:
    """Bind ``rows`` with the same chunk hash and checkpoint ``input_hash`` the referee recomputes.

    The receipt here is only the ``signals.input`` hashes ``check_input_files`` reads.
    It is not a match receipt and it is not signed.
    """
    by_seg: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_seg.setdefault(int(row["seg"]), []).append(dict(row))
    lines: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    head = GENESIS
    for idx, seg in enumerate(sorted(by_seg)):
        grouped = by_seg[seg]
        digest = sha256_hex(grouped)
        prev = head
        head = link(prev, idx, digest)
        lines.extend(grouped)
        chunk = {"type": "chunk", "seq": idx, "seg": seg, "n": len(grouped), "sha256": digest, "prev": prev, "head": head}
        lines.append(chunk)
        chunks.append(chunk)
    inputs = _dump_jsonl(lines)
    prev_cp = GENESIS
    checkpoints: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        ext = {"input_seg": chunk["seg"], "segment_seq": chunk["seg"], "trigger": "segment"}
        body = {
            "seq": i,
            "t_ms": int(chunk["seg"]) * SEG_S * 1000,
            "frames_hash": sha256_hex(ext),
            "input_hash": chunk["sha256"],
            "prev": prev_cp,
        }
        body["hash"] = sha256_hex(body)
        checkpoints.append({**body, "qorcap": ext})
        prev_cp = body["hash"]
    receipt = {"signals": {"input": {
        "inputs_sha256": file_sha(inputs),
        "inputs_chain_head": head,
        "status": "test-only",
    }}}
    bundle = {
        "schema": "qorcap-bundle/v0",
        "purpose": "TEST-ONLY redteam input log. Not a submission. No referee signature.",
        "match_id": "redteam-not-a-submission",
        "checkpoint_head": prev_cp,
    }
    note = {"type": "test-only", "note": "no video bytes; the motion series is synthetic"}
    return {
        "bundle.json": json.dumps(bundle, sort_keys=True).encode("utf-8"),
        "manifest.jsonl": _dump_jsonl([note]),
        "checkpoints.jsonl": _dump_jsonl(checkpoints),
        "inputs.jsonl": inputs,
        "receipt.json": json.dumps(receipt, sort_keys=True).encode("utf-8"),
    }


def rewrite_sealed(files: Mapping[str, bytes], variant: str) -> dict[str, bytes]:
    """Change controller rows and leave chunk lines, checkpoints, and the receipt hashes in place."""
    if variant == "control":
        return dict(files)
    records = _load_jsonl(files["inputs.jsonl"])
    rewritten: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") == "in" and variant == "full-idle":
            continue
        if record.get("type") == "in":
            rewritten.append(rewrite_rows([record], variant)[0])
        else:
            rewritten.append(record)
    out = dict(files)
    out["inputs.jsonl"] = _dump_jsonl(rewritten)
    return out


def input_proof_errors(files: Mapping[str, bytes]) -> tuple[str, ...]:
    """The three input-proof functions the referee runs in precheck, in that order."""
    bundle = load(dict(files))
    if bundle.errors:
        return tuple(bundle.errors)
    by_seg, errs = check_inputs(bundle)
    found = list(errs)
    found.extend(check_checkpoints(bundle, by_seg))
    _, file_errs = check_input_files(bundle)
    found.extend(file_errs)
    return tuple(found)


def sample_segments(rows: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    segs = sorted({int(row["seg"]) for row in rows if row.get("type") == "in"})
    return tuple(segs)


@dataclass
class VariantResult:
    sample: VariantSample
    input_rows: int
    right_stick_run_ms: int
    input_proof: str
    input_proof_errors: tuple[str, ...]


@dataclass
class Report:
    harness_version: str
    harness_sha256: str
    variants: list[VariantResult]
    scores: dict[str, dict[str, Score]]
    identities: dict[str, Any]
    rates: dict[str, RateResult]


def _harness_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _with_motion_scorer(scorers: Sequence[Scorer]) -> list[Scorer]:
    chosen = list(scorers)
    if not any(scorer.name == MotionRuleV3Scorer.name for scorer in chosen):
        chosen = [MotionRuleV3Scorer(), *chosen]
    names = [scorer.name for scorer in chosen]
    if len(names) != len(set(names)):
        raise ValueError(f"scorer names must be unique, got {names}")
    return chosen


def evaluate(scorers: Sequence[Scorer] | None = None) -> Report:
    """Score every variant. ``scorers`` replaces the default list; motion rule v3 is kept."""
    chosen = _with_motion_scorer(default_scorers() if scorers is None else scorers)
    motion = motion_series()
    base = control_rows()
    sealed = seal_input_log(base)
    segments = sample_segments(base)
    variants: list[VariantResult] = []
    for name in VARIANTS:
        files = rewrite_sealed(sealed, name)
        errors = input_proof_errors(files)
        bundle = load(files)
        rows = list(bundle.inputs)
        sample = VariantSample(
            name=name,
            label=LABELS[name],
            rows=rows,
            motion=motion,
            sample_segments=segments,
            duration_ms=SECONDS * 1000,
        )
        variants.append(VariantResult(
            sample=sample,
            input_rows=sum(row.get("type") == "in" for row in rows),
            right_stick_run_ms=max_right_stick_run_ms(rows),
            input_proof="pass" if not errors else "fail",
            input_proof_errors=errors,
        ))
    scores: dict[str, dict[str, Score]] = {}
    identities: dict[str, Any] = {}
    rates: dict[str, RateResult] = {}
    samples = [variant.sample for variant in variants]
    for scorer in chosen:
        by_name = {sample.name: scorer.score(sample) for sample in samples}
        identity = scorer.identity()
        scores[scorer.name] = by_name
        identities[scorer.name] = identity
        rates[scorer.name] = rates_for(samples, by_name, identity)
    return Report(
        harness_version=HARNESS_VERSION,
        harness_sha256=_harness_sha256(),
        variants=variants,
        scores=scores,
        identities=identities,
        rates=rates,
    )


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    rule = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _fmt_rate(hits: int, decided: int, total: int, rate: float | None) -> str:
    if decided == 0 or rate is None:
        return f"n/a (0/{total} decided)"
    return f"{hits}/{decided} ({rate:.2f})"


def _permille(score: Score) -> str:
    value = score.detail.get("motion_permille")
    return "" if value is None else str(value)


def render_report(report: Report | None = None) -> str:
    """Markdown report. The pytest locks ``docs/redteam/motion-rule-v3-aimbot.md`` to this text."""
    report = evaluate() if report is None else report
    motion = report.scores["motion-rule-v3"]
    variant_rows = []
    for variant in report.variants:
        score = motion[variant.sample.name]
        variant_rows.append([
            variant.sample.name,
            variant.sample.label,
            str(variant.input_rows),
            str(variant.right_stick_run_ms),
            _permille(score),
            str(score.detail["motion_check"]),
            variant.input_proof,
        ])
    scorer_rows = []
    decision_headers = ["variant"]
    decision_rows: dict[str, list[str]] = {variant.sample.name: [variant.sample.name] for variant in report.variants}
    for name, rate in report.rates.items():
        identity = report.identities[name]
        estimator = identity.estimator_version or "—"
        scorer_rows.append([
            name,
            rate.version,
            estimator,
            rate.sha256,
            _fmt_rate(rate.true_positives, rate.decided_attacks, rate.attacks, rate.catch_rate),
            _fmt_rate(rate.false_positives, rate.decided_benign, rate.benign, rate.false_alarm_rate),
            f"{rate.abstained}/{rate.attacks + rate.benign}",
        ])
        decision_headers.append(name)
        for variant in report.variants:
            decision_rows[variant.sample.name].append(report.scores[name][variant.sample.name].decision)
    payload = {
        "harness": {"version": report.harness_version, "sha256": report.harness_sha256},
        "scorers": [
            {
                "name": name,
                "version": report.rates[name].version,
                "sha256": report.rates[name].sha256,
                "estimator": report.identities[name].estimator_name,
                "estimator_version": report.identities[name].estimator_version,
                "catch_rate": report.rates[name].catch_rate,
                "false_alarm_rate": report.rates[name].false_alarm_rate,
                "true_positives": report.rates[name].true_positives,
                "false_positives": report.rates[name].false_positives,
                "decided_attacks": report.rates[name].decided_attacks,
                "decided_benign": report.rates[name].decided_benign,
                "abstained": report.rates[name].abstained,
            }
            for name in report.rates
        ],
        "variants": [
            {
                "name": variant.sample.name,
                "label": variant.sample.label,
                "input_rows": variant.input_rows,
                "right_stick_run_ms": variant.right_stick_run_ms,
                "motion_permille": motion[variant.sample.name].detail["motion_permille"],
                "motion_check": motion[variant.sample.name].detail["motion_check"],
                "input_proof": variant.input_proof,
                "decisions": {name: report.scores[name][variant.sample.name].decision for name in report.scores},
            }
            for variant in report.variants
        ],
    }
    variant_table = _md_table(
        ["variant", "ground truth", "input rows", "right-stick run (ms)", "motion_permille", "motion check", "input proof"],
        variant_rows,
    )
    scorer_table = _md_table(
        ["scorer", "version", "estimator", "sha256", "catch rate", "false-alarm rate", "abstained"],
        scorer_rows,
    )
    decision_table = _md_table(decision_headers, [decision_rows[variant.sample.name] for variant in report.variants])
    note_rows = [[name, VARIANT_NOTES[name]] for name in VARIANTS]
    notes = _md_table(["variant", "rewrite"], note_rows)
    blob = json.dumps(payload, indent=2, sort_keys=True)
    return f"""# Motion rule v3 red team: aimbot-style controller logs

Test-only harness under `tests/redteam/`. It scores rewritten controller logs by calling `input_energy_series`, `score_motion`, `check_inputs`, `check_checkpoints`, and `check_input_files` in process. It does not sign a receipt, does not hold a referee key, and does not open a network connection. Nothing in this directory is a submission.

Harness `{report.harness_version}` sha256 `{report.harness_sha256}`.

## Fixture

`tests/review_fixture.py` is the repo's controller-plus-video bundle. Every stick in that log is centered at 128, so the log cannot express a right-stick aimbot. This harness builds one deterministic synthetic base instead: {SECONDS} one-second bins of frame-difference energy, plus HID-like `inputs.jsonl` rows (`t_us`, `b`, `lx`, `ly`, `rx`, `ry`, `l2`, `r2`). The series is a formula. It is not a decoded capture and not a recording of a player.

Camera-pan seconds are 2–13 and 22–33. Those seconds contribute 40 controller rows and frame-difference energy 800. The other seconds contribute 3 rows and energy 20. Motion rule v3 correlates the row-count series with that energy. The floor is the shipped `MOTION_MIN_PERMILLE` (300). A score at or above the floor is `pass`. Anything else, including a flat series at −1000, is `partial`. A partial motion check does not become FAIL.

## Variants

{notes}

`snap-flicks` keeps the human row times. The right stick leaves center only inside a {FLICK_MS} ms window on each pan second, which is under the 300 ms bound in the review request. `blended-assist` writes `0.4 * human + 0.6 * target` into the same rows. The target is a smooth sine.

## Motion rule v3 on each variant

{variant_table}

Input proof is the direct result of `check_inputs`, `check_checkpoints`, and `check_input_files`. Control is sealed, so the chunk hashes and checkpoint `input_hash` values match the rows. Each other variant keeps those seals and changes the rows. That is the rewrite a live precheck rejects before motion is scored. The permille column is `score_motion` on the rewritten rows anyway.

## Pluggable scorers

A scorer implements `name`, `version`, `score(sample) -> Score`, and `identity()`. `Score.flagged` is true, false, or null (abstain). `identity().sha256` is recorded below so a re-run of the same tree matches and an edit to the scorer does not.

Catch rate is flagged attacks divided by decided attacks. False-alarm rate is flagged benign divided by decided benign. An abstention is neither a catch nor a false alarm. A rate is `n/a` when that class has no decision. Ground truth for this fixture: `control` is benign, and the other four variants are attacks.

`motion-rule-v3` calls the shipped rule. It flags a variant when the motion check is `partial`. `pan-agreement/v0` is an example decision over 100 ms camera bins. It is not a camera estimator. The estimator plug-in is `CameraPanEstimator.estimate(sample) -> list[PanBin]`. Each bin is `{{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}}`. Unreadable frames set `no_claim` true and leave yaw, pitch, and confidence null. The estimator plugged into this report is `example-no-claim/v0`, a stub that marks every frame `no_claim`.

{scorer_table}

{decision_table}

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
{blob}
```
"""
