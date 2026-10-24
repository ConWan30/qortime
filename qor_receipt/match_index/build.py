from __future__ import annotations

import bisect
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from qor_receipt.jcs import sha256_hex

SCHEMA = "qortime-match-index/v0"
FPS = 30.0
BUTTONS = ["CROSS", "CIRCLE", "SQUARE", "TRIANGLE", "L1", "R1", "L2", "R2", "CREATE", "OPTIONS", "L3", "R3",
           "UP", "DOWN", "LEFT", "RIGHT", "PS", "TOUCHPAD", "MUTE", "FN_L", "FN_R", "BACK_L", "BACK_R"]  # qorcap src/hid.rs
ADVISORY = "Advisory evidence only: controller inputs/haptics show activity in sync with the video, not who played or what happened."


def public_device(value: Any) -> str | None:
    """Controller product name only. Instance ids and local paths stay off the public page."""
    if not isinstance(value, str):
        return None
    name = value.split(" id=", 1)[0].strip()
    if not name or any(tok in name for tok in ("\\", "HID#", "@device", "/Users/", ":/")):
        return None
    return name


PRE_S, POST_S, N_FRAMES = 3.0, 2.0, 10
# Scoring plays: the scorebug only shows the new score after the play, celebration and replay (Madden: often 10-20 s after a TD
# crosses the goal line), so a burst at the score change shows the try/PAT setup. Scoring-play bursts therefore pre-roll back
# to just before the last read of the OLD score (the play happened between that read and the first new read).
PLAY_PAD_S, MAX_PRE_S, PLAY_FRAMES = 4.0, 40.0, 18
TRY_WINDOW_MS = 120_000  # a +1/+2 by the team that just went +6 within this window is the try after the touchdown
INPUT_WIN = (-5.0, 3.0)
STRIP_WIN, STRIP_BIN = (-10.0, 5.0), 0.25
STABLE_READS = 2


def _jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def open_bundle(path: str | Path) -> tuple[Path, Path | None]:
    """Return (dir with bundle.json, temp dir to clean). Accepts a dir, a bundle-meta.zip, or a dir holding one."""
    p = Path(path)
    if p.is_dir() and (p / "bundle.json").exists():
        return p, None
    z = p if p.is_file() else p / "bundle-meta.zip"
    if z.is_file() and zipfile.is_zipfile(z):
        tmp = Path(tempfile.mkdtemp(prefix="qt-index-"))
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp)
        hits = list(tmp.rglob("bundle.json"))
        return (hits[0].parent if hits else tmp), tmp
    raise FileNotFoundError(f"no bundle.json / bundle-meta.zip at {path}")


def bitnames(b: int) -> list[str]:
    return [n for i, n in enumerate(BUTTONS) if b >> i & 1]


