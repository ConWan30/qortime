"""Public match pages: ``qortime publish BUNDLE --site site [--video V] [--highlights DIR]`` -> site/matches/<slug>/.

Generalizes the hand-built match 021003 viewer: the same viewer (score timeline, burst-frame motion playback, inputs and
haptics strip, chain binding) plus the receipt status, verified highlight clips (``qorcap highlights``) linked from each
touchdown, and a /matches/ index. Rules:
  * a page is published only when the bundle has a signed ``receipt.json``; its status is the one the QorTime verifier
    computes now (VERIFIED / PARTIAL / SELF_ATTESTED / FAIL), never the receipt's own claim. FAIL needs ``allow_fail``
    and is labeled as such on the page and the index.
  * raw ``inputs.jsonl`` / ``haptics.jsonl``, device ids and local file paths are never published; only the derived
    windows the viewer shows (button presses and haptic energy a few seconds around each event) go out, labeled
    advisory evidence, not proof. A privacy scan refuses to write anything that still carries a path or device id.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from qor_receipt.match_index.build import build_index, open_bundle
from qor_receipt.receipt import FAIL, verify_receipt

PUBLIC_ADVISORY = ("Controller input and haptic signals are advisory evidence, not proof: they show activity in sync with "
                   "the video, not who was holding the controller or what happened on the field.")
# anything that looks like a local path, a Windows device id or a user folder must never reach the public site
PRIVATE_PATTERNS = [re.compile(p, re.I) for p in (r"[A-Z]:\\\\", r"[A-Z]:/Users/", r"\\\\Users\\\\", r"/home/[a-z]",
                                                  r"@device_pnp", r"\"device_id\"", r"/Users/[A-Za-z]")]
CLIP_LINK_MS = 20_000


class PublishRefused(Exception):
    pass


def privacy_problems(text: str) -> list[str]:
    return [p.pattern for p in PRIVATE_PATTERNS if p.search(text)]


def default_slug(match_id: str | None) -> str:
    m = re.search(r"(\d{8})-(\d{6})$", match_id or "")
    if m:
        return m.group(2)
    s = re.sub(r"[^a-z0-9-]+", "-", (match_id or "match").lower()).strip("-")
    return s or "match"


def _jsonl_ok(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _link_clips(events: list[dict], clips: list[dict]) -> None:
    for e in events:
        if e.get("kind") != "score_change":
            continue
        sa = e.get("score_after") or {}
        best = None
        for n, c in enumerate(clips):
            lk = c.get("lock") or {}
            if lk.get("score_after") == {"away": sa.get("away"), "home": sa.get("home")} and \
                    abs((lk.get("t_ms") or 0) - (e.get("t_ms") or 0)) <= CLIP_LINK_MS:
                d = abs((lk.get("t_ms") or 0) - (e.get("t_ms") or 0))
                if best is None or d < best[0]:
                    best = (d, n)
        if best:
            e["highlight"] = best[1]


def _compress(jpg: Path) -> None:
    tmp = jpg.with_suffix(".q.jpg")
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(jpg), "-q:v", "7", str(tmp)], check=False,
                           capture_output=True)
    except FileNotFoundError:
        return
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size < jpg.stat().st_size:
        tmp.replace(jpg)
    elif tmp.exists():
        tmp.unlink()


def _ffmpeg(args: list[str]) -> bool:
    """False when ffmpeg is missing or the command fails. Posters and thumbs are optional; the clip bytes stay as published."""
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=False, capture_output=True)
    except FileNotFoundError:
        return False
    return r.returncode == 0


def _thumb(jpg: Path) -> bool:
    """160px-wide filmstrip thumb next to a burst frame. The page loads these instead of full JPEGs."""
    thumb = jpg.with_name(jpg.stem + "_t.jpg")
    ok = _ffmpeg(["-i", str(jpg), "-vf", "scale=160:-2", "-q:v", "8", str(thumb)])
    if not (ok and thumb.exists() and thumb.stat().st_size > 0):
        if thumb.exists():
            thumb.unlink()
        return False
    return True


def _poster(mp4: Path, t: float, dest: Path, *, cover_burn: bool = False) -> bool:
    """One frame for the clip card. cover_burn paints over qorcap's burned LOCKED SCORE panel.

    The panel is drawn at k = height/360, box (10, 10, 330×58). The mp4 itself is not touched,
    so the manifest sha256 still matches the file qorcap wrote.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = "scale=320:-2"
    if cover_burn:
        vf = ("drawbox=x='6*ih/360':y='6*ih/360':w='344*ih/360':h='70*ih/360':color=0x081122:t=fill,"
              "drawbox=x='6*ih/360':y='6*ih/360':w='3*ih/360':h='70*ih/360':color=0x3fe0d0:t=fill,"
              "scale=320:-2")
    ok = _ffmpeg(["-ss", f"{max(0.0, t):.3f}", "-i", str(mp4), "-frames:v", "1", "-vf", vf, "-q:v", "5", str(dest)])
    if not (ok and dest.exists() and dest.stat().st_size > 0):
        if dest.exists():
            dest.unlink()
        return False
    return True


