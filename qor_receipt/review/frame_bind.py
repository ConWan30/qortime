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

import hashlib
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from qor_receipt.checkpoints import GENESIS
from qor_receipt.jcs import canonicalize, sha256_hex

# Spec order. These four values are plain str objects. Do not wrap them in Enum.
FRAME_BIND_STATUSES = ("verified", "missing", "stripped", "broken")
FRAME_BIND_VERIFIED = FRAME_BIND_STATUSES[0]
_MISSING = FRAME_BIND_STATUSES[1]
_STRIPPED = FRAME_BIND_STATUSES[2]
_BROKEN = FRAME_BIND_STATUSES[3]
_BUNDLE_MAGIC = b"qor-frame-bind-bundle/v0\n"

SCHEMA = "qorcap-frame-bind/v0"
READERS_SCHEMA = "qorcap-readers/v0"
FRAMES_FILE = "frames.jsonl"
READERS_FILE = "readers.json"
CHECKPOINTS_FILE = "checkpoints.jsonl"
_MEMBER_ORDER = (CHECKPOINTS_FILE, FRAMES_FILE, READERS_FILE)

PATH_PATTERN = (
    r"^(?!.*\.(?:[Kk][Ee][Yy]|[Pp][Ee][Mm]|[Pp]12|[Pp][Ff][Xx])$)"
    r"(?!.*(?:^|/)[Kk][Ee][Yy][Ss](?:/|$))"
    r"(?:[A-Za-z0-9_][A-Za-z0-9_.+-]*/)*[A-Za-z0-9_][A-Za-z0-9_.+-]*$"
)
_PATH_RE = re.compile(PATH_PATTERN)
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
        return _plain_status(_status(files))
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
        status = frame_bind_status(files)
        # Coverage is filled only when the commitments and files are both present
        # (verified, or broken on content) and the frame rows still parse. missing
        # and stripped report no coverage.
        coverage = _coverage(files) if status in (FRAME_BIND_VERIFIED, _BROKEN) and _rows_parse(files) else ()
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
        files = _files_from_bytes(raw)
        if files is None:
            raise ValueError("bundle bytes are not a frame-bind image or zip")
        return raw, files
    path = Path(bundle)
    if path.is_dir():
        files: dict[str, bytes] = {}
        for name in _MEMBER_ORDER:
            child = path / name
            if child.is_file():
                files[name] = child.read_bytes()
        return frame_bind_bundle_bytes(files), files
    if path.is_file():
        raw = path.read_bytes()
        files = _files_from_bytes(raw)
        if files is None:
            raise ValueError("bundle file is not a frame-bind image or zip")
        return raw, files
    raise ValueError("bundle path is missing")


def _files_from_bytes(raw: bytes) -> dict[str, bytes] | None:
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
        if i + length > len(raw):
            return None
        files[name] = raw[i:i + length]
        i += length
    return files


def _files_from_zip(raw: bytes) -> dict[str, bytes] | None:
    try:
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            files: dict[str, bytes] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if name not in _MEMBER_ORDER:
                    continue
                if name in files:
                    return None
                files[name] = zf.read(info)
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    return files


def _rows_parse(files: Mapping[str, bytes]) -> bool:
    return _frame_objects(files) is not None


def _coverage(files: Mapping[str, bytes]) -> tuple[FrameBindSegment, ...]:
    objs = _frame_objects(files)
    if not objs:
        return ()
    return _segments(objs)


def _frame_objects(files: Mapping[str, bytes]) -> list[dict[str, Any]] | None:
    raw = files.get(FRAMES_FILE)
    if not isinstance(raw, (bytes, bytearray)):
        return None
    lines = _split_lines(bytes(raw))
    if not lines:
        return None
    objs = _canonical_objects(lines)
    if objs is None or not _rows_ok(objs):
        return None
    return objs


def _segments(objs: list[dict[str, Any]]) -> tuple[FrameBindSegment, ...]:
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


