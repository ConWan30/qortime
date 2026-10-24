"""QorTime CLI (commands: `qortime` or `qor-receipt`).

  qor-receipt verify RECEIPT.json [--referee-pub HEX]     exit 0 VERIFIED|SELF_ATTESTED, 1 PARTIAL, 2 FAIL
  qor-receipt emit --demo OUT.json [--mode dual|single]   build a fully synthetic signed receipt
  qor-receipt pattern NONCE                               show the button pattern for a nonce
  qor-receipt agent --device "USB3.0 Video" --referee URL  live capture agent (Windows, capture card + DualSense USB)
  qor-receipt agent --replay FRAMES_DIR --hid-log FILE    offline replay through an in-process referee
  qor-receipt agent-fixture OUT_DIR                       write a synthetic replay session
  qortime referee-ai BUNDLE [--vision scorebug|meta|none] [--no-jev]   AI referee verdict (exit 0 accept, 1 abstain, 2 dispute)
  qortime referee-ai-verify VERDICT.json [--pub HEX]      check a signed referee verdict
  qortime index BUNDLE [--video V] [--out DIR]            play-by-play match index + burst frames + viewer.html
  qortime publish BUNDLE --site site [--video V] [--highlights DIR]   public match page site/matches/<slug>/ + index
  qortime referee-service [--host H] [--port P]           official referee service (verified uploads); key from env
"""
from __future__ import annotations

import argparse
import json
import sys

