"""Frame-bind sidecar contract. Status is advisory and is not a receipt verdict.

The four statuses are FRAME_BIND_STATUSES. Comparisons use that tuple and
FRAME_BIND_VERIFIED. The spec line `frame-bind status set:` is the other copy.
"""
from __future__ import annotations

import enum
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from qor_receipt.checkpoints import GENESIS, body_of
from qor_receipt.jcs import canonicalize, sha256_hex
from qor_receipt.receipt import verify_receipt
from qor_receipt.review.frame_bind import (
    CHECKPOINTS_FILE,
    FRAME_BIND_STATUSES,
    FRAME_BIND_VERIFIED,
    FRAMES_FILE,
    MAX_BUNDLE_BYTES,
    MAX_FRAME_LINE_BYTES,
    MAX_FRAMES_BYTES,
    MAX_LINE_BYTES,
    MAX_MEMBER_BYTES,
    PATH_PATTERN,
    READERS_FILE,
    READERS_SCHEMA,
    SCHEMA,
    FrameBindResult,
    _frame_indexes_covered,
    _read_limited,
    frame_bind_bundle_bytes,
    frame_bind_result_ok,
    frame_bind_status,
    is_frame_bind_verified,
    verify_frame_bind,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "schema" / "contracts"
VERIFIED, MISSING, STRIPPED, BROKEN = FRAME_BIND_STATUSES
assert VERIFIED == FRAME_BIND_VERIFIED

_AU = "ab" * 32


def _chain(lines: list[bytes]) -> list[str]:
    """Independent copy of the spec chain, so the verifier is not graded on its own helper."""
    head = GENESIS
    out = []
    for line in lines:
        head = sha256_hex({"prev": head, "sha256": hashlib.sha256(line).hexdigest()})
        out.append(head)
    return out


def _header() -> bytes:
    return canonicalize({
        "clock": "qorcap-mono-ms",
        "hash_of": "encoded-video-sample",
        "schema": SCHEMA,
        "time_base": {"den": 90000, "num": 1},
        "type": "header",
    })


def _frame(seg: int, index: int, pts: int, mono: int, au: str = _AU) -> bytes:
    return canonicalize({
        "au_sha256": au,
        "frame": index,
        "mono_ms": mono,
        "pts": pts,
        "seg": seg,
        "type": "frame",
    })


def _gap(seg: int, from_frame: int, samples: int, bound: int, reason: str) -> bytes:
    return canonicalize({
        "bound": bound,
        "from_frame": from_frame,
        "reason": reason,
        "samples": samples,
        "seg": seg,
        "type": "align_gap",
    })


def _readers_obj(**extra: object) -> dict:
    obj = {
        "qorcap_version": "0.3.32",
        "readers": [{
            "files": [
                {"kind": "code", "path": "sidecar/qorcap_standard.py", "sha256": "cd" * 32},
                {"kind": "pack", "path": "sidecar/packs/madden27_standard_score_110028.npz", "sha256": "ef" * 32},
            ],
            "id": "standard",
            "version": "0.3.32",
        }],
        "runtimes": [
            {"id": "ffmpeg", "sha256": "11" * 32, "version": "n7.0.1"},
            {"id": "cv2", "sha256": "22" * 32, "version": "4.10.0"},
        ],
        "schema": READERS_SCHEMA,
    }
    obj.update(extra)
    return obj


def _readers_bytes(obj: dict | None = None) -> bytes:
    return canonicalize(_readers_obj() if obj is None else obj) + b"\n"


def _jsonl(rows: list[dict]) -> bytes:
    return b"\n".join(canonicalize(row) for row in rows) + b"\n"


def _cp(seq: int, n: int, head: str, readers: dict | None = None, *, bind: bool = True) -> dict:
    qorcap: dict = {"trigger": "segment"}
    if bind:
        qorcap["frame_bind"] = {"head": head, "n": n, "schema": SCHEMA}
    if readers is not None:
        qorcap["readers"] = readers
    return {
        "frames_hash": "aa" * 32,
        "hash": "bb" * 32,
        "input_hash": "cc" * 32,
        "prev": GENESIS,
        "qorcap": qorcap,
        "seq": seq,
        "t_ms": 1000 + seq,
    }


def _seal(cps: list[dict]) -> None:
    """Fill frames_hash, prev, and hash from the checkpoint objects. Replaces fixture placeholders."""
    prev = GENESIS
    for cp in cps:
        q = cp.get("qorcap")
        if isinstance(q, dict):
            cp["frames_hash"] = sha256_hex(q)
        cp["prev"] = prev
        cp["hash"] = sha256_hex(body_of(cp))
        prev = cp["hash"]


def _bundle(lines: list[bytes], checkpoints: list[dict], readers: bytes | None = None, *,
            frames: bool = True, seal: bool = True) -> dict[str, bytes]:
    rows = [dict(cp) for cp in checkpoints]
    if seal:
        _seal(rows)
    files = {CHECKPOINTS_FILE: _jsonl(rows)}
    if frames:
        files[FRAMES_FILE] = b"\n".join(lines) + b"\n"
    if readers is not None:
        files[READERS_FILE] = readers
    return files


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buf.getvalue()


def _valid_parts(pts: tuple[int, ...] = (0, 3003), *, gap: bool = True):
    lines = [_header(), _frame(0, 0, pts[0], 1000), _frame(0, 1, pts[1], 1105)]
    if gap:
        lines.append(_gap(1, 2, 30, 0, "clock_lost"))
    readers = _readers_bytes()
    digest = hashlib.sha256(readers).hexdigest()
    heads = _chain(lines)
    commitment = {"schema": READERS_SCHEMA, "sha256": digest}
    cps = [
        _cp(0, 2, heads[1], commitment),
        _cp(1, len(lines), heads[-1]),
    ]
    return lines, cps, readers


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _registry():
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    pairs = []
    for path in CONTRACTS.glob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in doc:
            pairs.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
    return Registry().with_resources(pairs)


def test_status_words_are_written_once_and_match_the_spec():
    """Changing the set without editing the spec (and the JS mirror) fails here."""
    src = (ROOT / "qor_receipt" / "review" / "frame_bind.py").read_text(encoding="utf-8")
    for word in FRAME_BIND_STATUSES:
        assert src.count(f'"{word}"') == 1
    spec = (ROOT / "spec" / "match-receipt-v0.md").read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", spec)
    match = re.search(r"frame-bind status set: ([a-z, ]+)", flat)
    assert match, "spec is missing the frame-bind status set line"
    assert tuple(part.strip() for part in match.group(1).split(",")) == FRAME_BIND_STATUSES
    assert "missing does not prove binding was never enabled" in flat
    assert "indistinguishable from never-enabled" in flat
    assert "719 ms is an observed maximum across 3 clips in one session" in flat
    assert "It is not a limit" in flat
    assert "self-report does not count" in flat
    assert "no fixed-fps skew formula" in flat
    assert "inside an align_gap" in flat
    assert "not a trust token" in flat
    assert "must never trust a caller-supplied result" in flat
    assert "recompute the bundle digest from bytes" in flat
    assert "type(value) is str" in flat
    assert "verify_frame_bind" in spec
    assert "SHA-256(JCS(qorcap))" in spec
    assert "A frames.jsonl with no frame row is broken" in flat
    assert "about 20 MB per hour" in flat
    assert "96 MiB" in spec
    assert "at most 4096 bytes" in flat
    assert "contains no `[`" in spec
    assert "at most 24 `:`" in spec
    assert "at most 4 `{`" in spec
    assert "roughly 3.5–4 h" in flat
    assert "longer capture comes back broken" in flat
    assert "The schema check alone is not enough" in spec
    compat = json.loads((CONTRACTS / "compat.json").read_text(encoding="utf-8"))
    assert tuple(compat["frame_bind"]["statuses"]) == FRAME_BIND_STATUSES
    assert compat["frame_bind"]["verified"] == FRAME_BIND_VERIFIED
    js = (ROOT / "verify" / "frame-bind-status.js").read_text(encoding="utf-8")
    found = re.search(r"FRAME_BIND_STATUSES = Object\.freeze\(\[(.*?)\]\)", js, re.S)
    assert found
    assert tuple(re.findall(r'"([a-z]+)"', found.group(1))) == FRAME_BIND_STATUSES
    assert "FRAME_BIND_VERIFIED = FRAME_BIND_STATUSES[0]" in js
    assert "function isFrameBindVerified" in js
    assert 'typeof value === "string"' in js


def test_js_mirror_exports_the_same_set():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = (
        "import { FRAME_BIND_STATUSES, FRAME_BIND_VERIFIED, isFrameBindVerified }"
        " from './verify/frame-bind-status.js';"
        "if (FRAME_BIND_VERIFIED !== FRAME_BIND_STATUSES[0]) process.exit(2);"
        "if (!isFrameBindVerified(FRAME_BIND_VERIFIED)) process.exit(3);"
        "if (isFrameBindVerified(new String(FRAME_BIND_VERIFIED))) process.exit(4);"
        "if (FRAME_BIND_STATUSES.slice(1).some(isFrameBindVerified)) process.exit(5);"
        "if (!Object.isFrozen(FRAME_BIND_STATUSES)) process.exit(6);"
        "let rejected = false;"
        "try { FRAME_BIND_STATUSES.push('extra'); } catch (e) { rejected = true; }"
        "if (!rejected || FRAME_BIND_STATUSES.length !== 4) process.exit(7);"
        "process.stdout.write(JSON.stringify(FRAME_BIND_STATUSES));"
    )
    proc = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert tuple(json.loads(proc.stdout)) == FRAME_BIND_STATUSES


def test_verified_is_a_plain_str_and_lookalikes_are_not():
    """type(x) is str runs before equality, so a spoofed __eq__ never counts."""
    assert all(type(item) is str for item in FRAME_BIND_STATUSES)
    assert type(FRAME_BIND_VERIFIED) is str
    assert not isinstance(FRAME_BIND_VERIFIED, enum.Enum)
    assert is_frame_bind_verified(FRAME_BIND_VERIFIED)
    fresh = FRAME_BIND_VERIFIED.encode("ascii").decode("ascii")
    assert type(fresh) is str and fresh == FRAME_BIND_VERIFIED and fresh is not FRAME_BIND_VERIFIED
    assert is_frame_bind_verified(fresh)
    for other in FRAME_BIND_STATUSES[1:]:
        assert not is_frame_bind_verified(other)

    class Spoof(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    spoof = Spoof(FRAME_BIND_VERIFIED)
    assert spoof == FRAME_BIND_VERIFIED
    assert not (spoof != FRAME_BIND_VERIFIED)
    assert type(spoof) is not str
    assert not is_frame_bind_verified(spoof)

    class StrStatus(str, enum.Enum):
        VERIFIED = FRAME_BIND_STATUSES[0]

    assert StrStatus.VERIFIED == FRAME_BIND_VERIFIED
    assert type(StrStatus.VERIFIED) is not str
    assert not is_frame_bind_verified(StrStatus.VERIFIED)

    class PlainStatus(enum.Enum):
        VERIFIED = FRAME_BIND_STATUSES[0]

    assert type(PlainStatus.VERIFIED) is not str
    assert not is_frame_bind_verified(PlainStatus.VERIFIED)

    class HandBuilt:
        def __eq__(self, other: object) -> bool:
            return True

    assert not is_frame_bind_verified(HandBuilt())
    built = FrameBindResult(status=FRAME_BIND_VERIFIED, bundle_digest="ab", coverage=())
    assert is_frame_bind_verified(built.status)
    assert not is_frame_bind_verified(built)


def test_forged_result_is_rejected_when_the_bundle_bytes_are_checked(tmp_path: Path):
    """A hand-built FrameBindResult is not a claim. The helper recomputes from bytes."""
    lines, cps, readers = _valid_parts()
    files = _bundle(lines, cps, readers)
    blob = frame_bind_bundle_bytes(files)
    fresh = verify_frame_bind(blob)
    assert type(fresh) is FrameBindResult
    assert fresh.status == FRAME_BIND_VERIFIED
    assert type(fresh.status) is str
    assert is_frame_bind_verified(fresh.status)
    assert fresh.bundle_digest == hashlib.sha256(blob).hexdigest()
    assert frame_bind_result_ok(blob, fresh)

    for name, payload in files.items():
        (tmp_path / name).write_bytes(payload)
    from_dir = verify_frame_bind(tmp_path)
    assert from_dir == fresh
    assert from_dir.bundle_digest == hashlib.sha256(frame_bind_bundle_bytes(files)).hexdigest()
    assert frame_bind_result_ok(tmp_path, fresh)

    gaps = [seg for seg in fresh.coverage if seg.align_gap]
    assert len(gaps) == 1
    gap = gaps[0]
    assert (gap.seg, gap.from_frame, gap.samples, gap.bound, gap.reason) == (1, 2, 30, 0, "clock_lost")
    assert type(gap.reason) is str
    bound = [seg for seg in fresh.coverage if not seg.align_gap]
    assert len(bound) == 1
    assert (bound[0].seg, bound[0].from_frame, bound[0].samples, bound[0].bound, bound[0].reason) == (0, 0, 2, 2, None)

    forged = FrameBindResult(status=FRAME_BIND_VERIFIED, bundle_digest="f" * 64, coverage=())
    assert forged.status == FRAME_BIND_VERIFIED
    assert is_frame_bind_verified(forged.status)
    assert not frame_bind_result_ok(blob, forged)
    assert not frame_bind_result_ok(tmp_path, forged)

    copied_digest = FrameBindResult(status=FRAME_BIND_VERIFIED, bundle_digest=fresh.bundle_digest, coverage=())
    assert not frame_bind_result_ok(blob, copied_digest)

    class FakeResult(FrameBindResult):
        pass

    lookalike = FakeResult(status=fresh.status, bundle_digest=fresh.bundle_digest, coverage=fresh.coverage)
    assert lookalike.status == fresh.status
    assert lookalike.bundle_digest == fresh.bundle_digest
    assert lookalike.coverage == fresh.coverage
    assert type(lookalike) is not FrameBindResult
    assert not frame_bind_result_ok(blob, lookalike)

    tampered = list(lines)
    tampered[1] = _frame(0, 0, 0, 1001)
    broken_blob = frame_bind_bundle_bytes(_bundle(tampered, cps, readers))
    broken = verify_frame_bind(broken_blob)
    assert broken.status == BROKEN
    assert broken.coverage == fresh.coverage
    assert not frame_bind_result_ok(broken_blob, fresh)
    assert not frame_bind_result_ok(broken_blob, FrameBindResult(
        status=FRAME_BIND_VERIFIED, bundle_digest=broken.bundle_digest, coverage=broken.coverage,
    ))

    garbage = b"not-a-bundle"
    bad = verify_frame_bind(garbage)
    assert bad.status == BROKEN
    assert bad.bundle_digest == hashlib.sha256(garbage).hexdigest()
    assert bad.coverage == ()
    assert not frame_bind_result_ok(garbage, forged)

    empty = tmp_path / "empty"
    empty.mkdir()
    missing = verify_frame_bind(empty)
    assert missing.status == MISSING
    assert missing.coverage == ()
    assert not frame_bind_result_ok(empty, forged)


def test_valid_frame_bind_is_verified_and_schemas_accept_it():
    js = pytest.importorskip("jsonschema")
    lines, cps, readers = _valid_parts()
    files = _bundle(lines, cps, readers)
    got = frame_bind_status(files)
    assert got == FRAME_BIND_VERIFIED
    assert got in FRAME_BIND_STATUSES
    row_schema = _schema("qorcap-frame-bind.v0.json")
    for line in lines:
        js.validate(json.loads(line), row_schema)
    js.validate(json.loads(readers), _schema("qorcap-readers.v0.json"))
    checkpoint = _schema("qorcap-checkpoint.v0.json")
    validator = js.Draft202012Validator(checkpoint, registry=_registry())
    sealed = [json.loads(line) for line in files[CHECKPOINTS_FILE].splitlines()]
    for row in sealed:
        validator.validate(row)
        assert row["frames_hash"] == sha256_hex(row["qorcap"])
        assert row["hash"] == sha256_hex(body_of(row))
    assert sealed[0]["prev"] == GENESIS
    assert sealed[1]["prev"] == sealed[0]["hash"]
    header = json.loads(lines[0])
    assert "fps" not in header and "fps_num" not in header and "fps_den" not in header
    assert header["time_base"] == {"num": 1, "den": 90000}


def test_partial_coverage_gap_stays_verified():
    """One bound frame inside a wider segment span is coverage, and the bundle can still verify."""
    lines = [
        _header(),
        _frame(0, 0, 0, 1000),
        _gap(1, 1, 4, 1, "sample_pes_mismatch"),
        _frame(1, 1, 6006, 1400),
    ]
    readers = _readers_bytes()
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(readers).hexdigest()})]
    assert frame_bind_status(_bundle(lines, cps, readers)) == VERIFIED