class Clock:
    """wall_ms <-> recording video seconds, from manifest (mono_ms, cumulative frames) points."""

    def __init__(self, manifest: list[dict]):
        segs = [m for m in manifest if m.get("type") == "segment" and "mono_ms" in m and "wall_ms" in m]
        offs = sorted(m["wall_ms"] - m["mono_ms"] for m in segs)
        self.wall_minus_mono = offs[len(offs) // 2] if offs else None
        pts = [(m["wall_ms"], m["frames"] / FPS) for m in segs if isinstance(m.get("frames"), (int, float))]
        self.pts = sorted(pts)
        self.duration_s = self.pts[-1][1] if self.pts else None

    def video_s(self, wall_ms: float) -> float | None:
        if len(self.pts) < 2:
            return None
        xs = [p[0] for p in self.pts]
        i = min(max(bisect.bisect_left(xs, wall_ms), 1), len(xs) - 1)
        (x0, y0), (x1, y1) = self.pts[i - 1], self.pts[i]
        if x1 == x0:
            return y0
        return y0 + (wall_ms - x0) * (y1 - y0) / (x1 - x0)


def _inputs(rows: list[dict]) -> tuple[list[tuple[int, int]], dict]:
    """Rising-edge presses [(wall_ms, bits)] from qorcap-inputs/v0, plus header."""
    header = next((r for r in rows if r.get("type") == "header"), {})
    anchor = header.get("wall_anchor_ms")
    presses, prev = [], 0
    if anchor is None:
        return presses, header
    for r in rows:
        if r.get("type") != "in" or "b" not in r:
            continue
        b = int(r["b"])
        rise = b & ~prev
        prev = b
        if rise:
            presses.append((int(anchor + r["t_us"] / 1000), rise))
    return presses, header


def _score_reads(tl: list[dict]) -> list[dict]:
    return [r for r in tl if "home" in r and "away" in r and r.get("home") is not None and r.get("away") is not None]


def detect_events(tl: list[dict], result: dict | None, final: dict | None, final_confirmed: bool) -> list[dict]:
    reads = _score_reads(tl)
    ev: list[dict] = []
    if not reads:
        return ev
    first_clock = next((r for r in reads if r.get("quarter") == 1 and r.get("clock_s") is not None), None)
    k = first_clock or reads[0]
    ev.append({"kind": "kickoff", "t_ms": k["t_ms"], "score_after": {"away": k["away"], "home": k["home"]},
               "quarter": k.get("quarter"), "clock_s": k.get("clock_s"),
               "basis": "first scorebug read with a running Q1 clock" if first_clock else "first scorebug read (no clock read)",
               "confidence": 0.6 if first_clock else 0.3})
    stable = (reads[0]["away"], reads[0]["home"])
    stable_t = reads[0]["t_ms"]
    stable_q = next((r["quarter"] for r in reads if r.get("quarter") is not None), None)
    i = 0
    while i < len(reads):
        r = reads[i]
        cur = (r["away"], r["home"])
        if cur != stable:
            run = 1
            while i + run < len(reads) and (reads[i + run]["away"], reads[i + run]["home"]) == cur:
                run += 1
            if run >= STABLE_READS:
                after = reads[i:i + 12]
                agree = sum((x["away"], x["home"]) == cur for x in after) / len(after)
                delta = (cur[0] - stable[0], cur[1] - stable[1])
                legal = all(d >= 0 for d in delta) and sum(1 for d in delta if d) == 1 and max(delta) in {1, 2, 3, 6, 7, 8}
                ev.append({"kind": "score_change", "t_ms": r["t_ms"], "quarter": r.get("quarter"), "clock_s": r.get("clock_s"),
                           "score_before": {"away": stable[0], "home": stable[1]}, "score_after": {"away": cur[0], "home": cur[1]},
                           "delta": {"away": delta[0], "home": delta[1]}, "legal_step": legal,
                           "reads_agreeing": run, "basis": f"scorebug change held {run} consecutive reads",
                           "confidence": round(min(1.0, 0.4 + 0.5 * agree + (0.1 if legal else -0.3)), 2),
                           "last_old_read_t_ms": stable_t})
                ev[-1]["play"] = scoring_play(ev[-1], [e for e in ev[:-1] if e["kind"] == "score_change"])
                stable = cur
                stable_t = reads[i + run - 1]["t_ms"]
            i += run
            continue
        if cur == stable:
            stable_t = r["t_ms"]
        i += 1
    qs = [r for r in reads if r.get("quarter") is not None]
    for n in range(1, len(qs)):
        q = qs[n]["quarter"]
        if q != stable_q and n + 1 < len(qs) and qs[n + 1]["quarter"] == q:
            r = qs[n]
            ev.append({"kind": "quarter_change", "t_ms": r["t_ms"], "quarter": q, "clock_s": r.get("clock_s"),
                       "score_after": {"away": r["away"], "home": r["home"]}, "basis": f"scorebug quarter {stable_q}->{q} (held 2 reads)",
                       "confidence": 0.6 if q > (stable_q or 0) else 0.2})
            stable_q = q
    ev.sort(key=lambda e: e["t_ms"])
    last = reads[-1]
    absent = [o for o in tl if o.get("obs") == "scorebug_absent" and o["t_ms"] >= last["t_ms"]]
    end_t = absent[0]["t_ms"] if absent else last["t_ms"]
    res = result or {}
    fin_src = final if final else None
    ev.append({"kind": "end_of_game", "t_ms": end_t, "quarter": last.get("quarter"), "clock_s": last.get("clock_s"),
               "score_before": {"away": last["away"], "home": last["home"]},
               "score_after": ({"away": fin_src.get("away"), "home": fin_src.get("home")} if fin_src else None),
               "final_confirmed": bool(final_confirmed), "final_method": res.get("method"),
               "receipt_result": ({"away": res.get("away_score"), "home": res.get("home_score"), "final_t_ms": res.get("final_t_ms")}
                                  if res else None),
               "end_window_ms": [end_t, res.get("final_t_ms") or end_t],
               "basis": ("bundle.final (end-of-game menu read)" if fin_src else
                         "scorebug disappeared after last read; end-of-game menu NOT read by the engine (bundle.final is null)"),
               "confidence": 0.9 if (fin_src and final_confirmed) else (0.5 if fin_src else 0.2)})
    return ev


def scoring_play(e: dict, prior: list[dict]) -> str | None:
    """Name the scoring step from its size (and the previous step), so a PAT kick is not mistaken for a field goal."""
    if not e.get("legal_step"):
        return None
    d = e["delta"]
    side = "away" if d["away"] else "home"
    n = d[side]
    prev = prior[-1] if prior else None
    after_td = bool(prev and prev.get("delta", {}).get(side) == 6 and e["t_ms"] - prev["t_ms"] <= TRY_WINDOW_MS)
    if n == 1:
        return "extra point (PAT kick, +1)"
    if n == 2:
        return "two-point conversion (+2)" if after_td else "safety (+2)"
    return {3: "field goal (+3)", 6: "touchdown (+6, try not yet on the scorebug)",
            7: "touchdown + extra point (+7; the +6 state was never read)",
            8: "touchdown + two-point conversion (+8; the +6 state was never read)"}.get(n)


def burst_window(e: dict, rec_s: float, clock: "Clock") -> tuple[float, float, int, float]:
    """(start, end, n_frames, pre_roll_s) in recording seconds for an event's burst."""
    a, z, n_f = rec_s - PRE_S, rec_s + POST_S, N_FRAMES
    play = e.get("play") or ""
    if e["kind"] == "score_change" and play and not play.startswith(("extra point", "two-point conversion")) and e.get("last_old_read_t_ms"):
        old_s = clock.video_s(e["last_old_read_t_ms"])
        if old_s is not None:
            a = min(a, max(old_s - PLAY_PAD_S, rec_s - MAX_PRE_S))
            if rec_s - a > PRE_S + 1:
                n_f = PLAY_FRAMES
    return a, z, n_f, round(rec_s - a, 2)


def bind_checkpoints(e: dict, cps: list[dict], clips: list[dict]) -> dict:
    ts = [c.get("t_ms", 0) for c in cps]
    j = bisect.bisect_left(ts, e["t_ms"])

    def view(c: dict | None) -> dict | None:
        if not c:
            return None
        q = c.get("qorcap") or {}
        ok = (c.get("frames_hash") == sha256_hex(q)) if q else None
        return {"seq": c.get("seq"), "t_ms": c.get("t_ms"), "hash": c.get("hash"), "prev": c.get("prev"),
                "frames_hash": c.get("frames_hash"), "frames_hash_matches_evidence": ok, "trigger": q.get("trigger"),
                "segment_seq": q.get("segment_seq"), "segment_hash": q.get("segment_hash"), "chain_head": q.get("chain_head"),
                "score_in_evidence": q.get("score")}
    out = {"before": view(cps[j - 1] if j > 0 else None), "after": view(cps[j] if j < len(cps) else None), "trigger_match": None, "clip": None}
    sa = e.get("score_after")
    if e["kind"] == "score_change" and sa:
        for c in cps:
            q = c.get("qorcap") or {}
            s = q.get("score") or {}
            if q.get("trigger") == "score_change" and abs(c.get("t_ms", 0) - e["t_ms"]) <= 5000 and \
                    s.get("away") == sa["away"] and s.get("home") == sa["home"]:
                out["trigger_match"] = view(c)
                break
        name = f"score-a{sa['away']}-h{sa['home']}-q{e.get('quarter')}"
        clip = next((c for c in clips if c.get("name") == name), None)
        if clip:
            out["clip"] = {"name": clip["name"], "sha256": clip.get("sha256"), "wall_ms": clip.get("wall_ms"), "secs": clip.get("secs")}
    return out


def nearby(e: dict, presses: list[tuple[int, int]], haptics: list[dict]) -> dict:
    t = e["t_ms"]
    pt = [p[0] for p in presses]
    lo, hi = bisect.bisect_left(pt, t + INPUT_WIN[0] * 1000), bisect.bisect_right(pt, t + INPUT_WIN[1] * 1000)
    pl = [{"dt_ms": presses[k][0] - t, "buttons": bitnames(presses[k][1])} for k in range(lo, hi)]
    hl = [{"dt_ms": h["t_wall_ms"] - t, "duration_ms": h.get("duration_ms"), "energy": h.get("energy"),
           "confidence": h.get("confidence"), "confidence_label": h.get("confidence_label"), "flags": h.get("flags")}
          for h in haptics if "t_wall_ms" in h and INPUT_WIN[0] * 1000 <= h["t_wall_ms"] - t <= INPUT_WIN[1] * 1000]
    nb = int((STRIP_WIN[1] - STRIP_WIN[0]) / STRIP_BIN)
    pb, hb = [0] * nb, [0] * nb
    for k in range(bisect.bisect_left(pt, t + STRIP_WIN[0] * 1000), bisect.bisect_right(pt, t + STRIP_WIN[1] * 1000)):
        b = int(((presses[k][0] - t) / 1000 - STRIP_WIN[0]) / STRIP_BIN)
        if 0 <= b < nb:
            pb[b] += bin(presses[k][1]).count("1")
    for h in haptics:
        if "t_wall_ms" not in h:
            continue
        s = (h["t_wall_ms"] - t) / 1000
        if STRIP_WIN[0] <= s < STRIP_WIN[1]:
            hb[int((s - STRIP_WIN[0]) / STRIP_BIN)] += int(h.get("energy") or 0)
    return {"advisory": ADVISORY, "presses": pl, "haptics": hl,
            "strip": {"t0_s": STRIP_WIN[0], "bin_s": STRIP_BIN, "presses": pb, "haptic_energy": hb}}


def _ff(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)


def probe_duration(video: Path) -> float | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def detect_cuts(video: Path, start: float, dur: float, thresh: float = 0.3) -> list[float]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{max(0, start):.3f}", "-t", f"{dur:.3f}", "-i", str(video),
                        "-vf", f"scale=160:-2,select='gt(scene,{thresh})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True)
    out = []
    for line in r.stderr.splitlines():
        if "pts_time:" in line:
            try:
                out.append(round(max(0, start) + float(line.split("pts_time:")[1].split()[0]), 2))
            except ValueError:
                pass
    return out


def burst(video: Path, d: Path, times: list[float]) -> list[dict]:
    d.mkdir(parents=True, exist_ok=True)
    frames = []
    for n, t in enumerate(times):
        f = d / f"frame_{n:02d}.jpg"
        if not f.exists():
            _ff(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(f)])
        frames.append({"file": f.name, "video_s": round(t, 3), "ok": f.exists()})
    ok = [x for x in frames if x["ok"]]
    if ok and not (d / "contact.jpg").exists():
        cols = 5
        rows = (len(frames) + cols - 1) // cols
        _ff(["-framerate", "1", "-i", str(d / "frame_%02d.jpg"), "-vf", f"scale=320:-2,tile={cols}x{rows}:padding=4:color=0x060b16",
             "-frames:v", "1", "-q:v", "4", str(d / "contact.jpg")])
    return frames


