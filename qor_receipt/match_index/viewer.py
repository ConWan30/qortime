from __future__ import annotations

import base64
import copy
import html
import json
import struct
import zlib
from pathlib import Path


def _embed(out: Path, rel: str | None) -> str | None:
    if not rel:
        return None
    p = out / rel
    if not p.exists():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()


def write_viewer(idx: dict, out: Path, embed: bool = True, name: str = "viewer.html") -> Path:
    """embed=True: one self-contained file (frames as base64). embed=False: frames and thumbs stay files, loaded lazily."""
    data = copy.deepcopy(idx)
    for e in data["events"]:
        e["contact_sheet"] = None  # never inlined; the viewer does not show contact sheets
        for f in e.get("frames") or []:
            rel = f.get("file")
            thumb = f.get("thumb")
            if embed:
                f["src"] = _embed(out, rel)
                f.pop("thumb", None)
            else:
                f["src"] = rel if rel and (out / rel).exists() else None
                if not (thumb and (out / thumb).exists()):
                    f.pop("thumb", None)
    blob = json.dumps(data).replace("</", "<\\/")
    title = html.escape(f"QorTime · what the AI saw · {idx.get('match_id') or 'match'}")
    p = out / name
    # the published page gets the site favicon and per-match social tags; the local viewer.html stays free of any <link>
    head = ""
    if data.get("publish"):
        head = FAVICON + _public_head(data)
        (out / "og.png").write_bytes(og_png(data))
    page = TEMPLATE.replace("__TITLE__", title).replace("__HEAD__", head).replace("__DATA__", blob)
    p.write_text(page, encoding="utf-8")
    return p


_ORIGIN = "https://conwan30.github.io/qortime"
# 5x7 glyphs, low 5 bits. Used only for the social preview card.
_GLYPH = {
    " ": (0, 0, 0, 0, 0, 0, 0), "-": (0, 0, 0, 31, 0, 0, 0), ".": (0, 0, 0, 0, 0, 12, 12),
    "0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14), "2": (14, 17, 1, 2, 4, 8, 31),
    "3": (30, 1, 1, 14, 1, 1, 30), "4": (2, 6, 10, 18, 31, 2, 2), "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14), "7": (31, 1, 2, 4, 8, 8, 8), "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 1, 14), "A": (14, 17, 17, 31, 17, 17, 17), "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (15, 16, 16, 16, 16, 16, 15), "D": (30, 17, 17, 17, 17, 17, 30), "E": (31, 16, 16, 30, 16, 16, 31),
    "F": (31, 16, 16, 30, 16, 16, 16), "G": (15, 16, 16, 23, 17, 17, 15), "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14), "J": (7, 2, 2, 2, 2, 18, 12), "K": (17, 18, 20, 24, 20, 18, 17),
    "L": (16, 16, 16, 16, 16, 16, 31), "M": (17, 27, 21, 21, 17, 17, 17), "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14), "P": (30, 17, 17, 30, 16, 16, 16), "Q": (14, 17, 17, 17, 21, 18, 13),
    "R": (30, 17, 17, 30, 20, 18, 17), "S": (15, 16, 16, 14, 1, 1, 30), "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14), "V": (17, 17, 17, 17, 17, 10, 4), "W": (17, 17, 17, 21, 21, 21, 10),
    "X": (17, 17, 10, 4, 10, 17, 17), "Y": (17, 17, 10, 4, 4, 4, 4), "Z": (31, 1, 2, 4, 8, 16, 31),
}


def _rect(buf: bytearray, width: int, height: int, x: int, y: int, rw: int, rh: int, color: tuple[int, int, int]) -> None:
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + rw), min(height, y + rh)
    if x0 >= x1 or y0 >= y1:
        return
    row = bytes(color) * (x1 - x0)
    stride = width * 3
    for yy in range(y0, y1):
        start = yy * stride + x0 * 3
        buf[start:start + len(row)] = row


def _text(buf: bytearray, width: int, height: int, text: str, x: int, y: int, scale: int, color: tuple[int, int, int]) -> None:
    for ch in text.upper():
        rows = _GLYPH.get(ch, _GLYPH[" "])
        for ry, bits in enumerate(rows):
            for rx in range(5):
                if bits & (1 << (4 - rx)):
                    _rect(buf, width, height, x + rx * scale, y + ry * scale, scale, scale, color)
        x += 6 * scale