def test_unbound_readers_with_a_valid_frame_chain_is_verified():
    lines, cps, _readers = _valid_parts()
    cps[0]["qorcap"]["readers"] = {"schema": READERS_SCHEMA, "state": "unbound"}
    assert frame_bind_status(_bundle(lines, cps)) == VERIFIED


def test_missing_when_the_bundle_has_no_binding_material():
    plain = _cp(0, 0, GENESIS, bind=False)
    assert frame_bind_status({}) == MISSING
    assert frame_bind_status({CHECKPOINTS_FILE: b""}) == MISSING
    # A chain that recomputes and carries no binding is missing. Placeholders are broken.
    assert frame_bind_status(_bundle([], [plain], frames=False)) == MISSING
    assert frame_bind_status({CHECKPOINTS_FILE: _jsonl([plain])}) == BROKEN
    assert MISSING != FRAME_BIND_VERIFIED


def test_stripped_commitments_on_some_checkpoints_only():
    lines, cps, readers = _valid_parts()
    cps[1]["qorcap"].pop("frame_bind")
    assert frame_bind_status(_bundle(lines, cps, readers)) == STRIPPED
    # Same leftover with the file removed still shows the partial commitments.
    assert frame_bind_status(_bundle(lines, cps, readers, frames=False)) == STRIPPED


