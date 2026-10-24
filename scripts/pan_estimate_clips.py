"""Reproduce the camera-pan table in docs/redteam/camera-pan-estimator.md.

Runs the estimator on the three public, hash-locked 170123 highlight clips in
``site/matches/170123/clips`` at clip-relative time (offset 0, first bin at 0 ms)
and prints a Markdown table plus the sha256 of each clip's bins JSONL. Reads no
footage beyond those committed clips and writes nothing.

    python scripts/pan_estimate_clips.py

Needs the pinned OpenCV wheel (tests/constraints-pan-estimator.txt) to match the
documented output hashes.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qor_receipt.vision.pan_estimator import (  # noqa: E402
    BIN_MS,
    OpticalFlowPanEstimator,
    bins_jsonl,
    iter_video_frames,
)

CLIPS = ROOT / "site" / "matches" / "170123" / "clips"


def _pct(values: list[float], q: float):
    if not values:
        return None
    return values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))]


def _fmt(values: list[float]) -> str:
    return " / ".join(f"{v:.3g}" for v in (_pct(values, 0.5), _pct(values, 0.9), values[-1])) if values else "-"


def main() -> int:
    manifest = json.loads((CLIPS / "manifest.json").read_text(encoding="utf-8"))
    est = OpticalFlowPanEstimator()
    ident = est.identity()
    print(f"estimator sha256 {ident['sha256']} (opencv_pinned={ident['opencv_pinned']})")
    print()
    print("| clip | bins | claimed | no_claim | no_claim reasons | abs yaw p50 / p90 / max | abs pitch p50 / p90 / max |")
    print("|---|---|---|---|---|---|---|")
    digests = []
    for clip in manifest["clips"]:
        path = CLIPS / clip["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != clip["sha256"]:
            print(f"{clip['id']}: sha256 does not match manifest.json", file=sys.stderr)
            return 1
        frames = list(iter_video_frames(str(path), 0))
        if not frames:
            print(f"{clip['id']}: could not decode", file=sys.stderr)
            return 1
        start = (frames[0][0] // BIN_MS) * BIN_MS
        bins = est.estimate_frames(frames, start, frames[-1][0] + 1)
        claimed = [b for b in bins if not b.no_claim]
        reasons: dict[str, int] = {}
        for b in bins:
            if b.no_claim:
                reasons[b.reason] = reasons.get(b.reason, 0) + 1
        reason_txt = ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
        no_claim = len(bins) - len(claimed)
        yaw = sorted(abs(b.yaw_rate) for b in claimed)
        pitch = sorted(abs(b.pitch_rate) for b in claimed)
        print(
            f"| {clip['id']} | {len(bins)} | {len(claimed)} | {no_claim} ({100 * no_claim / len(bins):.1f} %) "
            f"| {reason_txt} | {_fmt(yaw)} | {_fmt(pitch)} |"
        )
        digests.append((clip["id"], hashlib.sha256(bins_jsonl(bins)).hexdigest()))
    print()
    for clip_id, digest in digests:
        print(f"bins sha256 {clip_id} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
