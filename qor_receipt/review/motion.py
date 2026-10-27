"""Motion rule v3/v4: correlate controller report rate with frame-difference energy on live-play seconds.

v3 (historical): stratified random spread of at most 24 media fragments from the referee's
``SystemRandom`` after commit. Old receipts store that integer and ``motion-rule/v3`` basis;
the verifier still only compares the stored integer with 300.

v4 (current): same correlation and exclusions, but (1) the sampling RNG is derived only from
bundle content heads (chain / checkpoint / inputs / mode) — never the submission id — so the
same bytes always draw the same sample, and (2) up to 48 segments per draw with the median of
K=3 independent deterministic stratum draws. Floor stays 300. Client labels are not an input.
"""
from __future__ import annotations

import hashlib
import random
import statistics
from typing import Any, Iterable

# Same set the live agent treats as "no scorebug strip" (runner.NOT_VISIBLE). A strip that is
# present but unreadable (blur, covered panel, unsure quarter) is not absence.
SCOREBUG_ABSENT_REASONS = frozenset({"layout_unknown"})
# The reader refused the frame geometry. That is not a measurement of the picture.
_GEOMETRY_REASONS = frozenset({"frame_too_small", "frame_invalid", "frame_aspect"})

MOTION_RULE_V3 = 3
MOTION_RULE = 4
MOTION_SAMPLE_MAX_SEGMENTS_V3 = 24
MOTION_SAMPLE_MAX_SEGMENTS = 48
MOTION_MEDIAN_DRAWS = 3
MOTION_MIN_BINS = 20
MOTION_MAX_LAG = 3
MOTION_BIN_MS = 1_000
MOTION_HZ = 10.0
MOTION_SIZE = (640, 360)

MOTION_BASIS_V3 = (
    "motion-rule/v3: controller report rate vs frame-difference energy per second on a stratified "
    "random spread of at most 24 media segments drawn after commit, timed with the fragment PTS "
    "clock (segment close stamps only when a fragment has no tfdt); bins dropped only for "
    "referee-measured scorebug absence, end menu, final screen, or challenge pause"
)
MOTION_BASIS_V4 = (
    "motion-rule/v4: controller report rate vs frame-difference energy per second; median of 3 "
    "deterministic stratified draws of at most 48 media segments each, seeded only from chain_head / "
    "checkpoint head / inputs chain head / mode (never submission id), timed with the fragment PTS "
    "clock (segment close stamps only when a fragment has no tfdt); bins dropped only for "
    "referee-measured scorebug absence, end menu, final screen, or challenge pause"
)
MOTION_BASIS = MOTION_BASIS_V4