def test_stripped_frame_bind_without_readers_on_seq0():
    lines, cps, readers = _valid_parts()
    cps[0]["qorcap"].pop("readers")
    assert frame_bind_status(_bundle(lines, cps, readers)) == STRIPPED


def test_stripped_readers_on_seq0_without_frame_bind():
    _lines, cps, readers = _valid_parts()
    for row in cps:
        row["qorcap"].pop("frame_bind")
    assert frame_bind_status(_bundle([], cps, readers, frames=False)) == STRIPPED


def test_stripped_files_present_without_commitments():
    lines, _cps, readers = _valid_parts()
    plain = _cp(0, 0, GENESIS, bind=False)
    base = _bundle([], [plain], frames=False)
    frames = b"\n".join(lines) + b"\n"
    assert frame_bind_status({**base, FRAMES_FILE: frames}) == STRIPPED
    assert frame_bind_status({**base, READERS_FILE: readers}) == STRIPPED
    assert frame_bind_status({FRAMES_FILE: frames}) == STRIPPED
    assert frame_bind_status({READERS_FILE: readers}) == STRIPPED
    # The same sidecar on a chain that does not recompute is broken, not stripped.
    assert frame_bind_status({CHECKPOINTS_FILE: _jsonl([plain]), FRAMES_FILE: frames}) == BROKEN


def test_stripped_commitments_whose_files_are_absent():
    lines, cps, readers = _valid_parts()
    assert frame_bind_status(_bundle(lines, cps, readers, frames=False)) == STRIPPED
    files = _bundle(lines, cps)
    files.pop(READERS_FILE, None)
    assert READERS_FILE not in files
    assert frame_bind_status(files) == STRIPPED


def test_stripped_readers_file_beside_unbound():
    lines, cps, readers = _valid_parts()
    cps[0]["qorcap"]["readers"] = {"schema": READERS_SCHEMA, "state": "unbound"}
    assert frame_bind_status(_bundle(lines, cps, readers)) == STRIPPED


def test_broken_when_a_committed_line_is_tampered():
    lines, cps, readers = _valid_parts()
    tampered = list(lines)
    tampered[1] = _frame(0, 0, 0, 1001)
    assert frame_bind_status(_bundle(tampered, cps, readers)) == BROKEN