def honest_vtt(text: str, status: str) -> str:
    """Caption track follows the receipt. qorcap highlights burns the word LOCKED before anyone verifies."""
    if status == "VERIFIED":
        return text
    return text.replace("LOCKED ", "SCOREBUG LOCK ").replace("score locked here", "scorebug read here")


_CP_ERR = re.compile(r"cp\[(\d+)\]:([A-Za-z0-9_]+)")


def _checkpoint_rows(receipt: dict) -> list[dict]:
    cps = receipt.get("checkpoints") or {}
    if isinstance(cps, list):
        return [c for c in cps if isinstance(c, dict) and "t_ms" in c]
    if not isinstance(cps, dict):
        return []
    ordered = []
    for key in ("home", "away"):
        rows = cps.get(key)
        if isinstance(rows, list) and rows:
            ordered.append(rows)
    for rows in cps.values():
        if isinstance(rows, list) and rows and rows not in ordered:
            ordered.append(rows)
    for rows in ordered:
        if isinstance(rows[0], dict) and "t_ms" in rows[0]:
            return rows
    return []


def _recording_fn(events: list[dict], origin_ms: float):
    pairs = sorted((e["t_ms"], float(e["recording_s"])) for e in events
                   if isinstance(e.get("t_ms"), (int, float)) and isinstance(e.get("recording_s"), (int, float)))
    if len(pairs) >= 2:
        (t0, r0), (t1, r1) = pairs[0], pairs[-1]
        span = (t1 - t0) or 1.0
        return lambda t: r0 + (t - t0) * (r1 - r0) / span
    return lambda t: (t - origin_ms) / 1000.0


def chain_rail(receipt: dict, events: list[dict], status: str, errors: list[str]) -> dict | None:
    """Compact 2s proof-chain for the viewer. A crack is a verifier error. seal is true only for VERIFIED."""
    rows = _checkpoint_rows(receipt)
    if len(rows) < 2:
        return None
    rec = _recording_fn(events, float(rows[0]["t_ms"]))
    faults: dict[int, list[str]] = {}
    for err in errors or []:
        m = _CP_ERR.search(str(err))
        if m:
            faults.setdefault(int(m.group(1)), []).append(m.group(2))
    cracks = [{"i": i, "why": ", ".join(why)} for i, why in sorted(faults.items()) if 0 <= i < len(rows)]
    sealed = status == "VERIFIED"
    note = "Each link is one checkpoint, about 2 seconds of the recording. "
    if cracks:
        note += "Cracks are where the verifier reported a gap or a clock skew. "
    note += "Referee-verified seal." if sealed else "This receipt is not referee-verified, so the rail is not a seal."
    return {
        "n": len(rows), "dt_s": 2.0,
        "t0_s": round(rec(float(rows[0]["t_ms"])), 3), "t1_s": round(rec(float(rows[-1]["t_ms"])), 3),
        "cracks": cracks, "seal": sealed, "sparse": False, "note": note,
    }


