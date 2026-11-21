"""Test-only red team for motion rule v3.

The headline threat is a passthrough adapter. QorCap's USB log records the human's
controller and is sealed at capture: chunk hashes and checkpoint ``input_hash``
are computed over those logged rows. The adapter injects aim, stick gestures, or
button macros into the console, so the screen shows actions the log does not
contain. Input proof passes. Motion rule v3 still sees the human row rate.

The log-tamper variants rewrite that sealed log and leave the seals in place.
They are a separate test. A live precheck rejects them before motion is scored.
This harness still calls ``score_motion`` on the rewritten rows.

There is no referee client, no network call, and no signing key. The series is
synthetic. ``tests/review_fixture.py`` centers every stick, so it cannot express
these cases.

Plug in another scorer with ``evaluate(scorers=[...])``. Motion rule v3 stays in
the list. A camera-pan estimator implements ``CameraPanEstimator``. A video-event
estimator implements ``VideoEventEstimator``.
"""
from __future__ import annotations

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
    BUTTON_ACTION,
    EVENT_MATCH_WINDOW_MS,
    PACKAGE_VERSION,
    STICK_ACTION,
    STICK_DEADZONE,
    MotionRuleV3Scorer,
    RateResult,
    Score,
    Scorer,
    VariantSample,
    VideoEvent,
    default_scorers,
    hash_file_set,
    logged_press_matches,
    rates_for,
)

HARNESS_VERSION = "redteam-aimbot/v2"
SECONDS = 60
SEG_S = 2
WALL_ANCHOR_MS = 1_700_000_000_000
# Half-open second ranges. Idle and menu are long enough to pull a perfect
# row/energy step off permille 1000. Play is the rest of the minute.
IDLE_RANGES = ((0, 8), (46, 54))
MENU_RANGES = ((20, 32), (54, 60))
SKILL_OFFSET_MS = 400
SKILL_HOLD_MS = 200
FLICK_START_MS = 100
FLICK_MS = 200
EVENT_AT_MS = 500
UNMATCHED_EVENTS = 4
HONEST_STICK_AIMBOTS = ("honest-passthrough-aim", "honest-blended-aim", "honest-snap-aim")
TAMPER_KINDS = ("idle-right-stick", "blended-assist", "snap-flicks", "full-idle")


def mix(sec: int, salt: int) -> int:
    """Deterministic 16-bit mix. The multipliers are the whole seed."""
    n = ((sec + 1) * 2_246_822_519 + salt * 3_266_489_917) & 0xFFFFFFFF
    n ^= n >> 16
    n = (n * 0x85EBCA6B) & 0xFFFFFFFF
    n ^= n >> 13
    return n & 0xFFFF


def _in_ranges(sec: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= sec < end for start, end in ranges)


def phase(sec: int) -> str:
    """``idle``, ``menu``, or ``play`` for one second of the synthetic minute."""
    if _in_ranges(sec, IDLE_RANGES):
        return "idle"
    if _in_ranges(sec, MENU_RANGES):
        return "menu"
    if 0 <= sec < SECONDS:
        return "play"
    raise ValueError(f"second {sec} is outside 0..{SECONDS}")


def report_count(sec: int) -> int:
    """Logged USB rows in this second. Menu navigation is busy while the picture is still."""
    kind = phase(sec)
    if kind == "idle":
        return 1 + (mix(sec, 2) % 4)  # 1..4
    if kind == "menu":
        return 22 + (mix(sec, 4) % 20)  # 22..41
    latent = 15 + (mix(sec, 1) % 55)  # 15..69
    return 12 + latent // 2 + (mix(sec, 6) % 9)


def frame_diff_energy(sec: int) -> int:
    """Stand-in for ``motion_series``. Play shares a latent with ``report_count``; menu does not."""
    kind = phase(sec)
    if kind == "idle":
        return 4 + (mix(sec, 3) % 25)  # 4..28
    if kind == "menu":
        return 5 + (mix(sec, 5) % 30)  # 5..34
    latent = 15 + (mix(sec, 1) % 55)
    energy = 180 + latent * 7 + (mix(sec, 7) % 160) - (mix(sec, 8) % 80)
    return energy if energy > 40 else 40


def motion_series() -> dict[int, int]:
    return {sec: frame_diff_energy(sec) for sec in range(SECONDS)}


def _clamp(value: int) -> int:
    return 0 if value < 0 else 255 if value > 255 else value