def og_png(idx: dict) -> bytes:
    """1200x630 preview. Gold appears only when the receipt status is VERIFIED."""
    width, height = 1200, 630
    pub = idx.get("publish") or {}
    status = str((pub.get("receipt") or {}).get("status") or "MISSING").upper()
    verified = status == "VERIFIED"
    accent = (240, 201, 92) if verified else (255, 109, 109) if status == "FAIL" else (63, 224, 208)
    shown = "SELF-SIGNED" if status == "SELF_ATTESTED" else status
    ink = (227, 237, 248)
    mute = (139, 155, 176)
    res = idx.get("result") or {}
    fin = res.get("final") if res.get("final_confirmed") else None
    score = f"FINAL {fin.get('away')}-{fin.get('home')}" if fin else "NO CONFIRMED FINAL"
    trust = "REFEREE VERIFIED" if verified else "NOT REFEREE VERIFIED"
    buf = bytearray((4, 8, 15) * (width * height))
    _rect(buf, width, height, 0, 0, width, 18, accent)
    _text(buf, width, height, "QORTIME", 64, 70, 8, ink)
    _text(buf, width, height, shown[:18], 64, 170, 12, accent)
    _text(buf, width, height, score, 64, 300, 8, ink)
    _text(buf, width, height, trust, 64, 400, 6, accent)
    _text(buf, width, height, str(idx.get("match_id") or pub.get("slug") or "MATCH")[:42], 64, 500, 4, mute)
    return _png(width, height, buf)


def _png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _public_head(idx: dict) -> str:
    pub = idx.get("publish") or {}
    slug = str(pub.get("slug") or "")
    status = str((pub.get("receipt") or {}).get("status") or "missing")
    res = idx.get("result") or {}
    fin = res.get("final") if res.get("final_confirmed") else None
    score = f"Final {fin.get('away')}–{fin.get('home')}" if fin else "No confirmed final"
    trust = "Referee-verified." if status == "VERIFIED" else "Not a referee-verified result."
    shown = "SELF-SIGNED" if status == "SELF_ATTESTED" else status
    mid = str(idx.get("match_id") or slug or "match")
    desc = f"{mid}. {score}. Receipt {shown}. {trust}"
    url = f"{_ORIGIN}/matches/{slug}/" if slug else f"{_ORIGIN}/"
    image = f"{url}og.png" if slug else f"{_ORIGIN}/assets/og.png"
    title = f"QorTime · {mid}"

    def meta(prop: str, content: str, *, name: bool = False) -> str:
        attr = "name" if name else "property"
        return f'<meta {attr}="{html.escape(prop)}" content="{html.escape(content)}">'

    return (
        f'<link rel="canonical" href="{html.escape(url)}">'
        f'<meta name="description" content="{html.escape(desc)}">'
        + meta("og:title", title) + meta("og:description", desc) + meta("og:url", url)
        + meta("og:image", image) + meta("og:type", "website")
        + meta("twitter:card", "summary_large_image", name=True) + meta("twitter:title", title, name=True)
        + meta("twitter:description", desc, name=True) + meta("twitter:image", image, name=True)
    )


TEMPLATE = (Path(__file__).with_name("viewer_template.html")).read_text(encoding="utf-8")
FAVICON = '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iMjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzJlZjJmZiIgc3Ryb2tlLXdpZHRoPSI1Ii8+PGcgc3Ryb2tlPSIjMmVmMmZmIiBzdHJva2Utd2lkdGg9IjMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCI+PHBhdGggZD0iTTMyIDl2NU01NSAzMmgtNU0zMiA1NXYtNU05IDMyaDUiLz48L2c+PHBhdGggZD0iTTMyIDMyVjE5IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PHBhdGggZD0iTTMyIDMybDE3IDE3IiBzdHJva2U9IiNmZjJlODgiIHN0cm9rZS13aWR0aD0iNiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iNSIgZmlsbD0iI2ZmMmU4OCIvPjwvc3ZnPgo=">'
