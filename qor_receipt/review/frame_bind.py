"""Advisory status of one bundle's frame-bind sidecars.

Gates call ``verify_frame_bind`` on the bundle directory, canonical bytes, or
zip. It returns a frozen ``FrameBindResult``. That object is not a trust token:
anyone can construct one. A gate must call ``verify_frame_bind`` itself, or
``frame_bind_result_ok(bundle, result)``, which recomputes the digest from the
bundle bytes and re-verifies. A caller-supplied result is not a claim.

``is_frame_bind_verified`` is true only for a plain ``str`` equal to
``FRAME_BIND_VERIFIED``. A ``str`` subclass or Enum is not verified.

``missing`` does not prove binding was never enabled: a complete strip and
reseal is indistinguishable from never-enabled here, because this function
does not check the referee signature. The status is not a receipt verdict.

See spec/match-receipt-v0.md §7.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from qor_receipt.checkpoints import GENESIS, body_of
from qor_receipt.jcs import canonicalize, sha256_hex
from qor_receipt.review.policy import MAX_META_BYTES

# Spec order. These four values are plain str objects. Do not wrap them in Enum.
FRAME_BIND_STATUSES = ("verified", "missing", "stripped", "broken")
FRAME_BIND_VERIFIED = FRAME_BIND_STATUSES[0]
_MISSING = FRAME_BIND_STATUSES[1]
_STRIPPED = FRAME_BIND_STATUSES[2]
_BROKEN = FRAME_BIND_STATUSES[3]
_BUNDLE_MAGIC = b"qor-frame-bind-bundle/v0\n"
# Same byte budget as read_meta_zip. The bundle and the referee upload share it.
MAX_BUNDLE_BYTES = MAX_META_BYTES
MAX_MEMBER_BYTES = MAX_META_BYTES
# checkpoints.jsonl is a few hundred bytes a row, about 900 rows an hour.
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
# readers.json is one closed object.
MAX_READERS_BYTES = 64 * 1024
# frames.jsonl is about 20 MB per hour. 64 MiB is a few hours and stays under the upload budget.
MAX_FRAMES_BYTES = 64 * 1024 * 1024
# A real frame row is under 300 bytes. A longer line is rejected before json.loads.
MAX_FRAME_LINE_BYTES = 4 * 1024
# A frame row is a flat object: one or two `{` and a handful of colons. An array, or a
# wider object, is rejected before json.loads so a 4 KiB line cannot expand into a tree.
MAX_FRAME_LINE_BRACES = 4
MAX_FRAME_LINE_COLONS = 24
MAX_ZIP_MEMBERS = 64
MAX_LINE_BYTES = 1024 * 1024
MAX_ROWS = 1_000_000
# Checkpoint and reader objects are a handful of levels deep. This stops a nested
# bomb before json.loads. Frame lines are also rejected for `[` and extra colons.
MAX_JSON_DEPTH = 32
_SYMLINK_MODE = 0o120000

SCHEMA = "qorcap-frame-bind/v0"
READERS_SCHEMA = "qorcap-readers/v0"
FRAMES_FILE = "frames.jsonl"
READERS_FILE = "readers.json"
CHECKPOINTS_FILE = "checkpoints.jsonl"
_MEMBER_ORDER = (CHECKPOINTS_FILE, FRAMES_FILE, READERS_FILE)

PATH_PATTERN = (
    r"^(?!.*\.(?:[Kk][Ee][Yy]|[Pp][Ee][Mm]|[Pp]12|[Pp][Ff][Xx]|[Jj][Ww][Kk]|[Pp][Pp][Kk]|[Jj][Kk][Ss])$)"
    r"(?!.*(?:^|/)[Kk][Ee][Yy][Ss](?:/|$))"
    r"(?!.*(?:^|/)[Ss][Ee][Cc][Rr][Ee][Tt][Ss]?(?:/|$))"
    r"(?!.*(?:^|/)[^/]*_[Kk][Ee][Yy](?:\.[^/]+)?(?:/|$))"
    r"(?!.*(?:^|/)[Ii][Dd]_(?:[Rr][Ss][Aa]|[Dd][Ss][Aa]|[Ee][Cc][Dd][Ss][Aa](?:_[Ss][Kk])?|[Ee][Dd]25519(?:_[Ss][Kk])?|[Ee][Dd]448(?:_[Ss][Kk])?)(?:\.[^/]+)?(?:/|$))"
    r"(?:[A-Za-z0-9_][A-Za-z0-9_.+-]*/)*[A-Za-z0-9_][A-Za-z0-9_.+-]*$"
)
_FORBIDDEN_SEGMENTS = frozenset({"keys", "secret", "secrets"})
_PATH_RE = re.compile(PATH_PATTERN)
_PRIVATE_KEY_NAME = re.compile(r"(?i)^id_(?:rsa|dsa|ecdsa(?:_sk)?|ed25519(?:_sk)?|ed448(?:_sk)?)$")
_RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_GAP_REASONS = frozenset({
    "pts_regression",
    "composition_offset",
    "sample_pes_mismatch",
    "clock_lost",
    "unreadable",
})
_MAX_INT = 2**53 - 1


@dataclass(frozen=True, slots=True)
class FrameBindSegment:
    """Coverage of one segment. ``align_gap`` is true for an ``align_gap`` line."""

    seg: int
    from_frame: int
    samples: int
    bound: int
    align_gap: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FrameBindResult:
    """Status, digest, and coverage computed from one bundle's bytes.

    Not a trust token. Call ``verify_frame_bind`` on the bundle, or pass the
    bundle bytes to ``frame_bind_result_ok``. Do not trust a result a caller built.
    """

    status: str
    bundle_digest: str
    coverage: tuple[FrameBindSegment, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not str or type(self.bundle_digest) is not str:
            raise TypeError("status and bundle_digest must be plain str")
        if type(self.coverage) is not tuple:
            object.__setattr__(self, "coverage", tuple(self.coverage))
        for segment in self.coverage:
            if type(segment) is not FrameBindSegment:
                raise TypeError("coverage entries must be FrameBindSegment")
            if segment.reason is not None and type(segment.reason) is not str:
                raise TypeError("segment reason must be a plain str or None")


def is_frame_bind_verified(value: object) -> bool:
    """True only for a plain str equal to ``FRAME_BIND_VERIFIED``.

    A str subclass is rejected before equality runs, so an object whose
    ``__eq__`` always returns true is not verified. Enum values are not str.
    """
    return type(value) is str and value == FRAME_BIND_VERIFIED


def frame_bind_status(files: Mapping[str, bytes]) -> str:
    """One of ``FRAME_BIND_STATUSES``. Never raises. Unknown shapes are ``broken``."""
    try:
        if not isinstance(files, Mapping):
            return _BROKEN
        status, _loaded = _classify(files)
        return _plain_status(status)
    except Exception:
        return _BROKEN


def verify_frame_bind(bundle: str | Path | bytes | bytearray) -> FrameBindResult:
    """Recompute the frame-bind result from a directory, canonical bytes, or zip.

    ``bundle_digest`` is the SHA-256 of the bytes this call actually read: the
    canonical image for a directory, or the file bytes for a zip or a canonical
    blob. The returned object is not a credential.
    """
    try:
        raw, files = _open_bundle(bundle)
        status, loaded = _classify(files)
        # Coverage is filled only when the commitments and files are both present
        # (verified, or broken on content) and the frame rows still parse. missing
        # and stripped report no coverage. The rows parsed for the status are reused.
        if status in (FRAME_BIND_VERIFIED, _BROKEN):
            if loaded is None and isinstance(files.get(FRAMES_FILE), (bytes, bytearray)):
                loaded = _load_frames(bytes(files[FRAMES_FILE]))
            coverage = _segments(loaded.objs) if loaded is not None and loaded.ok and loaded.objs else ()
        else:
            coverage = ()
    except Exception:
        raw = bytes(bundle) if isinstance(bundle, (bytes, bytearray)) else b""
        status = _BROKEN
        coverage = ()
    return _make_result(status, raw, coverage)


def frame_bind_result_ok(bundle: str | Path | bytes | bytearray, result: object) -> bool:
    """True only when ``result`` matches a fresh ``verify_frame_bind(bundle)`` and that status is verified.

    Recomputes the digest and the status from ``bundle``. A hand-built
    ``FrameBindResult`` with a fabricated digest does not match. This does not
    make the result unforgeable: gates must pass the bundle bytes and must never
    trust a caller-supplied result on its own.
    """
    if type(result) is not FrameBindResult:
        return False
    if not is_frame_bind_verified(result.status):
        return False
    fresh = verify_frame_bind(bundle)
    if not is_frame_bind_verified(fresh.status):
        return False
    if type(result.bundle_digest) is not str or result.bundle_digest != fresh.bundle_digest:
        return False
    if type(result.coverage) is not tuple or result.coverage != fresh.coverage:
        return False
    return True


def frame_bind_bundle_bytes(files: Mapping[str, bytes]) -> bytes:
    """Canonical byte image. Members are checkpoints, frames, readers, each only if present."""
    parts = [_BUNDLE_MAGIC]
    for name in _MEMBER_ORDER:
        if name not in files:
            continue
        payload = bytes(files[name])
        parts.append(name.encode("ascii") + b"\n" + str(len(payload)).encode("ascii") + b"\n" + payload)
    return b"".join(parts)


def _plain_status(value: object) -> str:
    if type(value) is not str:
        return _BROKEN
    for item in FRAME_BIND_STATUSES:
        if type(item) is str and value == item:
            return item
    return _BROKEN


def _make_result(status: object, raw: bytes, coverage: tuple[FrameBindSegment, ...]) -> FrameBindResult:
    plain = _plain_status(status)
    if type(plain) is not str:
        plain = _BROKEN
    return FrameBindResult(
        status=plain,
        bundle_digest=hashlib.sha256(raw).hexdigest(),
        coverage=tuple(coverage),
    )


def _open_bundle(bundle: str | Path | bytes | bytearray) -> tuple[bytes, dict[str, bytes]]:
    if isinstance(bundle, (bytes, bytearray)):
        raw = bytes(bundle)
        if len(raw) > MAX_BUNDLE_BYTES:
            raise ValueError("bundle bytes exceed the cap")
        files = _files_from_bytes(raw)
        if files is None:
            raise ValueError("bundle bytes are not a frame-bind image or zip")
        return raw, files
    path = Path(bundle)
    if path.is_symlink():
        raise ValueError("symlink")
    if path.is_dir():
        files = _files_from_dir(path)
        raw = frame_bind_bundle_bytes(files)
        if len(raw) > MAX_BUNDLE_BYTES:
            raise ValueError("bundle image exceeds the cap")
        return raw, files
    if path.is_file():
        raw = _read_path_limited(path, MAX_BUNDLE_BYTES)
        if raw is None:
            raise ValueError("bundle file exceeds the cap")
        files = _files_from_bytes(raw)
        if files is None:
            raise ValueError("bundle file is not a frame-bind image or zip")
        return raw, files
    raise ValueError("bundle path is missing")


def _files_from_dir(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    for name in _MEMBER_ORDER:
        child = path / name
        if child.is_symlink():
            raise ValueError("symlink")
        if not child.is_file():
            continue
        limit = _member_cap(name)
        size = child.stat().st_size
        if size > limit or total + size > MAX_BUNDLE_BYTES:
            raise ValueError("member exceeds the cap")
        payload = _read_path_limited(child, limit)
        if payload is None:
            raise ValueError("member exceeds the cap")
        total += len(payload)
        files[name] = payload
    return files


def _read_path_limited(path: Path, limit: int) -> bytes | None:
    if path.stat().st_size > limit:
        return None
    with path.open("rb") as src:
        return _read_limited(src, limit)


def _read_limited(src: Any, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes. One byte over the cap returns None and stops."""
    if limit < 0:
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        block = src.read(min(1024 * 1024, limit - total + 1))
        if not block:
            return b"".join(chunks)
        total += len(block)
        if total > limit:
            return None
        chunks.append(block)


