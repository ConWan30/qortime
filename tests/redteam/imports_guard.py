"""Import and process tripwire for the test-only red-team tree.

This is a tripwire, not a sandbox. The static check does not open a socket.
``urllib.request`` and ``http.client`` are covered because the root of a dotted
name is what matters, not only the exact string ``urllib``. Subprocess is
allowed for local tools; network clients are not. Dynamic imports
(``importlib.import_module`` and ``builtins.__import__``) are checked the same
way when the module name is a string literal.

``install_network_guard`` rejects those imports for the rest of the process and
installs an audit hook for ``socket.*``, ``subprocess.Popen``, ``os.system``,
``os.spawn*``, and ``ctypes.*``. A later call raises. The hook cannot be
removed, and it does not confine the process. ``os.fork`` followed by
``os.execv`` does not raise one of those audit events, so it slips past the hook.

The static verdict-path scan also treats ``exec`` / ``eval``, ``runpy``,
``importlib.util.spec_from_file_location``, ``getattr`` on ``importlib``, and
``__builtins__.__import__`` as loaders. A non-constant argument is an offender.
A loader hidden in a C extension, or a name built and passed through a function
this scan does not recognize, is not visible in the file.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import sys
from pathlib import Path

# Roots, so ``urllib.request`` and ``http.client`` match.
FORBIDDEN_IMPORT_ROOTS = frozenset({
    "urllib",
    "http",
    "socket",
    "requests",
    "httpx",
    "aiohttp",
    "ssl",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "xmlrpc",
    "telnetlib",
    "websockets",
    "urllib3",
})

_DYNAMIC_IMPORTS = frozenset({
    "importlib.import_module",
    "import_module",
    "builtins.__import__",
    "__import__",
})

_BLOCKED_ATTR_CALLS = frozenset({
    ("asyncio", "open_connection"),
    ("asyncio", "create_connection"),
    ("socket", "create_connection"),
})

NETWORK_CALLS = frozenset({
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execvp",
    "os.execve",
    "os.execvpe",
})

# argv0 of a subprocess that talks to the network. Local tools such as ffmpeg are absent.
NETWORK_TOOLS = frozenset({
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "ssh",
    "scp",
    "sftp",
    "socat",
    "telnet",
    "ftp",
    "ping",
    "dig",
    "nslookup",
})


def _forbidden_import(name: str) -> bool:
    return name.split(".", 1)[0] in FORBIDDEN_IMPORT_ROOTS


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return ""


def _const_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    return None


def _dynamic_import_target(node: ast.Call) -> str | None:
    if _call_name(node.func) not in _DYNAMIC_IMPORTS or not node.args:
        return None
    return _const_str(node.args[0])


def _blocked_network_call(func: ast.expr) -> str:
    """Name of a blocked socket or asyncio call, or ``""``."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        pair = (func.value.id, func.attr)
        if pair in _BLOCKED_ATTR_CALLS:
            return f"{pair[0]}.{pair[1]}"
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        inner = func.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "socket"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "socket"
        ):
            return "socket.socket.connect"
        if (
            isinstance(inner, ast.Attribute)
            and inner.attr == "socket"
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "socket"
        ):
            return "socket.socket.connect"
    return ""


