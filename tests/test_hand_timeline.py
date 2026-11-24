"""Tests for qor_receipt.hands.timeline and qorcap-hand-timeline/v0 golden pack (SYNTHETIC fixtures only)."""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
from pathlib import Path

import pytest

from qor_receipt.hands import sidecar as S
from qor_receipt.hands.synthetic import receipt_for
from qor_receipt.hands.timeline import (
    EDGE_BITS_MASK,
    RAW_RECORD_BYTES,
    normalize_in_records,
    verify_hand_timeline_dir,
)
from qor_receipt.jcs import canonicalize, sha256_hex

ROOT = Path(__file__).resolve().parents[1]
FIX_DIR = ROOT / "tests" / "fixtures" / "hand_timeline"
SCHEMA_DIR = ROOT / "tests" / "schemas" / "hand_timeline"
MANIFEST_SHA256 = "7b27ade3d0c4653ffd8e50095b3d40a153e03504f7ae382b8bb06ae9a13304f4"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rehash_timeline(tl_dir: Path, mutator) -> None:
    tl_path = tl_dir / "hand_timeline.v0.jsonl"
    rows = [json.loads(line) for line in tl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out_lines: list[str] = []
    pending: list[dict] = []
    head = "0" * 64
    for row in rows:
        mutator(row)
        if row.get("type") != "chunk":
            pending.append(row)
            continue
        seq = row["seq"]
        recs = pending
        pending = []
        sha = sha256_hex(recs)
        new_head = sha256_hex({"prev": head, "seq": seq, "sha256": sha})
        row["sha256"] = sha
        row["prev"] = head
        row["head"] = new_head
        head = new_head
        for item in [*recs, row]:
            out_lines.append(canonicalize(item).decode("utf-8"))
    tl_path.write_bytes(("\n".join(out_lines) + "\n").encode("utf-8"))


def test_vendored_manifest_and_fixtures_are_intact_and_lf():
    manifest_raw = (FIX_DIR / "MANIFEST.json").read_bytes()
    assert b"\r" not in manifest_raw
    assert _sha256_hex(manifest_raw) == MANIFEST_SHA256
    manifest = json.loads(manifest_raw.decode("utf-8"))
    assert len(manifest["files"]) == 22
    for entry in manifest["files"]:
        fp = FIX_DIR / entry["path"]
        data = fp.read_bytes()
        assert len(data) == entry["bytes"], entry["path"]
        assert _sha256_hex(data) == entry["sha256"], entry["path"]
        if entry["path"].endswith((".json", ".jsonl")):
            assert b"\r" not in data, f"CR byte found in {entry['path']}"


def test_schemas_validate_golden_records_and_summaries():
    jsonschema = pytest.importorskip("jsonschema")
    tl_schema = json.loads((SCHEMA_DIR / "hand_timeline.v0.schema.json").read_text(encoding="utf-8"))
    sum_schema = json.loads((SCHEMA_DIR / "hand_timeline_summary.v0.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(tl_schema)
    jsonschema.Draft202012Validator.check_schema(sum_schema)
    tl_val = jsonschema.Draft202012Validator(tl_schema)
    sum_val = jsonschema.Draft202012Validator(sum_schema)

    for case in ("187_edge_basic", "189_edge", "189_dualsense_fallback"):
        cdir = FIX_DIR / case
        for line in (cdir / "hand_timeline.v0.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                tl_val.validate(json.loads(line))
        summary = json.loads((cdir / "hand_timeline_summary.v0.json").read_text(encoding="utf-8"))
        sum_val.validate(summary)


def test_verify_matches_all_seven_manifest_fixtures():
    manifest = json.loads((FIX_DIR / "MANIFEST.json").read_text(encoding="utf-8"))["fixtures"]
    for name, spec in manifest.items():
        res = verify_hand_timeline_dir(FIX_DIR / name)
        mode = spec["mode"]
        if mode == "pass":
            assert res.ok, f"{name}: unexpected errors {res.errors}"
            assert res.off is spec["off"], name
            assert res.chunks == spec["chunks"], name
            if "head" in spec:
                assert res.head == spec["head"], name
            if "file_sha256" in spec:
                assert res.file_sha256 == spec["file_sha256"], name
            if "fallback" in spec:
                assert res.fallback is spec["fallback"], name
            assert len(res.warnings) >= spec.get("warnings_min", 0), name
            if "warning_contains" in spec:
                assert any(spec["warning_contains"] in w for w in res.warnings), res.warnings
        elif mode == "fail":
            assert not res.ok, f"{name}: expected fail"
            assert len(res.errors) >= spec.get("errors_min", 1), name
            if "error_contains" in spec:
                assert any(spec["error_contains"] in e for e in res.errors), res.errors
        elif mode == "err":
            assert not res.ok, f"{name}: expected missing error"
            assert any(spec["err_contains"] in e for e in res.errors), res.errors


def test_verify_rejects_crlf_tamper_raw_slice_gaps_and_dualsense_leak(tmp_path: Path):
    base = tmp_path / "edge"
    shutil.copytree(FIX_DIR / "189_edge", base)

    # 1. CRLF byte fails closed
    crlf_dir = tmp_path / "crlf"
    shutil.copytree(base, crlf_dir)
    tl_bytes = (crlf_dir / "hand_timeline.v0.jsonl").read_bytes().replace(b"\n", b"\r\n")
    (crlf_dir / "hand_timeline.v0.jsonl").write_bytes(tl_bytes)
    res_crlf = verify_hand_timeline_dir(crlf_dir)
    assert not res_crlf.ok and any("contains CR" in e for e in res_crlf.errors)

    # 2. Tampered in-row value breaks SHA/head
    flip_dir = tmp_path / "flip_in"
    shutil.copytree(base, flip_dir)
    txt = (flip_dir / "hand_timeline.v0.jsonl").read_text(encoding="utf-8")
    assert '"r2":0' in txt
    (flip_dir / "hand_timeline.v0.jsonl").write_bytes(txt.replace('"r2":0', '"r2":1', 1).encode("utf-8"))
    res_flip = verify_hand_timeline_dir(flip_dir)
    assert not res_flip.ok and any("records SHA-256" in e for e in res_flip.errors)

    # 3. Tampered raw binary slice
    raw_dir = tmp_path / "flip_raw"
    shutil.copytree(base, raw_dir)
    rb = bytearray((raw_dir / "hand_raw.v0.bin").read_bytes())
    rb[20] ^= 0xFF
    (raw_dir / "hand_raw.v0.bin").write_bytes(bytes(rb))
    res_raw = verify_hand_timeline_dir(raw_dir)
    assert not res_raw.ok and any("raw slice SHA-256" in e for e in res_raw.errors)

    # 4. Raw offset gap + overlap after chain re-link
    gap_dir = tmp_path / "gap"
    shutil.copytree(base, gap_dir)

    def _add_gap(row: dict) -> None:
        if row.get("type") == "raw_bind" and row.get("chunk_seq") == 1:
            row["offset"] += RAW_RECORD_BYTES
        elif row.get("type") == "chunk" and row.get("seq") == 1:
            row["raw_offset"] += RAW_RECORD_BYTES

    _rehash_timeline(gap_dir, _add_gap)
    res_gap = verify_hand_timeline_dir(gap_dir)
    assert not res_gap.ok and any("leaves a gap" in e for e in res_gap.errors)

    # 5. Backwards t_us inside raw binary file
    back_dir = tmp_path / "back_t_us"
    shutil.copytree(base, back_dir)
    rb2 = bytearray((back_dir / "hand_raw.v0.bin").read_bytes())
    struct.pack_into("<Q", rb2, RAW_RECORD_BYTES + 8, 5)
    (back_dir / "hand_raw.v0.bin").write_bytes(bytes(rb2))
    new_sha0 = _sha256_hex(bytes(rb2[: 2 * RAW_RECORD_BYTES]))

    def _rebind0(row: dict) -> None:
        if row.get("type") == "raw_bind" and row.get("chunk_seq") == 0:
            row["sha256"] = new_sha0
        elif row.get("type") == "chunk" and row.get("seq") == 0:
            row["raw_sha256"] = new_sha0

    _rehash_timeline(back_dir, _rebind0)
    res_back = verify_hand_timeline_dir(back_dir)
    assert not res_back.ok and any("decreases below previous" in e for e in res_back.errors)

    # 6. DualSense Edge-bits leak caught even when chain is re-linked
    ds_dir = tmp_path / "ds_leak"
    shutil.copytree(FIX_DIR / "189_dualsense_fallback", ds_dir)

    def _leak_edge(row: dict) -> None:
        if row.get("type") == "in":
            row["buttons"] = int(row.get("buttons") or 0) | EDGE_BITS_MASK

    _rehash_timeline(ds_dir, _leak_edge)
    res_ds = verify_hand_timeline_dir(ds_dir)
    assert not res_ds.ok and any("Edge-only bits" in e for e in res_ds.errors)


def test_resolve_confines_hand_timeline_dir_and_allows_explicit_root(tmp_path: Path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside"
    shutil.copytree(FIX_DIR / "189_edge", outside)

    # Path traversal rejected
    cp_trav = bundle / "checkpoints.jsonl"
    cp_trav.write_text(
        json.dumps({"seq": 0, "t_ms": 0, "qorcap": {"trigger": "start", "hand_timeline_dir": "../outside"}}) + "\n",
        encoding="utf-8",
    )
    res_trav = verify_hand_timeline_dir(bundle)
    assert not res_trav.ok and any("refusing" in e for e in res_trav.errors)

    # Absolute pointer rejected without allowed_roots, accepted when explicitly allowed
    cp_trav.write_text(
        json.dumps({"seq": 0, "t_ms": 0, "qorcap": {"trigger": "start", "hand_timeline_dir": str(outside)}}) + "\n",
        encoding="utf-8",
    )
    res_denied = verify_hand_timeline_dir(bundle)
    assert not res_denied.ok and any("refusing" in e for e in res_denied.errors)

    res_ok = verify_hand_timeline_dir(bundle, allowed_roots=[tmp_path])
    assert res_ok.ok, res_ok.errors


def test_build_from_hand_timeline_sanitizes_and_fails_closed(tmp_path: Path):
    chk = verify_hand_timeline_dir(FIX_DIR / "189_edge")
    norm = normalize_in_records(chk.records)
    assert len(norm) >= 2
    assert all("pad_id" not in r and "raw_sha256" not in r for r in norm)

    receipt = receipt_for([{**r, "seg": i // 2} for i, r in enumerate(norm)])
    clips = [{"id": "c0", "start_mono_ms": 0, "duration_s": 5.0, "actions_ms": [200, 700, 1200]}]
    doc = S.build_from_hand_timeline(receipt, FIX_DIR / "189_edge", clips)
    assert doc["schema"] == S.SCHEMA
    assert doc["source_format"] == "qorcap-hand-timeline/v0"
    assert doc["affects_verdict"] == "lower_only"
    assert doc["max_cap"] == "PARTIAL"
    assert doc["inputs_verified"] is True
    assert doc["inputs"]["chunks"] == 2
    assert doc["inputs"]["head"] == chk.head
    assert S.sanitize(doc) == doc
    raw_dump = json.dumps(doc)
    for secret in ("pad_id", "hand_dir", "raw_sha256", "HID\\", "VID_"):
        assert secret not in raw_dump

    # Tampered timeline fails closed with inputs_verified=False, cap=None, clips=[]
    tampered = tmp_path / "tampered"
    shutil.copytree(FIX_DIR / "189_edge", tampered)
    txt = (tampered / "hand_timeline.v0.jsonl").read_text(encoding="utf-8")
    assert '"r2":0' in txt
    (tampered / "hand_timeline.v0.jsonl").write_bytes(txt.replace('"r2":0', '"r2":1', 1).encode("utf-8"))
    bad_doc = S.build_from_hand_timeline(receipt, tampered, clips)
    assert bad_doc["inputs_verified"] is False
    assert bad_doc["clips"] == []
    assert bad_doc["cap"] is None

    # Off session stays clean with inputs_verified=False, no cap, no clips
    off_doc = S.build_from_hand_timeline(receipt, FIX_DIR / "flag_off_zero_hand_keys", clips)
    assert off_doc["hand_timeline_off"] is True
    assert off_doc["inputs_verified"] is False
    assert off_doc["cap"] is None