def _skill_second(sec: int) -> bool:
    """A short honest right-stick gesture. In Madden that is a skill move or hit stick."""
    return phase(sec) == "play" and sec % 5 == 0


def _axes(sec: int, offset_ms: int, k: int) -> tuple[int, int, int, int, int]:
    kind = phase(sec)
    if kind != "play":
        buttons = 1 if kind == "menu" and k % 3 == 0 else 0
        return 128, 128, 128, 128, buttons
    sign = 1 if sec % 2 == 0 else -1
    lx = _clamp(128 + sign * (24 + (k % 5)))
    ly = _clamp(128 - sign * 14)
    if _skill_second(sec) and SKILL_OFFSET_MS <= offset_ms < SKILL_OFFSET_MS + SKILL_HOLD_MS:
        rx, ry = 210, 36
    else:
        rx, ry = 128, 128
    buttons = 1 if sec % 4 == 0 and k % 8 == 0 else 0
    return lx, ly, rx, ry, buttons


def control_rows() -> list[dict[str, Any]]:
    """Honest human USB log. HID-like decoded axes, not a raw 64-byte report."""
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
            offset = (k * 1000) // n
            t_ms = sec * 1000 + offset
            lx, ly, rx, ry, buttons = _axes(sec, offset, k)
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


def header_only_rows() -> list[dict[str, Any]]:
    """No controller rows. The honest full-idle log is this, sealed on its own."""
    return [{
        "type": "header",
        "schema": "qorcap-inputs/v0",
        "t_us": 0,
        "seg": 0,
        "wall_anchor_ms": WALL_ANCHOR_MS,
    }]