def test_broken_when_pts_does_not_increase():
    for pts in ((100, 50), (100, 100)):
        lines, cps, readers = _valid_parts(pts, gap=False)
        assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN
    lines = [_header(), _frame(0, 0, 100, 1000), _gap(1, 1, 8, 0, "pts_regression"), _frame(2, 9, 40, 2000)]
    readers = _readers_bytes()
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(readers).hexdigest()})]
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN


def test_broken_when_gap_coverage_does_not_match_the_rows():
    lines = [_header(), _frame(0, 0, 0, 1000), _gap(1, 1, 4, 2, "clock_lost")]
    readers = _readers_bytes()
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(readers).hexdigest()})]
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN


def test_readers_extra_field_fails_schema_and_is_broken():
    js = pytest.importorskip("jsonschema")
    extra = json.loads((ROOT / "tests" / "fixtures" / "frame_bind" / "readers-extra.json").read_text(encoding="utf-8"))
    schema = _schema("qorcap-readers.v0.json")
    with pytest.raises(js.ValidationError):
        js.validate(extra, schema)
    raw = canonicalize(extra) + b"\n"
    lines = [_header(), _frame(0, 0, 0, 1000), _frame(0, 1, 1500, 1100)]
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(raw).hexdigest()})]
    assert frame_bind_status(_bundle(lines, cps, raw)) == BROKEN
    bad_path = _readers_obj()
    bad_path["readers"][0]["files"][0]["path"] = "../sidecar/qorcap_standard.py"
    with pytest.raises(js.ValidationError):
        js.validate(bad_path, schema)
    no_runtime = _readers_obj()
    no_runtime["runtimes"] = []
    with pytest.raises(js.ValidationError):
        js.validate(no_runtime, schema)


def test_schema_rejects_fps_fields_and_accepts_an_old_checkpoint():
    js = pytest.importorskip("jsonschema")
    row_schema = _schema("qorcap-frame-bind.v0.json")
    header = json.loads(_header())
    header["fps_num"] = 30
    header["fps_den"] = 1
    with pytest.raises(js.ValidationError):
        js.validate(header, row_schema)
    checkpoint = _schema("qorcap-checkpoint.v0.json")
    validator = js.Draft202012Validator(checkpoint, registry=_registry())
    old = {
        "seq": 0, "t_ms": 5, "frames_hash": "ab" * 32, "input_hash": "cd" * 32,
        "prev": GENESIS, "hash": "ef" * 32,
        "qorcap": {"trigger": "start", "chain_head": "11" * 32, "mono_ms": 5},
    }
    validator.validate(old)
    both = {"schema": READERS_SCHEMA, "sha256": "aa" * 32, "state": "unbound"}
    readers_schema = _schema("qorcap-readers.v0.json")
    commitment = {"$ref": "#/$defs/commitment", "$defs": readers_schema["$defs"]}
    with pytest.raises(js.ValidationError):
        js.validate(both, commitment)
    path = _schema("qorcap-readers.v0.json")["$defs"]["file"]["properties"]["path"]["pattern"]
    assert path == PATH_PATTERN


def test_unrelated_checkpoint_hashes_are_broken():
    """Placeholder frames_hash, hash, and prev do not verify. The sealed chain does."""
    lines, cps, readers = _valid_parts()
    assert frame_bind_status(_bundle(lines, cps, readers, seal=False)) == BROKEN
    assert frame_bind_status(_bundle(lines, cps, readers)) == VERIFIED
    forged = _bundle(lines, cps, readers)
    obj = json.loads(forged[CHECKPOINTS_FILE].splitlines()[0])
    obj["frames_hash"] = "00" * 32
    forged[CHECKPOINTS_FILE] = canonicalize(obj) + b"\n" + b"\n".join(forged[CHECKPOINTS_FILE].splitlines()[1:]) + b"\n"
    assert frame_bind_status(forged) == BROKEN


def test_frame_hole_without_align_gap_is_broken():
    lines = [_header(), _frame(0, 0, 0, 1000), _frame(0, 5, 3003, 1200)]
    readers = _readers_bytes()
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(readers).hexdigest()})]
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN
    covered = [
        _header(),
        _frame(0, 0, 0, 1000),
        _gap(0, 0, 6, 2, "clock_lost"),
        _frame(0, 5, 3003, 1200),
    ]
    readers = _readers_bytes()
    heads = _chain(covered)
    cps = [_cp(0, len(covered), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(readers).hexdigest()})]
    assert frame_bind_status(_bundle(covered, cps, readers)) == VERIFIED


def test_header_only_bundle_is_broken():
    """Commitments and the file are present, and no frame is bound, so this is broken."""
    lines = [_header()]
    readers = _readers_bytes()
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(readers).hexdigest()})]
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN


def test_interleaved_segments_are_broken():
    lines = [_header(), _frame(0, 0, 0, 1000), _frame(1, 1, 10, 1100), _frame(0, 2, 20, 1200)]
    readers = _readers_bytes()
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(readers).hexdigest()})]
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN


def test_duplicate_checkpoint_key_is_broken():
    lines, cps, readers = _valid_parts()
    files = _bundle(lines, cps, readers)
    first, rest = files[CHECKPOINTS_FILE].split(b"\n", 1)
    files[CHECKPOINTS_FILE] = b'{"qorcap":{"trigger":"segment"},' + first[1:] + b"\n" + rest
    assert frame_bind_status(files) == BROKEN
    nan = first.replace(b'"t_ms":1000', b'"t_ms":NaN', 1)
    files[CHECKPOINTS_FILE] = nan + b"\n" + rest
    assert frame_bind_status(files) == BROKEN


def test_key_like_reader_paths_are_rejected():
    js = pytest.importorskip("jsonschema")
    schema = _schema("qorcap-readers.v0.json")
    rejected = (
        "secrets/referee_key.json", "secret/reader.py", "sidecar/referee_key.json", "a/foo_KEY.py",
        "id_ed25519", "id_rsa", "ID_ED25519", "a/id_rsa.pub", "id_ecdsa_sk", "x.jwk", "a_key.tar.gz",
        "api_key/x.py", "id_rsa/x.py", "id_ed448", "id_ed448_sk", "a/id_ed448.pub", "id_ed448/x.py",
        "wallet.ppk", "store.jks", "DIR.PPK",
        "id_rsa.", "a_key.", "x/id_ed448./y",
        # One path per secret rule, with a trailing dot and a trailing space.
        "server.key.", "server.key ", "certs/tls.pem.", "cert.p12.", "cert.pfx.",
        "cert.jwk.", "cert.ppk.", "cert.jks.", "cert.KEY..", ".p12.", ".jks.", ".ppk.", ".jwk.",
        "server.pem ", "cert.P12 ",
        "keys./x.py", "keys /x.py", "secrets./x.py", "secret./x", "secrets /x.py", "a/keys.", "a/secret.",
        "a_key ", "api_key./x.py", "id_rsa ", "id_ed448 ", "x/id_ed25519. ",
        "file.", "secretary.", "foo..", "readme. ", "a./b.py",
    )
    allowed = (
        "secretary", "foo_keyish.py", "key.json", "monkey_keyboard.py",
        "sidecar/qorcap_standard.py", "foo.bar",
    )
    for path in rejected:
        obj = _readers_obj()
        obj["readers"][0]["files"][0]["path"] = path
        with pytest.raises(js.ValidationError):
            js.validate(obj, schema)
        lines, _cps, _readers = _valid_parts()
        raw = canonicalize(obj) + b"\n"
        heads = _chain(lines)
        cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(raw).hexdigest()})]
        assert frame_bind_status(_bundle(lines, cps, raw)) == BROKEN
    for path in allowed:
        obj = _readers_obj()
        obj["readers"][0]["files"][0]["path"] = path
        js.validate(obj, schema)
        lines, _cps, _readers = _valid_parts()
        raw = canonicalize(obj) + b"\n"
        heads = _chain(lines)
        cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(raw).hexdigest()})]
        assert frame_bind_status(_bundle(lines, cps, raw)) == VERIFIED