def _media_segments(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Media fragments only, ordered by manifest time. Init segments and client labels are ignored."""
    media = [m for m in segments if int(m.get("offset", 0) or 0) > 0]
    media.sort(key=lambda m: (int(m.get("mono_ms", 0) or 0), int(m["seq"])))
    return media


def _time_strata(media: list[dict[str, Any]], n: int) -> list[list[dict[str, Any]]]:
    """Split ``media`` into ``n`` equal spans of ``mono_ms``. The last fragment stays in the last stratum."""
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    t0 = int(media[0].get("mono_ms", 0) or 0)
    t1 = int(media[-1].get("mono_ms", 0) or 0)
    span = t1 - t0
    if span <= 0:
        buckets[0] = list(media)
        return buckets
    for m in media:
        mono = int(m.get("mono_ms", 0) or 0)
        if mono >= t1:
            idx = n - 1
        else:
            idx = int((mono - t0) * n / span)
            if idx < 0:
                idx = 0
            elif idx >= n:
                idx = n - 1
        buckets[idx].append(m)
    return buckets


def motion_sample_seqs(segments: Iterable[dict[str, Any]], rng: Any,
                       max_segments: int = MOTION_SAMPLE_MAX_SEGMENTS) -> list[int]:
    """One media seq per equal time stratum, chosen with ``rng``.

    At most ``max_segments`` fragments. A match with no more media than that is scored in full,
    and ``rng`` is not used. A longer match is split into ``max_segments`` equal ``mono_ms``
    strata; each non-empty stratum contributes ``rng.choice``. The choice uses only ``seq``,
    ``offset``, and ``mono_ms``. A client label cannot add or remove a segment.
    """
    media = _media_segments(segments)
    if max_segments <= 0 or not media:
        return []
    if len(media) <= max_segments:
        return [int(m["seq"]) for m in media]
    chosen: list[int] = []
    for bucket in _time_strata(media, max_segments):
        if not bucket:
            continue
        chosen.append(int(rng.choice(bucket)["seq"]))
    return sorted(set(chosen))


def motion_content_seed(*, chain_head: str, checkpoint_head: str, input_head: str, mode: str) -> int:
    """64-bit seed from bundle content heads only. Submission id must never enter this material."""
    material = "|".join([
        "motion-rule/v4",
        str(chain_head),
        str(checkpoint_head),
        str(input_head),
        str(mode),
    ]).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def motion_draw_rng(content_seed: int, draw_index: int) -> random.Random:
    """Independent deterministic RNG for draw ``draw_index`` (0..K-1)."""
    material = f"motion-rule/v4/draw|{content_seed}|{int(draw_index)}".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return random.Random(seed)


def motion_sample_draws(
    segments: Iterable[dict[str, Any]],
    *,
    chain_head: str,
    checkpoint_head: str,
    input_head: str,
    mode: str,
    max_segments: int = MOTION_SAMPLE_MAX_SEGMENTS,
    draws: int = MOTION_MEDIAN_DRAWS,
) -> list[list[int]]:
    """K deterministic stratified samples. Same content heads + mode → same draws every time."""
    base = motion_content_seed(
        chain_head=chain_head,
        checkpoint_head=checkpoint_head,
        input_head=input_head,
        mode=mode,
    )
    out: list[list[int]] = []
    for i in range(int(draws)):
        out.append(motion_sample_seqs(segments, motion_draw_rng(base, i), max_segments=max_segments))
    return out


def _pearson_permille(xs: list[int], ys: list[int]) -> int | None:
    """Pearson ×1000. None when either series has no variance. Same rounding as the list helper."""
    n = len(xs)
    if n < 8 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx == 0 or syy == 0:
        return None
    return int(round(1000 * sxy / (sxx * syy) ** 0.5))


def correlate_keyed(energy: dict[int, int], motion: dict[int, int], *, max_lag: int = MOTION_MAX_LAG,
                    min_bins: int = MOTION_MIN_BINS) -> int | None:
    """Best Pearson ×1000, video lagging the controller by 0..``max_lag`` absolute seconds.

    ``motion`` keys are the seconds being scored. Inside that span a missing controller second
    is 0, so an idle second stays in the series. A lag that would read past the first scored
    second drops that pair, the same way the list helper shortens by ``lag``, instead of
    inventing a 0 outside the span. None when fewer than ``min_bins`` video seconds remain.
    -1000 when every lag has a flat series (a constant report rate, including all zeros).
    """
    keys = sorted(motion)
    if len(keys) < min_bins:
        return None
    lo, hi = keys[0], keys[-1]
    best = -1000
    scored = False
    for lag in range(0, max_lag + 1):
        xs: list[int] = []
        ys: list[int] = []
        for k in keys:
            src = k - lag
            if src < lo or src > hi:
                continue
            xs.append(int(energy.get(src, 0)))
            ys.append(int(motion[k]))
        r = _pearson_permille(xs, ys)
        if r is None:
            continue
        scored = True
        if r > best:
            best = r
    return best if scored else -1000


def scorebug_absent_bins(frame_reasons: Iterable[tuple[int, str]], *, bin_ms: int = MOTION_BIN_MS) -> set[int]:
    """Seconds where the scorebug reader measured absence.

    ``layout_unknown`` counts only after this sample itself has one ``ok`` read. Geometry
    refusals (``frame_too_small``, ``frame_invalid``, ``frame_aspect``) are not absence and are
    not votes. A second is absent when at least half of its classified frames are
    ``layout_unknown``.
    """
    by_bin: dict[int, list[str]] = {}
    saw_ok = False
    for t, reason in frame_reasons:
        if reason == "ok":
            saw_ok = True
        if reason in _GEOMETRY_REASONS:
            continue
        by_bin.setdefault(int(t) // bin_ms, []).append(reason)
    if not saw_ok:
        return set()
    out: set[int] = set()
    for k, reasons in by_bin.items():
        if not reasons:
            continue
        absent = sum(1 for r in reasons if r in SCOREBUG_ABSENT_REASONS)
        if absent * 2 >= len(reasons):
            out.add(k)
    return out


def flagged_bins(hits: Iterable[tuple[int, bool]], *, bin_ms: int = MOTION_BIN_MS) -> set[int]:
    """Seconds in which at least one referee frame was the screen in question."""
    return {int(t) // bin_ms for t, hit in hits if hit}


def pause_span_bins(spans_mono: Iterable[tuple[int, int]], *, bin_ms: int = MOTION_BIN_MS) -> set[int]:
    """Seconds overlapping a measured pause span. The span is the menu, not the issue window."""
    out: set[int] = set()
    for a, z in spans_mono:
        a, z = int(a), int(z)
        if z < a:
            a, z = z, a
        for k in range(a // bin_ms, z // bin_ms + 1):
            out.add(k)
    return out


def exclusion_bins(*, scorebug_reasons: Iterable[tuple[int, str]] = (),
                   end_menu: Iterable[tuple[int, bool]] = (),
                   final_screen: Iterable[tuple[int, bool]] = (),
                   pause_spans_mono: Iterable[tuple[int, int]] = ()) -> dict[str, set[int]]:
    """Union of referee measurements. There is no parameter for a client label."""
    parts = {
        "scorebug_absent": scorebug_absent_bins(scorebug_reasons),
        "end_menu": flagged_bins(end_menu),
        "final_screen": flagged_bins(final_screen),
        "challenge_pause": pause_span_bins(pause_spans_mono),
    }
    parts["all"] = set().union(*[parts[k] for k in ("scorebug_absent", "end_menu", "final_screen", "challenge_pause")])
    return parts


def score_motion(energy: dict[int, int], motion: dict[int, int], *,
                 scorebug_reasons: Iterable[tuple[int, str]] = (),
                 end_menu: Iterable[tuple[int, bool]] = (),
                 final_screen: Iterable[tuple[int, bool]] = (),
                 pause_spans_mono: Iterable[tuple[int, int]] = (),
                 sample_segments: list[int],
                 clock_fallbacks: int = 0,
                 screen_readers: bool = False,
                 rule: int = MOTION_RULE,
                 basis: str | None = None,
                 sample_max_segments: int | None = None,
                 extra_detail: dict[str, Any] | None = None) -> tuple[int | None, dict[str, Any]]:
    """Permille and the review-check detail for one motion-rule sample.

    ``motion`` is every decoded video second before exclusions. Counts in ``excluded`` are those
    seconds, not spans that fell outside the sample.
    """
    parts = exclusion_bins(scorebug_reasons=scorebug_reasons, end_menu=end_menu, final_screen=final_screen,
                           pause_spans_mono=pause_spans_mono)
    present = set(motion)
    dropped = {k: len(parts[k] & present) for k in ("scorebug_absent", "end_menu", "final_screen", "challenge_pause")}
    dropped["total"] = len(parts["all"] & present)
    kept = {k: v for k, v in motion.items() if k not in parts["all"]}
    mp = correlate_keyed(energy, kept)
    detail: dict[str, Any] = {
        "rule": int(rule),
        "basis": basis if basis is not None else (MOTION_BASIS_V4 if rule == MOTION_RULE else MOTION_BASIS_V3),
        "bins": len(kept),
        "decoded_bins": len(motion),
        "sample_segments": list(sample_segments),
        "sample_max_segments": int(
            sample_max_segments if sample_max_segments is not None
            else (MOTION_SAMPLE_MAX_SEGMENTS if rule == MOTION_RULE else MOTION_SAMPLE_MAX_SEGMENTS_V3)
        ),
        "clock": "fragment_pts",
        "clock_fallbacks": int(clock_fallbacks),
        "screen_readers": bool(screen_readers),
        "excluded": dropped,
    }
    if extra_detail:
        detail.update(extra_detail)
    if mp is None:
        detail["note"] = "too few decoded seconds for a motion correlation"
    else:
        detail["motion_permille"] = mp
    return mp, detail


def bins_owned_by(bin_segs: dict[int, set[int]], sample: Iterable[int]) -> set[int]:
    """Seconds whose decoded frames came from at least one segment in ``sample``."""
    want = set(int(s) for s in sample)
    return {k for k, segs in bin_segs.items() if segs & want}


def median_permille(values: list[int | None]) -> int | None:
    """Median of scored draws. None only when every draw failed to score."""
    scored = [int(v) for v in values if v is not None]
    if not scored:
        return None
    return int(statistics.median(scored))


def score_motion_v4(
    energy: dict[int, int],
    motion: dict[int, int],
    bin_segs: dict[int, set[int]],
    draws: list[list[int]],
    *,
    scorebug_reasons: Iterable[tuple[int, str]] = (),
    end_menu: Iterable[tuple[int, bool]] = (),
    final_screen: Iterable[tuple[int, bool]] = (),
    pause_spans_mono: Iterable[tuple[int, int]] = (),
    clock_fallbacks: int = 0,
    screen_readers: bool = False,
) -> tuple[int | None, dict[str, Any]]:
    """Score each deterministic draw on its own bins; return the median permille and v4 detail."""
    draw_scores: list[int | None] = []
    draw_details: list[dict[str, Any]] = []
    for sample in draws:
        keep = bins_owned_by(bin_segs, sample)
        sub_motion = {k: v for k, v in motion.items() if k in keep}
        mp, det = score_motion(
            energy,
            sub_motion,
            scorebug_reasons=scorebug_reasons,
            end_menu=end_menu,
            final_screen=final_screen,
            pause_spans_mono=pause_spans_mono,
            sample_segments=list(sample),
            clock_fallbacks=clock_fallbacks,
            screen_readers=screen_readers,
            rule=MOTION_RULE,
            basis=MOTION_BASIS_V4,
            sample_max_segments=MOTION_SAMPLE_MAX_SEGMENTS,
        )
        draw_scores.append(mp)
        draw_details.append(det)
    med = median_permille(draw_scores)
    # Prefer the middle draw's bin/exclusion counts when all scored; else first scored draw.
    scored_dets = [d for s, d in zip(draw_scores, draw_details) if s is not None]
    if scored_dets:
        ranked = sorted(
            [(s, d) for s, d in zip(draw_scores, draw_details) if s is not None],
            key=lambda x: x[0],
        )
        mid = ranked[len(ranked) // 2][1]
    else:
        mid = draw_details[0] if draw_details else {}
    union = sorted(set().union(*[set(s) for s in draws])) if draws else []
    detail: dict[str, Any] = {
        "rule": MOTION_RULE,
        "basis": MOTION_BASIS_V4,
        "bins": mid.get("bins", 0),
        "decoded_bins": len(motion),
        "sample_segments": union,
        "sample_draws": [list(s) for s in draws],
        "draw_permilles": draw_scores,
        "median_draws": len(draws),
        "sample_max_segments": MOTION_SAMPLE_MAX_SEGMENTS,
        "clock": "fragment_pts",
        "clock_fallbacks": int(clock_fallbacks),
        "screen_readers": bool(screen_readers),
        "excluded": mid.get("excluded", {
            "scorebug_absent": 0, "end_menu": 0, "final_screen": 0, "challenge_pause": 0, "total": 0,
        }),
    }
    if med is None:
        detail["note"] = "too few decoded seconds for a motion correlation"
    else:
        detail["motion_permille"] = med
    return med, detail
