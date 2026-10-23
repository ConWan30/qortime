"""Decode the video segments the referee actually received, and run its own readers over them.

Segments are fMP4 fragments (QorCap: segment 0 of each file = ftyp+moov init, then moof+mdat fragments). A
contiguous run of fragments prefixed with its file's init segment is a decodable MP4. Frame times are put on
QorCap's monotonic clock (shared with inputs.jsonl) from the manifest's per-segment ``mono_ms`` stamps.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from qor_receipt.review.bundle import Bundle, segment_ranges


def ffmpeg_bin() -> str | None:
    return os.getenv("QORTIME_FFMPEG") or shutil.which("ffmpeg")


def runs(seqs: Iterable[int], b: Bundle) -> list[list[int]]:
    """Group media segment seqs into contiguous runs within the same file (init segments excluded)."""
    by_seq = {int(m["seq"]): m for m in b.segments}
    out: list[list[int]] = []
    for s in sorted(set(seqs)):
        m = by_seq.get(s)
        if m is None or int(m.get("offset", 0)) == 0:
            continue
        if out and out[-1][-1] == s - 1 and by_seq[s - 1]["file"] == m["file"]:
            out[-1].append(s)
        else:
            out.append([s])
    return out


def init_seq_for(b: Bundle, seq: int) -> int | None:
    by_seq = {int(m["seq"]): m for m in b.segments}
    f = by_seq[seq]["file"]
    cands = [int(m["seq"]) for m in b.segments if m["file"] == f and int(m.get("offset", 0)) == 0 and int(m["seq"]) <= seq]
    return max(cands) if cands else None


def decode_run(b: Bundle, run: list[int], read_seg: Callable[[int], bytes | None], *, hz: float,
               size: tuple[int, int] = (1280, 720), gray: bool = False) -> Iterator[tuple[int, Any]]:
    """Yield (mono_ms, frame) for one contiguous run. Frame time = run start (end of the previous segment) + n/hz."""
    import numpy as np

    ff = ffmpeg_bin()
    if not ff:
        raise RuntimeError("ffmpeg not available on the referee")
    init = init_seq_for(b, run[0])
    if init is None:
        return
    parts = [read_seg(init)] + [read_seg(s) for s in run]
    if any(p is None for p in parts):
        return
    start_mono = segment_ranges(b)[run[0]][0]
    w, h = size
    ch = 1 if gray else 3
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        for p in parts:
            tf.write(p)
        path = tf.name
    try:
        p = subprocess.Popen([ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", path, "-an",
                              "-vf", f"fps={hz},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray" if gray else "bgr24",
                              "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        n, fb = 0, w * h * ch
        try:
            while True:
                buf = p.stdout.read(fb)
                if len(buf) < fb:
                    break
                img = np.frombuffer(buf, np.uint8).reshape((h, w) if gray else (h, w, 3))
                yield start_mono + int(n * 1000 / hz), img
                n += 1
        finally:
            if p.poll() is None:
                p.kill()  # consumer stopped early (e.g. end menu found)
            p.stdout.close()
            p.wait()
        if n == 0:
            raise RuntimeError(f"ffmpeg decoded no frames from segments {run[0]}..{run[-1]}")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- final re-read (QorCap's own readers)
def sidecar_profiles(sidecar_dir: str | None = None) -> list[str]:
    """Scorebug profiles the referee can read: QorTime's vendored standard pack + packs shipped in the QorCap sidecar."""
    out = ["madden27_standard"]
    d = sidecar_dir or os.getenv("QORCAP_SIDECAR")
    if d and (Path(d) / "qorcap_mnp.py").exists() and (Path(d) / "packs" / "madden27_monday_night_v1.npz").exists():
        out.append("madden27_monday_night")
    if d and (Path(d) / "qorcap_snp.py").exists() and (Path(d) / "packs" / "madden27_sunday_night_v1.npz").exists():
        out.append("madden27_sunday_night")
    if d and (Path(d) / "qorcap_tnp.py").exists() and (Path(d) / "packs" / "madden27_thursday_night_v1.npz").exists():
        out.append("madden27_thursday_night")
    return out