def test_reader_path_pattern_matches_the_python_check():
    """The schema pattern and the Python segment check accept the same paths.

    Every secret rule is sampled with a trailing dot and a trailing space.
    A segment that ends in either is rejected on both sides. `foo.bar` still passes.
    """
    from qor_receipt.review.frame_bind import _PATH_RE, _reader_file_ok

    digest = "ab" * 32
    names = (
        "reader", "secretary", "foo_keyish", "key", "monkey_keyboard", "a_key", "api_key",
        "id_rsa", "id_ed448", "id_ed25519", "ID_ED25519", "id_ecdsa_sk",
        "server.key", "tls.pem", "cert.p12", "cert.pfx", "cert.jwk", "cert.ppk", "cert.jks",
        "keys", "secret", "secrets",
    )
    tails = ("", ".", "..", ". ", " ", " .", ".py", ".py.", ".py ", ".pub", ".tar.gz", "/", "/y", "./y", ".jks", ".ppk")
    samples = [
        "id_rsa.", "a_key.", "x/id_ed448./y", "id_ed25519.", "A_KEY.", "file.", "secretary.",
        "server.key.", "server.key ", "certs/tls.pem.", "cert.p12.", "cert.pfx.",
        "cert.jwk.", "cert.ppk.", "cert.jks.", "cert.KEY..", ".p12.", ".jks.", ".ppk.", ".jwk.",
        "keys./x.py", "keys /x.py", "secrets./x.py", "secret./x", "secrets /x.py",
        "a_key ", "api_key./x.py", "id_rsa ", "id_ed448 ", "foo.bar", "foo..", "readme. ",
    ]
    for name in names:
        for tail in tails:
            samples.append(name + tail)
            samples.append("x/" + name + tail)
            samples.append(name + "./y.py")
    rng = random.Random(0)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./+- "
    for _ in range(300):
        samples.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 48))))
    for path in samples:
        schema_ok = len(path) <= 240 and _PATH_RE.fullmatch(path) is not None
        python_ok = _reader_file_ok({"kind": "code", "path": path, "sha256": digest})
        assert schema_ok == python_ok, path
    rejected = (
        "server.key.", "server.key ", "certs/tls.pem.", "cert.p12.", "cert.jwk.", "cert.ppk.", "cert.jks.",
        "keys./x.py", "keys /x.py", "secret./x", "secrets./x.py", "secrets /x.py",
        "a_key.", "a_key ", "api_key./x.py", "id_rsa.", "id_rsa ", "x/id_ed448./y", "id_ed448 ",
        "file.", "secretary.", "foo..",
    )
    for path in rejected:
        assert _PATH_RE.fullmatch(path) is None, path
        assert not _reader_file_ok({"kind": "code", "path": path, "sha256": digest}), path
    for path in ("foo.bar", "secretary", "key.json", "sidecar/qorcap_standard.py"):
        assert _PATH_RE.fullmatch(path) is not None, path
        assert _reader_file_ok({"kind": "code", "path": path, "sha256": digest}), path


def _readers_status(obj: dict) -> str:
    lines = [_header(), _frame(0, 0, 0, 1000)]
    return frame_bind_status(_sealed(lines, canonicalize(obj) + b"\n"))


def test_duplicate_reader_ids_are_broken():
    js = pytest.importorskip("jsonschema")
    schema = _schema("qorcap-readers.v0.json")
    obj = _readers_obj()
    obj["readers"].append({
        "id": "standard",
        "version": "9.9.9",
        "files": [{"kind": "code", "path": "other/reader.py", "sha256": "ab" * 32}],
    })
    js.validate(obj, schema)
    assert _readers_status(obj) == BROKEN
    copied = _readers_obj()
    copied["readers"].append(json.loads(json.dumps(copied["readers"][0])))
    with pytest.raises(js.ValidationError):
        js.validate(copied, schema)
    assert _readers_status(copied) == BROKEN


def test_duplicate_runtime_ids_are_broken():
    js = pytest.importorskip("jsonschema")
    schema = _schema("qorcap-readers.v0.json")
    obj = _readers_obj()
    obj["runtimes"].append({"id": "ffmpeg", "sha256": "33" * 32, "version": "n9.0"})
    js.validate(obj, schema)
    assert _readers_status(obj) == BROKEN
    copied = _readers_obj()
    copied["runtimes"].append(json.loads(json.dumps(copied["runtimes"][0])))
    with pytest.raises(js.ValidationError):
        js.validate(copied, schema)
    assert _readers_status(copied) == BROKEN


def test_normalized_path_collisions_are_broken():
    """NFC, casefold, and trailing dot/space are one file, inside a reader and across readers."""
    js = pytest.importorskip("jsonschema")
    from qor_receipt.review.frame_bind import _path_identity

    schema = _schema("qorcap-readers.v0.json")
    assert _path_identity("Caf\u00e9/A.py.") == _path_identity("Cafe\u0301/a.py ")
    assert _path_identity("sidecar/x.py") == _path_identity("SIDECAR/X.py")
    assert _path_identity("sidecar/x.py") != _path_identity("sidecar/y.py")

    within = _readers_obj()
    within["readers"][0]["files"].append({
        "kind": "code", "path": "Sidecar/Qorcap_Standard.py", "sha256": "ab" * 32,
    })
    js.validate(within, schema)
    assert _readers_status(within) == BROKEN

    across = _readers_obj()
    across["readers"].append({
        "id": "vision",
        "version": "1",
        "files": [{"kind": "pack", "path": "SIDECAR/qorcap_standard.py", "sha256": "ab" * 32}],
    })
    js.validate(across, schema)
    assert _readers_status(across) == BROKEN

    dotted = _readers_obj()
    dotted["readers"][0]["files"].append({
        "kind": "code", "path": "sidecar/qorcap_standard.py.", "sha256": "ab" * 32,
    })
    with pytest.raises(js.ValidationError):
        js.validate(dotted, schema)
    assert _readers_status(dotted) == BROKEN

    spaced = _readers_obj()
    spaced["readers"][0]["files"].append({
        "kind": "code", "path": "sidecar/qorcap_standard.py ", "sha256": "ab" * 32,
    })
    with pytest.raises(js.ValidationError):
        js.validate(spaced, schema)
    assert _readers_status(spaced) == BROKEN

    composed = _readers_obj()
    composed["readers"][0]["files"] = [
        {"kind": "code", "path": "Caf\u00e9.py", "sha256": "ab" * 32},
        {"kind": "code", "path": "Cafe\u0301.py", "sha256": "cd" * 32},
    ]
    with pytest.raises(js.ValidationError):
        js.validate(composed, schema)
    assert _readers_status(composed) == BROKEN
    assert _readers_status(_readers_obj()) == VERIFIED


