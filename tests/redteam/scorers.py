"""Pluggable scorers for the aimbot red-team harness.

A scorer is a small object with ``name``, ``version``, ``score``, and ``identity``.
``identity().sha256`` covers the package version, the scorer and estimator version
strings, the canonical JSON of runtime settings, the scorer module, every in-repo
module that module reaches by import, the source of an injected estimator or
``core`` (outside the repo, recorded relative to the module root), and any
declared non-Python data files. A re-run of the same tree prints the same hash.
An edit to any of those files prints a different one. A file that cannot be read
is recorded as ``unhashed`` and is not dropped. An implementation whose file
cannot be resolved or read makes the identity ``unhashed``. An estimator sha256
is recorded only when the class object is ``OpticalFlowPanEstimator``, and then
it is recomputed from that module's source. A copied attribute, property,
``__module__``, or ``__qualname__`` is not trusted. An ``unhashed`` result is
not cached.

Two estimator plug-ins feed a scorer. Neither one is implemented here:

* ``CameraPanEstimator`` returns one bin per 100 ms:
  ``{mono_ms, yaw_rate, pitch_rate, confidence, no_claim}``.
* ``VideoEventEstimator`` returns events:
  ``{mono_ms, event_kind, confidence, no_claim}``.

Unreadable frames and unread events set ``no_claim`` and leave the measurements
null. The stubs ``NoClaimPanEstimator`` and ``NoClaimEventEstimator`` do that
and do not look at a picture. A scorer may compare either output with the
logged rows. Motion rule v3 uses neither; it counts controller rows.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import qor_receipt
from qor_receipt.review.media import input_energy_series
from qor_receipt.review.motion import score_motion
from qor_receipt.review.policy import PARTIAL_CHECK, PASS

PACKAGE_VERSION = qor_receipt.__version__
BIN_MS = 100
EVENT_MATCH_WINDOW_MS = 150
STICK_DEADZONE = 8
STICK_ACTION = "stick_action"
BUTTON_ACTION = "button_action"
EVENT_KINDS = frozenset({STICK_ACTION, BUTTON_ACTION})
LABELS = ("attack", "benign")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _motion_min_permille() -> int:
    """The shipped floor, read from ``qor_receipt/receipt.py`` without importing it.

    Importing that module also imports ``qor_receipt.trusted_referees``. This harness
    scores ``score_motion`` directly and does not load the trust root.
    """
    path = REPO_ROOT / "qor_receipt" / "receipt.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != "MOTION_MIN_PERMILLE":
                continue
            value = node.value.value if isinstance(node.value, ast.Constant) else None
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    raise RuntimeError("MOTION_MIN_PERMILLE is not an integer assignment in qor_receipt/receipt.py")


MOTION_MIN_PERMILLE = _motion_min_permille()


@dataclass(frozen=True)
class VariantSample:
    """One controller log plus the synthetic on-screen series it is scored against."""

    name: str
    label: str
    rows: list[dict[str, Any]]
    motion: dict[int, int]
    sample_segments: tuple[int, ...]
    duration_ms: int
    cohort: str = "honest-seal"
    events: tuple[VideoEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.label not in LABELS:
            raise ValueError(f"label must be attack or benign, got {self.label!r}")


@dataclass(frozen=True)
class Score:
    """``flagged`` is true (catch), false (clear), or None (abstain)."""

    flagged: bool | None
    detail: dict[str, Any]

    @property
    def decision(self) -> str:
        if self.flagged is None:
            return "abstain"
        return "flag" if self.flagged else "clear"


@dataclass(frozen=True)
class PanBin:
    """One 100 ms camera-pan bin. ``no_claim`` marks an unreadable frame."""

    mono_ms: int
    yaw_rate: float | None
    pitch_rate: float | None
    confidence: float | None
    no_claim: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mono_ms": self.mono_ms,
            "yaw_rate": self.yaw_rate,
            "pitch_rate": self.pitch_rate,
            "confidence": self.confidence,
            "no_claim": self.no_claim,
        }


@dataclass(frozen=True)
class VideoEvent:
    """One video-derived event. ``no_claim`` marks an event the estimator could not read.

    ``event_kind`` is game-agnostic. ``stick_action`` is a right-stick gesture
    (in Madden, a skill move or hit stick). ``button_action`` is a button press
    (in Madden, a user catch or any other button macro).
    """

    mono_ms: int
    event_kind: str | None
    confidence: float | None
    no_claim: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mono_ms": self.mono_ms,
            "event_kind": self.event_kind,
            "confidence": self.confidence,
            "no_claim": self.no_claim,
        }


def _finite_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _confidence(value: Any) -> float:
    number = _finite_number("confidence", value)
    if not 0.0 <= number <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return number


def validate_pan_bin(pan_bin: PanBin) -> None:
    """Reject a bin that does not match the estimator contract."""
    if isinstance(pan_bin.mono_ms, bool) or not isinstance(pan_bin.mono_ms, int):
        raise ValueError("mono_ms must be an integer")
    if pan_bin.mono_ms < 0 or pan_bin.mono_ms % BIN_MS != 0:
        raise ValueError("mono_ms must be a non-negative multiple of 100")
    if pan_bin.no_claim:
        if pan_bin.yaw_rate is not None or pan_bin.pitch_rate is not None or pan_bin.confidence is not None:
            raise ValueError("no_claim bins carry no yaw_rate, pitch_rate, or confidence")
        return
    _finite_number("yaw_rate", pan_bin.yaw_rate)
    _finite_number("pitch_rate", pan_bin.pitch_rate)
    _confidence(pan_bin.confidence)


def validate_video_event(event: VideoEvent) -> None:
    """Reject an event that does not match the estimator contract."""
    if isinstance(event.mono_ms, bool) or not isinstance(event.mono_ms, int) or event.mono_ms < 0:
        raise ValueError("mono_ms must be a non-negative integer")
    if event.no_claim:
        if event.event_kind is not None or event.confidence is not None:
            raise ValueError("no_claim events carry no event_kind or confidence")
        return
    if not isinstance(event.event_kind, str) or not event.event_kind:
        raise ValueError("event_kind must be a non-empty string when the event is claimed")
    _confidence(event.confidence)


@dataclass(frozen=True)
class ScorerIdentity:
    name: str
    version: str
    sha256: str
    package_version: str
    estimator_name: str | None = None
    estimator_version: str | None = None
    files: tuple[str, ...] = ()
    estimator_sha256: str | None = None


@dataclass(frozen=True)
class RateResult:
    """Catch rate and false-alarm rate over the variants a scorer actually decided.

    An abstention is neither a catch nor a false alarm. Both rates are None when
    that class has no decision (for example a no_claim stub).
    """

    scorer: str
    version: str
    sha256: str
    attacks: int
    benign: int
    true_positives: int
    false_negatives: int
    false_positives: int
    true_negatives: int
    abstained: int
    decided_attacks: int
    decided_benign: int
    catch_rate: float | None
    false_alarm_rate: float | None


def _resolve_module(name: str) -> Path | None:
    """Map an in-repo module name to a source file. Stdlib and third-party names return None."""
    parts = name.split(".")
    if not parts or parts[0] not in {"qor_receipt", "redteam"}:
        return None
    if parts[0] == "redteam":
        base = REPO_ROOT / "tests" / "redteam"
        rest = parts[1:]
    else:
        base = REPO_ROOT / "qor_receipt"
        rest = parts[1:]
    if not rest:
        init = base / "__init__.py"
        return init if init.is_file() else None
    file_py = base.joinpath(*rest).with_suffix(".py")
    if file_py.is_file():
        return file_py.resolve()
    init = base.joinpath(*rest, "__init__.py")
    if init.is_file():
        return init.resolve()
    return None


def _import_from_module(path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    rel = path.resolve().relative_to(REPO_ROOT)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        package = parts[:-1]
    else:
        package = parts[:-1]
    drop = node.level - 1
    if drop:
        package = package[:-drop]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package) if package else None


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_module(path, node)
            if not base:
                continue
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return names


def transitive_files(start: Sequence[Path]) -> tuple[Path, ...]:
    """In-repo source files reachable from ``start``, sorted by repo-relative path.

    A start file outside the repo is read so its in-repo imports are followed.
    The outside file itself is not part of this tuple.
    """
    seen: set[Path] = set()
    queue = [path.resolve() for path in start]
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            names = _imported_names(path)
        except (OSError, SyntaxError):
            continue
        for name in names:
            resolved = _resolve_module(name)
            if resolved is not None:
                queue.append(resolved)
    in_repo: list[Path] = []
    for path in seen:
        try:
            path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        in_repo.append(path)
    return tuple(sorted(in_repo, key=lambda item: item.relative_to(REPO_ROOT).as_posix()))


def _safe_label(label: str) -> str:
    """A path label safe to store. Newlines and absolute paths become ``unhashed``."""
    if not label or any(ch in label for ch in "\n\r") or label.startswith(("/", "\\")):
        return "unhashed"
    return label


def hash_file_set(
    start: Sequence[Path],
    prefix: Sequence[str],
    extras: Sequence[tuple[str, bytes | None]] = (),
) -> tuple[str, tuple[str, ...]]:
    """SHA-256 of ``prefix`` plus each file's recorded path and bytes.

    In-repo files are recorded repo-relative. ``extras`` are ``(label, bytes)``
    for sources outside the repo and for declared data files. A ``None`` blob
    records that label as ``unhashed`` instead of dropping it.
    """
    files = transitive_files(start)
    items: list[tuple[str, str]] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            token = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            token = "unhashed"
        items.append((rel, token))
    for label, blob in extras:
        rel = _safe_label(label)
        token = "unhashed" if blob is None else hashlib.sha256(blob).hexdigest()
        items.append((rel, token))
    items.sort()
    lines = list(prefix)
    rels: list[str] = []
    for rel, token in items:
        rels.append(rel if token != "unhashed" else f"{rel} unhashed")
        lines.append(f"{rel} {token}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(), tuple(rels)


def _own_functions(cls: type) -> list[Any]:
    """Functions defined on ``cls`` itself, including properties."""
    found: list[Any] = []
    for value in cls.__dict__.values():
        if isinstance(value, (staticmethod, classmethod)):
            found.append(value.__func__)
        elif isinstance(value, property):
            found.extend(fn for fn in (value.fget, value.fset, value.fdel) if fn is not None)
        elif inspect.isfunction(value):
            found.append(value)
    return found


def _code_path(fn: Any) -> Path | None:
    code = getattr(fn, "__code__", None)
    filename = getattr(code, "co_filename", None)
    if type(filename) is not str or not filename or filename.startswith("<"):
        return None
    try:
        return Path(filename).resolve()
    except OSError:
        return None


def _probed_path(cls: type) -> Path | None:
    """File ``getsourcefile`` names. This follows ``__module__`` and can be spoofed."""
    src: str | None = None
    try:
        src = inspect.getsourcefile(cls)
    except (TypeError, OSError):
        src = None
    if not src:
        module = sys.modules.get(getattr(cls, "__module__", None))
        file_attr = getattr(module, "__file__", None) if module is not None else None
        src = file_attr if isinstance(file_attr, str) else None
    if not src:
        return None
    try:
        return Path(src).resolve()
    except OSError:
        return None


def _module_holds_class(cls: type) -> bool:
    """True when ``cls`` is the object stored on its module under ``__qualname__``."""
    module_name = getattr(cls, "__module__", None)
    qual = getattr(cls, "__qualname__", None)
    if type(module_name) is not str or type(qual) is not str or not qual or "<locals>" in qual:
        return False
    module = sys.modules.get(module_name)
    if module is None:
        return False
    obj: Any = module
    for part in qual.split("."):
        if not part.isidentifier():
            return False
        obj = getattr(obj, part, None)
        if obj is None:
            return False
    return obj is cls


def _path_for_class(cls: type) -> Path | None:
    """Source file of ``cls``.

    A function's code object wins over ``__module__``. A class with no code of
    its own may use the module file only when that module does not already hold
    a different class under the same leaf name. Copying ``__module__`` and
    ``__qualname__`` from ``OpticalFlowPanEstimator`` therefore does not select
    that class's file.
    """
    code_paths: set[Path] = set()
    for fn in _own_functions(cls):
        path = _code_path(fn)
        if path is not None:
            code_paths.add(path)
    if len(code_paths) > 1:
        return None
    if len(code_paths) == 1:
        return next(iter(code_paths))
    probed = _probed_path(cls)
    if probed is None:
        return None
    qual = getattr(cls, "__qualname__", "")
    leaf = qual.split(".")[-1] if type(qual) is str else ""
    module = sys.modules.get(getattr(cls, "__module__", None))
    holder = getattr(module, leaf, None) if module is not None and type(leaf) is str and leaf.isidentifier() else None
    if isinstance(holder, type) and holder is not cls:
        return None
    return probed


def _class_path(obj: Any) -> Path | None:
    """Source file of ``type(obj)``. See ``_path_for_class``."""
    return _path_for_class(type(obj))


def _outside_rel(path: Path, module_name: str) -> str:
    """``path`` relative to the directory that contains its top-level module."""
    parts = [part for part in module_name.split(".") if part]
    if not parts:
        return path.name
    climb = len(parts) + (path.name == "__init__.py")
    root = path
    for _ in range(climb):
        if root.parent == root:
            return path.name
        root = root.parent
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _source_rel(path: Path, obj: Any) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return _outside_rel(path, getattr(type(obj), "__module__", "") or "")


_THRESHOLD_NAMES = (
    "min_confidence",
    "min_claimed_bins",
    "min_claimed",
    "rate_floor",
    "min_disagree_bins",
    "min_unmatched",
    "window_ms",
    "stick_deadzone",
)
_PAN_THRESHOLDS = frozenset({"min_claimed_bins", "min_confidence", "rate_floor", "min_disagree_bins", "stick_deadzone"})
_EVENT_THRESHOLDS = frozenset({"min_claimed", "min_confidence", "min_unmatched", "window_ms", "stick_deadzone"})


def _unhashed_identity(scorer: Any, estimator: Any | None, files: Sequence[str] = ()) -> ScorerIdentity:
    """An identity that records ``unhashed`` and does not carry an attacker-controlled string."""
    marked = [_safe_label(item) for item in files]
    if not any(item == "unhashed" or item.endswith(" unhashed") for item in marked):
        marked.append("unhashed")

    def plain(value: Any) -> str | None:
        if type(value) is not str or "\n" in value or "\r" in value:
            return None
        return value

    return ScorerIdentity(
        name=plain(getattr(scorer, "name", None)) or "",
        version=plain(getattr(scorer, "version", None)) or "",
        sha256="unhashed",
        package_version=PACKAGE_VERSION,
        estimator_name=plain(getattr(estimator, "name", None)) if estimator is not None else None,
        estimator_version=plain(getattr(estimator, "version", None)) if estimator is not None else None,
        files=tuple(marked),
        estimator_sha256=None,
    )


def _recomputed_pan_sha(obj: Any) -> str | None:
    """Sha of the real pan-estimator class, recomputed from source. Never an attribute.

    ``cls is OpticalFlowPanEstimator`` (the class object in that module). A
    property, a copied string, or a class that only copies ``__module__`` and
    ``__qualname__`` does not qualify. ``params`` must be the real ``PanParams``
    type, and each field must be an exact ``int`` or ``float`` (the same check
    ``estimator_known`` uses). The digest is ``estimator_sha256(params)`` from the
    module, not ``obj.sha256``. A rejected instance (extra attribute, replaced
    ``_pair``, replaced ``_mask_cache``, an ``int`` subclass, or a ``bool``) does
    not record a digest, including the allowlisted hash. The allowlist itself is
    not required: non-default ``PanParams`` with exact field types still hash.
    """
    mod = sys.modules.get("qor_receipt.vision.pan_estimator")
    if mod is None or obj is None:
        return None
    cls = getattr(mod, "OpticalFlowPanEstimator", None)
    params_cls = getattr(mod, "PanParams", None)
    fn = getattr(mod, "estimator_sha256", None)
    instance_ok = getattr(mod, "_estimator_instance_ok", None)
    fields_exact = getattr(mod, "_params_field_types_exact", None)
    if (
        not isinstance(cls, type)
        or not isinstance(params_cls, type)
        or not callable(fn)
        or not callable(instance_ok)
        or not callable(fields_exact)
    ):
        return None
    if type(obj) is not cls or type(getattr(obj, "params", None)) is not params_cls:
        return None
    try:
        if instance_ok(obj) is not True or fields_exact(obj.params) is not True:
            return None
        digest = fn(obj.params)
    except Exception:
        return None
    if type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    return digest


def _trusted_estimator_sha(estimator: Any | None) -> str | None:
    """Recomputed pan-estimator sha, or None. Attributes on ``estimator`` are ignored."""
    if estimator is None:
        return None
    for obj in (estimator, getattr(estimator, "core", None)):
        digest = _recomputed_pan_sha(obj)
        if digest is not None:
            return digest
    return None


def _class_block(source: str, qualname: str) -> str | None:
    """The ``class`` block for the last component of ``qualname``, read from the file text."""
    name = qualname.split(".")[-1]
    if not name.isidentifier():
        return None
    lines = source.splitlines(keepends=True)
    start = None
    indent = 0
    prefix = f"class {name}"
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(prefix):
            continue
        rest = stripped[len(prefix):]
        if rest[:1] not in ("", "(", ":", " "):
            continue
        start = index
        indent = len(line) - len(stripped)
        break
    if start is None:
        return None
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        block.append(line)
    return "".join(block)


def _enclosing_class_block(source: str, lineno: int) -> str | None:
    """Class block that contains the 1-based ``lineno``, found by indentation."""
    lines = source.splitlines(keepends=True)
    if lineno < 1 or lineno > len(lines):
        return None
    start = None
    indent = 0
    target = lines[lineno - 1]
    target_indent = len(target) - len(target.lstrip())
    for index in range(lineno - 1, -1, -1):
        raw = lines[index]
        stripped = raw.lstrip()
        if not stripped.startswith("class "):
            continue
        name = stripped[6:].split("(", 1)[0].split(":", 1)[0].strip()
        if not name.isidentifier():
            continue
        this_indent = len(raw) - len(stripped)
        if index < lineno - 1 and target.strip() and this_indent >= target_indent:
            continue
        start = index
        indent = this_indent
        break
    if start is None:
        return None
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        block.append(line)
    return "".join(block)


def _encode_cell(value: Any) -> bytes | None:
    """Deterministic bytes for a closure cell or a plain attribute. None if it is not safe to hash."""
    if value is None or type(value) is bool or type(value) is int:
        return repr(value).encode("ascii")
    if type(value) is float and math.isfinite(value):
        return repr(value).encode("ascii")
    if type(value) is str:
        raw = value.encode("utf-8", "surrogateescape")
        return b"s" + str(len(raw)).encode("ascii") + b":" + raw
    if type(value) is bytes:
        return b"b" + str(len(value)).encode("ascii") + b":" + value
    if type(value) is tuple:
        parts: list[bytes] = []
        for item in value:
            encoded = _encode_cell(item)
            if encoded is None:
                return None
            parts.append(encoded)
        return b"t" + b",".join(parts)
    return None


def _closure_functions(cls: type) -> list[Any]:
    """Functions on ``cls`` that close over a cell, including properties."""
    found: list[Any] = []
    for base in cls.__mro__:
        if base is object:
            continue
        for value in base.__dict__.values():
            funcs: list[Any] = []
            if isinstance(value, (staticmethod, classmethod)):
                funcs.append(value.__func__)
            elif isinstance(value, property):
                funcs.extend(fn for fn in (value.fget, value.fset, value.fdel) if fn is not None)
            elif inspect.isfunction(value):
                funcs.append(value)
            for fn in funcs:
                if getattr(fn, "__closure__", None):
                    found.append(fn)
    return found


def _closure_material(cls: type) -> bytes:
    """Bytes that differ when two classes from the same source close over different values.

    Encodable cells contribute their contents. A cell that cannot be encoded contributes
    its identity, so two classes still do not hash the same.
    """
    parts: list[bytes] = []
    for fn in _closure_functions(cls):
        closure = fn.__closure__ or ()
        for index, cell in enumerate(closure):
            try:
                value = cell.cell_contents
            except ValueError:
                parts.append(f"empty:{id(cell)}".encode("ascii"))
                continue
            encoded = _encode_cell(value)
            if encoded is None:
                encoded = f"opaque:{type(value).__name__}:{id(cell)}".encode("ascii", "replace")
            parts.append(f"{fn.__name__}:{index}:".encode("ascii", "replace") + encoded)
    return b"\n".join(parts)


def _class_body(cls: type) -> str:
    """Class block of ``cls`` from the file its own code names, not ``inspect.getsource``."""
    path = _path_for_class(cls)
    if path is None:
        return ""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    qual = cls.__qualname__ if type(getattr(cls, "__qualname__", None)) is str else ""
    linenos = [
        fn.__code__.co_firstlineno
        for fn in _own_functions(cls)
        if getattr(fn, "__code__", None) is not None and type(getattr(fn.__code__, "co_firstlineno", None)) is int
    ]
    found: str | None = None
    if source and linenos:
        found = _enclosing_class_block(source, min(linenos))
    if found is None and source and qual and _module_holds_class(cls):
        found = _class_block(source, qual)
    if found is None and source and qual:
        leaf = qual.split(".")[-1]
        module_obj = sys.modules.get(getattr(cls, "__module__", None))
        holder = getattr(module_obj, leaf, None) if module_obj is not None else None
        if not (isinstance(holder, type) and holder is not cls):
            found = _class_block(source, qual)
    return found or ""


def _inherited_source(cls: type) -> bytes:
    """Class block of each base, so same-named subclasses of different bases do not match.

    The block is read from the file the base's own functions name. ``object`` is
    skipped, so a class that inherits nothing keeps the same payload as before.
    """
    parts: list[bytes] = []
    for base in cls.__mro__[1:]:
        if base is object:
            continue
        qual = base.__qualname__ if type(getattr(base, "__qualname__", None)) is str else getattr(base, "__name__", "")
        if type(qual) is not str:
            qual = ""
        parts.append(f"{qual}\n{_class_body(base)}".encode("utf-8", "replace"))
    return b"\n".join(parts)


def _class_source_tag(obj: Any) -> str:
    """Bound class name plus a hash of that class's own source and its bases.

    The module name is published only when the module actually holds this class
    object. Otherwise the label is the file the code object (or the defining
    file) names, so a copied ``__module__`` / ``__qualname__`` is not the label
    of ``OpticalFlowPanEstimator``.
    """
    cls = type(obj)
    path = _class_path(obj)
    held = _module_holds_class(cls)
    qual = cls.__qualname__ if type(getattr(cls, "__qualname__", None)) is str else ""
    if held:
        module = cls.__module__ if type(cls.__module__) is str else ""
    else:
        qual = cls.__name__ if type(getattr(cls, "__name__", None)) is str else qual.split(".")[-1]
        module = ""
        if path is not None:
            try:
                module = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                module = path.name
    label = f"{module}.{qual}" if module else qual
    if not label or any(ch in label for ch in "\n\r"):
        label = "unhashed"
    text: str | None = None
    source: str | None = None
    if path is not None:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            source = None
    linenos = [
        fn.__code__.co_firstlineno
        for fn in _own_functions(cls)
        if getattr(fn, "__code__", None) is not None and type(getattr(fn.__code__, "co_firstlineno", None)) is int
    ]
    if source and held and qual:
        text = _class_block(source, qual)
    elif source and linenos:
        text = _enclosing_class_block(source, min(linenos))
    elif source and qual and path is not None and not held:
        leaf = qual.split(".")[-1]
        module_obj = sys.modules.get(getattr(cls, "__module__", None))
        holder = getattr(module_obj, leaf, None) if module_obj is not None else None
        if not (isinstance(holder, type) and holder is not cls):
            text = _class_block(source, qual)
    if not text and held:
        try:
            found = inspect.getsource(cls)
        except (OSError, TypeError):
            found = None
        text = found if type(found) is str and found else None
    closed = _closure_material(cls)
    payload = (text or "").encode("utf-8")
    if closed:
        payload = payload + b"\n" + closed
    inherited = _inherited_source(cls)
    if inherited:
        payload = payload + b"\ninherit\n" + inherited
    if not payload:
        return f"{label} unhashed"
    digest = hashlib.sha256(payload).hexdigest()
    return f"{label} {digest}"


def _jsonable(value: Any) -> Any:
    if type(value) is str:
        if "\n" in value or "\r" in value:
            raise TypeError
        return value
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise TypeError


def _settings_json(scorer: Any) -> str:
    """Canonical JSON of the thresholds that change this scorer's output."""
    names = getattr(scorer, "identity_settings", None)
    if names is None:
        names = _THRESHOLD_NAMES
    payload: dict[str, Any] = {}
    for name in names:
        if type(name) is not str or not name.isidentifier() or not hasattr(scorer, name):
            if type(name) is not str or not name.isidentifier():
                raise TypeError
            continue
        payload[name] = _jsonable(getattr(scorer, name))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _apply_thresholds(obj: Any, thresholds: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = [key for key in thresholds if key not in allowed]
    if unknown:
        raise TypeError(f"unknown threshold {unknown[0]}")
    for key, value in thresholds.items():
        setattr(obj, key, value)


def _identity_objects(scorer: Any, estimator: Any | None) -> list[Any]:
    """Scorer, estimator, and an injected ``core`` or ``estimator`` implementation."""
    objects: list[Any] = []

    def add(obj: Any) -> None:
        if obj is None or any(obj is seen for seen in objects):
            return
        objects.append(obj)

    add(scorer)
    add(estimator)
    add(getattr(scorer, "estimator", None))
    add(getattr(scorer, "core", None))
    if estimator is not None:
        add(getattr(estimator, "core", None))
    return objects


def _declared_paths(obj: Any) -> list[Path]:
    raw = getattr(obj, "data_files", None)
    if raw is None:
        return []
    if isinstance(raw, (str, Path)):
        return [Path(raw)]
    if isinstance(raw, (bytes, bytearray)) or not isinstance(raw, Sequence):
        raise TypeError
    return [Path(item) for item in raw]


def _data_label(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _data_extras(scorer: Any, estimator: Any | None) -> tuple[list[tuple[str, bytes]], list[str]]:
    extras: list[tuple[str, bytes]] = []
    missing: list[str] = []
    for obj in (scorer, estimator):
        if obj is None:
            continue
        for path in _declared_paths(obj):
            label = _safe_label(_data_label(path))
            try:
                extras.append((label, path.read_bytes()))
            except OSError:
                missing.append("unhashed" if label == "unhashed" else f"{label} unhashed")
    return extras, missing


def source_identity(scorer: Any, estimator: Any | None = None) -> ScorerIdentity:
    """Identity of one scorer: package version, version strings, settings, and source files."""
    resolved: list[tuple[Path, Any]] = []
    missing: list[str] = []
    for obj in _identity_objects(scorer, estimator):
        path = _class_path(obj)
        if path is None:
            missing.append("unhashed")
            continue
        label = _safe_label(_source_rel(path, obj))
        try:
            path.read_bytes()
        except OSError:
            missing.append("unhashed" if label == "unhashed" else f"{label} unhashed")
            continue
        resolved.append((path, obj))
    if missing:
        return _unhashed_identity(scorer, estimator, missing)

    reported = _trusted_estimator_sha(estimator)
    labels = [_safe_label(_source_rel(path, obj)) for path, obj in resolved]
    try:
        settings = _settings_json(scorer)
        data_extras, data_missing = _data_extras(scorer, estimator)
    except (TypeError, ValueError):
        return _unhashed_identity(scorer, estimator, labels)
    if data_missing:
        return _unhashed_identity(scorer, estimator, data_missing)

    scorer_name = getattr(scorer, "name", "")
    scorer_version = getattr(scorer, "version", "")
    estimator_name = "" if estimator is None else getattr(estimator, "name", "")
    estimator_version = "" if estimator is None else getattr(estimator, "version", "")
    texts = (scorer_name, scorer_version, estimator_name, estimator_version)
    if any(type(text) is not str or "\n" in text or "\r" in text for text in texts):
        return _unhashed_identity(scorer, estimator, labels)

    prefix = [
        f"qor-receipt=={PACKAGE_VERSION}",
        f"scorer={scorer_name}",
        f"version={scorer_version}",
        f"settings={settings}",
        f"estimator={estimator_name}",
        f"estimator_version={estimator_version}",
    ]
    for tag in sorted({_class_source_tag(obj) for obj in _identity_objects(scorer, estimator)}):
        prefix.append(f"class={tag}")
    # The pan estimator's sha joins the prefix only when the class object is
    # OpticalFlowPanEstimator and the digest was recomputed from source.
    # Attributes are not read.
    if type(reported) is str:
        prefix.append(f"estimator_sha256={reported}")
    outside: list[tuple[str, bytes | None]] = []
    seen: set[Path] = set()
    for path, obj in resolved:
        if path in seen:
            continue
        seen.add(path)
        try:
            path.relative_to(REPO_ROOT)
        except ValueError:
            try:
                outside.append((_safe_label(_source_rel(path, obj)), path.read_bytes()))
            except OSError:
                return _unhashed_identity(scorer, estimator, labels)
    digest, rels = hash_file_set([path for path, _obj in resolved], prefix, (*outside, *data_extras))
    return ScorerIdentity(
        name=scorer_name,
        version=scorer_version,
        sha256=digest,
        package_version=PACKAGE_VERSION,
        estimator_name=None if estimator is None else estimator_name,
        estimator_version=None if estimator is None else estimator_version,
        files=rels,
        estimator_sha256=reported if type(reported) is str else None,
    )


def _callable_path(fn: Any) -> str:
    try:
        src = inspect.getsourcefile(fn) or ""
    except (TypeError, OSError):
        src = ""
    if not src:
        code = getattr(fn, "__code__", None)
        src = getattr(code, "co_filename", "") if code is not None else ""
    if not isinstance(src, str) or not src:
        return "unhashed"
    path = Path(src)
    try:
        return _safe_label(path.resolve().relative_to(REPO_ROOT).as_posix())
    except (OSError, ValueError):
        return _safe_label(path.name)


def _lp(blob: bytes) -> bytes:
    """Length-prefixed blob, so ``b'a\\nb'`` does not join into ``b'a' + b'b'``."""
    return str(len(blob)).encode("ascii") + b":" + blob


def _pixel_bytes(item: Any) -> bytes | None:
    """Raw bytes of an array-like frame, including shape and dtype."""
    tobytes = getattr(item, "tobytes", None)
    if not callable(tobytes):
        return None
    try:
        blob = tobytes()
    except Exception:
        return None
    if type(blob) is not bytes:
        return None
    shape = getattr(item, "shape", ())
    dtype = getattr(item, "dtype", "")
    header = f"{shape!r}|{dtype!s}".encode("ascii", "replace")
    return _lp(header) + _lp(blob)


def _typed_chunk(item: Any, body: bytes) -> bytes:
    """Length-prefixed chunk with a type tag, so ``bytes`` and ``bytearray`` do not collide."""
    tag = type(item).__name__.encode("ascii", "replace")
    return _lp(tag + b":" + body)


def _frame_material(frames: Sequence[Any]) -> bytes:
    chunks: list[bytes] = []
    for item in frames:
        if isinstance(item, (bytes, bytearray, memoryview)):
            chunks.append(_typed_chunk(item, bytes(item)))
            continue
        pixels = _pixel_bytes(item)
        if pixels is not None:
            chunks.append(_typed_chunk(item, pixels))
            continue
        if isinstance(item, (tuple, list)) and len(item) == 2:
            inner = item[1]
            body = _pixel_bytes(inner)
            if body is None and isinstance(inner, (bytes, bytearray, memoryview)):
                body = bytes(inner)
            if body is not None:
                mono = str(item[0]).encode("utf-8", "replace")
                chunks.append(_typed_chunk(item, _lp(mono) + _typed_chunk(inner, body)))
                continue
        path = item if isinstance(item, (str, Path)) else getattr(item, "path", None)
        if isinstance(path, (str, Path)) and Path(path).is_file():
            try:
                chunks.append(_typed_chunk(item, Path(path).read_bytes()))
                continue
            except OSError:
                chunks.append(_typed_chunk(item, b"unhashed"))
                continue
        chunks.append(_typed_chunk(item, repr(item).encode("utf-8", "replace")))
    return b"".join(chunks)


def _callable_tag(value: Any) -> bytes:
    """Stable label for an instance-level function, without hashing ``co_code``."""
    func = getattr(value, "__func__", value)
    code = getattr(func, "__code__", None)
    if code is None:
        return type(value).__name__.encode("ascii", "replace")
    filename = getattr(code, "co_filename", "")
    lineno = getattr(code, "co_firstlineno", 0)
    name = getattr(code, "co_name", "")
    return f"{name}:{lineno}:{filename}".encode("utf-8", "replace")


def _instance_state(obj: Any, seen: set[int] | None = None, depth: int = 0) -> bytes:
    """Public instance attributes, so a mutation after the first estimate changes the cache key."""
    if obj is None or depth > 3:
        return b""
    encoded = _encode_cell(obj)
    if encoded is not None:
        return encoded
    if seen is None:
        seen = set()
    if id(obj) in seen:
        return b"cycle"
    seen.add(id(obj))
    data = getattr(obj, "__dict__", None)
    if not isinstance(data, dict):
        return f"id:{id(obj)}".encode("ascii")
    parts: list[bytes] = []
    for key in sorted(data):
        if type(key) is not str or key.startswith("_"):
            continue
        value = data[key]
        if callable(value):
            parts.append(key.encode("utf-8", "replace") + b"=fn:" + _callable_tag(value))
            continue
        parts.append(key.encode("utf-8", "replace") + b"=" + _instance_state(value, seen, depth + 1))
    return b"\n".join(parts)


def frame_source_identity(frame_source: Any, frames: Sequence[Any]) -> str:
    """Path of the frame source plus a content hash of the frames it produced."""
    digest = hashlib.sha256(_frame_material(frames)).hexdigest()
    return f"{_callable_path(frame_source)} {digest}"


def estimate_cache_key(scorer: Any, estimator: Any | None, frame_source: Any, frames: Sequence[Any]) -> tuple[str, str, str] | None:
    """Key by the full scorer identity, the frame-source identity, and instance state.

    The identity covers the core class source, inherited base source, closure
    cells, and the scorer settings. The frame half is the source path plus a hash
    of the frame bytes (shape, dtype, and the chunk's type tag, each chunk
    length-prefixed). Instance state is the public attributes, including
    instance-level functions, so mutating one or replacing a method after the
    first estimate misses.
    Returns None when the identity is ``unhashed``: two unresolvable cores must
    not share a stored result.
    """
    ident = source_identity(scorer, estimator)
    if ident.sha256 == "unhashed":
        return None
    state = hashlib.sha256(_instance_state(scorer) + b"\n" + _instance_state(estimator)).hexdigest()
    return ident.sha256, frame_source_identity(frame_source, frames), state


class EstimateCache:
    """Per-instance store of estimate results.

    There is no class-level cache. A second scorer cannot read the first scorer's
    bins, even when a caller picks the same short cache key for both.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[Any, ...], tuple[Any, ...]] = {}

    def get(self, key: tuple[Any, ...]) -> tuple[Any, ...] | None:
        hit = self._store.get(key)
        return None if hit is None else tuple(hit)

    def put(self, key: tuple[Any, ...], value: Sequence[Any]) -> None:
        self._store[key] = tuple(value)


def motion_check_state(permille: int | None) -> str:
    """Same comparison ``Referee.review`` uses for the motion check.

    ``pass`` when the permille is at or above ``MOTION_MIN_PERMILLE``, otherwise
    ``partial``. This harness does not call the referee. A partial motion check
    is not a FAIL.
    """
    if permille is not None and permille >= MOTION_MIN_PERMILLE:
        return PASS
    return PARTIAL_CHECK


class _InputLog:
    """``input_energy_series`` only reads ``.inputs``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.inputs = rows


@runtime_checkable
class Scorer(Protocol):
    name: str
    version: str

    def score(self, sample: VariantSample) -> Score:
        """Return a flag, a clear, or an abstention for one variant."""

    def identity(self) -> ScorerIdentity:
        """Package version plus a hash of the resolved implementation."""


@runtime_checkable
class CameraPanEstimator(Protocol):
    """Plug-in for a future camera-pan estimator. This protocol does not compute yaw or pitch."""

    name: str
    version: str

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        """One bin per 100 ms. Unreadable frames are ``no_claim`` with null rates."""


@runtime_checkable
class VideoEventEstimator(Protocol):
    """Plug-in for a future video-event estimator. This protocol does not detect events."""

    name: str
    version: str

    def estimate(self, sample: VariantSample) -> list[VideoEvent]:
        """Events the picture suggests. Unread events are ``no_claim``."""


class MotionRuleV3Scorer:
    """Shipped motion rule: controller-row rate versus frame-difference energy.

    The score comes from ``input_energy_series`` and ``score_motion``. Row counts
    are the controller signal. Stick axes and button bits are not an input.
    """

    name = "motion-rule-v3"
    version = "motion-rule/v3"

    def score(self, sample: VariantSample) -> Score:
        energy = input_energy_series(_InputLog(sample.rows))
        permille, detail = score_motion(energy, sample.motion, sample_segments=list(sample.sample_segments))
        state = motion_check_state(permille)
        return Score(flagged=state == PARTIAL_CHECK, detail={
            "motion_permille": permille,
            "motion_check": state,
            "bins": detail.get("bins"),
            "rule": detail.get("rule"),
            "basis": detail.get("basis"),
            "excluded_total": (detail.get("excluded") or {}).get("total"),
        })

    def identity(self) -> ScorerIdentity:
        return source_identity(self)


class NoClaimPanEstimator:
    """Stub pan estimator. Every frame is unreadable. It does not estimate yaw or pitch."""

    name = "example-no-claim"
    version = "example-no-claim/v0"

    def estimate(self, sample: VariantSample) -> list[PanBin]:
        return [
            PanBin(mono_ms=t, yaw_rate=None, pitch_rate=None, confidence=None, no_claim=True)
            for t in range(0, sample.duration_ms, BIN_MS)
        ]


class NoClaimEventEstimator:
    """Stub event estimator. Every second is unreadable. It does not detect events."""

    name = "example-no-claim-events"
    version = "example-no-claim-events/v0"

    def estimate(self, sample: VariantSample) -> list[VideoEvent]:
        return [
            VideoEvent(mono_ms=t, event_kind=None, confidence=None, no_claim=True)
            for t in range(0, sample.duration_ms, 1000)
        ]


def right_stick_idle(rows: Sequence[Mapping[str, Any]], start_ms: int, end_ms: int, deadzone: int) -> bool:
    """True when every right-stick sample in ``[start_ms, end_ms)`` is inside the deadzone.

    A window with no rows is idle. ``deadzone`` matches the agent's stick decoder
    (centered at 128). This is example-scorer policy, not a verification threshold.
    """
    for row in rows:
        if row.get("type") != "in" or not isinstance(row.get("t_us"), int):
            continue
        t_ms = int(row["t_us"]) // 1000
        if t_ms < start_ms or t_ms >= end_ms:
            continue
        if abs(int(row.get("rx", 128)) - 128) > deadzone or abs(int(row.get("ry", 128)) - 128) > deadzone:
            return False
    return True


def logged_press_matches(rows: Sequence[Mapping[str, Any]], mono_ms: int, event_kind: str,
                         window_ms: int, deadzone: int) -> bool:
    """True when the log has the press a claimed video event would require.

    ``stick_action`` matches an off-center right stick. ``button_action`` matches
    a non-zero button mask. The window is inclusive. Other kinds do not match.
    """
    lo, hi = mono_ms - window_ms, mono_ms + window_ms
    for row in rows:
        if row.get("type") != "in" or not isinstance(row.get("t_us"), int):
            continue
        t_ms = int(row["t_us"]) // 1000
        if t_ms < lo or t_ms > hi:
            continue
        if event_kind == STICK_ACTION:
            rx = abs(int(row.get("rx", 128)) - 128)
            ry = abs(int(row.get("ry", 128)) - 128)
            if rx > deadzone or ry > deadzone:
                return True
        elif event_kind == BUTTON_ACTION and int(row.get("b") or 0) != 0:
            return True
    return False


class PanAgreementScorer:
    """Example scorer over a camera-pan estimator's 100 ms bins.

    A claimed bin disagrees when the estimator reports a camera rate with enough
    confidence and the logged right stick in that same 100 ms is idle. The scorer
    flags the variant when those disagreements are at least ``min_disagree_bins``
    and at least half of the claimed bins. Fewer than ``min_claimed_bins``
    readable frames is an abstention, which is how the no_claim stub scores.

    The numbers below belong to this example. They are not ``MOTION_MIN_PERMILLE``
    and the verifier does not read them.
    """

    name = "pan-agreement"
    version = "pan-agreement/v0"
    min_claimed_bins = 20
    min_confidence = 0.5
    rate_floor = 10.0
    min_disagree_bins = 10
    stick_deadzone = STICK_DEADZONE

    def __init__(self, estimator: CameraPanEstimator, **thresholds: Any) -> None:
        self.estimator = estimator
        _apply_thresholds(self, thresholds, _PAN_THRESHOLDS)

    def score(self, sample: VariantSample) -> Score:
        bins = self.estimator.estimate(sample)
        for pan_bin in bins:
            validate_pan_bin(pan_bin)
        claimed = [pan_bin for pan_bin in bins if not pan_bin.no_claim]
        detail: dict[str, Any] = {
            "estimator": self.estimator.name,
            "estimator_version": self.estimator.version,
            "bins": len(bins),
            "claimed": len(claimed),
            "no_claim": len(bins) - len(claimed),
        }
        if len(claimed) < self.min_claimed_bins:
            detail["reason"] = "too few claimed bins"
            return Score(flagged=None, detail=detail)
        disagree = 0
        for pan_bin in claimed:
            yaw = _finite_number("yaw_rate", pan_bin.yaw_rate)
            pitch = _finite_number("pitch_rate", pan_bin.pitch_rate)
            confidence = _confidence(pan_bin.confidence)
            moving = abs(yaw) >= self.rate_floor or abs(pitch) >= self.rate_floor
            if confidence < self.min_confidence or not moving:
                continue
            if right_stick_idle(sample.rows, pan_bin.mono_ms, pan_bin.mono_ms + BIN_MS, self.stick_deadzone):
                disagree += 1
        detail["disagree_bins"] = disagree
        flagged = disagree >= self.min_disagree_bins and disagree * 2 >= len(claimed)
        return Score(flagged=flagged, detail=detail)

    def identity(self) -> ScorerIdentity:
        return source_identity(self, self.estimator)


class EventPressAgreementScorer:
    """Example scorer over video events and the logged presses.

    A claimed ``stick_action`` with no off-center right stick in the match window,
    or a claimed ``button_action`` with no logged button in that window, is unmatched.
    The scorer flags the variant when unmatched events are at least ``min_unmatched``
    and at least half of the claimed events. Fewer than ``min_claimed`` claimed
    events is an abstention, which is how the no_claim stub scores.

    These numbers are example policy. The verifier does not read them.
    """

    name = "event-press-agreement"
    version = "event-press-agreement/v0"
    min_claimed = 3
    min_confidence = 0.5
    min_unmatched = 3
    window_ms = EVENT_MATCH_WINDOW_MS
    stick_deadzone = STICK_DEADZONE

    def __init__(self, estimator: VideoEventEstimator, **thresholds: Any) -> None:
        self.estimator = estimator
        _apply_thresholds(self, thresholds, _EVENT_THRESHOLDS)

    def score(self, sample: VariantSample) -> Score:
        events = self.estimator.estimate(sample)
        for event in events:
            validate_video_event(event)
        claimed = [event for event in events if not event.no_claim]
        detail: dict[str, Any] = {
            "estimator": self.estimator.name,
            "estimator_version": self.estimator.version,
            "events": len(events),
            "claimed": len(claimed),
            "no_claim": len(events) - len(claimed),
        }
        if len(claimed) < self.min_claimed:
            detail["reason"] = "too few claimed events"
            return Score(flagged=None, detail=detail)
        unmatched = 0
        for event in claimed:
            confidence = _confidence(event.confidence)
            if confidence < self.min_confidence or event.event_kind not in EVENT_KINDS:
                continue
            if not logged_press_matches(sample.rows, event.mono_ms, str(event.event_kind), self.window_ms, self.stick_deadzone):
                unmatched += 1
        detail["unmatched"] = unmatched
        flagged = unmatched >= self.min_unmatched and unmatched * 2 >= len(claimed)
        return Score(flagged=flagged, detail=detail)

    def identity(self) -> ScorerIdentity:
        return source_identity(self, self.estimator)


def rates_for(samples: Sequence[VariantSample], scores: Mapping[str, Score], identity: ScorerIdentity) -> RateResult:
    """Catch rate = true positives / decided attacks. False-alarm rate = false positives / decided benign."""
    attacks = [sample for sample in samples if sample.label == "attack"]
    benign = [sample for sample in samples if sample.label == "benign"]
    if len(attacks) + len(benign) != len(samples):
        raise ValueError("every sample needs an attack or benign label")

    def flagged(sample: VariantSample) -> bool | None:
        return scores[sample.name].flagged

    true_positives = sum(flagged(sample) is True for sample in attacks)
    false_negatives = sum(flagged(sample) is False for sample in attacks)
    false_positives = sum(flagged(sample) is True for sample in benign)
    true_negatives = sum(flagged(sample) is False for sample in benign)
    abstained = sum(flagged(sample) is None for sample in samples)
    decided_attacks = true_positives + false_negatives
    decided_benign = false_positives + true_negatives
    catch = None if decided_attacks == 0 else true_positives / decided_attacks
    false_alarm = None if decided_benign == 0 else false_positives / decided_benign
    return RateResult(
        scorer=identity.name,
        version=identity.version,
        sha256=identity.sha256,
        attacks=len(attacks),
        benign=len(benign),
        true_positives=true_positives,
        false_negatives=false_negatives,
        false_positives=false_positives,
        true_negatives=true_negatives,
        abstained=abstained,
        decided_attacks=decided_attacks,
        decided_benign=decided_benign,
        catch_rate=catch,
        false_alarm_rate=false_alarm,
    )


def default_scorers() -> list[Scorer]:
    """Motion rule v3, plus the example pan and event scorers with no_claim stubs."""
    return [
        MotionRuleV3Scorer(),
        PanAgreementScorer(NoClaimPanEstimator()),
        EventPressAgreementScorer(NoClaimEventEstimator()),
    ]