def sidecar_core_factory(sidecar_dir: str | None = None):
    """ReaderCore exactly as QorCap builds it (scorebug + end-of-game menu readers), imported from a pinned QorCap
    sidecar checkout (``$QORCAP_SIDECAR``). Returns None when unavailable (the review then fails closed)."""
    d = sidecar_dir or os.getenv("QORCAP_SIDECAR")
    if not d or not (Path(d) / "qorcap_reader.py").exists():
        return None
    if d not in sys.path:
        sys.path.insert(0, d)
    from qorcap_endmenu import EndMenuReader  # type: ignore
    from qorcap_mnp import AutoProfileReader  # type: ignore
    from qorcap_reader import ReaderCore, madden_final_screen, prep  # type: ignore

    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    make_reader = sidecar_standard_reader(d)

    def factory(post):
        return ReaderCore(post, AutoProfileReader(make_reader(), prep=prep), AgreementWindow(), PauseDetector(),
                          detect_final, final_screen_detector=madden_final_screen, end_menu_reader=EndMenuReader())
    return factory


def sidecar_standard_reader(sidecar_dir: str | None = None):
    """Constructor for the standard-scorebug reader QorCap itself uses: ``qorcap_standard.standard_reader()``
    (QorTime's ``madden27_standard`` pack plus QorCap's real-glyph variants, e.g. the 084523 red "4"; same 0.70 gate).
    QorCap builds every ReaderCore with it (Start Match, finalize, offline re-read). The referee used QorTime's bare
    ``ScorebugReader()`` and so could not read scorebugs QorCap reads (match-20261018-084523: 0 tail reads ->
    final_reread FAIL). Older sidecars without ``qorcap_standard`` fall back to the bare reader."""
    d = sidecar_dir or os.getenv("QORCAP_SIDECAR")
    if d and (Path(d) / "qorcap_standard.py").exists():
        if d not in sys.path:
            sys.path.insert(0, d)
        from qorcap_standard import standard_reader  # type: ignore
        return standard_reader
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    return ScorebugReader


def sidecar_corroboration(bundle_dir: str | None, sidecar_dir: str | None = None) -> list[dict]:
    d = sidecar_dir or os.getenv("QORCAP_SIDECAR")
    if not bundle_dir or not d or not (Path(d) / "qorcap_reader.py").exists():
        return []
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        from qorcap_reader import load_delta_corroboration_from_bundle  # type: ignore
        return load_delta_corroboration_from_bundle(bundle_dir)
    except Exception:
        return []


def reread_final(frames: Iterable[tuple[int, Any]], core_factory, *, corroboration: list[dict] | None = None,
                 timeline_locks: list[dict] | None = None) -> dict[str, Any]:
    """Feed the referee's decoded tail frames (t_ms = wall clock) through a fresh ReaderCore. No laptop timeline seed.

    ``timeline_locks`` (the bundle's committed ``timeline.jsonl`` score locks) never supply a score. They are only
    used by ``end_menu_fallback`` to REFUSE an end-of-game menu that conflicts with, or cannot follow, a locked read."""
    events: list[dict] = []
    core = core_factory(lambda e: events.append(e) or {})
    core.delta_corroboration = list(corroboration or [])
    for t, img in frames:
        core.on_frame(t, img)
        if getattr(core, "end_menu", None) is not None:
            break
    reads = [e for e in events if e.get("type") == "score"]
    rejected = getattr(core, "end_menu_rejected", None)
    final = core.final
    fallback = None
    if final is None and rejected:
        final, why = end_menu_fallback(rejected, reads, timeline_locks)
        fallback = {"accepted": final is not None, "why": why}
    return {"final": final, "last_read": reads[-1] if reads else None, "reads": len(reads),
            "end_menu": getattr(core, "end_menu", None), "end_menu_rejected": rejected,
            "end_menu_fallback": fallback, "stats": dict(core.stats)}