def _files_from_bytes(raw: bytes) -> dict[str, bytes] | None:
    if len(raw) > MAX_BUNDLE_BYTES:
        return None
    if raw[:2] == b"PK":
        return _files_from_zip(raw)
    return _parse_bundle_bytes(raw)


def _parse_bundle_bytes(raw: bytes) -> dict[str, bytes] | None:
    if not raw.startswith(_BUNDLE_MAGIC):
        return None
    i = len(_BUNDLE_MAGIC)
    files: dict[str, bytes] = {}
    seen = -1
    while i < len(raw):
        nl = raw.find(b"\n", i)
        if nl < 0:
            return None
        try:
            name = raw[i:nl].decode("ascii")
        except UnicodeError:
            return None
        if name not in _MEMBER_ORDER:
            return None
        idx = _MEMBER_ORDER.index(name)
        if idx <= seen:
            return None
        seen = idx
        i = nl + 1
        nl = raw.find(b"\n", i)
        if nl < 0:
            return None
        length_txt = raw[i:nl]
        if not length_txt.isdigit() or length_txt.decode("ascii") != str(int(length_txt)):
            return None
        length = int(length_txt)
        i = nl + 1
        if length > _member_cap(name) or i + length > len(raw):
            return None
        files[name] = raw[i:i + length]
        i += length
    return files