def _argv(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split()
    if isinstance(node, (ast.List, ast.Tuple)):
        tokens: list[str] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                tokens.append(elt.value)
            else:
                break
        return tokens
    return []


def _tool_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def audit_source(source: str, *, filename: str = "<redteam>") -> list[str]:
    """Return one finding per forbidden import, dynamic import, or network call."""
    tree = ast.parse(source, filename=filename)
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_import(alias.name):
                    findings.append(f"{filename}:{node.lineno}: forbidden import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and _forbidden_import(node.module):
            findings.append(f"{filename}:{node.lineno}: forbidden import {node.module}")
        elif isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target and _forbidden_import(target):
                findings.append(f"{filename}:{node.lineno}: forbidden import {target}")
                continue
            blocked = _blocked_network_call(node.func)
            if blocked:
                findings.append(f"{filename}:{node.lineno}: network call {blocked}")
                continue
            if not node.args:
                continue
            name = _call_name(node.func)
            if name not in NETWORK_CALLS:
                continue
            tokens = _argv(node.args[0])
            if tokens and _tool_name(tokens[0]) in NETWORK_TOOLS:
                findings.append(f"{filename}:{node.lineno}: network tool {tokens[0]} via {name}")
    return findings


class _NetworkFinder:
    """Meta-path finder that refuses the forbidden network modules."""

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if isinstance(fullname, str) and _forbidden_import(fullname):
            raise ImportError(f"blocked import {fullname}")
        return None


_GUARD_INSTALLED = False


def _audit_hook(event: str, args: tuple[object, ...]) -> None:
    """Tripwire for a few process calls. Not a sandbox: other events still run."""
    if event.startswith(("socket.", "ctypes.", "os.spawn", "os.posix_spawn")) or event in {"subprocess.Popen", "os.system"}:
        raise RuntimeError(f"blocked audit event {event}")


def install_network_guard() -> None:
    """Reject forbidden network imports for the rest of this process.

    Call this after the harness modules have been imported and before scoring.
    ``pathlib`` imports ``urllib.parse`` while those modules load; the guard then
    stops a later import of ``socket``, ``requests``, or ``urllib.request``.
    The audit hook is the same kind of tripwire for ``socket.*``,
    ``subprocess.Popen``, ``os.system``, ``os.spawn*``, and ``ctypes.*``.
    ``os.fork`` followed by ``os.execv`` does not trip that hook.
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    sys.addaudithook(_audit_hook)
    sys.meta_path.insert(0, _NetworkFinder())
    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def guarded_import(name: str, globals: dict | None = None, locals: dict | None = None,
                       fromlist: tuple = (), level: int = 0):
        if level == 0 and isinstance(name, str) and _forbidden_import(name):
            raise ImportError(f"blocked import {name}")
        return real_import(name, globals, locals, fromlist, level)

    def guarded_import_module(name: str, package: str | None = None):
        if isinstance(name, str) and _forbidden_import(name):
            raise ImportError(f"blocked import {name}")
        return real_import_module(name, package)

    builtins.__import__ = guarded_import
    importlib.import_module = guarded_import_module
    _GUARD_INSTALLED = True


# Verdict-path import ban. The network guard above is a different check.
_VISION = "qor_receipt.vision"
_REDTEAM = "redteam"
_SCAN_SKIP = frozenset({
    "tests", "docs", "site", "verify", "schema", ".git", "node_modules", "__pycache__",
})


def _banned_module(name: str, *, redteam: bool) -> bool:
    if name == "runpy" or name.startswith("runpy."):
        return True
    if name == _VISION or name.startswith(_VISION + "."):
        return True
    if redteam and (_REDTEAM == name or name.startswith(_REDTEAM + ".") or _REDTEAM in name.split(".")):
        return True
    return False


def _module_package(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    return ".".join(parts[:-1])


def _resolve_relative(name: str, package: str) -> str | None:
    """``importlib``-style resolution of a dotted relative name against ``package``."""
    level = len(name) - len(name.lstrip("."))
    tail = name[level:]
    if level == 0:
        return name
    if not package:
        return None
    bits = package.rsplit(".", level - 1)
    if len(bits) < level:
        return None
    base = bits[0]
    return f"{base}.{tail}" if tail else base


def _expr_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _const_text(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    return None


def _str_elts(node: ast.expr | None) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names: list[str] = []
    for elt in node.elts:
        text = _const_text(elt)
        if text is None:
            return None
        names.append(text)
    return names


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local names to a loader kind, following aliases and ``getattr``."""
    known = {
        "import_module": "import_module",
        "importlib.import_module": "import_module",
        "__import__": "__import__",
        "builtins.__import__": "__import__",
        "__builtins__.__import__": "__import__",
        "importlib.util.spec_from_file_location": "spec",
        "runpy.run_path": "runpy",
        "runpy.run_module": "runpy",
    }
    assigns: list[tuple[str, str]] = []
    getattr_assigns: list[tuple[str, str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name
                leaf = alias.asname or alias.name.split(".")[-1]
                if alias.name == "importlib":
                    bound = alias.asname or alias.name
                    known[f"{bound}.import_module"] = "import_module"
                    known[f"{bound}.util.spec_from_file_location"] = "spec"
                elif alias.name == "importlib.util":
                    known[f"{leaf}.spec_from_file_location"] = "spec"
                elif alias.name == "importlib.import_module":
                    known[alias.asname or "import_module"] = "import_module"
                elif alias.name == "builtins":
                    known[f"{alias.asname or alias.name}.__import__"] = "__import__"
                elif alias.name == "runpy":
                    known[f"{leaf}.run_path"] = "runpy"
                    known[f"{leaf}.run_module"] = "runpy"
                elif alias.name in {"import_module", "__import__"}:
                    known[local] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins", "runpy"}:
            for alias in node.names:
                if alias.name in {"import_module", "__import__"}:
                    known[alias.asname or alias.name] = alias.name
                elif node.module == "importlib" and alias.name == "util":
                    known[f"{alias.asname or alias.name}.spec_from_file_location"] = "spec"
                elif node.module == "runpy" and alias.name in {"run_path", "run_module"}:
                    known[alias.asname or alias.name] = "runpy"
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib.util":
            for alias in node.names:
                if alias.name == "spec_from_file_location":
                    known[alias.asname or alias.name] = "spec"
        elif isinstance(node, ast.Assign):
            got = _getattr_parts(node.value)
            if got is not None:
                base, attr = got
                for target in node.targets:
                    name = _expr_name(target)
                    if name:
                        getattr_assigns.append((name, base, attr))
                continue
            src = _expr_name(node.value)
            if src is None:
                continue
            for target in node.targets:
                name = _expr_name(target)
                if name:
                    assigns.append((name, src))
    changed = True
    while changed:
        changed = False
        for name, src in assigns:
            if src in known and known.get(name) != known[src]:
                known[name] = known[src]
                changed = True
        for name, base, attr in getattr_assigns:
            if not _is_loader_base(base, known):
                continue
            kind = _loader_attr_kind(attr)
            if known.get(name) != kind:
                known[name] = kind
                changed = True
    return known


def _getattr_parts(node: ast.expr) -> tuple[str, str | None] | None:
    """``(base, attr)`` for ``getattr(base, attr)``. ``attr`` is None when it is not a string."""
    if not isinstance(node, ast.Call) or _expr_name(node.func) != "getattr" or len(node.args) < 2:
        return None
    base = _expr_name(node.args[0])
    if base is None:
        return None
    return base, _const_text(node.args[1])


def _is_loader_base(base: str, known: dict[str, str]) -> bool:
    if base in {"importlib", "builtins", "__builtins__", "importlib.util"}:
        return True
    return any(name.startswith(base + ".") and known[name] in {"import_module", "__import__", "spec", "runpy"} for name in known)


def _loader_attr_kind(attr: str | None) -> str:
    if attr == "import_module":
        return "import_module"
    if attr == "__import__":
        return "__import__"
    if attr == "spec_from_file_location":
        return "spec"
    if attr in {"run_path", "run_module"}:
        return "runpy"
    return "<dynamic>"


def _from_targets(path: Path, root: Path, node: ast.ImportFrom) -> list[str]:
    names = [alias.name for alias in node.names]
    if node.level == 0:
        base = node.module or ""
        targets = [base] if base else []
        targets.extend(f"{base}.{name}" if base else name for name in names)
        return targets
    package = _module_package(path, root)
    rel = "." * node.level + (node.module or "")
    base = _resolve_relative(rel, package)
    if base is None:
        return ["<unresolved-relative>"]
    return [base, *[f"{base}.{name}" for name in names]]


def _dynamic_targets(node: ast.Call, aliases: dict[str, str], path: Path, root: Path) -> list[str] | None:
    """Resolved module names, or ``['<dynamic>']`` when the call is not a literal."""
    kind = aliases.get(_expr_name(node.func) or "")
    if kind not in {"import_module", "__import__"}:
        return None
    name = _const_text(node.args[0]) if node.args else None
    package: str | None = None
    fromlist: list[str] | None = []
    level = 0
    if kind == "import_module":
        if len(node.args) >= 2:
            package = _const_text(node.args[1])
            if package is None:
                return ["<dynamic>"]
        for kw in node.keywords:
            if kw.arg == "name":
                name = _const_text(kw.value)
            elif kw.arg == "package":
                package = _const_text(kw.value)
                if package is None:
                    return ["<dynamic>"]
        if name is None:
            return ["<dynamic>"]
        resolved = _resolve_relative(name, package or "")
        return ["<dynamic>"] if resolved is None else [resolved]
    if len(node.args) >= 4:
        fromlist = _str_elts(node.args[3])
        if fromlist is None:
            return ["<dynamic>"]
    if len(node.args) >= 5:
        if not isinstance(node.args[4], ast.Constant) or type(node.args[4].value) is not int:
            return ["<dynamic>"]
        level = node.args[4].value
    for kw in node.keywords:
        if kw.arg == "fromlist":
            fromlist = _str_elts(kw.value)
            if fromlist is None:
                return ["<dynamic>"]
        elif kw.arg == "level":
            if not isinstance(kw.value, ast.Constant) or type(kw.value.value) is not int:
                return ["<dynamic>"]
            level = kw.value.value
        elif kw.arg == "name":
            name = _const_text(kw.value)
    if name is None or fromlist is None:
        return ["<dynamic>"]
    if level:
        name = _resolve_relative("." * level + name, _module_package(path, root))
        if name is None:
            return ["<dynamic>"]
    targets = [name]
    for item in fromlist:
        targets.append(item if item.startswith(".") else f"{name}.{item}")
    return targets


def _getattr_loader(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """Loader kind for ``getattr`` on ``importlib`` or ``builtins``, or ``'<dynamic>'``."""
    got = _getattr_parts(node)
    if got is None:
        return None
    base, attr = got
    if not _is_loader_base(base, aliases):
        return None
    return _loader_attr_kind(attr)


def _call_loader(node: ast.Call, aliases: dict[str, str]) -> str | None:
    kind = aliases.get(_expr_name(node.func) or "")
    if kind in {"spec", "import_module", "__import__", "runpy", "<dynamic>"}:
        return kind
    got = _getattr_loader(node.func, aliases)
    if got is not None:
        return got
    name = _expr_name(node.func) or ""
    if name in {"exec", "eval", "builtins.exec", "builtins.eval", "__builtins__.exec", "__builtins__.eval"}:
        return "exec"
    if name == "spec_from_file_location" or name.endswith(".spec_from_file_location"):
        return "spec"
    if name in {"runpy.run_path", "runpy.run_module"} or name.endswith(".run_path") or name.endswith(".run_module"):
        if name.startswith("runpy.") or aliases.get(name) == "runpy" or "." not in name:
            return "runpy"
    return None


def _exec_targets(node: ast.Call, path: Path, root: Path, *, redteam: bool, allow_vision: bool) -> list[str]:
    """Scan a literal ``exec``/``eval`` string. A non-literal is ``<dynamic>``."""
    arg = node.args[0] if node.args else None
    for kw in node.keywords:
        if kw.arg in {None, "source", "expression"} and arg is None:
            arg = kw.value
    text = _const_text(arg)
    if text is None:
        return ["<dynamic>"]
    try:
        inner = ast.parse(text, filename=path.name)
    except SyntaxError:
        return ["<dynamic>"]
    if _tree_imports_banned(inner, path, root, redteam=redteam, allow_vision=allow_vision):
        return ["<dynamic>"]
    return []


def _tree_imports_banned(tree: ast.AST, path: Path, root: Path, *, redteam: bool, allow_vision: bool = False) -> bool:
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = _from_targets(path, root, node)
        elif isinstance(node, ast.Call):
            loader = _call_loader(node, aliases)
            if loader == "exec":
                targets = _exec_targets(node, path, root, redteam=redteam, allow_vision=allow_vision)
            elif loader in {"spec", "runpy", "<dynamic>"}:
                targets = ["<dynamic>"]
            elif loader in {"import_module", "__import__"}:
                dynamic = _dynamic_targets(node, aliases, path, root)
                if dynamic is None and isinstance(node.func, ast.Call):
                    dynamic = _dynamic_targets_from_args(node, loader, path, root)
                if dynamic is None:
                    continue
                targets = dynamic
            else:
                dynamic = _dynamic_targets(node, aliases, path, root)
                if dynamic is None:
                    continue
                targets = dynamic
        else:
            continue
        if "<unresolved-relative>" in targets or "<dynamic>" in targets:
            return True
        for target in targets:
            if allow_vision and (target == _VISION or target.startswith(_VISION + ".")):
                continue
            if _banned_module(target, redteam=redteam):
                return True
    return False


def _dynamic_targets_from_args(node: ast.Call, kind: str, path: Path, root: Path) -> list[str]:
    """Module names from ``getattr(...)(name)`` using the same literal rules as a direct call."""
    fake_name = "__import__" if kind == "__import__" else "import_module"
    fake = ast.Call(func=ast.Name(id=fake_name), args=node.args, keywords=node.keywords)
    return _dynamic_targets(fake, {fake_name: fake_name}, path, root) or ["<dynamic>"]


def _python_under(root: Path, *parts: str) -> list[Path]:
    base = root.joinpath(*parts)
    if not base.is_dir():
        return []
    found: list[Path] = []
    for path in base.rglob("*.py"):
        if any(part in {"node_modules", "__pycache__"} or part.endswith(".egg-info") for part in path.parts):
            continue
        found.append(path)
    return found


# The estimator CLI's job is to import the vision package. Every other script is scanned.
_ESTIMATOR_CLI = "scripts/pan_estimate_clips.py"


def verdict_path_import_offenders(root: Path) -> list[str]:
    """Files on the verdict path that import the pan estimator, or that cannot be parsed.

    The scan covers ``qor_receipt/`` except the vision package, every Python file
    under ``services/`` and ``scripts/`` (vision imports and ``redteam`` imports),
    and any other referee path outside ``qor_receipt/``. A file that is not UTF-8
    or does not parse is an offender. A non-literal ``import_module`` or
    ``__import__``, ``exec`` / ``eval``, ``runpy``, ``spec_from_file_location``,
    and ``getattr`` on ``importlib`` or ``builtins`` are offenders. A loader
    hidden in a C extension, or a name passed through a function this scan does
    not recognize, is not visible. ``scripts/pan_estimate_clips.py`` may import
    the vision package; a dynamic import or a ``redteam`` import there is still
    an offender. Published pages, the browser verifier, and the receipt schema
    are not scanned.
    """
    paths: list[tuple[Path, bool]] = []
    package = root / "qor_receipt"
    if package.is_dir():
        for path in _python_under(root, "qor_receipt"):
            if path.relative_to(package).parts[:1] == ("vision",):
                continue
            paths.append((path, False))
    for path in _python_under(root, "services"):
        paths.append((path, True))
    for path in _python_under(root, "scripts"):
        paths.append((path, True))
    if root.is_dir():
        for path in root.rglob("*.py"):
            parts = path.relative_to(root).parts
            if not parts or parts[0] in _SCAN_SKIP or parts[0] == "qor_receipt" or parts[0].endswith(".egg-info"):
                continue
            if any(part.endswith(".egg-info") or part in {"node_modules", "__pycache__"} for part in parts):
                continue
            if any("referee" in part for part in parts):
                paths.append((path, False))
    offenders: list[str] = []
    chosen: dict[Path, bool] = {}
    for path, redteam in paths:
        chosen[path] = chosen.get(path, False) or redteam
    for path in sorted(chosen, key=lambda item: item.as_posix()):
        redteam = chosen[path]
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            offenders.append(rel)
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            offenders.append(rel)
            continue
        if _tree_imports_banned(tree, path, root, redteam=redteam, allow_vision=rel == _ESTIMATOR_CLI):
            offenders.append(rel)
    return offenders
