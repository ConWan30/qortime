"""python -m qor_receipt.ai_ruling RECEIPT [--pub HEX] [--out ai_ruling.json]

Runs the normal verifier, prints its verdict (unchanged), then asks Muse Spark for an advisory explanation.
Key from env META_API_KEY (or MODEL_API_KEY). On any AI failure the verdict is printed alone and no sidecar is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qor_receipt.ai_ruling.explainer import FILENAME, explain
from qor_receipt.ai_ruling.muse import DEFAULT_MODEL, MuseClient
from qor_receipt.receipt import verify_receipt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m qor_receipt.ai_ruling", description=__doc__.splitlines()[0])
    ap.add_argument("receipt")
    ap.add_argument("--pub", help="referee pubkey to pin (hex)")
    ap.add_argument("--out", help=f"sidecar path (default: {FILENAME} next to the receipt)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--sign-key", help="AI ADVISORY key file (never a referee key); signs ai_ruling.json as advisory_signature")
    a = ap.parse_args(argv)
    raw = Path(a.receipt).read_bytes()
    receipt = json.loads(raw)
    v = verify_receipt(receipt, referee_pub=a.pub)
    print(f"verdict (verifier): {v['status']}")
    if v.get("partial"):
        print("partial: " + ", ".join(v["partial"]))
    if v.get("errors"):
        print("errors: " + ", ".join(v["errors"]))
    res = explain(receipt, v, client=MuseClient(model=a.model), receipt_bytes=raw)
    if not res.ok:
        print(f"AI explanation: none ({res.reason}{': ' + ','.join(res.problems) if res.problems else ''}). Verdict unchanged.")
        return 0
    print("\nAI explanation (advisory, does not change the verdict):\n" + res.explanation)
    doc = res.sidecar
    if a.sign_key:
        from qor_receipt.ai_ruling.signing import sign_ruling
        from qor_receipt.keys import FileKey
        doc = sign_ruling(doc, FileKey(Path(a.sign_key)))
    if not a.no_write:
        out = Path(a.out) if a.out else Path(a.receipt).with_name(FILENAME)
        out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