def test_same_path_as_code_and_pack_is_broken():
    js = pytest.importorskip("jsonschema")
    schema = _schema("qorcap-readers.v0.json")
    obj = _readers_obj()
    obj["readers"][0]["files"].append({
        "kind": "pack", "path": "sidecar/qorcap_standard.py", "sha256": "ab" * 32,
    })
    js.validate(obj, schema)
    assert _readers_status(obj) == BROKEN


def test_same_path_with_two_sha256_is_broken():
    js = pytest.importorskip("jsonschema")
    schema = _schema("qorcap-readers.v0.json")
    obj = _readers_obj()
    obj["readers"][0]["files"].append({
        "kind": "code", "path": "sidecar/qorcap_standard.py", "sha256": "ab" * 32,
    })
    js.validate(obj, schema)
    assert _readers_status(obj) == BROKEN
    copied = _readers_obj()
    copied["readers"][0]["files"].append(json.loads(json.dumps(copied["readers"][0]["files"][0])))
    with pytest.raises(js.ValidationError):
        js.validate(copied, schema)
    assert _readers_status(copied) == BROKEN


def _sealed(lines: list[bytes], readers: bytes | None = None) -> dict[str, bytes]:
    blob = _readers_bytes() if readers is None else readers
    heads = _chain(lines)
    cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(blob).hexdigest()})]
    return _bundle(lines, cps, blob)


def test_zero_frame_rows_and_zero_width_gaps_are_broken():
    """D4/D5: a header plus only an align_gap binds nothing. D6: samples 0 beside a real segment."""
    readers = _readers_bytes()
    for gap in (_gap(0, 0, 0, 0, "clock_lost"), _gap(0, 0, 100, 0, "unreadable")):
        lines = [_header(), gap]
        assert frame_bind_status(_sealed(lines, readers)) == BROKEN
    beside = [
        _header(),
        _frame(0, 0, 0, 1000),
        _frame(0, 1, 3003, 1100),
        _gap(1, 2, 0, 0, "clock_lost"),
    ]
    assert frame_bind_status(_sealed(beside, readers)) == BROKEN


def test_frame_index_holes_across_segments_are_broken():
    """N4: a jump across segments, or a first frame above 0, needs an align_gap over the missing indexes."""
    hole = [
        _header(),
        _frame(0, 0, 0, 1000),
        _frame(0, 1, 3003, 1100),
        _frame(1, 5, 6006, 1200),
        _frame(1, 6, 9009, 1300),
    ]
    assert frame_bind_status(_sealed(hole)) == BROKEN
    leading = [_header(), _frame(0, 100, 0, 1000), _frame(0, 101, 3003, 1100)]
    assert frame_bind_status(_sealed(leading)) == BROKEN
    covered = [
        _header(),
        _frame(0, 0, 0, 1000),
        _frame(0, 1, 3003, 1100),
        _gap(0, 0, 5, 2, "clock_lost"),
        _frame(1, 5, 6006, 1200),
        _frame(1, 6, 9009, 1300),
    ]
    assert frame_bind_status(_sealed(covered)) == VERIFIED
    # samples 6 reaches frame 5, which is a row of the next segment. That index would be counted twice.
    overlap = [
        _header(),
        _frame(0, 0, 0, 1000),
        _frame(0, 1, 3003, 1100),
        _gap(0, 0, 6, 2, "clock_lost"),
        _frame(1, 5, 6006, 1200),
        _frame(1, 6, 9009, 1300),
    ]
    assert frame_bind_status(_sealed(overlap)) == BROKEN
    leading_gap = [
        _header(),
        _gap(0, 0, 100, 0, "unreadable"),
        _frame(1, 100, 0, 1000),
        _frame(1, 101, 3003, 1100),
    ]
    assert frame_bind_status(_sealed(leading_gap)) == VERIFIED
    contiguous = [
        _header(),
        _frame(0, 0, 0, 1000),
        _frame(0, 1, 3003, 1100),
        _frame(1, 2, 6006, 1200),
        _frame(1, 3, 9009, 1300),
    ]
    assert frame_bind_status(_sealed(contiguous)) == VERIFIED


def test_chain_tamper_is_broken_before_stripped_or_missing():
    """N3: a chain that does not recompute is broken. A present frames file that does not match is broken."""
    lines, cps, readers = _valid_parts()
    files = _bundle(lines, cps, readers)
    rows = [json.loads(line) for line in files[CHECKPOINTS_FILE].splitlines()]
    rows[0]["seq"] = 9
    files[CHECKPOINTS_FILE] = _jsonl(rows)
    assert frame_bind_status(files) == BROKEN

    files = _bundle(lines, cps, readers)
    rows = [json.loads(line) for line in files[CHECKPOINTS_FILE].splitlines()]
    for row in rows:
        row["seq"] = row["seq"] + 1
    files[CHECKPOINTS_FILE] = _jsonl(rows)
    assert frame_bind_status(files) == BROKEN

    files = _bundle(lines, cps, readers)
    files[FRAMES_FILE] = files[FRAMES_FILE].replace(_AU.encode(), b"ff" * 32, 1)
    del files[READERS_FILE]
    assert frame_bind_status(files) == BROKEN

    # Deleting frames.jsonl leaves a sealed chain and an absent file: that leftover is stripped.
    files = _bundle(lines, cps, readers, frames=False)
    assert frame_bind_status(files) == STRIPPED

    # Stripping commitments without resealing no longer looks like missing.
    files = _bundle(lines, cps, readers)
    stripped = [json.loads(line) for line in files[CHECKPOINTS_FILE].splitlines()]
    for row in stripped:
        row["qorcap"].pop("frame_bind", None)
        row["qorcap"].pop("readers", None)
    bare = {CHECKPOINTS_FILE: _jsonl(stripped)}
    assert frame_bind_status(bare) == BROKEN
    assert frame_bind_status({CHECKPOINTS_FILE: _jsonl(_seal_copy(stripped))}) == MISSING


def _seal_copy(rows: list[dict]) -> list[dict]:
    copied = [dict(row) for row in rows]
    _seal(copied)
    return copied


