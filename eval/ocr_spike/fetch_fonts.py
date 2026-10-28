"""Download the open-licence fonts listed in fonts.json into a local directory and check their sha256.

    python eval/ocr_spike/fetch_fonts.py --out FONT_DIR

DejaVu and Liberation are copied from the OS (Debian/Ubuntu fonts-dejavu-core, fonts-liberation) when present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OS_PATHS = {"DejaVuSans-Bold.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "LiberationSans-Bold.ttf": "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for f in json.loads((HERE / "fonts.json").read_text())["fonts"]:
        dest = out / f["file"]
        if f["file"] in OS_PATHS and Path(OS_PATHS[f["file"]]).exists():
            shutil.copyfile(OS_PATHS[f["file"]], dest)
        elif f["url"].startswith("https://raw.githubusercontent.com/"):
            with urllib.request.urlopen(f["url"], timeout=60) as r:
                dest.write_bytes(r.read())
        ok = dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == f["sha256"]
        bad += not ok
        print(("ok   " if ok else "MISS ") + f["file"])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