# ---------------------------------------------------------------- end-of-game menu without a tail scorebug read
END_MENU_FALLBACK_REASON = "no Q4 scorebug read before the end-of-game menu"
END_MENU_FALLBACK_MIN_CONF = 0.90
END_MENU_FALLBACK_MIN_FRAMES = 2
LEGAL_STEPS = frozenset({1, 2, 3, 6, 7, 8})


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def locked_reads(rows: Iterable[dict] | None) -> list[dict]:
    """Agreed score locks (``home``/``away`` ints, not an ``obs`` row) in time order."""
    out = [r for r in rows or [] if isinstance(r, dict) and "obs" not in r and _int(r.get("home")) and _int(r.get("away"))
           and r["home"] >= 0 and r["away"] >= 0]
    return sorted(out, key=lambda r: r.get("t_ms") if _int(r.get("t_ms")) else 0)


def timeline_locks_from_bytes(raw: bytes | None) -> list[dict]:
    import json

    rows = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict):
            rows.append(o)
    return locked_reads(rows)


def end_menu_fallback(rejected: dict, tail_reads: list[dict] | None, timeline_locks: list[dict] | None
                      ) -> tuple[dict | None, str]:
    """Accept an end-of-game menu that QorCap's ReaderCore rejected ONLY because no Q4 scorebug was read in the tail.

    Fail closed. Every one of these must hold, otherwise ``(None, reason)``:
      * the rejection reason is exactly "no Q4 scorebug read before the end-of-game menu" (a clock, illegal-step,
        tie or any other rejection stays refused);
      * the menu read is real-template (not a partly covered "corroborated" panel), confidence >= 0.90 over >= 2
        agreeing frames, not tied;
      * there is at least one locked read to compare with (the referee's own tail reads first, else the bundle's
        committed timeline locks); none of them is above the menu score on either side (scores never go down);
      * the menu is reachable from the last locked read (each side's increase is 0 or made of legal plays).
    ``final_confirmed`` is true only when the menu equals the last locked read, or is one legal play past a last
    locked read in Q4/OT. Anything else is posted unconfirmed, which the referee reports as PARTIAL, never PASS."""
    if not isinstance(rejected, dict) or rejected.get("why") != END_MENU_FALLBACK_REASON:
        return None, f"end menu rejected for another reason: {(rejected or {}).get('why')}"
    away, home = rejected.get("away"), rejected.get("home")
    if not (_int(away) and _int(home)) or away < 0 or home < 0:
        return None, "end menu score missing"
    conf, frames = rejected.get("conf"), rejected.get("frames")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or conf < END_MENU_FALLBACK_MIN_CONF:
        return None, f"end menu confidence {conf} below {END_MENU_FALLBACK_MIN_CONF}"
    if not _int(frames) or frames < END_MENU_FALLBACK_MIN_FRAMES:
        return None, f"end menu agreed on {frames} frame(s), need {END_MENU_FALLBACK_MIN_FRAMES}"
    if "corroborated" in (rejected.get("template_sources") or []):
        return None, "end menu panel partly covered (corroboration only confirms a scorebug final)"
    if home == away:
        return None, "tied score on the end-of-game menu"
    if 1 in (home, away):
        return None, "a final of 1 point is not a legal football score here"
    tail = locked_reads(tail_reads)
    tl = locked_reads(timeline_locks)
    every = tail + tl
    if not every:
        return None, "no locked score read to check the end menu against"
    for r in every:
        if r["away"] > away or r["home"] > home:
            return None, (f"end menu AWAY {away} - HOME {home} conflicts with locked read AWAY {r['away']} - "
                          f"HOME {r['home']} (scores never go down)")
    ref = tail[-1] if tail else tl[-1]
    d = [away - ref["away"], home - ref["home"]]
    exact = d == [0, 0]
    one_step = min(d) == 0 and max(d) in LEGAL_STEPS
    # +1 alone is only a try after a touchdown already on the board; any other side total is a sum of 2/3/6/7/8.
    if 1 in d and not one_step:
        return None, f"end menu is not reachable from AWAY {ref['away']} - HOME {ref['home']}"
    q = ref.get("quarter")
    late = _int(q) and q >= 4
    confirmed = exact or (one_step and late)
    src = "referee tail read" if tail else "bundle timeline lock"
    notes = [f"referee end-menu fallback: menu read at {conf:.3f} over {frames} frames with no Q4 scorebug read in "
             f"the tail; checked against the last locked read ({src}) AWAY {ref['away']} - HOME {ref['home']}"
             + (f" Q{q}" if _int(q) else "")]
    if not confirmed:
        notes.append("end menu not equal to (or one legal play past a Q4) locked read: final_confirmed=false")
    final = {"type": "final", "basis": "end_menu", "method": "end_menu_read", "home": home, "away": away,
             "quarter": max(4, int(q) if _int(q) else 4), "t_ms": rejected.get("t_ms"),
             "end_menu": {k: rejected.get(k) for k in ("away", "home", "t_ms", "until_ms", "frames", "conf", "margin",
                                                       "template_sources", "xcheck")},
             "scorebug_last": {k: ref.get(k) for k in ("home", "away", "quarter", "t_ms")},
             "notes": notes, "final_confirmed": bool(confirmed), "fallback": "end_menu_without_tail_scorebug",
             "fallback_reference": src}
    return final, "accepted"