def test_readers_on_later_checkpoints_are_broken():
    lines, cps, readers = _valid_parts()
    digest = cps[0]["qorcap"]["readers"]
    cps[1]["qorcap"]["readers"] = {"schema": READERS_SCHEMA, "sha256": "99" * 32}
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN
    cps[1]["qorcap"]["readers"] = dict(digest)
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN


def test_checkpoint_and_readers_caps_and_nesting_fail_closed():
    from qor_receipt.review.frame_bind import MAX_CHECKPOINT_BYTES, MAX_JSON_DEPTH, MAX_READERS_BYTES, _loads_strict

    assert frame_bind_status({CHECKPOINTS_FILE: b"x" * (MAX_CHECKPOINT_BYTES + 1)}) == BROKEN
    assert frame_bind_status({READERS_FILE: b"{" + b"a" * MAX_READERS_BYTES}) == BROKEN
    shallow = '{"a":' + "[" * 3 + "1" + "]" * 3 + "}"
    assert _loads_strict(shallow)["a"] == [[[1]]]
    deep = "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)
    with pytest.raises(ValueError):
        _loads_strict(deep)


def test_oversized_checkpoints_and_newline_runs_stay_bounded():
    """N2: a checkpoints member past 4 MiB is refused before parse, and row count does not split the bytes."""
    script = r"""
import gc, io, json, zipfile
from qor_receipt.review.frame_bind import (
    CHECKPOINTS_FILE, FRAMES_FILE, MAX_CHECKPOINT_BYTES, frame_bind_status, verify_frame_bind,
)

def rss():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise SystemExit("no VmRSS")

chunk = b'{"a":[' + (b"[]," * 20000) + b"]}\n"
buf = io.BytesIO()
written = 0
with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    with zf.open(CHECKPOINTS_FILE, "w") as member:
        while written <= MAX_CHECKPOINT_BYTES * 8:
            member.write(chunk)
            written += len(chunk)
raw = buf.getvalue()
declared = None
with zipfile.ZipFile(io.BytesIO(raw)) as zf:
    declared = zf.getinfo(CHECKPOINTS_FILE).file_size
del buf
gc.collect()
before = rss()
status = verify_frame_bind(raw).status
after = rss()
lines = b"1\n" * 4_000_000
gc.collect()
before_lines = rss()
line_status = frame_bind_status({FRAMES_FILE: lines})
after_lines = rss()
print(json.dumps({
    "status": status,
    "growth": after - before,
    "zip": len(raw),
    "declared": declared,
    "line_status": line_status,
    "line_growth": after_lines - before_lines,
}))
"""
    proc = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
    got = json.loads(proc.stdout)
    assert got["status"] == BROKEN
    assert got["declared"] > 4 * 1024 * 1024
    assert got["growth"] < 16 * 1024
    assert got["line_status"] == BROKEN
    assert got["line_growth"] < 16 * 1024


def test_gap_coverage_stays_linear_at_100k_segments():
    """X1: each segment is a gap and a jump. The old walk re-sorted every span per jump."""
    k = 100_000
    frames = [{"frame": 2 * i + 1, "seg": i} for i in range(k)]
    gaps = {i: {"from_frame": 2 * i, "samples": 2, "seg": i} for i in range(k)}
    started = time.perf_counter()
    assert _frame_indexes_covered(frames, gaps) is True
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed


def test_oversized_frames_lines_stay_bounded():
    """X2: a deflated frames.jsonl of lines over 4 KiB is broken before json.loads."""
    script = r"""
import gc, io, json, zipfile
from qor_receipt.review.frame_bind import (
    CHECKPOINTS_FILE, FRAMES_FILE, MAX_FRAME_LINE_BYTES, verify_frame_bind,
)

def rss():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise SystemExit("no VmRSS")

checkpoint = (
    b'{"frames_hash":"' + b"ab" * 32 + b'","hash":"' + b"cd" * 32
    + b'","input_hash":"' + b"ef" * 32 + b'","prev":"' + b"00" * 32
    + b'","qorcap":{"frame_bind":{"head":"' + b"11" * 32
    + b'","n":1,"schema":"qorcap-frame-bind/v0"}},"seq":0,"t_ms":1}\n'
)
line = b'{"a":[' + (b"[]," * 20000) + b"1]}\n"
if len(line) <= MAX_FRAME_LINE_BYTES:
    raise SystemExit("line is not over the cap")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    zf.writestr(CHECKPOINTS_FILE, checkpoint)
    with zf.open(FRAMES_FILE, "w") as member:
        for _ in range(40):
            member.write(line)
raw = buf.getvalue()
del line, buf
gc.collect()
before = rss()
status = verify_frame_bind(raw).status
after = rss()
print(json.dumps({"status": status, "growth": after - before, "zip": len(raw)}))
"""
    proc = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
    got = json.loads(proc.stdout)
    assert got["status"] == BROKEN
    assert got["zip"] < 200 * 1024
    assert got["growth"] < 16 * 1024


def test_a_verified_bundle_parses_each_sidecar_once(monkeypatch):
    """frames.jsonl and the checkpoint chain are not walked again for coverage."""
    import qor_receipt.review.frame_bind as fb

    loads = {"n": 0}
    chains = {"n": 0}
    real_loads = fb.json.loads
    real_chain = fb._checkpoint_chain_ok

    def counting_loads(text, *args, **kwargs):
        loads["n"] += 1
        return real_loads(text, *args, **kwargs)

    def counting_chain(cps):
        chains["n"] += 1
        return real_chain(cps)

    monkeypatch.setattr(fb.json, "loads", counting_loads)
    monkeypatch.setattr(fb, "_checkpoint_chain_ok", counting_chain)
    lines, cps, readers = _valid_parts()
    blob = frame_bind_bundle_bytes(_bundle(lines, cps, readers))
    loads["n"] = 0
    chains["n"] = 0
    got = verify_frame_bind(blob)
    assert got.status == VERIFIED
    assert loads["n"] == len(lines) + len(cps) + 1
    assert chains["n"] == 1


def test_a_bad_frame_row_stops_the_parse(monkeypatch):
    """A row that is not a frame is rejected when it is parsed, and later lines are not."""
    import qor_receipt.review.frame_bind as fb

    bad = canonicalize({"type": "frame"})
    later = [_frame(0, 0, 0, 1000), _frame(0, 1, 3003, 1100)]
    lines = [_header(), bad, *later]
    seen: list[str] = []
    real_loads = fb.json.loads

    def counting_loads(text, *args, **kwargs):
        seen.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(fb.json, "loads", counting_loads)
    assert frame_bind_status(_sealed(lines)) == BROKEN
    assert bad.decode() in seen
    assert all(row.decode() not in seen for row in later)


def test_frame_lines_over_4kib_or_with_an_array_are_broken():
    lines, cps, readers = _valid_parts()
    files = _bundle(lines, cps, readers)
    files[FRAMES_FILE] = b"{" + b"a" * MAX_FRAME_LINE_BYTES + b"}\n"
    assert frame_bind_status(files) == BROKEN
    wide = dict(files)
    wide[FRAMES_FILE] = b'{"a":[[]]}\n'
    assert len(wide[FRAMES_FILE]) < MAX_FRAME_LINE_BYTES
    assert frame_bind_status(wide) == BROKEN
    assert MAX_FRAMES_BYTES < MAX_MEMBER_BYTES
    assert MAX_FRAME_LINE_BYTES < MAX_LINE_BYTES


