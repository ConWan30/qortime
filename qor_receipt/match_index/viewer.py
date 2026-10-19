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
    # the published page gets the site favicon; the local viewer.html stays free of any <link>
    head = FAVICON if data.get("publish") else ""
    page = TEMPLATE.replace("__TITLE__", title).replace("__HEAD__", head).replace("__DATA__", blob)
    p.write_text(page, encoding="utf-8")
    return p


TEMPLATE = (Path(__file__).with_name("viewer_template.html")).read_text(encoding="utf-8")
FAVICON = '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iMjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzJlZjJmZiIgc3Ryb2tlLXdpZHRoPSI1Ii8+PGcgc3Ryb2tlPSIjMmVmMmZmIiBzdHJva2Utd2lkdGg9IjMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCI+PHBhdGggZD0iTTMyIDl2NU01NSAzMmgtNU0zMiA1NXYtNU05IDMyaDUiLz48L2c+PHBhdGggZD0iTTMyIDMyVjE5IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PHBhdGggZD0iTTMyIDMybDE3IDE3IiBzdHJva2U9IiNmZjJlODgiIHN0cm9rZS13aWR0aD0iNiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iNSIgZmlsbD0iI2ZmMmU4OCIvPjwvc3ZnPgo=">'