# ---------------------------------------------------------------- pause onset (challenges) + motion
@dataclass
class PauseScan:
    """What the pause detector saw. Notes are built from this; it is not a receipt field."""

    onsets: list[int]
    frames_decoded: int
    samples: list[dict[str, Any]]
    error: str | None = None
    max_luma: float = 16.0
    max_lower: float = 8.0
    bright_min: float = 0.005
    bright_max: float = 0.05


def scan_pause(frames: Iterable[tuple[int, Any]], detector=None) -> PauseScan:
    """Run the detector and keep per-frame luma so a miss can say what the darkest frame was."""
    from qor_receipt.agent.pause_detect import PauseDetector

    det = detector or PauseDetector()
    th = det.th
    samples: list[dict[str, Any]] = []
    error = None
    try:
        for t, img in frames:
            det.update(int(t), img)
            f = det.last
            samples.append({"t_ms": int(t),
                            "full_luma": round(float(f.get("full_luma", 0.0)), 2),
                            "lower_luma": round(float(f.get("lower_luma", 0.0)), 2),
                            "bright_frac": round(float(f.get("bright_frac", 0.0)), 4)})
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    return PauseScan(onsets=[] if error else list(det.onsets), frames_decoded=len(samples), samples=samples,
                     error=error, max_luma=float(th.dark_menu_max_luma), max_lower=float(th.dark_menu_max_lower),
                     bright_min=float(th.dark_menu_bright_min), bright_max=float(th.dark_menu_bright_max))


def pause_onsets(frames: Iterable[tuple[int, Any]], detector=None) -> list[int]:
    """Mono times where the pause menu first appears in the decoded frames."""
    from qor_receipt.agent.pause_detect import PauseDetector

    det = detector or PauseDetector()
    for t, img in frames:
        det.update(t, img)
    return list(det.onsets)


def _caps_text(scan: PauseScan | None, thresholds) -> str:
    if scan is not None:
        luma, lower, lo, hi = scan.max_luma, scan.max_lower, scan.bright_min, scan.bright_max
    elif thresholds is not None:
        luma, lower = thresholds.dark_menu_max_luma, thresholds.dark_menu_max_lower
        lo, hi = thresholds.dark_menu_bright_min, thresholds.dark_menu_bright_max
    else:
        return ""
    return f"; caps full<={luma:.0f} lower<={lower:.0f} bright={lo:.3f}..{hi:.2f}"


def _dark_text(sample: dict | None) -> str:
    if not sample:
        return "none"
    return (f"t_ms={sample['t_ms']} full_luma={sample['full_luma']:.2f} "
            f"lower_luma={sample['lower_luma']:.2f} bright_frac={sample['bright_frac']:.4f}")