def publish(bundle: str | Path, site: str | Path, video: str | Path | None = None, highlights: str | Path | None = None,
            slug: str | None = None, allow_fail: bool = False, frames: bool = True, now_ms: int | None = None,
            include_names: bool = False, keep_receipt: bool = False) -> dict:
    bdir, tmp = open_bundle(bundle)
    try:
        return _publish(bdir, Path(site), Path(video) if video else None, Path(highlights) if highlights else None,
                        slug, allow_fail, frames, now_ms, include_names, keep_receipt)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _check_line(name: str, check: dict) -> str | None:
    """Short line from fields already on the signed review. No extra numbers."""
    detail = check.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    if not isinstance(detail, dict):
        return None
    if name == "video_bytes" and detail.get("segments_verified") is not None and detail.get("segments_total") is not None:
        method = detail.get("method") or "check"
        return f"{method} {detail['segments_verified']}/{detail['segments_total']} segments"
    if name == "challenges" and detail.get("count") is not None:
        rule = detail.get("rule")
        return f"{detail['count']} challenges" + (f", rule {rule}" if rule is not None else "")
    if name == "motion" and detail.get("motion_permille") is not None:
        bits = []
        if detail.get("rule") is not None:
            bits.append(f"rule {detail['rule']}")
        bits.append(f"{detail['motion_permille']} per mille")
        if detail.get("bins") is not None:
            bits.append(f"{detail['bins']} bins")
        return ", ".join(bits)
    if name == "input_integrity":
        bits = []
        if detail.get("presses") is not None:
            bits.append(f"{detail['presses']} presses")
        if detail.get("haptic_events") is not None:
            bits.append(f"{detail['haptic_events']} haptic events")
        return ", ".join(bits) or None
    if name == "final_reread":
        value = check.get("value") if isinstance(check.get("value"), dict) else {}
        if value.get("away") is not None and value.get("home") is not None:
            return f"away {value['away']}, home {value['home']}"
    return None


def _public_corroboration(co: dict) -> dict:
    """Fields the match page renders. The verifier's detail sentence names status codes and stays off the page."""
    keep = ("schema", "status", "reasons", "tolerance_ms", "affects_verdict", "pairing_id", "finals")
    return {k: co[k] for k in keep if k in co}


def _review_checks(receipt: dict) -> list[dict]:
    checks = ((receipt.get("review") or {}) if isinstance(receipt.get("review"), dict) else {}).get("checks")
    if not isinstance(checks, dict):
        return []
    out = []
    for name, check in checks.items():
        if not isinstance(check, dict):
            continue
        out.append({"name": name, "state": check.get("state"), "line": _check_line(name, check)})
    return out


def _recorded_ct(receipt: dict) -> str | None:
    """Clock on the match page. The index uses the same fixed US Central offset."""
    match = receipt.get("match") or {}
    start = match.get("started_ms")
    if not isinstance(start, (int, float)):
        return None

    def _fmt(ms: float, pat: str) -> str:
        return time.strftime(pat, time.gmtime(ms / 1000 - 5 * 3600))
    start_s = _fmt(start, "%b %d, %Y, %-I:%M %p")
    end = match.get("ended_ms")
    if isinstance(end, (int, float)):
        return f"Recorded {start_s}–{_fmt(end, '%-I:%M %p')} CT."
    return f"Recorded {start_s} CT."