def _files_from_zip(raw: bytes) -> dict[str, bytes] | None:
    try:
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                return None
            files: dict[str, bytes] = {}
            total = 0
            for info in infos:
                if _zip_member_forbidden(info):
                    return None
                if info.is_dir():
                    continue
                name = info.filename
                limit = _member_cap(name)
                if name in files or info.file_size > limit or total + info.file_size > MAX_BUNDLE_BYTES:
                    return None
                with zf.open(info, "r") as src:
                    payload = _read_limited(src, limit)
                if payload is None or total + len(payload) > MAX_BUNDLE_BYTES:
                    return None
                total += len(payload)
                files[name] = payload
    except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, EOFError):
        return None
    return files


def _zip_member_forbidden(info: zipfile.ZipInfo) -> bool:
    """Exact member names only. Traversal, absolute paths, backslashes, and links are rejected."""
    name = info.filename
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return True
    parts = [part for part in name.split("/") if part]
    if any(part in ("..", ".") for part in parts):
        return True
    mode = (info.external_attr >> 16) & 0o170000
    if mode == _SYMLINK_MODE or info.flag_bits & 0x1:
        return True
    if info.is_dir():
        return False
    return name not in _MEMBER_ORDER


@dataclass(frozen=True, slots=True)
class _LoadedFrames:
    """One parse of ``frames.jsonl``. ``ok`` means the rows satisfy the frame rules."""

    lines: tuple[bytes, ...]
    objs: tuple[dict[str, Any], ...] | None
    heads: tuple[str, ...]
    ok: bool