def _status(files: Mapping[str, bytes]) -> str:
    for key, val in files.items():
        if not isinstance(key, str) or not isinstance(val, (bytes, bytearray)):
            return _BROKEN
    raw_cps = files.get(CHECKPOINTS_FILE)
    if raw_cps is None:
        cps: list[dict[str, Any]] = []
    else:
        parsed = _parse_objects(bytes(raw_cps))
        if parsed is None:
            return _BROKEN
        cps = parsed

    has_frames = FRAMES_FILE in files
    has_readers_file = READERS_FILE in files
    flagged = [_has_frame_bind(cp) for cp in cps]
    any_fb = any(flagged)
    all_fb = bool(cps) and all(flagged)
    readers_present, readers_val = _readers_on_seq0(cps)

    if cps and any_fb and not all_fb:
        return _STRIPPED
    if any_fb and not readers_present:
        return _STRIPPED
    if readers_present and not all_fb:
        return _STRIPPED
    if has_frames and not any_fb:
        return _STRIPPED
    if has_readers_file and not readers_present:
        return _STRIPPED
    if any_fb and not has_frames:
        return _STRIPPED
    if readers_present and _sha_shaped(readers_val) and not has_readers_file:
        return _STRIPPED
    if readers_present and _unbound_shaped(readers_val) and has_readers_file:
        return _STRIPPED
    if not any_fb and not readers_present and not has_frames and not has_readers_file:
        return _MISSING
    if not all_fb or not readers_present or not has_frames:
        return _BROKEN
    if _unbound_shaped(readers_val):
        if has_readers_file or not _content_ok(cps, bytes(files[FRAMES_FILE]), readers_val, None):
            return _BROKEN
        return FRAME_BIND_VERIFIED
    if not _sha_shaped(readers_val) or not has_readers_file:
        return _BROKEN
    if not _content_ok(cps, bytes(files[FRAMES_FILE]), readers_val, bytes(files[READERS_FILE])):
        return _BROKEN
    return FRAME_BIND_VERIFIED


def _content_ok(cps: list[dict[str, Any]], frames: bytes, readers_val: Any,
                readers_file: bytes | None) -> bool:
    lines = _split_lines(frames)
    if not lines:
        return False
    objs = _canonical_objects(lines)
    if objs is None or not _rows_ok(objs):
        return False
    heads = _prefix_heads(lines)
    prev_n = -1
    for cp in cps:
        q = cp.get("qorcap")
        if not isinstance(q, dict):
            return False
        fb = q.get("frame_bind")
        if not _frame_commitment_ok(fb):
            return False
        n = fb["n"]
        if n < prev_n or n > len(lines):
            return False
        expect = GENESIS if n == 0 else heads[n - 1]
        if fb["head"] != expect:
            return False
        prev_n = n
    if prev_n != len(lines):
        return False
    if readers_file is None:
        return _unbound_commitment_ok(readers_val)
    return _hashed_commitment_ok(readers_val) and _readers_file_ok(readers_file, readers_val["sha256"])


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
    prev_pts = None
    prev_frame = None
    by_seg: dict[int, list[dict[str, Any]]] = {}
    for row in frames:
        if prev_pts is not None and row["pts"] <= prev_pts:
            return False
        if prev_frame is not None and row["frame"] <= prev_frame:
            return False
        prev_pts = row["pts"]
        prev_frame = row["frame"]
        by_seg.setdefault(row["seg"], []).append(row)
    for seg, gap in gaps.items():
        rows = by_seg.get(seg, [])
        if len(rows) != gap["bound"] or gap["bound"] > gap["samples"]:
            return False
        start = gap["from_frame"]
        end = start + gap["samples"]
        for row in rows:
            if gap["samples"] == 0 or not (start <= row["frame"] < end):
                return False
    return True


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
            and _is_int(obj["samples"], 0) and _is_int(obj["bound"], 0))


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
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
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
    return all(part.lower() != "keys" for part in path.split("/"))


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
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        out.append(obj)
    return out


def _canonical_objects(lines: list[bytes]) -> list[dict[str, Any]] | None:
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None
        if not isinstance(obj, dict) or canonicalize(obj) != line:
            return None
        out.append(obj)
    return out


def _split_lines(raw: bytes) -> list[bytes] | None:
    if b"\r" in raw:
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
