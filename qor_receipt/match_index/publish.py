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
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(jpg), "-q:v", "7", str(tmp)], check=False,
                       capture_output=True)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size < jpg.stat().st_size:
        tmp.replace(jpg)
    elif tmp.exists():
        tmp.unlink()


def publish(bundle: str | Path, site: str | Path, video: str | Path | None = None, highlights: str | Path | None = None,
            slug: str | None = None, allow_fail: bool = False, frames: bool = True, now_ms: int | None = None) -> dict:
    bdir, tmp = open_bundle(bundle)
    try:
        return _publish(bdir, Path(site), Path(video) if video else None, Path(highlights) if highlights else None,
                        slug, allow_fail, frames, now_ms)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _publish(bdir: Path, site: Path, video: Path | None, hdir: Path | None, slug: str | None, allow_fail: bool,
             frames: bool, now_ms: int | None) -> dict:
    rp = bdir / "receipt.json"
    if not rp.exists():
        raise PublishRefused("no receipt.json: pages are published only for matches with a signed receipt")
    receipt = json.loads(rp.read_text(encoding="utf-8"))
    ver = verify_receipt(receipt)
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
                clips_pub.append({
                    "id": c["id"], "url": f"clips/{c['file']}", "vtt": f"clips/{c['vtt']}" if c.get("vtt") else None,
                    "sha256": c.get("sha256"), "duration_s": c.get("duration_s"), "lock_at_s": c.get("lock_at_s"),
                    "overlay": c.get("overlay"),
                    "lock": {k: lk.get(k) for k in ("source", "seq", "t_ms", "hash", "frames_hash", "frames_hash_matches_evidence",
                                                    "in_receipt", "score_before", "score_after", "quarter", "clock_s", "play",
                                                    "delta")},
                    # gold only when the lock is inside a receipt that does not FAIL (fail closed)
                    "locked": bool(lk.get("in_receipt") and lk.get("frames_hash_matches_evidence") and status != FAIL),
                })
        _link_clips(idx["events"], clips_pub)
        if status == FAIL:
            for e in idx["events"]:
                e["locked"] = False
        res = receipt.get("result") or {}
        label = {"VERIFIED": "VERIFIED · co-signed by the QorTime referee",
                 "PARTIAL": "PARTIAL · some checks not covered",
                 "SELF_ATTESTED": "SELF_ATTESTED · signed on the player's own PC, not by the QorTime referee",
                 "FAIL": "FAIL · this receipt does not verify; nothing on this page is a verified result"}[status]
        idx["result"]["trust_tier"] = label
        idx["publish"] = {
            "slug": slug, "generated_ms": now_ms or int(time.time() * 1000),
            "receipt": {"url": "receipt.json", "status": status, "label": label, "errors": ver["errors"],
                        "partial": ver["partial"], "trust_tier": ver.get("trust_tier"),
                        "referee_kind": (receipt.get("referee") or {}).get("key_kind"),
                        "result": {k: res.get(k) for k in ("away_score", "home_score", "winner", "method", "final_confirmed", "quarter")},
                        "verify_url": "../../verify/"},
            "highlights": clips_pub, "highlights_manifest": "clips/manifest.json" if hman else None,
            "advisory": PUBLIC_ADVISORY,
        }
        idx["claims"] = (f"Receipt status (computed by the QorTime verifier when this page was built): {label}. " + idx["claims"])
        blob = json.dumps(idx)
        bad = privacy_problems(blob)
        if bad:
            raise PublishRefused(f"privacy scan matched {bad}; refusing to publish")
        rtext = rp.read_text(encoding="utf-8")
        publish_receipt = not privacy_problems(rtext)

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
            e["contact_sheet"] = None
        if clips_pub:
            (out / "clips").mkdir()
            for c in hman["clips"]:
                for n in (c["file"], c.get("vtt")):
                    if n and (hdir / n).exists():
                        shutil.copy2(hdir / n, out / "clips" / n)
            (out / "clips" / "manifest.json").write_text(json.dumps(hman, indent=2), encoding="utf-8")
        if publish_receipt:
            (out / "receipt.json").write_text(rtext, encoding="utf-8")
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
    gold = bool(fin)
    st = m.get("receipt_status") or "?"
    cls = {"VERIFIED": "a", "FAIL": "w"}.get(st, "w" if st in ("SELF_ATTESTED", "PARTIAL") else "")
    when = time.strftime("%b %d, %Y", time.gmtime((m.get("started_ms") or 0) / 1000 - 5 * 3600)) if m.get("started_ms") else ""
    score = (f'<span class="{"lockg" if gold else "unl"}">{e(str(shown["away"]))}<i>–</i>{e(str(shown["home"]))}</span>'
             if shown else '<span class="miss">no score read</span>')
    basis = "final confirmed · locked" if gold else "last scorebug read · not confirmed"
    if st == "FAIL":
        basis = "receipt FAILS verification · not a verified result"
    return (f'<a class="mc glass" href="{e(m["url"])}"><div class="top"><span class="lbl">{e(m.get("game") or "match")} · {e(when)}</span>'
            f'<span class="tag {cls}">{e(st)}</span></div><div class="sc">{score}</div><div class="sub">away – home · {e(basis)}</div>'
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
    p = root / "index.html"
    root.mkdir(parents=True, exist_ok=True)
    p.write_text(INDEX_TEMPLATE.replace("__CARDS__", cards).replace("__N__", str(len(ms))), encoding="utf-8")
    return p


INDEX_TEMPLATE = (Path(__file__).with_name("index_template.html")).read_text(encoding="utf-8")