from qor_receipt.challenge import pattern_for
from qor_receipt.receipt import FAIL, PARTIAL, verify_receipt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qortime", description="QorTime: signed, verifiable console match results")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("receipt")
    v.add_argument("--referee-pub")
    e = sub.add_parser("emit")
    e.add_argument("--demo", required=True, metavar="OUT")
    e.add_argument("--mode", choices=["dual", "single"], default="dual")
    p = sub.add_parser("pattern")
    p.add_argument("nonce")
    g = sub.add_parser("agent", help="capture agent (live or --replay)")
    g.add_argument("--device", default="USB3.0 Video", help="DirectShow capture device name (or index)")
    g.add_argument("--referee", help="referee base URL (live)")
    g.add_argument("--role", choices=["home", "away"], default="home")
    g.add_argument("--player-id", default="local:player")
    g.add_argument("--match-id", help="join an existing match (player pubkey already registered)")
    g.add_argument("--key", help="player key file (default ~/.qor-receipt/player.key; replay: ephemeral)")
    g.add_argument("--out", help="receipt JSON path")
    g.add_argument("--obs-text", help="also write the challenge prompt to this text file (OBS Text source)")
    g.add_argument("--window", action="store_true", help="show the prompt in a small always-on-top window")
    g.add_argument("--pause-config", help="JSON overriding pause-detector thresholds")
    g.add_argument("--cp-interval", type=float, default=4.0, help="checkpoint interval seconds")
    g.add_argument("--replay", metavar="FRAMES_DIR")
    g.add_argument("--hid-log", metavar="FILE")
    g.add_argument("--print-pubkey", action="store_true")
    fx = sub.add_parser("agent-fixture")
    fx.add_argument("out")
    ra = sub.add_parser("referee-ai", help="AI referee over a QorCap match bundle (signed verdict)")
    ra.add_argument("bundle", help="QorCap bundle directory (bundle.json, manifest.jsonl, checkpoints.jsonl, ...)")
    ra.add_argument("--vision", choices=["scorebug", "meta", "none"], default="scorebug",
                    help="seeing adapter (meta = Muse Spark stub, makes no calls)")
    ra.add_argument("--no-jev", action="store_true", help="skip the Jev judgment layer")
    ra.add_argument("--jev-model", default=None, help="Jev model id (default jev-1.14.0)")
    ra.add_argument("--max-frames", type=int, default=24, help="max evidence frames decoded from the video")
    ra.add_argument("--referee-pub", help="pin the receipt referee key (default: bundle referee.pub)")
    ra.add_argument("--key", help="referee-ai signing key file (default ~/.qor-receipt/referee-ai.key)")
    ra.add_argument("--out", help="verdict path (default BUNDLE/referee_verdict.json)")
    rv = sub.add_parser("referee-ai-verify")
    rv.add_argument("verdict")
    rv.add_argument("--pub")
    ix = sub.add_parser("index", help="play-by-play match index, burst frames, 'what the AI saw' viewer")
    ix.add_argument("bundle", help="QorCap bundle dir, bundle-meta.zip, or a dir holding bundle-meta.zip")
    ix.add_argument("--video", help="match video (full recording or a tail clip)")
    ix.add_argument("--video-tail", choices=["auto", "yes", "no"], default="auto", help="video is the END of the recording")
    ix.add_argument("--out", default="qt-index", help="output dir")
    ix.add_argument("--no-frames", action="store_true")
    ix.add_argument("--force", action="store_true", help="ignore cached match_index.json")
    pb = sub.add_parser("publish", help="public match page under SITE/matches/<slug>/ (needs a signed receipt.json)")
    pb.add_argument("bundle", help="QorCap bundle dir (with receipt.json), bundle-meta.zip, or a dir holding one")
    pb.add_argument("--site", default="site", help="site root (default: site)")
    pb.add_argument("--video", help="match video (full recording or a tail clip)")
    pb.add_argument("--highlights", help="qorcap highlights folder (default: BUNDLE/highlights if present)")
    pb.add_argument("--slug", help="URL slug (default: HHMMSS of the match id)")
    pb.add_argument("--allow-fail", action="store_true", help="publish even if the receipt FAILs (labeled as failing)")
    pb.add_argument("--include-names", action="store_true",
                    help="publish a receipt that carries both players' on-screen PSN names (match.observed_handles)")
    pb.add_argument("--no-frames", action="store_true")
    sub.add_parser("matches-index", help="rebuild SITE/matches/index.html").add_argument("--site", default="site")
    rs = sub.add_parser("referee-service", help="official referee service for verified uploads (QORTIME_REFEREE_SEED env)")
    rs.add_argument("--host", default="0.0.0.0")
    rs.add_argument("--port", type=int, default=int(__import__("os").getenv("PORT", "8080")))
    a = ap.parse_args(argv)
    if a.cmd == "referee-service":
        import uvicorn

        from qor_receipt.referee_service.app import create_app

        uvicorn.run(create_app(), host=a.host, port=a.port)
        return 0
    if a.cmd == "publish":
        from qor_receipt.match_index.publish import PublishRefused, publish

        try:
            out = publish(a.bundle, a.site, video=a.video, highlights=a.highlights, slug=a.slug, allow_fail=a.allow_fail,
                          frames=not a.no_frames, include_names=a.include_names)
        except PublishRefused as e:
            print(json.dumps({"published": False, "reason": str(e)}))
            return 4
        print(json.dumps({"published": True, **out}))
        return 0
    if a.cmd == "matches-index":
        from qor_receipt.match_index.publish import write_index

        print(write_index(a.site))
        return 0
    if a.cmd == "index":
        from qor_receipt.match_index import build_index

        idx = build_index(a.bundle, a.out, video=a.video, video_tail={"auto": None, "yes": True, "no": False}[a.video_tail],
                          frames=not a.no_frames, force=a.force)
        print(json.dumps({"out": a.out, "events": [(e["id"], e.get("score_after"), e["confidence"]) for e in idx["events"]],
                          "missing": idx["missing"]}))
        return 0
    if a.cmd == "referee-ai":
        return _referee_ai(a)
    if a.cmd == "referee-ai-verify":
        from qor_receipt.referee_ai.verdict import verify_verdict

        with open(a.verdict, encoding="utf-8") as fh:
            v = json.load(fh)
        errs = verify_verdict(v, pub=a.pub)
        print(json.dumps({"ok": not errs, "errors": errs, "verdict": v.get("verdict")}))
        return 2 if errs else 0
    if a.cmd == "agent":
        return _agent(a)
    if a.cmd == "agent-fixture":
        from qor_receipt.agent.fixture import make_session

        s = make_session(a.out)
        print(json.dumps({"frames_dir": s["frames_dir"], "hid_log": s["hid_log"]}))
        return 0
    if a.cmd == "verify":
        try:
            with open(a.receipt, encoding="utf-8", newline="") as fh:
                out = verify_receipt(fh.read(), referee_pub=a.referee_pub)
        except Exception:
            out = {"status": FAIL, "trust_tier": None, "errors": ["noncanonical_json"], "partial": [],
                   "referee_trust": "unknown"}
        print(json.dumps(out, indent=2))
        return {FAIL: 2, PARTIAL: 1}.get(out["status"], 0)  # VERIFIED|SELF_ATTESTED -> 0
    if a.cmd == "emit":
        from qor_receipt.demo import demo_receipt

        rec, ref_pub = demo_receipt(a.mode)
        with open(a.demo, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2)
        print(json.dumps({"wrote": a.demo, "referee_pub": ref_pub}))
        return 0
    print(" ".join(pattern_for(a.nonce)))
    return 0