def _blend(row: Mapping[str, Any]) -> tuple[int, int]:
    """Log-tamper only: human right stick plus a smooth injected aim point."""
    t = (int(row["t_us"]) // 1000) / 1000.0
    target_rx = int(round(128 + 70 * math.sin(t * 1.1)))
    target_ry = int(round(128 + 45 * math.cos(t * 0.7)))
    rx = int(round(0.4 * int(row["rx"]) + 0.6 * target_rx))
    ry = int(round(0.4 * int(row["ry"]) + 0.6 * target_ry))
    return _clamp(rx), _clamp(ry)


def _snap(row: Mapping[str, Any]) -> tuple[int, int]:
    """Log-tamper only: right stick centered except a window shorter than 300 ms."""
    t_ms = int(row["t_us"]) // 1000
    if phase(t_ms // 1000) == "play" and FLICK_START_MS <= (t_ms % 1000) < FLICK_START_MS + FLICK_MS:
        return 240, 40
    return 128, 128


def rewrite_rows(rows: Sequence[Mapping[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Controller records after a log tamper. ``kind`` is not an honest-seal variant."""
    if kind not in TAMPER_KINDS:
        raise ValueError(f"unknown tamper {kind!r}")
    if kind == "full-idle":
        return [dict(row) for row in rows if row.get("type") != "in"]
    out: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        if copied.get("type") == "in" and kind == "idle-right-stick":
            copied["rx"] = 128
            copied["ry"] = 128
        elif copied.get("type") == "in" and kind == "blended-assist":
            copied["rx"], copied["ry"] = _blend(copied)
        elif copied.get("type") == "in" and kind == "snap-flicks":
            copied["rx"], copied["ry"] = _snap(copied)
        out.append(copied)
    return out


def motion_passthrough(base: Mapping[int, int]) -> dict[int, int]:
    """Screen aim added on play seconds. The USB log is unchanged."""
    out = dict(base)
    for sec, energy in base.items():
        if phase(sec) == "play":
            out[sec] = int(energy) + 30 + (mix(sec, 9) % 50)
    return out


def motion_blended(base: Mapping[int, int]) -> dict[int, int]:
    """Play energy kept, plus a smooth injected component (20% of a sine target)."""
    out: dict[int, int] = {}
    for sec, energy in base.items():
        target = 200 + int(100 * math.sin(sec / 7.0))
        out[sec] = max(0, int(round(0.80 * int(energy) + 0.20 * max(target, 0))))
    return out


def motion_snap(base: Mapping[int, int]) -> dict[int, int]:
    """A one-second bin absorbs a sub-300 ms aim jump as extra energy on some play seconds."""
    out = dict(base)
    for sec, energy in base.items():
        if phase(sec) == "play" and sec % 4 == 0:
            out[sec] = int(energy) + 130
    return out


def motion_bumped(base: Mapping[int, int], times_ms: Sequence[int], amount: int) -> dict[int, int]:
    out = {sec: int(energy) for sec, energy in base.items()}
    for t_ms in times_ms:
        sec = int(t_ms) // 1000
        out[sec] = out[sec] + amount
    return out


def unmatched_times(rows: Sequence[Mapping[str, Any]], event_kind: str, limit: int = UNMATCHED_EVENTS) -> tuple[int, ...]:
    """Play-second timestamps whose ±window contains no matching logged press."""
    found: list[int] = []
    for sec in range(SECONDS):
        if phase(sec) != "play":
            continue
        t_ms = sec * 1000 + EVENT_AT_MS
        if logged_press_matches(rows, t_ms, event_kind, EVENT_MATCH_WINDOW_MS, STICK_DEADZONE):
            continue
        found.append(t_ms)
        if len(found) == limit:
            return tuple(found)
    raise RuntimeError(f"fixture placed {len(found)} {event_kind} events, need {limit}")


def screen_events(times_ms: Sequence[int], event_kind: str) -> tuple[VideoEvent, ...]:
    return tuple(
        VideoEvent(mono_ms=t_ms, event_kind=event_kind, confidence=0.9, no_claim=False)
        for t_ms in times_ms
    )


def max_right_stick_run_ms(rows: Sequence[Mapping[str, Any]], deadzone: int = STICK_DEADZONE) -> int:
    """Longest contiguous run of off-center right-stick samples, in milliseconds."""
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
    """Bind ``rows`` with the chunk hash and checkpoint ``input_hash`` the referee recomputes.

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


def rewrite_sealed(files: Mapping[str, bytes], kind: str) -> dict[str, bytes]:
    """Change controller rows and leave chunk lines, checkpoints, and the receipt hashes in place."""
    records = _load_jsonl(files["inputs.jsonl"])
    rewritten: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") == "in" and kind == "full-idle":
            continue
        if record.get("type") == "in":
            rewritten.append(rewrite_rows([record], kind)[0])
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
    return tuple(sorted({int(row["seg"]) for row in rows if row.get("type") == "in"}))


@dataclass(frozen=True)
class _Built:
    name: str
    label: str
    cohort: str
    note: str
    files: dict[str, bytes]
    motion: dict[int, int]
    events: tuple[VideoEvent, ...]


def _builds() -> list[_Built]:
    human = control_rows()
    base_motion = motion_series()
    sealed_human = seal_input_log(human)
    sealed_empty = seal_input_log(header_only_rows())
    stick_times = unmatched_times(human, STICK_ACTION)
    button_times = unmatched_times(human, BUTTON_ACTION)
    stick_events = screen_events(stick_times, STICK_ACTION)
    button_events = screen_events(button_times, BUTTON_ACTION)
    honest: list[tuple[str, str, str, dict[str, bytes], dict[int, int], tuple[VideoEvent, ...]]] = [
        ("control", "benign", "honest human log and the matching screen", sealed_human, dict(base_motion), ()),
        ("honest-passthrough-aim", "attack",
         "log is the human USB capture; play seconds gain adapter aim the log does not contain",
         sealed_human, motion_passthrough(base_motion), ()),
        ("honest-blended-aim", "attack",
         "log is the human capture; screen energy is 80% human plus 20% of a smooth aim target",
         sealed_human, motion_blended(base_motion), ()),
        ("honest-snap-aim", "attack",
         "log is the human capture; some play seconds absorb a sub-300 ms aim jump",
         sealed_human, motion_snap(base_motion), ()),
        ("honest-stick-action", "attack",
         "on-screen stick_action bursts (Madden: skill move / hit stick) with no logged right-stick press",
         sealed_human, motion_bumped(base_motion, stick_times, 150), stick_events),
        ("honest-button-macro", "attack",
         "on-screen button_action events (Madden: user catch or any button macro) with no logged button",
         sealed_human, motion_bumped(base_motion, button_times, 140), button_events),
        ("honest-full-idle", "attack",
         "honest empty log, sealed on its own, while the control screen still moves",
         sealed_empty, dict(base_motion), ()),
    ]
    tamper_notes = {
        "idle-right-stick": "rows rewritten so the right stick is centered; seals left on the human log",
        "blended-assist": "right-stick values rewritten to 40% human plus 60% target; seals left in place",
        "snap-flicks": "right stick rewritten to a sub-300 ms jump; seals left in place",
        "full-idle": "controller rows deleted after sealing; seals still describe the human log",
    }
    built = [
        _Built(name, label, "honest-seal", note, files, motion, events)
        for name, label, note, files, motion, events in honest
    ]
    for kind in TAMPER_KINDS:
        built.append(_Built(
            name=f"tamper-{kind}",
            label="attack",
            cohort="log-tamper",
            note=tamper_notes[kind],
            files=rewrite_sealed(sealed_human, kind),
            motion=dict(base_motion),
            events=(),
        ))
    return built


@dataclass
class VariantResult:
    sample: VariantSample
    note: str
    input_rows: int
    right_stick_run_ms: int
    screen_events: int
    input_proof: str
    input_proof_errors: tuple[str, ...]


@dataclass
class Report:
    harness_version: str
    package_version: str
    harness_sha256: str
    harness_files: tuple[str, ...]
    variants: list[VariantResult]
    scores: dict[str, dict[str, Score]]
    identities: dict[str, Any]
    rates_headline: dict[str, RateResult]
    rates_tamper: dict[str, RateResult]


def _harness_identity() -> tuple[str, tuple[str, ...]]:
    prefix = [f"qor-receipt=={PACKAGE_VERSION}", f"harness={HARNESS_VERSION}"]
    return hash_file_set([Path(__file__).resolve()], prefix)


def _with_motion_scorer(scorers: Sequence[Scorer]) -> list[Scorer]:
    chosen = list(scorers)
    if not any(scorer.name == MotionRuleV3Scorer.name for scorer in chosen):
        chosen = [MotionRuleV3Scorer(), *chosen]
    names = [scorer.name for scorer in chosen]
    if len(names) != len(set(names)):
        raise ValueError(f"scorer names must be unique, got {names}")
    return chosen


def _result(built: _Built) -> VariantResult:
    errors = input_proof_errors(built.files)
    bundle = load(built.files)
    rows = list(bundle.inputs)
    sample = VariantSample(
        name=built.name,
        label=built.label,
        rows=rows,
        motion=built.motion,
        sample_segments=sample_segments(rows),
        duration_ms=SECONDS * 1000,
        cohort=built.cohort,
        events=built.events,
    )
    return VariantResult(
        sample=sample,
        note=built.note,
        input_rows=sum(row.get("type") == "in" for row in rows),
        right_stick_run_ms=max_right_stick_run_ms(rows),
        screen_events=sum(not event.no_claim for event in built.events),
        input_proof="pass" if not errors else "fail",
        input_proof_errors=errors,
    )


def evaluate(scorers: Sequence[Scorer] | None = None) -> Report:
    """Score every variant. ``scorers`` replaces the default list; motion rule v3 is kept."""
    chosen = _with_motion_scorer(default_scorers() if scorers is None else scorers)
    variants = [_result(built) for built in _builds()]
    scores: dict[str, dict[str, Score]] = {}
    identities: dict[str, Any] = {}
    rates_headline: dict[str, RateResult] = {}
    rates_tamper: dict[str, RateResult] = {}
    headline = [variant.sample for variant in variants if variant.sample.cohort == "honest-seal"]
    tamper = [variant.sample for variant in variants if variant.sample.cohort == "log-tamper"]
    for scorer in chosen:
        by_name = {sample.name: scorer.score(sample) for sample in (headline + tamper)}
        identity = scorer.identity()
        scores[scorer.name] = by_name
        identities[scorer.name] = identity
        rates_headline[scorer.name] = rates_for(headline, by_name, identity)
        rates_tamper[scorer.name] = rates_for(tamper, by_name, identity)
    harness_sha, harness_files = _harness_identity()
    return Report(
        harness_version=HARNESS_VERSION,
        package_version=PACKAGE_VERSION,
        harness_sha256=harness_sha,
        harness_files=harness_files,
        variants=variants,
        scores=scores,
        identities=identities,
        rates_headline=rates_headline,
        rates_tamper=rates_tamper,
    )


def stick_aimbot_caught(report: Report, scorer: str = "motion-rule-v3") -> int:
    """How many of the three honest-seal stick-aimbot variants this scorer flags."""
    scores = report.scores[scorer]
    return sum(scores[name].flagged is True for name in HONEST_STICK_AIMBOTS)


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


def _variant_table(report: Report, cohort: str) -> str:
    motion = report.scores["motion-rule-v3"]
    rows = []
    for variant in report.variants:
        if variant.sample.cohort != cohort:
            continue
        score = motion[variant.sample.name]
        rows.append([
            variant.sample.name,
            variant.sample.label,
            str(variant.input_rows),
            str(variant.right_stick_run_ms),
            str(variant.screen_events),
            _permille(score),
            str(score.detail["motion_check"]),
            variant.input_proof,
        ])
    return _md_table(
        ["variant", "ground truth", "input rows", "right-stick run (ms)", "screen events",
         "motion_permille", "motion check", "input proof"],
        rows,
    )


def render_report(report: Report | None = None) -> str:
    """Markdown report. The pytest locks ``docs/redteam/motion-rule-v3-aimbot.md`` to this text."""
    report = evaluate() if report is None else report
    motion_scores = report.scores["motion-rule-v3"]
    caught = stick_aimbot_caught(report)
    notes = _md_table(
        ["variant", "cohort", "what the fixture does"],
        [[variant.sample.name, variant.sample.cohort, variant.note] for variant in report.variants],
    )
    decision_headers = ["variant", "cohort"]
    decision_rows = []
    for variant in report.variants:
        row = [variant.sample.name, variant.sample.cohort]
        for name in report.scores:
            row.append(report.scores[name][variant.sample.name].decision)
        decision_rows.append(row)
    decision_headers.extend(report.scores)
    scorer_rows = []
    for name, rate in report.rates_headline.items():
        identity = report.identities[name]
        scorer_rows.append([
            name,
            rate.version,
            identity.package_version,
            identity.estimator_version or "—",
            f"{len(identity.files)} files",
            rate.sha256,
            _fmt_rate(rate.true_positives, rate.decided_attacks, rate.attacks, rate.catch_rate),
            _fmt_rate(rate.false_positives, rate.decided_benign, rate.benign, rate.false_alarm_rate),
            f"{rate.abstained}/{rate.attacks + rate.benign}",
        ])
    payload = {
        "harness": {
            "version": report.harness_version,
            "package_version": report.package_version,
            "sha256": report.harness_sha256,
            "files": list(report.harness_files),
        },
        "headline_stick_aimbots": {
            "names": list(HONEST_STICK_AIMBOTS),
            "flagged_by_motion_rule_v3": caught,
            "of": len(HONEST_STICK_AIMBOTS),
        },
        "scorers": [
            {
                "name": name,
                "version": identity.version,
                "package_version": identity.package_version,
                "sha256": identity.sha256,
                "estimator": identity.estimator_name,
                "estimator_version": identity.estimator_version,
                "files": list(identity.files),
                "headline_catch_rate": report.rates_headline[name].catch_rate,
                "headline_false_alarm_rate": report.rates_headline[name].false_alarm_rate,
                "headline_true_positives": report.rates_headline[name].true_positives,
                "headline_false_positives": report.rates_headline[name].false_positives,
                "headline_decided_attacks": report.rates_headline[name].decided_attacks,
                "headline_abstained": report.rates_headline[name].abstained,
                "log_tamper_catch_rate": report.rates_tamper[name].catch_rate,
                "log_tamper_true_positives": report.rates_tamper[name].true_positives,
                "log_tamper_decided_attacks": report.rates_tamper[name].decided_attacks,
            }
            for name, identity in report.identities.items()
        ],
        "variants": [
            {
                "name": variant.sample.name,
                "cohort": variant.sample.cohort,
                "label": variant.sample.label,
                "input_rows": variant.input_rows,
                "right_stick_run_ms": variant.right_stick_run_ms,
                "screen_events": variant.screen_events,
                "motion_permille": motion_scores[variant.sample.name].detail["motion_permille"],
                "motion_check": motion_scores[variant.sample.name].detail["motion_check"],
                "input_proof": variant.input_proof,
                "decisions": {name: report.scores[name][variant.sample.name].decision for name in report.scores},
            }
            for variant in report.variants
        ],
    }
    idle = ", ".join(f"{start}–{end - 1}" for start, end in IDLE_RANGES)
    menu = ", ".join(f"{start}–{end - 1}" for start, end in MENU_RANGES)
    lines = [
        "# Motion rule v3 red team: passthrough aim and log tamper",
        "",
        "Test-only harness under `tests/redteam/`. It scores controller logs by calling `input_energy_series`, `score_motion`, `check_inputs`, `check_checkpoints`, and `check_input_files` in process. It does not sign a receipt, does not hold a referee key, and does not open a network connection. Nothing here is a submission.",
        "",
        "The network guard a test can install around a run is a tripwire. It refuses network modules and a few process calls (socket, subprocess.Popen, os.system, os.spawn, ctypes) for the rest of that process. os.fork followed by os.execv does not trip that hook.",
        "",
        "The AST import ban is best-effort. It does not follow a walrus or tuple alias, functools.partial, map, or a class-attribute holder. It does not see find_spec().loader / exec_module, zipimport, or sys.modules.get passed through another name. It does not read .pyw files, follow a symlinked directory, or notice a new top-level directory. It does not see qorcap sidecar code the referee image downloads.",
        "",
        f"Harness `{report.harness_version}` for package `qor-receipt=={report.package_version}`. Harness sha256 `{report.harness_sha256}` over {len(report.harness_files)} files.",
        "",
        "The harness sha256 moved because `tests/redteam/scorers.py` and `tests/redteam/aimbot_harness.py` changed. It is still the package version plus the sorted in-repo Python files reachable from the harness. It does not include scorer settings.",
        "",
        "## Headline: honest USB log, assisted screen",
        "",
        "A passthrough adapter injects aim into the console. QorCap's USB log records the human and is sealed at capture, so chunk hashes and checkpoint `input_hash` match the logged rows. Input proof passes. The screen shows the injected aim. Motion rule v3 correlates row counts with frame-difference energy and does not read stick axes.",
        "",
        f"Stick-aimbot result for motion rule v3: **{caught}/{len(HONEST_STICK_AIMBOTS)}** flagged (`honest-passthrough-aim`, `honest-blended-aim`, `honest-snap-aim`). The expected result on this rule is 0/3.",
        "",
        "The captured game in view is Madden, where the right stick performs skill moves and hit-stick tackles rather than steering the camera. The two extra honest-seal rows use game-agnostic event kinds: `stick_action` (a right-stick gesture the log does not contain) and `button_action` (a button macro, such as a frame-perfect user catch, with no logged button).",
        "",
        _variant_table(report, "honest-seal"),
        "",
        "`honest-full-idle` is the row-count case the rule does catch: an honestly sealed empty log while the control screen still moves. Its check is `partial`, which is not a FAIL. The stick-aimbot and macro rows stay `pass`.",
        "",
        "## Fixture parameters",
        "",
        f"`tests/review_fixture.py` centers every stick, so this base is synthetic. It is {SECONDS} one-second bins, not a decoded capture and not a recording of a player. `mix(sec, salt)` is a 16-bit mix: start from `((sec + 1) * 2246822519 + salt * 3266489917)`, xor-shift by 16, multiply by `0x85EBCA6B`, xor-shift by 13, keep 16 bits.",
        "",
        f"Idle seconds {idle}: 1–4 rows, frame-difference energy 4–28. Menu seconds {menu}: 22–41 rows (button navigation) and energy 5–34, so the picture is quiet while the log is busy. Play seconds are the rest: latent `15 + mix(sec, 1) % 55`, rows `12 + latent // 2 + mix(sec, 6) % 9`, energy `180 + latent * 7 + mix(sec, 7) % 160 - mix(sec, 8) % 80` with a floor of 40. Play shares the latent across rows and energy. Menu and idle do not. The control permille is above the shipped floor of 300 and below 1000.",
        "",
        f"Honest right-stick skill gestures last {SKILL_HOLD_MS} ms starting at offset {SKILL_OFFSET_MS} on play seconds divisible by 5. Buttons are logged on menu rows (`k % 3 == 0`) and on play seconds divisible by 4 (`k % 8 == 0`). `honest-stick-action` places {UNMATCHED_EVENTS} `stick_action` events at offset {EVENT_AT_MS} where the ±{EVENT_MATCH_WINDOW_MS} ms window has no off-center right stick, and adds 150 energy in those seconds. `honest-button-macro` does the same for `button_action` with no logged button and adds 140 energy.",
        "",
        "Honest aim screens, still on the human log: passthrough adds `30 + mix(sec, 9) % 50` energy on play seconds; blended replaces energy with `0.80 * human + 0.20 * max(0, 200 + 100 * sin(sec / 7))`; snap adds 130 energy on play seconds divisible by 4.",
        "",
        "## Log-tamper test",
        "",
        "These variants rewrite the human rows and leave the human chunk hashes and checkpoint `input_hash` values in place. Input proof fails. The permille column is `score_motion` on the rewritten rows anyway. Stick rewrites keep the row count, so they score like the control. Deleting the rows is the flat series.",
        "",
        _variant_table(report, "log-tamper"),
        "",
        "## Variant index",
        "",
        notes,
        "",
        "## Pluggable scorers",
        "",
        "A scorer implements `name`, `version`, `score(sample) -> Score`, and `identity()`. `Score.flagged` is true, false, or null (abstain). A scorer may compare 100 ms pan bins with the right stick, or video events with logged presses. Both outputs are plug-ins. This report uses stubs that mark every frame and every second `no_claim`.",
        "",
        "Pan bins are `{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}`. Events are `{mono_ms, event_kind, confidence, no_claim}`. `no_claim` leaves the measurements null. Non-finite rates and confidence, and confidence outside 0..1, are rejected.",
        "",
        "Catch rate is flagged attacks divided by decided attacks. False-alarm rate is flagged benign divided by decided benign. An abstention is neither. The rates below are the honest-seal cohort, which is the traffic input proof would let through. Log-tamper rates are in the JSON block.",
        "",
        (
            f"Each scorer sha256 is the hash of `qor-receipt=={report.package_version}`, the scorer and estimator version strings, "
            "the canonical JSON of runtime settings (such as `min_confidence` and any thresholds passed at construction), "
            "an estimator sha256 only when the class object is OpticalFlowPanEstimator, the instance still has the attributes __init__ wrote, and each PanParams field is an exact int or float, recomputed from that module's source (a copied attribute, property, __module__, or __qualname__ is not trusted, and an int subclass or bool does not record the allowlisted hash; patching a method on the OpticalFlowPanEstimator class itself still reads as known), "
            "the sorted repo-relative path plus bytes of the scorer module and its transitive in-repo imports, "
            "the source of an injected estimator or `core` (a hash of that class's own source plus the source of each class it inherits, and, outside the repo, a path relative to the module root), "
            "and declared non-Python weight, model, or data files hashed by bytes. "
            "A file that cannot be read is recorded as `unhashed` and is not dropped. "
            "An implementation whose file cannot be resolved or read makes the identity `unhashed`, and an unhashed result is not cached. "
            "The hashes in the table moved because that coverage is new and because `tests/redteam/scorers.py` changed."
        ),
        "",
        "**SYNTHETIC-DATA CAVEAT:** these rates come from synthetic data with only one benign sample (n=1 control) and are NOT real-world or measured detection/false-alarm rates; '0/1 false alarms' is not a false-alarm rate.",
        "",
        "The 150 ms event window is valid only on synthetic data with a shared clock. On real footage, no video-vs-log mismatch claim is ever allowed: the mismatch claim gate has no allow path, so every mismatch claim is no-claim with ceiling PARTIAL, for every bundle, and older footage stays no-claim. A frame_bind chain (qorcap #75, --frame-bind) that verifies proves frames were not altered after recording, not press-to-frame timing. An allow path may come only in a separate reviewed PR that verifies the bundle itself; even then a mismatch signal may lower a verdict to at most PARTIAL and never raises one.",
        "",
        _md_table(
            ["scorer", "version", "package", "estimator", "file set", "sha256", "headline catch rate", "headline false-alarm rate", "abstained"],
            scorer_rows,
        ),
        "",
        _md_table(decision_headers, decision_rows),
        "",
        "`flag` on `motion-rule-v3` is the `partial` check. `clear` is `pass`. `abstain` means the scorer declined. The stubs decline because they never claim a frame or an event.",
        "",
        "## Reproducing",
        "",
        "```bash",
        "pytest -m known_gap -s tests/test_redteam_aimbot_motion.py",
        "```",
        "",
        "Put `tests` on `PYTHONPATH` before importing the harness yourself (the pytest module does this):",
        "",
        "```python",
        "from redteam.aimbot_harness import evaluate",
        "from redteam.scorers import EventPressAgreementScorer, PanAgreementScorer",
        "",
        "report = evaluate(scorers=[PanAgreementScorer(MyPanEstimator()), EventPressAgreementScorer(MyEventEstimator())])",
        "```",
        "",
        "`evaluate` keeps `motion-rule-v3` in the list when the caller does not pass it.",
        "",
        "```json",
        json.dumps(payload, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)
