"""Read-only streaming verifier for ``qorcap-hand-timeline/v0`` + ``hand_raw.v0.bin`` v1."""
from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from qor_receipt.jcs import canonicalize, sha256_hex

SCHEMA = "qorcap-hand-timeline/v0"
SUMMARY_SCHEMA = "qorcap-hand-timeline-summary/v0"
FILE_NAME = "hand_timeline.v0.jsonl"
SUMMARY_FILE = "hand_timeline_summary.v0.json"
RAW_FILE = "hand_raw.v0.bin"
RAW_RECORD_BYTES = 80
RAW_LAYOUT = "v1"
CHUNK_US = 1_000_000
GENESIS = "0" * 64
UNMEASURED = "UNMEASURED"
# FN_LEFT | FN_RIGHT | BACK_LEFT | BACK_RIGHT bits (qorcap hid.rs bits 19..22)
EDGE_BITS_MASK = (1 << 19) | (1 << 20) | (1 << 21) | (1 << 22)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class HandTimelineCheck:
    ok: bool
    off: bool = False
    chunks: int = 0
    head: str = GENESIS
    file_sha256: str = ""
    device_models: list[str] = field(default_factory=list)
    fallback: bool = False
    raw_files: int = 0
    raw_slices: int = 0
    raw_missing: bool = False
    segment_bindings: int = 0
    stop_bound: bool = False
    hand_dir: Path | None = None
    hand_dir_source: str | None = None
    hand_dir_fallback_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    raw_records_count: int = 0


def _t_of(rec: dict[str, Any]) -> int | None:
    t = rec.get("t_us")
    if isinstance(t, int) and not isinstance(t, bool):
        return t
    t2 = rec.get("t_arrival_us")
    if isinstance(t2, int) and not isinstance(t2, bool):
        return t2
    return None