def _referee_ai(a) -> int:
    from pathlib import Path

    from qor_receipt.keys import FileKey
    from qor_receipt.referee_ai import verdict as vd
    from qor_receipt.referee_ai.jev import DEFAULT_MODEL, JevJudge
    from qor_receipt.referee_ai.referee import referee_bundle
    from qor_receipt.referee_ai.vision import get_vision

    vision = None if a.vision == "none" else get_vision(a.vision)
    jev = None if a.no_jev else JevJudge(model=a.jev_model or DEFAULT_MODEL)
    v = referee_bundle(a.bundle, vision=vision, jev=jev, referee_pub=a.referee_pub, max_frames=a.max_frames)
    key = FileKey(Path(a.key) if a.key else Path.home() / ".qor-receipt" / "referee-ai.key")
    v = vd.sign(v, key)
    out = Path(a.out) if a.out else Path(a.bundle) / vd.FILENAME
    out.write_text(json.dumps(v, indent=2, sort_keys=True), encoding="utf-8")
    j = v["judgment"]
    print(json.dumps({"verdict": v["verdict"], "reasons": v["reasons"], "wrote": str(out),
                      "integrity_ok": v["integrity"]["ok"], "evidence_frames": len(v["evidence"]),
                      "jev": {"available": j["available"], "calls": j["calls"], "input_tokens": j["input_tokens"]},
                      "referee_ai_pub": v["referee_ai"]["pubkey"]}, indent=2))
    return {vd.ACCEPT: 0, vd.ABSTAIN: 1}.get(v["verdict"], 2)


def _agent(a) -> int:
    from pathlib import Path

    from qor_receipt.agent.pause_detect import PauseThresholds
    from qor_receipt.agent.runner import AgentConfig

    cfg = AgentConfig(role=a.role, player_id=a.player_id, cp_interval_ms=int(a.cp_interval * 1000),
                      pause=PauseThresholds.load(a.pause_config))
    key = None
    if a.key or not a.replay:
        from qor_receipt.keys import FileKey

        key = FileKey(Path(a.key) if a.key else Path.home() / ".qor-receipt" / "player.key")
    if a.print_pubkey:
        print(key.pubkey_hex if key else "replay uses an ephemeral key")
        return 0
    if a.replay:
        from qor_receipt.agent.runner import run_replay

        if not a.hid_log:
            print("--replay needs --hid-log", file=sys.stderr)
            return 2
        r = run_replay(a.replay, a.hid_log, cfg=cfg, key=key, quiet=False)
        out = a.out or "replay-receipt.json"
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(r["receipt"], fh, indent=2)
        print(json.dumps({"wrote": out, "referee_pub": r["referee_pub"], "verify": r["verify"], "stats": r["stats"]}))
        return {FAIL: 2, PARTIAL: 1}.get(r["verify"]["status"], 0)  # VERIFIED|SELF_ATTESTED -> 0
    if not a.referee:
        print("live mode needs --referee URL (or use --replay)", file=sys.stderr)
        return 2
    from qor_receipt.agent.runner import run_live

    out = Path(a.out or f"qor-receipt-{__import__('time').strftime('%Y%m%d-%H%M%S')}.json")
    r = run_live(device=a.device, referee=a.referee, cfg=cfg, out=out, key=key, match_id=a.match_id,
                 obs_text=a.obs_text, window=a.window)
    if r is None:
        return 1
    return {FAIL: 2, PARTIAL: 1}.get(r["verify"]["status"], 0)  # VERIFIED|SELF_ATTESTED -> 0


if __name__ == "__main__":
    sys.exit(main())