def _publish(bdir: Path, site: Path, video: Path | None, hdir: Path | None, slug: str | None, allow_fail: bool,
             frames: bool, now_ms: int | None, include_names: bool = False, keep_receipt: bool = False) -> dict:
    rp = bdir / "receipt.json"
    if not rp.exists():
        raise PublishRefused("no receipt.json: pages are published only for matches with a signed receipt")
    receipt = json.loads(rp.read_text(encoding="utf-8"))
    if (receipt.get("match") or {}).get("observed_handles") and not include_names:
        # the signed receipt carries both players' on-screen PSN names; it cannot be redacted without breaking the
        # signature, so publishing it is an explicit choice (the opponent's name would be public)
        raise PublishRefused("receipt carries on-screen PSN names (match.observed_handles) of both players; pass "
                             "include_names to publish them")
    ref = receipt.get("referee") if isinstance(receipt.get("referee"), dict) else {}
    ref_pub = ref.get("pubkey") if isinstance(ref.get("pubkey"), str) else None
    # Pin the key named on the receipt. That is the same input the public verifier uses for VERIFIED.
    ver = verify_receipt(receipt, referee_pub=ref_pub)
    status = ver["status"]
    if status == FAIL and not allow_fail:
        raise PublishRefused(f"receipt does not verify (FAIL: {', '.join(ver['errors'][:4])}); pass allow_fail to publish "
                             "it labeled as failing")
    bundle = _jsonl_ok(bdir / "bundle.json") or {}
    match_id = (receipt.get("match") or {}).get("match_id") or bundle.get("match_id")
    slug = slug or default_slug(match_id)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise PublishRefused(f"bad slug {slug!r}")
    out = site / "matches" / slug
    prev = _jsonl_ok(out / "match.json")
    if prev and prev.get("match_id") != match_id:
        raise PublishRefused(f"/matches/{slug}/ already holds {prev.get('match_id')}; pass another slug")
    if (out / "index.html").exists() and not prev:
        raise PublishRefused(f"/matches/{slug}/ is a hand-built page (no match.json); not overwriting it")
    hman = None
    if hdir is None and (bdir / "highlights" / "manifest.json").exists():
        hdir = bdir / "highlights"
    if hdir is not None:
        hman = _jsonl_ok(hdir / "manifest.json")
        if hman and hman.get("receipt", {}).get("sha256"):
            import hashlib
            if hashlib.sha256(rp.read_bytes()).hexdigest() != hman["receipt"]["sha256"]:
                raise PublishRefused("highlights manifest belongs to a different receipt.json")

    work = Path(tempfile.mkdtemp(prefix="qt-publish-"))
    try:
        idx = build_index(bdir, work, video=video, frames=frames, force=True)
        idx.pop("cache_key", None)
        clips_pub = []
        if hman:
            for c in hman.get("clips") or []:
                lk = c.get("lock") or {}
                src = c.get("source") or {}
                clips_pub.append({
                    "id": c["id"], "url": f"clips/{c['file']}", "vtt": f"clips/{c['vtt']}" if c.get("vtt") else None,
                    "sha256": c.get("sha256"), "duration_s": c.get("duration_s"), "lock_at_s": c.get("lock_at_s"),
                    "overlay": c.get("overlay"),
                    "overlay_label": "LOCKED SCORE" if status == "VERIFIED" else "SCOREBUG LOCK",
                    "poster": None,
                    "source_start_s": src.get("start_s"), "source_end_s": src.get("end_s"),
                    "lock": {k: lk.get(k) for k in ("source", "seq", "t_ms", "hash", "frames_hash", "frames_hash_matches_evidence",
                                                    "in_receipt", "score_before", "score_after", "quarter", "clock_s", "play",
                                                    "delta")},
                    # gold / "locked" only for a referee-verified receipt. A FAIL or self-signed lock is not a seal.
                    "locked": bool(status == "VERIFIED" and lk.get("in_receipt") and lk.get("frames_hash_matches_evidence")),
                })
        _link_clips(idx["events"], clips_pub)
        if status == FAIL:
            for e in idx["events"]:
                e["locked"] = False
        res = receipt.get("result") or {}
        label = {"VERIFIED": "VERIFIED · co-signed by the QorTime referee",
                 "PARTIAL": "PARTIAL · some checks not covered",
                 "SELF_ATTESTED": "SELF-SIGNED · signed on the player's own PC, not by the QorTime referee",
                 "FAIL": "FAIL · this receipt does not verify; nothing on this page is a verified result"}[status]
        if status == "VERIFIED" and ver.get("referee_id"):
            deleg = ver.get("delegation") if isinstance(ver.get("delegation"), dict) else {}
            label = f"VERIFIED · co-signed by {ver['referee_id']}"
            if deleg.get("serial") is not None and deleg.get("delegated_by"):
                label += f" (delegation serial {deleg['serial']} from root {deleg['delegated_by']})"
        idx["result"]["trust_tier"] = label
        idx["publish"] = {
            "slug": slug, "generated_ms": now_ms or int(time.time() * 1000),
            "receipt": {"url": "receipt.json", "status": status, "label": label, "errors": ver["errors"],
                        "partial": ver["partial"],
                        "trust_tier": {"single-attested": "one-player tier", "dual-attested": "two-player tier"}.get(
                            ver.get("trust_tier"), ver.get("trust_tier")),
                        "referee_kind": (receipt.get("referee") or {}).get("key_kind"),
                        "result": {k: res.get(k) for k in ("away_score", "home_score", "winner", "method", "final_confirmed", "quarter")},
                        "verify_url": "../../verify/",
                        **({"referee_pub": ref_pub} if ref_pub else {}),
                        **({"not_proven": list(ver["review"]["not_proven"])} if isinstance((ver.get("review") or {}).get("not_proven"), list) else {}),
                        **({"checks": _review_checks(receipt)} if _review_checks(receipt) else {}),
                        **({"corroboration": _public_corroboration(ver["corroboration"])} if isinstance(ver.get("corroboration"), dict) else {}),
                        **({"device_key": ver["device_key"]} if isinstance(ver.get("device_key"), dict) else {})},
            "highlights": clips_pub, "highlights_manifest": "clips/manifest.json" if hman else None,
            "advisory": PUBLIC_ADVISORY,
            "chain_rail": chain_rail(receipt, idx["events"], status, ver["errors"]),
        }
        idx["claims"] = (f"Receipt status (computed by the QorTime verifier when this page was built): {label}. " + idx["claims"])
        match = receipt.get("match") or {}
        idx["capture"] = {k: match[k] for k in ("platform", "game", "mode", "scorebug_profile") if match.get(k) not in (None, "")}
        recorded = _recorded_ct(receipt)
        if recorded:
            idx["publish"]["recorded"] = recorded
        if not clips_pub and not any(e.get("frames") for e in idx["events"]):
            note = ("Footage is not published. The recording stays on the player's PC. "
                    "This page has the signed receipt, the score timeline, and the proof chain.")
            idx["publish"]["footage_note"] = note
            for e in idx["events"]:
                if not e.get("frames"):
                    e["frames_missing_reason"] = "Footage is not published. The recording stays on the player's PC."
        blob = json.dumps(idx)
        bad = privacy_problems(blob)
        if bad:
            raise PublishRefused(f"privacy scan matched {bad}; refusing to publish")
        rtext = rp.read_text(encoding="utf-8")
        receipt_private = privacy_problems(rtext)
        # keep_receipt writes the signed file unchanged. The page blob above is still refused if it leaks.
        publish_receipt = keep_receipt or not receipt_private

        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        # frames as files next to the page (021003 embedded ~12 MB of base64; files load lazily and cache)
        for e in idx["events"]:
            for f in e.get("frames") or []:
                src = work / f["file"]
                if src.exists():
                    dst = out / f["file"]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    _compress(dst)
                    if _thumb(dst):
                        f["thumb"] = str(Path(f["file"]).with_name(dst.stem + "_t.jpg")).replace("\\", "/")
            e["contact_sheet"] = None
        if clips_pub:
            (out / "clips").mkdir()
            by_id = {c["id"]: c for c in hman["clips"]}
            for c in clips_pub:
                src_clip = by_id.get(c["id"]) or {}
                for n in (src_clip.get("file"), src_clip.get("vtt")):
                    if not n or not (hdir / n).exists():
                        continue
                    dest = out / "clips" / n
                    if str(n).endswith(".vtt"):
                        dest.write_text(honest_vtt((hdir / n).read_text(encoding="utf-8"), status), encoding="utf-8")
                    else:
                        shutil.copy2(hdir / n, dest)
                mp4 = out / "clips" / src_clip.get("file", "")
                poster = out / "clips" / f"{c['id']}_poster.jpg"
                if mp4.is_file() and _poster(mp4, float(c.get("lock_at_s") or 0) + 0.15, poster, cover_burn=status != "VERIFIED"):
                    c["poster"] = f"clips/{c['id']}_poster.jpg"
            (out / "clips" / "manifest.json").write_text(json.dumps(hman, indent=2), encoding="utf-8")
        if publish_receipt:
            (out / "receipt.json").write_bytes(rp.read_bytes())
        else:
            idx["publish"]["receipt"]["url"] = None
        from qor_receipt.match_index.viewer import write_viewer

        write_viewer(idx, out, embed=False, name="index.html")
        summary = {
            "schema": "qortime-match-page/v0", "slug": slug, "match_id": match_id, "url": f"{slug}/",
            "started_ms": (receipt.get("match") or {}).get("started_ms"), "game": (receipt.get("match") or {}).get("game"),
            "final": idx["result"].get("final") and {k: idx["result"]["final"].get(k) for k in ("away", "home")},
            "final_confirmed": bool(idx["result"].get("final_confirmed")),
            "receipt_result": {"away": res.get("away_score"), "home": res.get("home_score"), "method": res.get("method")},
            "receipt_status": status, "receipt_label": label, "clips": len(clips_pub),
            "events": len(idx["events"]), "touchdowns": sum(1 for e in idx["events"] if "touchdown" in str(e.get("play") or "")),
        }
        if idx["publish"].get("footage_note"):
            summary["note"] = idx["publish"]["footage_note"]
        (out / "match.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        write_index(site)
        return summary
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ /matches/ index
def _card(m: dict) -> str:
    e = html.escape
    fin = m.get("final") if m.get("final_confirmed") else None
    rr = m.get("receipt_result") or {}
    shown = fin or ({"away": rr.get("away"), "home": rr.get("home")} if rr.get("away") is not None else None)
    st = m.get("receipt_status") or "?"
    # gold is a referee-verified seal, not "we read a final on the recording"
    gold = bool(fin) and st == "VERIFIED"
    cls = "g" if st == "VERIFIED" else "x" if st == "FAIL" else "w" if st in ("SELF_ATTESTED", "PARTIAL") else ""
    tag = "SELF-SIGNED" if st == "SELF_ATTESTED" else st
    when = time.strftime("%b %d, %Y", time.gmtime((m.get("started_ms") or 0) / 1000 - 5 * 3600)) if m.get("started_ms") else ""
    score = (f'<span class="{"lockg" if gold else "unl"}">{e(str(shown["away"]))}<i>–</i>{e(str(shown["home"]))}</span>'
             if shown else '<span class="miss">no score read</span>')
    if st == "FAIL":
        basis = "receipt FAILS verification · not a verified result"
    elif gold:
        basis = "final confirmed · referee-verified"
    elif fin:
        basis = "final confirmed in the recording · not referee-signed"
    else:
        basis = "last scorebug read · not a verified result"
    return (f'<a class="mc glass" href="{e(m["url"])}"><div class="top"><span class="lbl">{e(m.get("game") or "match")} · {e(when)}</span>'
            f'<span class="tag {cls}">{e(tag)}</span></div><div class="sc">{score}</div><div class="sub">away – home · {e(basis)}</div>'
            f'<div class="meta"><span>{e(str(m.get("events", 0)))} events</span><span>{e(str(m.get("touchdowns", 0)))} TDs</span>'
            f'<span>{e(str(m.get("clips", 0)))} highlight clips</span></div><div class="id mono">{e(m.get("match_id") or "")}</div></a>')


def write_index(site: str | Path) -> Path:
    site = Path(site)
    root = site / "matches"
    ms = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        m = _jsonl_ok(d / "match.json") if d.is_dir() else None
        if m and (d / "index.html").exists():
            ms.append(m)
    ms.sort(key=lambda m: m.get("started_ms") or 0, reverse=True)
    cards = "\n".join(_card(m) for m in ms) or '<p class="miss">No published matches yet.</p>'
    base = "These pages are separate recordings, not a ranking."
    verified = [m for m in ms if m.get("receipt_status") == "VERIFIED"]
    if not ms:
        compare = ""
    elif not verified:
        compare = html.escape(base + " No page here is referee-verified, so the scores are not a verified comparison.")
    else:
        links = ", ".join(
            f'<a href="{html.escape(str(m.get("url") or ""))}">{html.escape(str(m.get("match_id") or m.get("slug") or "match"))}</a>'
            for m in verified)
        lead = " The first card is the referee-verified match." if ms[0].get("receipt_status") == "VERIFIED" else ""
        compare = html.escape(f"{base} Referee-verified receipts on this list: n = {len(verified)}.") + lead + " " + links + "."
    p = root / "index.html"
    root.mkdir(parents=True, exist_ok=True)
    p.write_text(INDEX_TEMPLATE.replace("__CARDS__", cards).replace("__N__", str(len(ms))).replace("__COMPARE__", compare),
                 encoding="utf-8")
    return p


INDEX_TEMPLATE = (Path(__file__).with_name("index_template.html")).read_text(encoding="utf-8")