def test_zip_names_symlinks_and_size_caps_are_broken(tmp_path: Path):
    lines, cps, readers = _valid_parts()
    files = _bundle(lines, cps, readers)
    members = list(files.items())
    assert verify_frame_bind(_zip(members)).status == VERIFIED
    assert verify_frame_bind(_zip([("../" + name, data) for name, data in members])).status == BROKEN
    assert verify_frame_bind(_zip([("/abs/dir/" + name, data) for name, data in members])).status == BROKEN
    mixed = [
        ("a/" + CHECKPOINTS_FILE, files[CHECKPOINTS_FILE]),
        ("b/c/" + FRAMES_FILE, files[FRAMES_FILE]),
        ("..\\" + READERS_FILE, files[READERS_FILE]),
    ]
    assert verify_frame_bind(_zip(mixed)).status == BROKEN
    claimed = bytearray(_zip(members))
    huge = (1 << 30).to_bytes(4, "little")
    for sig, offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        start = 0
        while True:
            at = claimed.find(sig, start)
            if at < 0:
                break
            claimed[at + offset:at + offset + 4] = huge
            start = at + 4
    assert verify_frame_bind(bytes(claimed)).status == BROKEN

    class Endless:
        def read(self, n: int) -> bytes:
            return b"\0" * n

    assert _read_limited(Endless(), 8) is None

    outside = tmp_path / "outside-readers.json"
    outside.write_bytes(files[READERS_FILE])
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name, data in files.items():
        if name == READERS_FILE:
            (bundle / name).symlink_to(outside)
        else:
            (bundle / name).write_bytes(data)
    assert verify_frame_bind(bundle).status == BROKEN
    linked = tmp_path / "linked"
    linked.symlink_to(bundle, target_is_directory=True)
    assert verify_frame_bind(linked).status == BROKEN

    sparse = tmp_path / "sparse"
    sparse.mkdir()
    for name, data in files.items():
        (sparse / name).write_bytes(data)
    os.truncate(sparse / FRAMES_FILE, MAX_MEMBER_BYTES + 1)
    assert verify_frame_bind(sparse).status == BROKEN
    assert frame_bind_status({CHECKPOINTS_FILE: b"{" + b"a" * (MAX_LINE_BYTES + 1)}) == BROKEN
    assert MAX_BUNDLE_BYTES == MAX_MEMBER_BYTES


def test_unknown_shapes_are_not_verified():
    lines, cps, readers = _valid_parts()
    cps[0]["qorcap"]["frame_bind"]["schema"] = "qorcap-frame-bind/v9"
    assert frame_bind_status(_bundle(lines, cps, readers)) == BROKEN
    assert frame_bind_status({CHECKPOINTS_FILE: b"this is not json\n"}) == BROKEN
    assert frame_bind_status({FRAMES_FILE: "not-bytes"}) == BROKEN  # type: ignore[dict-item]
    cr = _bundle(lines, cps, readers)
    cr[FRAMES_FILE] = cr[FRAMES_FILE].replace(b"\n", b"\r\n")
    assert frame_bind_status(cr) == BROKEN
    for got in (BROKEN, MISSING, STRIPPED):
        assert got != FRAME_BIND_VERIFIED
        assert got in FRAME_BIND_STATUSES


def test_body_of_drops_qorcap_and_the_receipt_schema_has_no_frame_bind_field():
    lines, cps, _readers = _valid_parts()
    assert set(body_of(cps[0])) == {"seq", "t_ms", "frames_hash", "input_hash", "prev"}
    receipt = json.loads((ROOT / "schema" / "receipt.v0.json").read_text(encoding="utf-8"))
    assert receipt["additionalProperties"] is False
    assert "frame_bind" not in receipt["properties"]
    assert "readers" not in receipt["properties"]
    text = (ROOT / "qor_receipt" / "receipt.py").read_text(encoding="utf-8")
    assert "frame_bind_status" not in text
    compat = json.loads((CONTRACTS / "compat.json").read_text(encoding="utf-8"))
    assert FRAMES_FILE in compat["bundle_files"]["meta"]
    assert READERS_FILE in compat["bundle_files"]["meta"]
    assert FRAMES_FILE not in compat["bundle_files"]["required"]
    assert READERS_FILE not in compat["bundle_files"]["required"]


def test_new_contract_files_are_lf_only():
    paths = [
        CONTRACTS / "qorcap-frame-bind.v0.json",
        CONTRACTS / "qorcap-readers.v0.json",
        CONTRACTS / "qorcap-checkpoint.v0.json",
        CONTRACTS / "compat.json",
        ROOT / "qor_receipt" / "review" / "frame_bind.py",
        ROOT / "spec" / "match-receipt-v0.md",
        ROOT / "verify" / "frame-bind-status.js",
    ]
    for path in paths:
        raw = path.read_bytes()
        assert b"\r" not in raw, path
        assert raw.endswith(b"\n"), path


def test_published_matches_keep_their_verdicts():
    """125430 and 170123. Frame-bind status is not an input to these verdicts."""
    pinned = {
        "site/matches/125430/receipt.json": (
            "FAIL", "single-attested",
            ["home:cp[0]:gap_too_long", "home:cp[591]:gap_too_long", "home:cp[591]:clock_skew", "home:cp[592]:clock_skew"],
            ["referee_key_not_pinned", "referee_not_on_allowlist", "referee_local_self_attested",
             "home:challenge_missing:kickoff", "home:challenge_missing:halftime", "home:challenge_missing:end",
             "home:motion_signal_missing"],
        ),
    }
    for rel, expect in pinned.items():
        raw = (ROOT / rel).read_bytes()
        rec = json.loads(raw)
        before = verify_receipt(rec)
        assert frame_bind_status({CHECKPOINTS_FILE: b"", "receipt.json": raw}) == MISSING
        after = verify_receipt(rec)
        got = (after["status"], after.get("trust_tier"), after["errors"], after["partial"])
        assert before["status"] == after["status"]
        assert got == expect

    raw = (ROOT / "site/matches/170123/receipt.json").read_bytes()
    body = json.loads(raw)
    before = verify_receipt(body)
    assert frame_bind_status({}) == MISSING
    bare = verify_receipt(body)
    pinned_v = verify_receipt(body, referee_pub=body["referee"]["pubkey"])
    assert before["status"] == bare["status"] == "PARTIAL"
    assert bare["errors"] == [] and bare["partial"] == ["referee_key_not_pinned"]
    assert pinned_v["status"] == "VERIFIED" and pinned_v["errors"] == [] and pinned_v["partial"] == []
    assert "frame_bind" not in bare and "frame_bind" not in pinned_v
