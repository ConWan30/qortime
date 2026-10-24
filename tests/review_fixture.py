"""Synthetic QorCap bundle with a real fragmented MP4 (ffmpeg) for referee-service tests.

96 s of 640x360 video in 2 s fMP4 fragments (48 media segments). "Busy" seconds have a fast-moving block
and lots of controller reports; "still" seconds are static with few reports (so the referee's motion
correlation is high). Three challenge windows show a Madden-like dark pause menu right after an OPTIONS
press. The length leaves media unrequested after motion rule v3 takes a spread of 24 fragments, the
recorded tail, the challenge windows, and the random spot-check.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from qor_receipt import receipt as R
from qor_receipt.challenge import pattern_for
from qor_receipt.checkpoints import countersign
from qor_receipt.jcs import sha256_hex
from qor_receipt.keys import SoftwareKey

W0 = 1_792_000_000_000
FPS = 30
SECS = 96
SEG_S = 2
# (phase, issued offset ms, OPTIONS press video ms, pause on video ms, pause off video ms)
CHALLENGES = [("kickoff", 3_000, 4_800, 5_000, 7_000), ("halftime", 15_000, 16_800, 17_000, 19_000),
              ("end", 27_000, 28_800, 29_000, 31_000)]
BITS = {"CROSS": 0, "CIRCLE": 1, "SQUARE": 2, "TRIANGLE": 3, "OPTIONS": 9, "UP": 12, "DOWN": 13, "LEFT": 14, "RIGHT": 15}
NONCES = {"kickoff": "00112233445566778899aabbccddeeff", "halftime": "ffeeddccbbaa99887766554433221100",
          "end": "0123456789abcdef0123456789abcdef"}


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def busy(sec: int) -> bool:
    return sec % 3 != 0


def in_pause(ms: int) -> bool:
    return any(on <= ms < off for _, _, _, on, off in CHALLENGES)


def make_video(path: Path) -> None:
    import numpy as np

    w, h = 640, 360
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
                          "-s", f"{w}x{h}", "-r", str(FPS), "-i", "pipe:0", "-c:v", "libx264", "-preset", "ultrafast",
                          "-pix_fmt", "yuv420p", "-g", str(FPS * SEG_S), "-keyint_min", str(FPS * SEG_S), "-sc_threshold", "0",
                          "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(path)],
                         stdin=subprocess.PIPE)
    base = np.tile(np.linspace(60, 140, w, dtype=np.uint8), (h, 1))
    for n in range(FPS * SECS):
        ms = n * 1000 // FPS
        if in_pause(ms):
            f = np.zeros((h, w), np.uint8)
            f[40:58, 280:420] = 235  # small white menu text block on black
        else:
            f = base.copy()
            if busy(ms // 1000):
                x = (n * 37) % (w - 120)
                y = (n * 23) % (h - 120)
                f[y:y + 120, x:x + 120] = 250
        p.stdin.write(f.tobytes())
    p.stdin.close()
    assert p.wait() == 0


def split_fmp4(data: bytes) -> list[bytes]:
    """init (everything before the first moof), then moof+mdat... per fragment (QorCap Mp4Splitter rule)."""
    boxes, i = [], 0
    while i + 8 <= len(data):
        size = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        if size == 1:
            size = int.from_bytes(data[i + 8:i + 16], "big")
        boxes.append((typ, data[i:i + size]))
        i += size
    segs: list[bytes] = [b""]
    for typ, b in boxes:
        if typ == b"moof":
            segs.append(b)
        else:
            segs[-1] += b
    return segs


def link(prev: str, seq: int, sha: str) -> str:
    return sha256_hex({"prev": prev, "seq": seq, "sha256": sha})


def build(out: Path, *, capture: SoftwareKey | None = None, device: SoftwareKey | None = None,
          challenge_tokens: list[dict] | None = None, claim=(14, 7)) -> dict:
    """Write a bundle into ``out``. ``challenge_tokens`` = official tokens (from Referee.issue_challenge) to embed."""
    import hashlib

    out.mkdir(parents=True, exist_ok=True)
    capture = capture or SoftwareKey()
    device = device or SoftwareKey()
    make_video(out / "video.mp4")
    data = (out / "video.mp4").read_bytes()
    segs = split_fmp4(data)
    match_id = "local-test-review"
    role = "home"
    # manifest
    man, head, off = [], "0" * 64, 0
    for i, s in enumerate(segs):
        sha = hashlib.sha256(s).hexdigest()
        prev = head
        head = link(head, i, sha)
        mono = 0 if i == 0 else i * SEG_S * 1000
        man.append({"type": "segment", "seq": i, "file": "video.mp4", "offset": off, "bytes": len(s), "sha256": sha,
                    "prev": prev, "chain_head": head, "mono_ms": mono, "wall_ms": W0 + mono,
                    "kind": "init" if i == 0 else "media", "frames": FPS * SEG_S})
        off += len(s)
    # inputs: busy seconds -> 40 reports, still -> 3; challenge presses
    tokens = {t["phase"]: t for t in (challenge_tokens or [])}
    presses: list[tuple[int, str]] = []
    challenges = []
    for phase, iss, opt, on, _off in CHALLENGES:
        tok = tokens.get(phase)
        nonce = tok["nonce"] if tok else NONCES[phase]
        pat = pattern_for(nonce)
        presses.append((opt, "OPTIONS"))
        for j, btn in enumerate(pat[1:]):
            presses.append((on + 2_500 + j * 400, btn))
        issued = tok["issued_ms"] if tok else W0 + iss
        challenges.append({"id": nonce[:8], "role": role, "phase": phase, "nonce": nonce, "pattern": pat,
                           "issued_ms": issued, "deadline_ms": tok["deadline_ms"] if tok else issued + 20_000, "result": None})
    recs: list[dict] = [{"type": "header", "schema": "qorcap-inputs/v0", "t_us": 0, "seg": 0, "wall_anchor_ms": W0}]
    events: list[tuple[int, int]] = []  # (mono_ms, mask)
    for sec in range(SECS):
        n = 40 if busy(sec) and not in_pause(sec * 1000) else 3
        for k in range(n):
            events.append((sec * 1000 + k * (1000 // n), 0))
    for t, btn in presses:
        events.append((t, 1 << BITS[btn]))
        events.append((t + 80, 0))
    events.sort()
    for t, mask in events:
        recs.append({"type": "in", "t_us": t * 1000, "b": mask, "lx": 128, "ly": 128, "rx": 128, "ry": 128, "l2": 0, "r2": 0})
    # assign seg = the media segment that covers t (sealed when segment i is hashed at mono i*2000)
    for r in recs:
        if r["type"] == "in":
            r["seg"] = min(len(segs) - 1, r["t_us"] // 1000 // (SEG_S * 1000) + 1)
    haptic = {"type": "haptic", "t_us": 10_000_000, "t_mono_ms": 10_000, "t_wall_ms": W0 + 10_000, "confidence": 900,
              "confidence_label": "high", "seg": 5}
    recs.append(haptic)
    by_seg: dict[int, list[dict]] = {}
    for r in recs:
        by_seg.setdefault(r["seg"], []).append(r)
    lines, chunks, ihead = [], [], "0" * 64
    for idx, seg in enumerate(sorted(by_seg)):
        rs = by_seg[seg]
        sha = sha256_hex(rs)
        prev = ihead
        ihead = link(ihead, idx, sha)
        lines += rs
        c = {"type": "chunk", "seq": idx, "seg": seg, "n": len(rs), "sha256": sha, "prev": prev, "head": ihead}
        lines.append(c)
        chunks.append(c)
    inputs_bytes = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    chunk_by_seg = {c["seg"]: c["sha256"] for c in chunks}
    # checkpoints: one per media segment
    cps, cs_signed, prev = [], [], "0" * 64
    for i in range(len(segs)):
        m = man[i]
        ext = {"chain_head": m["chain_head"], "segment_seq": i, "segment_hash": m["sha256"], "input_seg": i,
               "match_id": match_id, "role": role, "mono_ms": m["mono_ms"], "trigger": "segment"}
        body = {"seq": i, "t_ms": m["wall_ms"], "frames_hash": sha256_hex(ext),
                "input_hash": chunk_by_seg.get(i, sha256_hex([])), "prev": prev}
        body["hash"] = sha256_hex(body)
        cp = dict(body, qorcap=ext)
        cps.append(cp)
        cs_signed.append(countersign(capture, match_id, role, body, body["t_ms"] + 30))
        prev = body["hash"]
    started, ended = W0, man[-1]["wall_ms"] + 30
    hap_bytes = (json.dumps(haptic) + "\n").encode()
    sig_input = {"inputs_sha256": hashlib.sha256(inputs_bytes).hexdigest(), "inputs_chain_head": ihead, "haptic_events": 1,
                 "haptic_high_confidence": 1, "haptics_sha256": hashlib.sha256(hap_bytes).hexdigest(),
                 "human_permille": 900, "match_permille": 600, "chance_permille": 200, "status": "ok"}
    home, away = claim
    rec = R.build(match={"match_id": match_id, "game": "madden27", "platform": "ps5", "scorebug_profile": "madden27_standard",
                         "mode": "single", "started_ms": started, "ended_ms": ended},
                  result={"home_score": home, "away_score": away, "quarter": 4, "final_t_ms": ended, "winner": "home" if home > away else "away",
                          "method": "end_menu_read", "final_basis": "end_menu", "final_confirmed": True},
                  players=[{"role": role, "player_id": "local:player", "pubkey": device.pubkey_hex, "key_kind": "software-file"}],
                  referee={"pubkey": capture.pubkey_hex, "key_id": capture.key_id, "key_kind": "local-self-attested"},
                  challenges=challenges, checkpoints={role: cs_signed},
                  agreement={"timelines_agree": None, "mismatches": []},
                  signals={"motion_permille": {role: None}, "input": sig_input})
    rec = R.sign_player(R.sign_referee(rec, capture), role, device)
    meta = {"schema": "qorcap-bundle/v0", "match_id": match_id, "role": role, "chain_head": head,
            "checkpoint_head": cps[-1]["hash"], "segments": len(segs), "qorcap_version": "test", "clips": []}
    files = {
        "bundle.json": json.dumps(meta).encode(),
        "manifest.jsonl": ("\n".join(json.dumps(m) for m in man) + "\n").encode(),
        "checkpoints.jsonl": ("\n".join(json.dumps(c) for c in cps) + "\n").encode(),
        "countersigned.jsonl": ("\n".join(json.dumps(c) for c in cs_signed) + "\n").encode(),
        "inputs.jsonl": inputs_bytes,
        "haptics.jsonl": hap_bytes,
        "receipt.json": json.dumps(rec).encode(),
    }
    if challenge_tokens:
        files["official_challenges.jsonl"] = ("\n".join(json.dumps(t) for t in challenge_tokens) + "\n").encode()
    for k, v in files.items():
        (out / k).write_bytes(v)
    return {"files": files, "segments": segs, "manifest": man, "capture": capture, "device": device, "receipt": rec}


class FakeCore:
    """Stands in for QorCap's ReaderCore (pinned sidecar in production): reads a fixed end-of-game menu."""

    def __init__(self, post, home=14, away=7, basis="end_menu", confirmed=True):
        self.post, self.home, self.away, self.basis, self.confirmed = post, home, away, basis, confirmed
        self.final = None
        self.end_menu = None
        self.end_menu_rejected = None
        self.stats = {"frames": 0, "agreed": 0, "errors": 0, "paused": 0}
        self.delta_corroboration = []
        self.timeline = []

    def on_frame(self, t, img):
        self.stats["frames"] += 1
        if self.stats["frames"] == 2:
            self.post({"type": "score", "home": self.home, "away": self.away, "quarter": 4, "clock_s": 0, "t_ms": t})
        if self.stats["frames"] >= 4 and self.final is None:
            self.final = {"type": "final", "basis": self.basis, "home": self.home, "away": self.away, "quarter": 4, "t_ms": t,
                          "final_confirmed": self.confirmed, "notes": []}
            if self.basis == "end_menu":
                self.end_menu = {"home": self.home, "away": self.away}
            self.post(self.final)


def fake_factory(**kw):
    return lambda post: FakeCore(post, **kw)
