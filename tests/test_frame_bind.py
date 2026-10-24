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
import re
import shutil
import subprocess
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
    MAX_LINE_BYTES,
    MAX_MEMBER_BYTES,
    PATH_PATTERN,
    READERS_FILE,
    READERS_SCHEMA,
    SCHEMA,
    FrameBindResult,
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
    assert frame_bind_status({CHECKPOINTS_FILE: _jsonl([plain])}) == MISSING
    assert frame_bind_status({CHECKPOINTS_FILE: b""}) == MISSING
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
    base = {CHECKPOINTS_FILE: _jsonl([plain])}
    assert frame_bind_status({**base, FRAMES_FILE: b"\n".join(lines) + b"\n"}) == STRIPPED
    assert frame_bind_status({**base, READERS_FILE: readers}) == STRIPPED
    assert frame_bind_status({FRAMES_FILE: b"\n".join(lines) + b"\n"}) == STRIPPED
    assert frame_bind_status({READERS_FILE: readers}) == STRIPPED


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
    for path in ("secrets/referee_key.json", "secret/reader.py", "sidecar/referee_key.json", "a/foo_KEY.py"):
        obj = _readers_obj()
        obj["readers"][0]["files"][0]["path"] = path
        with pytest.raises(js.ValidationError):
            js.validate(obj, schema)
        lines, _cps, _readers = _valid_parts()
        raw = canonicalize(obj) + b"\n"
        heads = _chain(lines)
        cps = [_cp(0, len(lines), heads[-1], {"schema": READERS_SCHEMA, "sha256": hashlib.sha256(raw).hexdigest()})]
        assert frame_bind_status(_bundle(lines, cps, raw)) == BROKEN


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
    """021003, 125430, and 170123. Frame-bind status is not an input to these verdicts."""
    pinned = {
        "site/matches/021003/receipt.json": (
            "SELF_ATTESTED", "single-attested", [],
            ["referee_key_not_pinned", "referee_not_on_allowlist", "referee_local_self_attested",
             "away:challenge_missing:kickoff", "away:challenge_missing:halftime", "away:challenge_missing:end",
             "away:motion_signal_missing"],
        ),
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