def _is_within(candidate: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        if candidate == root or root in candidate.parents:
            return True
    return False


def checkpoint_flags(bundle_dir: Path) -> tuple[bool, bool, bool]:
    """Return ``(hand_timeline_off, raw_usb_off, has_stop_sha)`` from checkpoints.jsonl."""
    cp_path = bundle_dir / "checkpoints.jsonl"
    if not cp_path.is_file():
        return (False, False, False)
    hand_off = False
    raw_off = False
    has_sha = False
    for raw_line in cp_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            cp = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        q = cp.get("qorcap") if isinstance(cp, dict) else None
        if not isinstance(q, dict):
            continue
        if q.get("hand_timeline") is False:
            hand_off = True
        if q.get("raw_usb") is False:
            raw_off = True
        if isinstance(q.get("hand_timeline_sha256"), str):
            has_sha = True
    return (hand_off, raw_off, has_sha)


def resolve_hand_dir(
    bundle_dir: Path,
    *,
    allowed_roots: Sequence[Path] | None = None,
) -> tuple[Path, bool, list[str]]:
    """Resolve hand directory inside ``bundle_dir`` or confined ``hand_timeline_dir``."""
    base = bundle_dir.resolve()
    roots = [base, *(Path(r).resolve() for r in (allowed_roots or ()))]
    if (base / FILE_NAME).is_file():
        return (base, False, [])

    cp_path = base / "checkpoints.jsonl"
    if not cp_path.is_file():
        return (base, False, [])

    ptr: str | None = None
    bound = False
    for raw_line in cp_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            cp = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        q = cp.get("qorcap") if isinstance(cp, dict) else None
        if not isinstance(q, dict):
            continue
        if isinstance(q.get("hand_timeline_sha256"), str) or isinstance(q.get("hand_head"), str):
            bound = True
        raw_ptr = q.get("hand_timeline_dir")
        if isinstance(raw_ptr, str) and raw_ptr.strip():
            ptr = raw_ptr
            bound = True

    if ptr is None:
        return (base, bound, [])

    if _CONTROL_RE.search(ptr) or ptr.startswith("\\\\") or ".." in Path(ptr).parts:
        return (base, True, [f"hand_timeline_dir {ptr!r} is confined or contains traversal; refusing"])

    p = Path(ptr)
    target = (base / p) if not p.is_absolute() else p
    try:
        resolved = target.resolve()
    except OSError as exc:
        return (base, True, [f"hand_timeline_dir {ptr!r} cannot be resolved ({type(exc).__name__})"])

    if not _is_within(resolved, roots):
        return (base, True, [f"hand_timeline_dir {ptr!r} resolves outside allowed roots; refusing"])
    return (resolved, True, [])


def verify_hand_timeline_dir(
    bundle_dir: Path,
    *,
    allowed_roots: Sequence[Path] | None = None,
) -> HandTimelineCheck:
    """Read-only streaming verification of ``hand_timeline.v0.jsonl`` and ``hand_raw.v0.bin``."""
    hand_dir, _, resolve_errs = resolve_hand_dir(bundle_dir, allowed_roots=allowed_roots)
    if resolve_errs:
        return HandTimelineCheck(ok=False, hand_dir=hand_dir, errors=resolve_errs)

    tl_path = hand_dir / FILE_NAME
    if not tl_path.is_file():
        hand_off, _, has_sha = checkpoint_flags(bundle_dir)
        if hand_off and not has_sha:
            return HandTimelineCheck(ok=True, off=True, hand_dir=hand_dir)
        if hand_off and has_sha:
            return HandTimelineCheck(
                ok=False,
                off=True,
                hand_dir=hand_dir,
                errors=["checkpoints mark hand_timeline false but also bind a file that is missing"],
            )
        return HandTimelineCheck(
            ok=False,
            hand_dir=hand_dir,
            errors=[f"{FILE_NAME}: not found (looked in {hand_dir})"],
        )

    errors: list[str] = []
    warnings: list[str] = []
    heads: dict[int, str] = {}
    device_models: list[str] = []
    in_rows: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    file_hasher = hashlib.sha256()
    head = GENESIS
    chunks = 0
    fallback = False
    last_seq: int | None = None
    last_in_t: int | None = None

    raw_path = hand_dir / RAW_FILE
    raw_fh = None
    raw_len: int | None = None
    raw_open_err: str | None = None
    raw_missing = False
    raw_missing_reported = False
    raw_active_seen = False
    raw_off_only = False
    raw_slices = 0
    raw_records_count = 0
    expected_raw_offset = 0
    last_raw_t_us: int | None = None

    header_hand_dir: str | None = None
    header_source: str | None = None
    header_reason: Any = None
    header_raw_file: Any = None
    has_header_raw_file = False

    try:
        with tl_path.open("rb") as tfh:
            line_no = 0
            for raw_bytes in tfh:
                line_no += 1
                file_hasher.update(raw_bytes)
                if b"\r" in raw_bytes:
                    errors.append(f"line {line_no}: contains CR (LF required)")
                line_bytes = raw_bytes[:-1] if raw_bytes.endswith(b"\n") else raw_bytes
                if not line_bytes:
                    continue
                try:
                    line_str = line_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    return HandTimelineCheck(
                        ok=False,
                        hand_dir=hand_dir,
                        errors=[f"{FILE_NAME} is not UTF-8"],
                    )
                try:
                    obj = json.loads(line_str)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {line_no}: not JSON ({exc})")
                    continue
                if not isinstance(obj, dict):
                    errors.append(f"line {line_no}: record is not a JSON object")
                    continue
                try:
                    if canonicalize(obj).decode("utf-8") != line_str:
                        errors.append(f"line {line_no}: not in JCS form")
                except ValueError as exc:
                    errors.append(f"line {line_no}: {exc}")

                if obj.get("type") != "chunk":
                    if obj.get("type") == "in":
                        t_val = _t_of(obj)
                        if t_val is not None:
                            if last_in_t is not None and t_val < last_in_t:
                                errors.append(f"line {line_no}: in t_us goes backwards")
                            last_in_t = t_val
                        in_rows.append(obj)
                    pending.append(obj)
                    continue

                seq = obj.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                    errors.append(f"line {line_no}: chunk without seq")
                    pending.clear()
                    continue

                records = pending
                pending = []

                if last_seq is None:
                    first = records[0] if records else None
                    if not first or first.get("type") != "header" or first.get("schema") != SCHEMA:
                        errors.append(f"chunk {seq}: the first window must start with the {SCHEMA} header")
                    else:
                        if first.get("raw_layout") != RAW_LAYOUT or first.get("raw_record_bytes") != RAW_RECORD_BYTES:
                            errors.append(
                                f"chunk {seq}: unsupported raw layout {first.get('raw_layout')!r} "
                                f"({first.get('raw_record_bytes')} B), expected {RAW_LAYOUT!r} ({RAW_RECORD_BYTES} B)"
                            )
                        if isinstance(first.get("hand_dir"), str):
                            header_hand_dir = first["hand_dir"]
                        if isinstance(first.get("hand_dir_source"), str):
                            header_source = first["hand_dir_source"]
                        header_reason = first.get("hand_dir_fallback_reason")
                        if "raw_file" in first:
                            header_raw_file = first.get("raw_file")
                            has_header_raw_file = header_raw_file is not None
                elif seq != last_seq + 1:
                    errors.append(f"chunk {seq}: expected seq {last_seq + 1}")
                last_seq = seq

                lo = seq * CHUNK_US
                hi = (seq + 1) * CHUNK_US
                for rec in records:
                    if rec.get("chunk_seq") != seq:
                        errors.append(f"chunk {seq}: a {rec.get('type')} record names chunk_seq {rec.get('chunk_seq')}")
                    if rec.get("type") in ("in", "status", "frame", "profile_swap_candidate"):
                        t_val = _t_of(rec)
                        if t_val is not None:
                            is_late = rec.get("late") is True
                            if not is_late and not (lo <= t_val < hi):
                                errors.append(f"chunk {seq}: {rec.get('type')} at {t_val} is outside the window")
                            if is_late and t_val >= lo:
                                errors.append(f"chunk {seq}: a late record at {t_val} is not from an earlier window")

                try:
                    rec_sha = sha256_hex(records)
                except ValueError as exc:
                    errors.append(f"chunk {seq}: {exc}")
                    continue

                if obj.get("sha256") != rec_sha:
                    errors.append(f"chunk {seq}: records SHA-256 does not match")
                if obj.get("n") != len(records):
                    errors.append(f"chunk {seq}: n does not match the record count")
                if obj.get("prev") != head:
                    errors.append(f"chunk {seq}: prev does not match the previous head")
                next_head = sha256_hex({"prev": head, "seq": seq, "sha256": rec_sha})
                if obj.get("head") != next_head:
                    errors.append(f"chunk {seq}: head does not re-link")
                head = next_head
                heads[seq] = head
                chunks += 1

                if len(records) < 2:
                    errors.append(f"chunk {seq}: must end with hb then raw_bind")
                    continue
                hb, rb = records[-2], records[-1]
                if hb.get("type") != "hb" or rb.get("type") != "raw_bind":
                    errors.append(f"chunk {seq}: must end with hb then raw_bind")
                    continue
                if obj.get("device_model") != hb.get("device_model") or obj.get("fallback") != hb.get("fallback"):
                    errors.append(f"chunk {seq}: device_model differs from the sealed hb")
                if (
                    obj.get("raw_sha256") != rb.get("sha256")
                    or obj.get("raw_file") != rb.get("file")
                    or obj.get("raw_offset") != rb.get("offset")
                    or obj.get("raw_len") != rb.get("len")
                ):
                    errors.append(f"chunk {seq}: raw binding differs from the sealed raw_bind")

                hb_dm = hb.get("device_model")
                if isinstance(hb_dm, str) and hb_dm not in ("none", "UNKNOWN") and hb_dm not in device_models:
                    device_models.append(hb_dm)
                if hb.get("fallback") is True:
                    fallback = True

                for rec in records:
                    if rec.get("device_model") == "dualsense" and rec.get("type") == "in":
                        btns = rec.get("buttons") if isinstance(rec.get("buttons"), int) else 0
                        if rec.get("edge_bits") != UNMEASURED or (btns & EDGE_BITS_MASK) != 0:
                            errors.append(f"chunk {seq}: a dualsense in row carries Edge-only bits")
                if hb_dm == "dualsense" and any(r.get("type") == "profile_swap_candidate" for r in records):
                    errors.append(f"chunk {seq}: profile_swap_candidate on a standard DualSense")

                if rb.get("layout") != RAW_LAYOUT or rb.get("record_bytes") != RAW_RECORD_BYTES:
                    errors.append(
                        f"chunk {seq}: unsupported raw_bind layout {rb.get('layout')!r} "
                        f"({rb.get('record_bytes')} B), expected {RAW_LAYOUT!r} ({RAW_RECORD_BYTES} B)"
                    )
                    continue

                raw_usb_on = rb.get("raw_usb") is True
                nrep = rb.get("n_reports") if isinstance(rb.get("n_reports"), int) else 0
                if not raw_usb_on:
                    raw_off_only = True
                    if any(rb.get(k) is not None for k in ("file", "offset", "len", "sha256")):
                        errors.append(f"chunk {seq}: raw_usb is false but raw slice fields are not null")
                    continue
                if rb.get("paused") is not None and nrep == 0:
                    continue

                raw_active_seen = True
                rf_name = rb.get("file")
                if not isinstance(rf_name, str):
                    errors.append(f"chunk {seq}: raw on with no raw_file")
                    continue
                if rf_name != RAW_FILE:
                    errors.append(f"chunk {seq}: raw file name {rf_name!r} is not {RAW_FILE}")
                    continue
                offset = rb.get("offset")
                raw_slice_len = rb.get("len")
                want_sha = rb.get("sha256")
                if (
                    not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or offset < 0
                    or not isinstance(raw_slice_len, int)
                    or isinstance(raw_slice_len, bool)
                    or raw_slice_len < 0
                    or not isinstance(want_sha, str)
                ):
                    errors.append(f"chunk {seq}: raw_bind missing offset, len, or sha256")
                    continue
                if raw_slice_len % RAW_RECORD_BYTES != 0 or raw_slice_len != nrep * RAW_RECORD_BYTES or rb.get("bytes") != raw_slice_len:
                    errors.append(f"chunk {seq}: raw slice length {raw_slice_len} does not match n_reports x {RAW_RECORD_BYTES}")
                if offset < expected_raw_offset:
                    errors.append(f"chunk {seq}: raw_offset {offset} overlaps previous slice ending at {expected_raw_offset}")
                    continue
                if offset > expected_raw_offset:
                    errors.append(f"chunk {seq}: raw_offset {offset} leaves a gap after {expected_raw_offset}")
                    continue
                expected_raw_offset = offset + raw_slice_len

                if raw_fh is None and raw_open_err is None:
                    try:
                        raw_fh = raw_path.open("rb")
                        raw_len = raw_path.stat().st_size
                    except FileNotFoundError as exc:
                        raw_missing = True
                        raw_open_err = f"raw file {RAW_FILE} missing ({exc})"
                    except OSError as exc:
                        raw_open_err = f"raw file {RAW_FILE} unreadable ({exc})"

                if raw_open_err is not None:
                    if not raw_missing_reported:
                        errors.append(f"chunk {seq}: {raw_open_err}")
                        raw_missing_reported = True
                    continue

                assert raw_fh is not None and raw_len is not None
                end = offset + raw_slice_len
                if end > raw_len:
                    errors.append(f"chunk {seq}: {RAW_FILE} truncated (slice {offset}..{end} exceeds file length {raw_len})")
                    continue
                raw_slices += 1
                raw_fh.seek(offset)
                slice_bytes = raw_fh.read(raw_slice_len)
                if len(slice_bytes) != raw_slice_len:
                    errors.append(f"chunk {seq}: {RAW_FILE} short read at {offset}..{end}")
                    continue
                if hashlib.sha256(slice_bytes).hexdigest() != want_sha:
                    errors.append(f"chunk {seq}: raw slice SHA-256 does not match")

                if raw_slice_len > 0:
                    shas: set[str] = set()
                    t_back_reported = False
                    for idx in range(raw_slice_len // RAW_RECORD_BYTES):
                        rec_start = idx * RAW_RECORD_BYTES
                        rec_t_us = struct.unpack_from("<Q", slice_bytes, rec_start + 8)[0]
                        if not t_back_reported and last_raw_t_us is not None and rec_t_us < last_raw_t_us:
                            errors.append(
                                f"chunk {seq}: raw record {idx} t_us {rec_t_us} decreases below previous {last_raw_t_us}"
                            )
                            t_back_reported = True
                        last_raw_t_us = rec_t_us
                        shas.add(hashlib.sha256(slice_bytes[rec_start + 16 : rec_start + RAW_RECORD_BYTES]).hexdigest())
                        raw_records_count += 1
                    for rec in records:
                        if rec.get("type") == "in":
                            rsha = rec.get("raw_sha256")
                            if not isinstance(rsha, str) or rsha not in shas:
                                errors.append(f"chunk {seq}: an in row's raw_sha256 is not in the raw slice")
                                break
    finally:
        if raw_fh is not None:
            raw_fh.close()

    file_sha256 = file_hasher.hexdigest()

    raw_files = 0
    if raw_active_seen and raw_open_err is None and raw_len is not None:
        raw_files = 1
        if raw_len > expected_raw_offset:
            warnings.append(
                f"{RAW_FILE}: {raw_len - expected_raw_offset} trailing byte(s) after sealed offset "
                f"{expected_raw_offset} (UNMEASURED unsealed tail, not counted, never padded)"
            )
    elif raw_off_only and raw_path.exists():
        warnings.append(f"{RAW_FILE} exists although raw_usb is false")

    if pending:
        warnings.append(f"{len(pending)} record(s) after the last chunk are an unsealed tail (not counted)")

    sum_path = hand_dir / SUMMARY_FILE
    if sum_path.is_file():
        try:
            sv = json.loads(sum_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{SUMMARY_FILE}: not JSON ({exc})")
        else:
            if sv.get("sha256") != file_sha256:
                errors.append(f"{SUMMARY_FILE}: sha256 does not match {FILE_NAME}")
            if sv.get("head") != head:
                errors.append(f"{SUMMARY_FILE}: head does not match the last chunk")

    segment_bindings = 0
    stop_bound = False
    cp_path = bundle_dir / "checkpoints.jsonl"
    if cp_path.is_file():
        for raw_line in cp_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw_line.strip():
                continue
            try:
                cp = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            q = cp.get("qorcap") if isinstance(cp, dict) else None
            if not isinstance(q, dict):
                continue
            cp_seq = cp.get("seq")
            if isinstance(q.get("hand_head"), str):
                segment_bindings += 1
                cseq = q.get("hand_chunk_seq")
                if not isinstance(cseq, int) or heads.get(cseq) != q["hand_head"]:
                    errors.append(f"checkpoint {cp_seq}: hand_head does not match chunk {cseq}")
            if isinstance(q.get("hand_timeline_sha256"), str):
                stop_bound = True
                if q["hand_timeline_sha256"] != file_sha256:
                    errors.append(f"checkpoint {cp_seq}: hand_timeline_sha256 does not match {FILE_NAME}")
                if q.get("hand_timeline_head") != head:
                    errors.append(f"checkpoint {cp_seq}: hand_timeline_head does not match the last chunk")
                if isinstance(q.get("hand_timeline_dir_actual"), str) and header_hand_dir is not None:
                    if q["hand_timeline_dir_actual"] != header_hand_dir:
                        errors.append(f"checkpoint {cp_seq}: hand_timeline_dir_actual differs from sealed header")
                if isinstance(q.get("hand_dir_source"), str) and header_source is not None:
                    if q["hand_dir_source"] != header_source:
                        errors.append(f"checkpoint {cp_seq}: hand_dir_source differs from sealed header")
                if q.get("hand_dir_fallback_reason") is not None and q.get("hand_dir_fallback_reason") != header_reason:
                    errors.append(f"checkpoint {cp_seq}: hand_dir_fallback_reason differs from sealed header")
                if "hand_raw_file" in q and has_header_raw_file and q.get("hand_raw_file") != header_raw_file:
                    errors.append(f"checkpoint {cp_seq}: hand_raw_file differs from sealed header")

    return HandTimelineCheck(
        ok=not errors,
        off=False,
        chunks=chunks,
        head=head,
        file_sha256=file_sha256,
        device_models=device_models,
        fallback=fallback,
        raw_files=raw_files,
        raw_slices=raw_slices,
        raw_missing=raw_missing,
        segment_bindings=segment_bindings,
        stop_bound=stop_bound,
        hand_dir=hand_dir,
        hand_dir_source=header_source,
        hand_dir_fallback_reason=header_reason if isinstance(header_reason, str) else None,
        warnings=warnings,
        errors=errors,
        records=in_rows,
        raw_records_count=raw_records_count,
    )


def normalize_in_records(in_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project verified ``hand_timeline.v0.jsonl`` ``in`` rows into sanitized sidecar records."""
    out: list[dict[str, Any]] = []
    for row in in_rows:
        t_us = row.get("t_us")
        if not isinstance(t_us, int) or isinstance(t_us, bool):
            continue
        item: dict[str, Any] = {
            "type": "in",
            "t_us": t_us,
            "b": int(row.get("buttons") or 0),
            "lx": int(row.get("lx") if isinstance(row.get("lx"), int) else 128),
            "ly": int(row.get("ly") if isinstance(row.get("ly"), int) else 128),
            "rx": int(row.get("rx") if isinstance(row.get("rx"), int) else 128),
            "ry": int(row.get("ry") if isinstance(row.get("ry"), int) else 128),
            "l2": int(row.get("l2") or 0),
            "r2": int(row.get("r2") or 0),
        }
        hw = row.get("sensor_ts")
        if isinstance(hw, int) and not isinstance(hw, bool):
            item["hw"] = hw
        out.append(item)
    out.sort(key=lambda r: r["t_us"])
    return out