def pause_not_seen_note(phase: str, scan: PauseScan | None, press_ms: int | None = None, *,
                        missing: list[int] | None = None, prior: str | None = None,
                        thresholds=None, rule: int | None = None) -> str:
    """Free-form review note. The result reason stays ``pause_not_seen_on_video``."""
    from qor_receipt.agent.pause_detect import ONSET_HORIZON_MS, ONSET_SLACK_MS

    samples = [] if scan is None or missing else scan.samples
    near: list[dict] = []
    darkest = None
    if press_ms is not None and samples:
        near = [s for s in samples if press_ms - ONSET_SLACK_MS <= s["t_ms"] <= press_ms + ONSET_HORIZON_MS]
        if near:
            darkest = min(near, key=lambda s: (s["full_luma"], s["t_ms"]))
    if missing:
        shown = ",".join(str(s) for s in list(missing)[:12])
        more = f" (+{len(missing) - 12} more)" if len(missing) > 12 else ""
        cause, frames_decoded, frames_near, darkest = f"required segments missing seq={shown}{more}", 0, 0, None
    elif scan is None:
        cause = "ffmpeg not available" if not prior or "ffmpeg" in prior.lower() else prior
        frames_decoded, frames_near, darkest = 0, 0, None
    elif scan.error and not scan.frames_decoded:
        cause, frames_decoded, frames_near, darkest = f"decode failed ({scan.error})", 0, 0, None
    elif scan.error:
        cause, frames_decoded, frames_near = f"decode failed ({scan.error})", scan.frames_decoded, len(near)
    elif not scan.frames_decoded:
        cause, frames_decoded, frames_near, darkest = "no frames decoded", 0, 0, None
    elif press_ms is not None and not near:
        cause = "frames decoded but none in the onset window around the press"
        frames_decoded, frames_near, darkest = scan.frames_decoded, 0, None
    else:
        in_window = [o for o in scan.onsets if press_ms is not None and press_ms - ONSET_SLACK_MS <= o <= press_ms + ONSET_HORIZON_MS]
        if scan.onsets and not in_window:
            shown = ",".join(str(o) for o in scan.onsets[:6])
            cause = f"frames decoded but no menu in the onset window (onsets at {shown})"
        else:
            cause = "frames decoded but no menu found"
        frames_decoded, frames_near = scan.frames_decoded, len(near)
    rule_bit = f"; rule={rule}" if rule is not None else ""
    return (f"{phase}: pause_not_seen_on_video: {cause}{rule_bit}; frames_decoded={frames_decoded}; "
            f"frames_near_press={frames_near}; darkest_near_press={_dark_text(darkest)}{_caps_text(scan, thresholds)}")


def install_rule_pause(core: Any, rule: int) -> Any:
    """Point a ReaderCore's pause detector at this challenge rule's caps.

    Cores with no pause detector (the review test double) are left alone. Rule v1 keeps the
    historical caps so a brighter static screen is not newly treated as a pause during the
    final re-read. Rule v2 uses the measured capture-path caps. ReaderCore checks the
    end-of-game menu before this detector.
    """
    if getattr(core, "pause", None) is None:
        return core
    from qor_receipt.agent.pause_detect import detector_for_challenge_rule

    core.pause = detector_for_challenge_rule(rule)
    return core


def motion_series(frames: Iterable[tuple[int, Any]], bin_ms: int = 1_000) -> dict[int, int]:
    """Per-bin mean absolute frame difference (gray thumbnails) keyed by bin index on the mono clock."""
    import numpy as np

    prev, acc = None, {}
    for t, img in frames:
        g = img if img.ndim == 2 else img.mean(axis=2)
        g = g.astype(np.int16)
        if prev is not None and prev.shape == g.shape:
            k = t // bin_ms
            s, n = acc.get(k, (0, 0))
            acc[k] = (s + int(np.abs(g - prev).mean() * 100), n + 1)
        prev = g
    return {k: s // max(n, 1) for k, (s, n) in acc.items()}


def input_energy_series(b: Bundle, bin_ms: int = 1_000) -> dict[int, int]:
    """Per-bin count of logged controller reports that changed buttons/triggers/sticks (mono clock)."""
    acc: dict[int, int] = {}
    for r in b.inputs:
        if r.get("type") == "in" and isinstance(r.get("t_us"), int):
            k = (r["t_us"] // 1000) // bin_ms
            acc[k] = acc.get(k, 0) + 1
    return acc
