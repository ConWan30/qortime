from __future__ import annotations

import base64
import copy
import html
import json
from pathlib import Path


def _embed(out: Path, rel: str | None) -> str | None:
    if not rel:
        return None
    p = out / rel
    if not p.exists():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()


def write_viewer(idx: dict, out: Path, embed: bool = True, name: str = "viewer.html") -> Path:
    """embed=True: one self-contained file (frames as base64). embed=False: frames stay files next to the page."""
    data = copy.deepcopy(idx)
    for e in data["events"]:
        for f in e.get("frames") or []:
            f["src"] = _embed(out, f["file"]) if embed else (f["file"] if (out / f["file"]).exists() else None)
        # contact sheets stay on disk next to viewer.html; the viewer never shows them, so embedding them
        # only made the single file ~25% bigger (slower to parse on phones)
    blob = json.dumps(data).replace("</", "<\\/")
    title = html.escape(f"QorTime · what the AI saw · {idx.get('match_id') or 'match'}")
    p = out / name
    # the published page gets the site favicon; the local viewer.html stays free of any <link>
    head = FAVICON if data.get("publish") else ""
    page = TEMPLATE.replace("__TITLE__", title).replace("__HEAD__", head).replace("__DATA__", blob)
    p.write_text(page, encoding="utf-8")
    return p


TEMPLATE = (Path(__file__).with_name("viewer_template.html")).read_text(encoding="utf-8")
FAVICON = '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iMjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzJlZjJmZiIgc3Ryb2tlLXdpZHRoPSI1Ii8+PGcgc3Ryb2tlPSIjMmVmMmZmIiBzdHJva2Utd2lkdGg9IjMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCI+PHBhdGggZD0iTTMyIDl2NU01NSAzMmgtNU0zMiA1NXYtNU05IDMyaDUiLz48L2c+PHBhdGggZD0iTTMyIDMyVjE5IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PHBhdGggZD0iTTMyIDMybDE3IDE3IiBzdHJva2U9IiNmZjJlODgiIHN0cm9rZS13aWR0aD0iNiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iNSIgZmlsbD0iI2ZmMmU4OCIvPjwvc3ZnPgo=">'
