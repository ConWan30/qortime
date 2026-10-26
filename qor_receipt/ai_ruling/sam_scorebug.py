"""SAM 3.1 scorebug LOCATOR (info only, never affects a status). Built during the Meta AI Developer Hackathon, Oct 26 - Nov 4 2026.

Asks Meta SAM 3.1 for a "scoreboard" box on a frame and reports whether it sits where the game's scorebug profile
says. Used for demo visuals (drawing the box/mask over the scorebug) and as a crop sanity check. It is NOT a status
signal: measured on public 170123 clip frames (1280x720, QorCap burn-in label masked) SAM found the real scorebug on
only 4 of 6 legit frames, and on the small posters it boxed QorCap's own burned-in label. The status-affecting
second reader is Muse Spark (``score_crosscheck.py``).

Request shape (PREREQS section 11): POST /v1/responses, model sam-3.1, input_text "scoreboard" + input_image,
non-streaming; boxes parsed from ``<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|>``. $2.50 per 1,000 images.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from qor_receipt.ai_ruling.muse import BASE_URL, _open, api_key_from_env
from qor_receipt.jcs import canonicalize

SCHEMA = "qortime.sam_scorebug.v0"
FILENAME = "sam_scorebug.json"
MODEL = "sam-3.1"
PROMPT = "scoreboard"
USD_PER_IMAGE_MICRO = 2500           # $0.0025 per image, in micro-dollars (integers only)
MAX_IMAGES = 12                      # hard per-run cap on paid calls
LABEL = "INFO ONLY. Meta SAM 3.1 located the scorebug on these frames. It never affects a status."

# Expected scorebug region per profile, in permille of the frame: the box centre must be inside
# [x_min, y_min, x_max, y_max] and the box at least min_width wide. madden27_standard = full-width bottom strip,
# measured on full-res (1280x720) frames of the public 170123 clips (SAM box 0,676-1279,719 => centre y 968).
# "mask" = QorCap's burned-in "SCOREBUG LOCK" label (top-left) on highlight clips: blank it before sending, or SAM
# boxes that label instead of the game's scorebug (seen on all 170123 posters). Unknown profile => unchecked.
PROFILES: dict[str, dict[str, Any]] = {
    "madden27_standard": {"centre_region": [300, 880, 700, 1000], "min_width_permille": 500, "mask": [0, 0, 560, 200]},
}


def mask_burn_in(image: bytes, profile: str) -> bytes:
    """Blank the profile's mask region (QorCap burn-in label) and return JPEG bytes. Needs opencv (vision extra)."""
    import cv2
    import numpy as np

    prof = PROFILES[profile]
    img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("unreadable image")
    h, w = img.shape[:2]
    x1, y1, x2, y2 = prof["mask"]
    img[y1 * h // 1000:y2 * h // 1000, x1 * w // 1000:x2 * w // 1000] = 0
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("encode failed")
    return buf.tobytes()


_BOX = re.compile(r"<\|box;x1=(-?\d+);y1=(-?\d+);x2=(-?\d+);y2=(-?\d+);w=(\d+);h=(\d+)\|>")


class SamError(Exception):
    """API/network/format failure (message never contains the key)."""


def parse_boxes(text: str) -> list[dict[str, int]]:
    out = []
    for m in _BOX.finditer(text or ""):
        x1, y1, x2, y2, w, h = (int(g) for g in m.groups())
        if w > 0 and h > 0 and x2 > x1 and y2 > y1:
            out.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "w": w, "h": h})
    return out


def classify(boxes: list[dict[str, int]], profile: str) -> str:
    prof = PROFILES.get(profile)
    if prof is None:
        return "unchecked"
    if not boxes:
        return "no_scorebug"
    rx1, ry1, rx2, ry2 = prof["centre_region"]
    for b in boxes:
        cx = (b["x1"] + b["x2"]) * 500 // b["w"]
        cy = (b["y1"] + b["y2"]) * 500 // b["h"]
        wid = (b["x2"] - b["x1"]) * 1000 // b["w"]
        if rx1 <= cx <= rx2 and ry1 <= cy <= ry2 and wid >= prof["min_width_permille"]:
            return "scorebug_in_region"
    return "scorebug_out_of_region"


def summarize(results) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r] = out.get(r, 0) + 1
    return out


def _urllib_transport(url: str, key: str, body: dict, timeout_s: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with _open(req, timeout_s) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001
        raise SamError(f"network:{type(e).__name__}") from None


@dataclass
class SamClient:
    api_key: str | None = None
    base_url: str = BASE_URL
    timeout_s: float = 60.0
    transport: Callable[..., tuple[int, Any]] | None = None

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = api_key_from_env()
        self.transport = self.transport or _urllib_transport

    def __repr__(self) -> str:
        return f"SamClient(key={'set' if self.api_key else 'missing'})"

    def boxes(self, image: bytes, mime: str = "image/jpeg") -> list[dict[str, int]]:
        if not self.api_key:
            raise SamError("api_key_missing")
        url = f"data:{mime};base64," + base64.b64encode(image).decode()
        body = {"model": MODEL, "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": PROMPT}, {"type": "input_image", "image_url": url}]}]}
        status, d = self.transport(f"{self.base_url.rstrip('/')}/responses", self.api_key, body, self.timeout_s)
        if status != 200 or not isinstance(d, dict):
            raise SamError(f"http_{status}")
        text = ""
        try:
            for o in d.get("output") or []:
                for c in o.get("content") or []:
                    if c.get("type") == "output_text":
                        text += c.get("text") or ""
        except (AttributeError, TypeError) as e:
            raise SamError(f"malformed_response:{type(e).__name__}") from None
        return parse_boxes(text)


def check(receipt: dict[str, Any], frames: list[tuple[str, bytes]], *, client: SamClient | None = None,
          profile: str | None = None, max_images: int = MAX_IMAGES) -> dict[str, Any]:
    """frames = [(frame_id, jpeg_or_png_bytes)] that should show live play. Returns the sidecar. Never raises."""
    client = client or SamClient()
    prof = profile or str(((receipt or {}).get("match") or {}).get("scorebug_profile") or "")
    rows, calls = [], 0
    for fid, img in frames:
        row: dict[str, Any] = {"id": str(fid), "sha256": hashlib.sha256(img or b"").hexdigest()}
        if prof not in PROFILES:
            row.update(result="unchecked", reason="unknown_profile")
        elif not client.api_key:
            row.update(result="unchecked", reason="api_key_missing")
        elif calls >= max_images:
            row.update(result="unchecked", reason="max_images")
        elif not img:
            row.update(result="unchecked", reason="empty_frame")
        else:
            calls += 1
            try:
                mime = "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                bxs = client.boxes(img, mime)
                row.update(result=classify(bxs, prof), boxes=bxs[:4])
            except SamError as e:
                row.update(result="unchecked", reason=f"api_error:{e}")
            except Exception as e:  # noqa: BLE001  advisory: never take anything down
                row.update(result="unchecked", reason=f"error:{type(e).__name__}")
        rows.append(row)
    return {"schema": SCHEMA, "label": LABEL, "built_during": "Meta AI Developer Hackathon, Oct 26 - Nov 4 2026",
            "affects_verdict": False, "model": MODEL, "prompt": PROMPT, "profile": prof,
            "receipt_jcs_sha256": hashlib.sha256(canonicalize(receipt)).hexdigest(),
            "frames": rows, "summary": summarize(r["result"] for r in rows),
            "cost_micro_usd": calls * USD_PER_IMAGE_MICRO, "calls": calls}
