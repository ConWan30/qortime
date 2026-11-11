"""Deterministic synthetic scorebug generator (Madden-style bottom strip, 1280x720 geometry).

Everything is drawn from scratch with Pillow + OpenCV + numpy. No game footage, no game
art, no trademark text: team names are random 2-4 letter strings and the wordmark box holds
random letters. Fonts are open-licence files listed in ``fonts.json`` and pinned by sha256.

Each sample is a 1280x60 band (frame rows 660..720, the band the current reader cuts) plus
per-field labels. Field crops use the same ROI table as the smoke test (``roi.FIELDS``).

Determinism: sample ``i`` of split ``s`` uses ``numpy.random.default_rng([seed, split_id, i])``,
so output does not depend on worker count or order. Byte equality of the PNGs also needs the
same Pillow/FreeType, OpenCV/libjpeg and font files; the manifest records all of them.

SYNTHETIC DATA ONLY. Scores on these images never count toward replacing the current reader.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .roi import BAND_H, BAND_Y0, FIELD_NAMES, FRAME_W, crop_band_field

GENERATOR_VERSION = "ocr-spike-synth/0.1"
SPLIT_IDS = {"train": 1, "val": 2, "probe": 3}
SS = 2  # supersampling factor for drawing
QUARTERS = ("1st", "2nd", "3rd", "4th", "OT")
DOWN_SUFFIX = {1: "ST", 2: "ND", 3: "RD", 4: "TH"}
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class FontSpec:
    file: str
    sha256: str
    wght: int | None = None  # variable-font weight axis


def load_font_specs(fonts_json: Path) -> list[FontSpec]:
    data = json.loads(Path(fonts_json).read_text(encoding="utf-8"))
    return [FontSpec(f["file"], f["sha256"], f.get("wght")) for f in data["fonts"]]


def verify_fonts(font_dir: Path, specs: list[FontSpec]) -> list[FontSpec]:
    """Return the specs whose file exists with the pinned sha256 (others are skipped, never substituted)."""
    ok = []
    for s in specs:
        p = Path(font_dir) / s.file
        if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == s.sha256:
            ok.append(s)
    return ok


class FontBank:
    def __init__(self, font_dir: Path, specs: list[FontSpec]) -> None:
        if not specs:
            raise ValueError("no verified fonts")
        self.font_dir = Path(font_dir)
        self.specs = specs
        self._cache: dict[tuple[int, int], ImageFont.FreeTypeFont] = {}

    def get(self, idx: int, px: int) -> ImageFont.FreeTypeFont:
        key = (idx, px)
        f = self._cache.get(key)
        if f is None:
            s = self.specs[idx]
            f = ImageFont.truetype(str(self.font_dir / s.file), px)
            if s.wght is not None:
                f.set_variation_by_axes([s.wght])
            self._cache[key] = f
        return f


# ---------------------------------------------------------------- labels
def sample_labels(rng: np.random.Generator) -> dict[str, str]:
    def team() -> str:
        n = int(rng.choice([2, 3, 3, 3, 4]))
        return "".join(LETTERS[int(k)] for k in rng.integers(0, 26, n))

    def score() -> str:
        v = int(rng.integers(0, 100)) if rng.random() < 0.25 else int(rng.integers(0, 50))
        return str(v)

    q = QUARTERS[int(rng.choice(5, p=[0.24, 0.24, 0.24, 0.24, 0.04]))]
    m = int(rng.integers(0, 16))
    s = 0 if m == 15 else int(rng.integers(0, 60))
    down = int(rng.integers(1, 5))
    r = rng.random()
    dist = "GOAL" if r < 0.08 else str(int(rng.integers(1, 11)) if r < 0.8 else int(rng.integers(11, 40)))
    return {
        "team_l": team(),
        "score_l": score(),
        "score_r": score(),
        "team_r": team(),
        "quarter": q,
        "game_clock": f"{m}:{s:02d}",
        "play_clock": f":{int(rng.integers(0, 41)):02d}",
        "down_distance": f"{down}{DOWN_SUFFIX[down]} & {dist}",
    }


def normalize(field: str, text: str) -> str:
    """Comparison form used for exact match: no whitespace, upper case."""
    return "".join(str(text).split()).upper()


# ---------------------------------------------------------------- drawing
def _rgb(rng: np.random.Generator, lo: int = 0, hi: int = 256) -> tuple[int, int, int]:
    c = rng.integers(lo, hi, 3)
    return (int(c[0]), int(c[1]), int(c[2]))


def _team_color(rng: np.random.Generator) -> tuple[int, int, int]:
    # saturated-ish colours, some dark
    hsv = np.uint8([[[int(rng.integers(0, 180)), int(rng.integers(120, 256)), int(rng.integers(60, 230))]]])
    r, g, b = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)[0, 0]
    return (int(r), int(g), int(b))


def _box(d: ImageDraw.ImageDraw, x1: float, y1: float, x2: float, y2: float, fill) -> None:
    d.rectangle([x1 * SS, (y1 - BAND_Y0) * SS, x2 * SS - 1, (y2 - BAND_Y0) * SS - 1], fill=fill)


def _gradient(img: Image.Image, x1: int, y1: int, x2: int, y2: int, c1, c2, vertical: bool) -> None:
    w, h = (x2 - x1) * SS, (y2 - y1) * SS
    if w <= 0 or h <= 0:
        return
    t = np.linspace(0.0, 1.0, h if vertical else w, dtype=np.float32)
    a, b = np.array(c1, np.float32), np.array(c2, np.float32)
    line = (a[None] * (1 - t[:, None]) + b[None] * t[:, None]).round().astype(np.uint8)
    arr = np.repeat(line[:, None, :], w, 1) if vertical else np.repeat(line[None, :, :], h, 0)
    img.paste(Image.fromarray(arr, "RGB"), (x1 * SS, (y1 - BAND_Y0) * SS))


def _text(d: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill, anchor: str = "mm") -> tuple[int, int, int, int]:
    x, y = cx * SS, (cy - BAND_Y0) * SS
    d.text((x, y), text, font=font, fill=fill, anchor=anchor)
    return d.textbbox((x, y), text, font=font, anchor=anchor)


def render_band(rng: np.random.Generator, labels: dict[str, str], fonts: FontBank) -> tuple[np.ndarray, dict[str, Any]]:
    """Draw one clean band (RGB, 1280x60) for ``labels``; returns (image, render params)."""
    W, H = FRAME_W * SS, BAND_H * SS
    nf = len(fonts.specs)
    p: dict[str, Any] = {}
    bg_top = int(rng.integers(20, 120))
    img = Image.new("RGB", (W, H), (bg_top, int(bg_top * 1.3) % 256, bg_top // 2))  # field/grass-ish backdrop
    # backdrop texture: random low-frequency blobs so the strip edge varies
    noise = rng.integers(0, 60, (3, 8), dtype=np.int32).astype(np.uint8)
    bd = cv2.resize(noise, (W, H), interpolation=cv2.INTER_CUBIC)
    base = np.asarray(img, np.int16) + bd[..., None].astype(np.int16) - 30
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img)

    strip_y1, strip_y2 = 674, 718
    light = int(rng.integers(225, 256))
    _gradient(img, 0, strip_y1, FRAME_W, strip_y2, (light,) * 3, (max(0, light - int(rng.integers(0, 35))),) * 3, True)
    fi = {k: int(rng.integers(0, nf)) for k in ("team", "score", "clock", "quarter", "down", "logo")}
    p["fonts"] = {k: fonts.specs[v].file for k, v in fi.items()}

    # wordmark box with random letters (not any real mark)
    logo_font = fonts.get(fi["logo"], int(rng.integers(26, 34)))
    _text(d, 84, 696, "".join(LETTERS[int(k)] for k in rng.integers(0, 26, 6)), logo_font, (10, 10, 10))

    cl, cr = _team_color(rng), _team_color(rng)
    for (x1, x2), c in (((170, 318), cl), ((686, 834), cr)):
        c2 = tuple(int(v * float(rng.uniform(0.55, 1.0))) for v in c)
        _gradient(img, x1, strip_y1, x2, strip_y2, c, c2, bool(rng.random() < 0.5))

    # team abbreviations + timeout bars
    tpx = int(rng.integers(32, 41))
    tcol = _rgb(rng, 0, 50)
    for cx, key, c in ((376, "team_l", cl), (628, "team_r", cr)):
        _text(d, cx + float(rng.uniform(-3, 3)), 692 + float(rng.uniform(-1, 1)), labels[key], fonts.get(fi["team"], tpx), tcol)
        used = int(rng.integers(0, 4))
        for k in range(3):
            bx = cx - 30 + k * 22
            _box(d, bx, 705, bx + 16, 708, c if k < 3 - used else (170, 170, 170))

    # scores, possession dot, separator
    spx = int(rng.integers(40, 52))
    for cx, key, c in ((464, "score_l", cl), (540, "score_r", cr)):
        col = c if rng.random() < 0.5 else _rgb(rng, 0, 70)
        _text(d, cx + float(rng.uniform(-3, 3)), 698 + float(rng.uniform(-1.5, 1.5)), labels[key], fonts.get(fi["score"], spx), col)
    if rng.random() < 0.8:
        dot_x = 434 if rng.random() < 0.5 else 578
        d.ellipse([(dot_x - 3) * SS, (697 - 3 - BAND_Y0) * SS, (dot_x + 3) * SS, (697 + 3 - BAND_Y0) * SS], fill=(20, 20, 20))
    _text(d, 502, 698, "x", fonts.get(fi["team"], 22), (150, 150, 150))

    # clock box: quarter, game clock, play clock
    dark = int(rng.integers(0, 40))
    _box(d, 852, 677, 1030, 716, (dark, dark, dark))
    white = (245, 245, 245) if rng.random() < 0.85 else (230, 40, 40)  # red final-minute clock sometimes
    _text(d, 877, 697, labels["quarter"], fonts.get(fi["quarter"], int(rng.integers(22, 28))), (235, 235, 235))
    _text(d, 926 + float(rng.uniform(-2, 2)), 697, labels["game_clock"], fonts.get(fi["clock"], int(rng.integers(32, 39))), white)
    pc_bg = (245, 245, 245) if rng.random() < 0.8 else (dark + 30, dark + 30, dark + 30)
    pc_fg = (15, 15, 15) if pc_bg[0] > 128 else (245, 245, 245)
    _box(d, 966, 683, 1020, 711, pc_bg)
    _text(d, 993, 697, labels["play_clock"], fonts.get(fi["clock"], int(rng.integers(26, 34))), pc_fg)

    # down & distance with superscript suffix
    _box(d, 1037, 677, 1180, 716, (dark, dark, dark))
    num, rest = labels["down_distance"][0], labels["down_distance"][1:]
    suffix, tail = rest[:2], rest[2:]
    big = fonts.get(fi["down"], int(rng.integers(30, 37)))
    small = fonts.get(fi["down"], int(big.size * 0.6))
    widths = [d.textlength(num, font=big), d.textlength(suffix, font=small), d.textlength(tail, font=big)]
    x = (1108 * SS) - sum(widths) / 2
    y = (697 - BAND_Y0) * SS
    sup = rng.random() < 0.7
    d.text((x, y), num, font=big, fill=(240, 240, 240), anchor="lm")
    x += widths[0]
    d.text((x, y - (big.size * 0.18 if sup else 0)), suffix, font=small if sup else big, fill=(240, 240, 240), anchor="lm")
    x += widths[1] if sup else d.textlength(suffix, font=big)
    d.text((x, y), tail, font=big, fill=(240, 240, 240), anchor="lm")

    # right info box (not labelled)
    _box(d, 1190, 680, 1272, 714, (250, 250, 250))
    _text(d, 1231, 697, str(int(rng.integers(1, 99))), fonts.get(fi["team"], 24), (20, 20, 20))

    arr = np.asarray(img)
    arr = cv2.resize(arr, (FRAME_W, BAND_H), interpolation=cv2.INTER_AREA)
    return arr, p


# ---------------------------------------------------------------- degradations
def degrade(rng: np.random.Generator, rgb: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    """Capture/encode artefacts. Input/output RGB uint8 1280x60. Returns (image, params)."""
    p: dict[str, Any] = {}
    img = rgb
    h, w = img.shape[:2]
    # misalignment + scale (the reader's anchor search covers +-3 px at 640x360 = +-6 px at 720p)
    s = float(rng.uniform(0.97, 1.03))
    dx, dy = float(rng.uniform(-4, 4)), float(rng.uniform(-2, 2))
    M = np.float32([[s, 0, (1 - s) * w / 2 + dx], [0, s, (1 - s) * h / 2 + dy]])
    img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    p["affine"] = [round(s, 4), round(dx, 3), round(dy, 3)]
    # source-resolution round trip (640x360 path, 1080p path)
    r = rng.random()
    if r < 0.35:
        f = float(rng.choice([0.5, 0.6, 0.75]))
        small = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
        img = cv2.resize(small, (w, h), interpolation=int(rng.choice([cv2.INTER_LINEAR, cv2.INTER_CUBIC])))
        p["rescale"] = f
    if rng.random() < 0.5:
        sigma = float(rng.uniform(0.3, 1.1))
        img = cv2.GaussianBlur(img, (0, 0), sigma)
        p["blur"] = round(sigma, 3)
    if rng.random() < 0.7:  # 4:2:0 chroma subsampling
        ycc = cv2.cvtColor(img, cv2.COLOR_RGB2YCrCb)
        for c in (1, 2):
            ch = cv2.resize(ycc[..., c], (w // 2, h // 2), interpolation=cv2.INTER_AREA)
            ycc[..., c] = cv2.resize(ch, (w, h), interpolation=cv2.INTER_LINEAR)
        img = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
        p["chroma420"] = True
    if rng.random() < 0.4:  # H.264-like blockwise DCT quantisation on luma
        q = float(rng.uniform(4, 22))
        ycc = cv2.cvtColor(img, cv2.COLOR_RGB2YCrCb)
        y = ycc[..., 0].astype(np.float32)
        hh, ww = (h // 8) * 8, (w // 8) * 8
        blocks = y[:hh, :ww].reshape(hh // 8, 8, ww // 8, 8).transpose(0, 2, 1, 3).reshape(-1, 8, 8)
        for b in range(blocks.shape[0]):
            c = cv2.dct(blocks[b])
            blocks[b] = cv2.idct(np.round(c / q) * q)
        y[:hh, :ww] = blocks.reshape(hh // 8, ww // 8, 8, 8).transpose(0, 2, 1, 3).reshape(hh, ww)
        ycc[..., 0] = np.clip(np.round(y), 0, 255).astype(np.uint8)
        img = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
        p["dct_q"] = round(q, 2)
    if rng.random() < 0.5:
        sigma = float(rng.uniform(1, 6))
        n = rng.normal(0, sigma, img.shape)
        img = np.clip(np.round(img.astype(np.float64) + n), 0, 255).astype(np.uint8)
        p["noise"] = round(sigma, 2)
    if rng.random() < 0.8:
        qual = int(rng.integers(35, 96))
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, qual])
        assert ok
        img = cv2.cvtColor(cv2.imdecode(buf, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        p["jpeg"] = qual
    return np.ascontiguousarray(img), p


def make_sample(seed: int, split: str, index: int, fonts: FontBank) -> dict[str, Any]:
    """One synthetic sample: BGR band, per-field BGR crops, labels and params. Pure function of its inputs."""
    rng = np.random.default_rng([int(seed), SPLIT_IDS[split], int(index)])
    labels = sample_labels(rng)
    clean, rp = render_band(rng, labels, fonts)
    deg, dp = degrade(rng, clean)
    bgr = cv2.cvtColor(deg, cv2.COLOR_RGB2BGR)
    crops = {}
    jit = {}
    for f in FIELD_NAMES:
        jx, jy = int(rng.integers(-1, 2)), int(rng.integers(-1, 2))
        jit[f] = [jx, jy]
        crops[f] = crop_band_field(bgr, f, jx, jy)
    return {"labels": labels, "band": bgr, "crops": crops, "params": {"render": rp, "degrade": dp, "crop_jitter": jit}}


def png_bytes(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    assert ok
    return buf.tobytes()