_NO_FRAMES = _LoadedFrames((), None, (), False)


def _segments(objs: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> tuple[FrameBindSegment, ...]:
    by_seg: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for obj in objs:
        if obj.get("type") != "frame":
            continue
        seg = obj["seg"]
        if seg not in by_seg:
            order.append(seg)
            by_seg[seg] = []
        by_seg[seg].append(obj)
    gaps = {obj["seg"]: obj for obj in objs if obj.get("type") == "align_gap"}
    segments: list[FrameBindSegment] = []
    seen: set[int] = set()
    for seg in order:
        rows = by_seg[seg]
        if seg in gaps:
            segments.append(_gap_segment(gaps[seg]))
        else:
            indexes = [row["frame"] for row in rows]
            segments.append(FrameBindSegment(
                seg=seg,
                from_frame=min(indexes),
                samples=max(indexes) - min(indexes) + 1,
                bound=len(rows),
                align_gap=False,
                reason=None,
            ))
        seen.add(seg)
    for seg, gap in gaps.items():
        if seg not in seen:
            segments.append(_gap_segment(gap))
    return tuple(segments)


def _gap_segment(gap: dict[str, Any]) -> FrameBindSegment:
    reason = gap["reason"]
    if type(reason) is not str:
        raise TypeError("align_gap reason must be a plain str")
    return FrameBindSegment(
        seg=gap["seg"],
        from_frame=gap["from_frame"],
        samples=gap["samples"],
        bound=gap["bound"],
        align_gap=True,
        reason=reason,
    )


def _member_cap(name: str) -> int:
    if name == CHECKPOINTS_FILE:
        return MAX_CHECKPOINT_BYTES
    if name == READERS_FILE:
        return MAX_READERS_BYTES
    if name == FRAMES_FILE:
        return MAX_FRAMES_BYTES
    return MAX_MEMBER_BYTES


def _line_cap(name: str) -> int:
    if name == FRAMES_FILE:
        return MAX_FRAME_LINE_BYTES
    return MAX_LINE_BYTES


def _classify(files: Mapping[str, bytes]) -> tuple[str, _LoadedFrames | None]:
    """Status, plus the frames parse when this call already did one.

    The checkpoint chain is checked once. ``frames.jsonl`` is parsed once, and only
    when a commitment points at it. ``None`` means this call did not parse frames
    (a later coverage fill may parse them once).
    """
    total = 0
    for key, val in files.items():
        if not isinstance(key, str) or not isinstance(val, (bytes, bytearray)):
            return _BROKEN, None
        blob = bytes(val)
        total += len(blob)
        cap = _member_cap(key)
        if len(blob) > cap or total > MAX_BUNDLE_BYTES or _payload_over_cap(
            blob, line_limit=_line_cap(key), member_limit=cap,
        ):
            return _BROKEN, None
    raw_cps = files.get(CHECKPOINTS_FILE)
    if raw_cps is None or bytes(raw_cps) == b"":
        cps: list[dict[str, Any]] = []
    else:
        parsed = _parse_objects(bytes(raw_cps))
        # A checkpoint file that does not parse, or whose chain does not recompute,
        # is broken before leftover classification. missing and stripped require a
        # chain that matches, or no checkpoint bytes.
        if parsed is None or not _checkpoint_chain_ok(parsed):
            return _BROKEN, None
        cps = parsed

    has_frames = FRAMES_FILE in files
    has_readers_file = READERS_FILE in files
    flagged = [_has_frame_bind(cp) for cp in cps]
    any_fb = any(flagged)
    all_fb = bool(cps) and all(flagged)
    readers_present, readers_val = _readers_on_seq0(cps)

    loaded: _LoadedFrames | None = None
    # A frames file that is present and does not match its commitments is broken
    # even when another sidecar is missing. Absence of the file, on a chain that
    # recomputes, stays a leftover.
    if has_frames and any_fb:
        loaded = _load_frames(bytes(files[FRAMES_FILE]))
        if not _frames_prefix_ok(cps, loaded):
            return _BROKEN, loaded

    if cps and any_fb and not all_fb:
        return _STRIPPED, loaded
    if any_fb and not readers_present:
        return _STRIPPED, loaded
    if readers_present and not all_fb:
        return _STRIPPED, loaded
    if has_frames and not any_fb:
        return _STRIPPED, loaded
    if has_readers_file and not readers_present:
        return _STRIPPED, loaded
    if any_fb and not has_frames:
        return _STRIPPED, loaded
    if readers_present and _sha_shaped(readers_val) and not has_readers_file:
        return _STRIPPED, loaded
    if readers_present and _unbound_shaped(readers_val) and has_readers_file:
        return _STRIPPED, loaded
    if not any_fb and not readers_present and not has_frames and not has_readers_file:
        return _MISSING, loaded
    if not all_fb or not readers_present or not has_frames:
        return _BROKEN, loaded
    if _unbound_shaped(readers_val):
        if has_readers_file or not _readers_tail_ok(cps, loaded, readers_val, None):
            return _BROKEN, loaded
        return FRAME_BIND_VERIFIED, loaded
    if not _sha_shaped(readers_val) or not has_readers_file:
        return _BROKEN, loaded
    if not _readers_tail_ok(cps, loaded, readers_val, bytes(files[READERS_FILE])):
        return _BROKEN, loaded
    return FRAME_BIND_VERIFIED, loaded


def _checkpoint_chain_ok(cps: list[dict[str, Any]]) -> bool:
    """Recompute frames_hash, the body hash, prev, and seq from the checkpoint bytes.

    ``input_hash`` is inside the hashed body. This image has no ``inputs.jsonl``, so that field is not
    recomputed from HID events. ``readers`` sha256 is checked against the file bytes in ``_readers_file_ok``.
    """
    prev = GENESIS
    for index, cp in enumerate(cps):
        if not isinstance(cp, dict) or not _is_int(cp.get("seq"), 0) or cp["seq"] != index:
            return False
        if cp.get("prev") != prev:
            return False
        q = cp.get("qorcap")
        if not isinstance(q, dict):
            return False
        # Later checkpoints omit readers. Any readers object on seq >= 1 is broken.
        if index > 0 and "readers" in q:
            return False
        try:
            if cp.get("frames_hash") != sha256_hex(q):
                return False
            digest = sha256_hex(body_of(cp))
        except (KeyError, TypeError, ValueError):
            return False
        if cp.get("hash") != digest:
            return False
        prev = digest
    return True


def _rows_ok(objs: list[dict[str, Any]]) -> bool:
    if not _header_ok(objs[0]):
        return False
    frames: list[dict[str, Any]] = []
    gaps: dict[int, dict[str, Any]] = {}
    for obj in objs[1:]:
        kind = obj.get("type")
        if kind == "frame":
            if not _frame_ok(obj):
                return False
            frames.append(obj)
        elif kind == "align_gap":
            if not _gap_ok(obj):
                return False
            seg = obj["seg"]
            if seg in gaps:
                return False
            gaps[seg] = obj
        else:
            return False
    # A header, or a header plus only align_gap lines, binds no frame.
    if not frames:
        return False
    prev_pts = None
    prev_frame = None
    prev_seg = None
    by_seg: dict[int, list[dict[str, Any]]] = {}
    for row in frames:
        if prev_pts is not None and row["pts"] <= prev_pts:
            return False
        if prev_frame is not None and row["frame"] <= prev_frame:
            return False
        if prev_seg is not None and row["seg"] < prev_seg:
            return False
        prev_pts = row["pts"]
        prev_frame = row["frame"]
        prev_seg = row["seg"]
        by_seg.setdefault(row["seg"], []).append(row)
    for seg, rows in by_seg.items():
        if seg in gaps:
            continue
        indexes = [row["frame"] for row in rows]
        if any(indexes[i] + 1 != indexes[i + 1] for i in range(len(indexes) - 1)):
            return False
    for seg, gap in gaps.items():
        rows = by_seg.get(seg, [])
        if len(rows) != gap["bound"] or gap["bound"] > gap["samples"]:
            return False
        start = gap["from_frame"]
        end = start + gap["samples"]
        for row in rows:
            if gap["samples"] == 0 or not (start <= row["frame"] < end):
                return False
    return _frame_indexes_covered(frames, gaps)


def _frame_indexes_covered(frames: list[dict[str, Any]], gaps: dict[int, dict[str, Any]]) -> bool:
    """Frame indexes run from 0 with no hole, unless an align_gap span covers the jump.

    Gap spans are sorted and merged once. Each jump then advances a cursor over that
    list, so the walk is linear after the sort. A span that also contains a frame
    row of a different segment is rejected: that index would be counted twice.
    """
    if _gap_overlaps_other_segment(frames, gaps):
        return False
    merged = _merged_spans(gaps)
    cursor = 0
    at = 0
    for row in frames:
        index = row["frame"]
        if index > cursor:
            while at < len(merged) and merged[at][1] <= cursor:
                at += 1
            if at >= len(merged) or merged[at][0] > cursor or merged[at][1] < index:
                return False
        cursor = index + 1
    return True


def _merged_spans(gaps: dict[int, dict[str, Any]]) -> list[tuple[int, int]]:
    """Half-open spans, sorted and folded so overlapping or abutting spans are one."""
    spans = sorted((gap["from_frame"], gap["from_frame"] + gap["samples"]) for gap in gaps.values())
    merged: list[tuple[int, int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            prev_lo, prev_hi = merged[-1]
            if hi > prev_hi:
                merged[-1] = (prev_lo, hi)
        else:
            merged.append((lo, hi))
    return merged


def _gap_overlaps_other_segment(frames: list[dict[str, Any]], gaps: dict[int, dict[str, Any]]) -> bool:
    """True when a gap span contains a frame row whose ``seg`` is not the gap's.

    Frame rows are strictly increasing and ``seg`` is non-decreasing, so a span
    holds another segment's row exactly when either end of the covered slice differs.
    """
    if not frames or not gaps:
        return False
    indexes = [row["frame"] for row in frames]
    for gap in gaps.values():
        lo = gap["from_frame"]
        hi = lo + gap["samples"]
        left = bisect.bisect_left(indexes, lo)
        right = bisect.bisect_left(indexes, hi)
        if left >= right:
            continue
        seg = gap["seg"]
        if frames[left]["seg"] != seg or frames[right - 1]["seg"] != seg:
            return True
    return False


def _load_frames(raw: bytes) -> _LoadedFrames:
    """Parse ``frames.jsonl`` once. A line over the frame cap is not passed to ``json.loads``."""
    lines = _split_lines(raw, line_limit=MAX_FRAME_LINE_BYTES, member_limit=MAX_FRAMES_BYTES)
    if not lines:
        return _NO_FRAMES
    objs = _canonical_objects(lines, frame=True)
    if objs is None or not _rows_ok(objs):
        return _LoadedFrames(tuple(lines), None, (), False)
    return _LoadedFrames(tuple(lines), tuple(objs), tuple(_prefix_heads(lines)), True)


def _frames_prefix_ok(cps: list[dict[str, Any]], loaded: _LoadedFrames) -> bool:
    """True when every frame_bind commitment matches a prefix of the parsed lines.

    A trailing uncommitted line is still a leftover, so this does not require the
    last commitment to cover the whole file. A file that is present and does not
    match is not classified as stripped.
    """
    if not loaded.ok:
        return False
    prev_n = -1
    saw = False
    for cp in cps:
        q = cp.get("qorcap")
        if not isinstance(q, dict) or "frame_bind" not in q:
            continue
        fb = q.get("frame_bind")
        if not _frame_commitment_ok(fb):
            return False
        n = fb["n"]
        if n < prev_n or n > len(loaded.lines):
            return False
        expect = GENESIS if n == 0 else loaded.heads[n - 1]
        if fb["head"] != expect:
            return False
        prev_n = n
        saw = True
    return saw


def _readers_tail_ok(cps: list[dict[str, Any]], loaded: _LoadedFrames | None, readers_val: Any,
                     readers_file: bytes | None) -> bool:
    """The last commitment covers every line, and the readers commitment matches.

    Prefix heads were checked in ``_frames_prefix_ok``. The chain was checked once
    before this runs.
    """
    if loaded is None or not loaded.ok:
        return False
    last = -1
    for cp in cps:
        q = cp.get("qorcap")
        if not isinstance(q, dict):
            return False
        fb = q.get("frame_bind")
        if not _frame_commitment_ok(fb):
            return False
        last = fb["n"]
    if last != len(loaded.lines):
        return False
    if readers_file is None:
        return _unbound_commitment_ok(readers_val)
    return _hashed_commitment_ok(readers_val) and _readers_file_ok(readers_file, readers_val["sha256"])


def _header_ok(obj: dict[str, Any]) -> bool:
    if set(obj) != {"type", "schema", "hash_of", "clock", "time_base"}:
        return False
    if obj["type"] != "header" or obj["schema"] != SCHEMA:
        return False
    if obj["hash_of"] != "encoded-video-sample" or obj["clock"] != "qorcap-mono-ms":
        return False
    base = obj["time_base"]
    if not isinstance(base, dict) or set(base) != {"num", "den"}:
        return False
    return _is_int(base["num"], 1) and _is_int(base["den"], 1)


def _frame_ok(obj: dict[str, Any]) -> bool:
    if set(obj) != {"type", "seg", "frame", "pts", "mono_ms", "au_sha256"}:
        return False
    if obj["type"] != "frame":
        return False
    return (_is_int(obj["seg"], 0) and _is_int(obj["frame"], 0) and _is_int(obj["pts"], 0)
            and _is_int(obj["mono_ms"], 0) and _hex64(obj["au_sha256"]))


def _gap_ok(obj: dict[str, Any]) -> bool:
    if set(obj) != {"type", "seg", "from_frame", "samples", "bound", "reason"}:
        return False
    if obj["type"] != "align_gap" or obj["reason"] not in _GAP_REASONS:
        return False
    return (_is_int(obj["seg"], 0) and _is_int(obj["from_frame"], 0)
            and _is_int(obj["samples"], 1) and _is_int(obj["bound"], 0))


def _frame_commitment_ok(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"schema", "n", "head"}:
        return False
    return obj["schema"] == SCHEMA and _is_int(obj["n"], 0) and _hex64(obj["head"])


def _hashed_commitment_ok(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"schema", "sha256"}:
        return False
    return obj["schema"] == READERS_SCHEMA and _hex64(obj["sha256"])


def _unbound_commitment_ok(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"schema", "state"}:
        return False
    return obj["schema"] == READERS_SCHEMA and obj["state"] == "unbound"


def _readers_file_ok(raw: bytes, want: str) -> bool:
    if hashlib.sha256(raw).hexdigest() != want:
        return False
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n") or b"\r" in raw:
        return False
    body = raw[:-1]
    if len(body) > MAX_LINE_BYTES:
        return False
    try:
        obj = _loads_strict(body.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return False
    if not isinstance(obj, dict) or canonicalize(obj) != body:
        return False
    return _readers_obj_ok(obj)


def _readers_obj_ok(obj: dict[str, Any]) -> bool:
    if set(obj) != {"schema", "qorcap_version", "readers", "runtimes"}:
        return False
    if obj["schema"] != READERS_SCHEMA or not _short_str(obj["qorcap_version"]):
        return False
    readers = obj["readers"]
    runtimes = obj["runtimes"]
    if not isinstance(readers, list) or not readers or not isinstance(runtimes, list) or not runtimes:
        return False
    return all(_reader_ok(r) for r in readers) and all(_runtime_ok(r) for r in runtimes)


def _reader_ok(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"id", "version", "files"}:
        return False
    files = obj["files"]
    if not _short_str(obj["id"]) or not _short_str(obj["version"]) or not isinstance(files, list) or not files:
        return False
    return all(_reader_file_ok(f) for f in files)


def _reader_file_ok(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"kind", "path", "sha256"}:
        return False
    path = obj["path"]
    if obj["kind"] not in ("code", "pack") or not isinstance(path, str) or not _hex64(obj["sha256"]):
        return False
    if len(path) > 240 or _PATH_RE.fullmatch(path) is None:
        return False
    for part in path.split("/"):
        if part.lower() in _FORBIDDEN_SEGMENTS:
            return False
        stem = part.split(".", 1)[0]
        if stem.lower().endswith("_key") or _PRIVATE_KEY_NAME.fullmatch(stem):
            return False
    return True


def _runtime_ok(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"id", "version", "sha256"}:
        return False
    ident = obj["id"]
    if not isinstance(ident, str) or _RUNTIME_ID_RE.fullmatch(ident) is None:
        return False
    return _short_str(obj["version"]) and _hex64(obj["sha256"])


def _prefix_heads(lines: list[bytes]) -> list[str]:
    head = GENESIS
    heads: list[str] = []
    for line in lines:
        digest = hashlib.sha256(line).hexdigest()
        head = sha256_hex({"prev": head, "sha256": digest})
        heads.append(head)
    return heads


def _has_frame_bind(cp: dict[str, Any]) -> bool:
    q = cp.get("qorcap")
    return isinstance(q, dict) and "frame_bind" in q


def _readers_on_seq0(cps: list[dict[str, Any]]) -> tuple[bool, Any]:
    for cp in cps:
        if _is_int(cp.get("seq"), 0) and cp["seq"] == 0:
            q = cp.get("qorcap")
            if isinstance(q, dict) and "readers" in q:
                return True, q.get("readers")
            return False, None
    return False, None


def _sha_shaped(val: Any) -> bool:
    return isinstance(val, dict) and "sha256" in val and "state" not in val


def _unbound_shaped(val: Any) -> bool:
    return isinstance(val, dict) and "sha256" not in val and val.get("state") == "unbound"


def _parse_objects(raw: bytes) -> list[dict[str, Any]] | None:
    lines = _split_lines(raw)
    if lines is None:
        return None
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = _loads_strict(line.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            return None
        if not isinstance(obj, dict):
            return None
        out.append(obj)
    return out


def _frame_line_hostile(line: bytes) -> bool:
    """True when a frames line is an array or a wider object than a frame row.

    Checked on the raw bytes, before ``json.loads``. A real row has no ``[`` and
    at most a handful of keys.
    """
    return (
        len(line) > MAX_FRAME_LINE_BYTES
        or b"[" in line
        or line.count(b"{") > MAX_FRAME_LINE_BRACES
        or line.count(b":") > MAX_FRAME_LINE_COLONS
    )


def _canonical_objects(lines: list[bytes], *, frame: bool = False) -> list[dict[str, Any]] | None:
    out: list[dict[str, Any]] = []
    for line in lines:
        if frame and _frame_line_hostile(line):
            return None
        try:
            obj = _loads_strict(line.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            return None
        if not isinstance(obj, dict) or canonicalize(obj) != line:
            return None
        out.append(obj)
    return out


def _json_too_deep(text: str) -> bool:
    """True when ``{`` / ``[`` nesting exceeds ``MAX_JSON_DEPTH``. Strings are skipped."""
    depth = 0
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif ch in "}]":
            if depth == 0:
                return True
            depth -= 1
    return False


def _loads_strict(text: str) -> Any:
    """JSON object parse that rejects duplicate keys, NaN/Infinity, and deep nesting."""
    if _json_too_deep(text):
        raise ValueError("nesting")

    def pairs(items: list[tuple[Any, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in items:
            if type(key) is not str or key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj

    def reject(constant: str) -> Any:
        raise ValueError(constant)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)


def _payload_over_cap(raw: bytes, *, line_limit: int = MAX_LINE_BYTES,
                      member_limit: int = MAX_MEMBER_BYTES) -> bool:
    """True when ``raw`` is over its member, row, or line cap.

    Row count is ``newline count + 1`` on the body. The member is not split into
    a list of lines, so a run of ``1\\n`` cannot allocate one object per row.
    """
    if len(raw) > member_limit:
        return True
    body = raw[:-1] if raw.endswith(b"\n") else raw
    if body == b"":
        return False
    if body.count(b"\n") + 1 > MAX_ROWS:
        return True
    start = 0
    length = len(body)
    while start < length:
        nl = body.find(b"\n", start)
        if nl < 0:
            return length - start > line_limit
        if nl - start > line_limit:
            return True
        start = nl + 1
    return False


def _split_lines(raw: bytes, *, line_limit: int = MAX_LINE_BYTES,
                 member_limit: int = MAX_MEMBER_BYTES) -> list[bytes] | None:
    if _payload_over_cap(raw, line_limit=line_limit, member_limit=member_limit) or b"\r" in raw:
        return None
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw == b"":
        return []
    parts = raw.split(b"\n")
    if any(part == b"" for part in parts):
        return None
    return parts


def _is_int(val: Any, lo: int) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and lo <= val <= _MAX_INT


def _hex64(val: Any) -> bool:
    return isinstance(val, str) and _HEX64_RE.fullmatch(val) is not None


def _short_str(val: Any) -> bool:
    return isinstance(val, str) and 1 <= len(val) <= 80