def _cache_key(bdir: Path, video: Path | None) -> str:
    h = hashlib.sha256(SCHEMA.encode())
    for n in sorted(p.name for p in bdir.iterdir() if p.is_file()):
        h.update(n.encode() + hashlib.sha256((bdir / n).read_bytes()).digest())
    if video:
        st = video.stat()
        h.update(f"{video.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()


def build_index(bundle: str | Path, out: str | Path, video: str | Path | None = None, video_tail: bool | None = None,
                frames: bool = True, force: bool = False) -> dict:
    bdir, tmp = open_bundle(bundle)
    try:
        return _build(bdir, Path(out), Path(video) if video else None, video_tail, frames, force)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _build(bdir: Path, out: Path, video: Path | None, video_tail: bool | None, frames: bool, force: bool) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    key = _cache_key(bdir, video)
    cached = _json(out / "match_index.json")
    if cached and not force and cached.get("cache_key") == key:
        return cached
    bundle = _json(bdir / "bundle.json") or {}
    receipt = _json(bdir / "receipt.json") or {}
    proof = _json(bdir / "input_proof.json")
    tl = _jsonl(bdir / "timeline.jsonl")
    cps = sorted(_jsonl(bdir / "checkpoints.jsonl"), key=lambda c: c.get("t_ms", 0))
    manifest = _jsonl(bdir / "manifest.jsonl")
    haptics = sorted((h for h in _jsonl(bdir / "haptics.jsonl") if h.get("type") == "haptic"), key=lambda h: h.get("t_wall_ms", 0))
    presses, ih = _inputs(_jsonl(bdir / "inputs.jsonl"))
    clock = Clock(manifest)
    clips = bundle.get("clips") or [m for m in manifest if m.get("type") == "clip"]
    missing = [n for n in ("timeline.jsonl", "checkpoints.jsonl", "manifest.jsonl", "inputs.jsonl", "haptics.jsonl",
                           "input_proof.json", "receipt.json") if not (bdir / n).exists()]
    if bundle.get("final") is None:
        missing.append("bundle.final (end-of-game menu read)")
    vdur = probe_duration(video) if video and video.exists() else None
    vid_offset = 0.0
    align = "none"
    if video and vdur and clock.duration_s:
        tail = video_tail if video_tail is not None else (clock.duration_s - vdur > 5)
        vid_offset = clock.duration_s - vdur if tail else 0.0
        align = "tail" if tail else "full"
    events = detect_events(tl, receipt.get("result"), bundle.get("final"), bool(bundle.get("final_confirmed")))
    for n, e in enumerate(events):
        e["id"] = f"e{n:02d}_{e['kind']}"
        e["confidence_note"] = "heuristic from scorebug read stability; not a signed result"
        rec_s = clock.video_s(e["t_ms"])
        e["recording_s"] = round(rec_s, 3) if rec_s is not None else None
        e["checkpoints"] = bind_checkpoints(e, cps, clips)
        e["inputs"] = nearby(e, presses, haptics) if presses or haptics else None
        b = e["checkpoints"]
        e["locked"] = bool(e["kind"] == "score_change" and e.get("legal_step") and b.get("trigger_match")
                           and b["trigger_match"].get("frames_hash_matches_evidence") and e["confidence"] >= 0.85) or \
            bool(e["kind"] == "end_of_game" and e.get("final_confirmed") and e.get("score_after"))
        e["frames"], e["contact_sheet"], e["cuts_video_s"], e["frames_missing_reason"] = [], None, None, None
        if rec_s is None:
            e["frames_missing_reason"] = "no manifest frame clock"
            continue
        if not (video and vdur):
            e["frames_missing_reason"] = "no video supplied"
            continue
        if e["kind"] == "end_of_game":
            a = clock.video_s(e["end_window_ms"][0]) - vid_offset - PRE_S
            z = clock.video_s(e["end_window_ms"][1]) - vid_offset
            n_f = 12
        else:
            a, z, n_f, e["pre_roll_s"] = burst_window(e, rec_s, clock)
            a, z = a - vid_offset, z - vid_offset
        a, z = max(0.0, a), min(vdur - 0.05, z)
        if z <= 0 or a >= vdur:
            e["frames_missing_reason"] = f"event at recording {rec_s:.1f}s is outside the supplied video ({align}, covers recording {vid_offset:.1f}..{vid_offset + vdur:.1f}s)"
            continue
        if not frames:
            e["frames_missing_reason"] = "frame extraction disabled"
            continue
        times = [a + (z - a) * k / (n_f - 1) for k in range(n_f)] if z > a else [a]
        d = out / "events" / e["id"]
        e["frames"] = burst(video, d, times)
        for f in e["frames"]:
            f["file"] = f"events/{e['id']}/{f['file']}"
            f["recording_s"] = round(f["video_s"] + vid_offset, 3)
            f["dt_s"] = round(f["recording_s"] - rec_s, 2)
        e["contact_sheet"] = f"events/{e['id']}/contact.jpg" if (d / "contact.jpg").exists() else None
        e["cuts_video_s"] = detect_cuts(video, a, z - a)
    idx = {
        "schema": SCHEMA, "cache_key": key, "match_id": bundle.get("match_id"), "role": bundle.get("role"),
        "qorcap_version": bundle.get("qorcap_version"),
        "claims": "Play-by-play derived from the QorCap scorebug timeline and checkpoint chain. Frames are what the reader looked at; "
                  "scores are reads, not signed results. " + ADVISORY,
        "chain": {"chain_head": bundle.get("chain_head"), "checkpoint_head": bundle.get("checkpoint_head"),
                  "checkpoints": len(cps), "segments": bundle.get("segments")},
        "result": {"final": bundle.get("final"), "final_confirmed": bool(bundle.get("final_confirmed")),
                   "receipt_result": receipt.get("result"), "trust_tier": receipt.get("trust_tier")},
        "input_summary": ({k: (bundle.get("input") or {}).get(k) for k in ("status", "human_label", "human_permille",
                                                                          "match_permille", "chance_permille", "offset_ms", "haptic_events")}
                          | {"device": public_device((bundle.get("input") or {}).get("device")),
                             "advisory": ADVISORY, "input_proof_present": proof is not None,
                             "wall_anchor_ms": ih.get("wall_anchor_ms"), "presses": len(presses), "haptics": len(haptics)}),
        "video": {"file": video.name if video else None, "duration_s": vdur, "alignment": align,
                  "recording_offset_s": round(vid_offset, 3), "recording_duration_s": clock.duration_s,
                  "alignment_note": "video time = cumulative manifest frames/30 interpolated by wall clock; tail videos assume they end where the recording ends (+-~2 s)"},
        "missing": missing, "events": events,
    }
    (out / "match_index.json").write_text(json.dumps(idx, indent=2), encoding="utf-8")
    from qor_receipt.match_index.viewer import write_viewer

    write_viewer(idx, out)
    return idx
