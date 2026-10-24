"""Import and process guard for the test-only red-team tree.

The static check does not open a socket. ``urllib.request`` and ``http.client``
are covered because the root of a dotted name is what matters, not only the
exact string ``urllib``. Subprocess is allowed for local tools; network clients
are not. Dynamic imports (``importlib.import_module`` and ``builtins.__import__``)
are checked the same way when the module name is a string literal.

``install_network_guard`` rejects those imports for the rest of the process, so
a harness run cannot open them afterwards.
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


def install_network_guard() -> None:
    """Reject forbidden network imports for the rest of this process.

    Call this after the harness modules have been imported and before scoring.
    ``pathlib`` imports ``urllib.parse`` while those modules load; the guard then
    stops a later import of ``socket``, ``requests``, or ``urllib.request``.
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
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
    if name == _VISION or name.startswith(_VISION + "."):
        return True
    if redteam and (name == _REDTEAM or name.startswith(_REDTEAM + ".")):
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
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
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
    """Map local names to ``import_module`` or ``__import__``, following aliases."""
    known = {
        "import_module": "import_module",
        "importlib.import_module": "import_module",
        "__import__": "__import__",
        "builtins.__import__": "__import__",
    }
    assigns: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[-1]
                if alias.name == "importlib":
                    known[f"{alias.asname or alias.name}.import_module"] = "import_module"
                elif alias.name == "importlib.import_module":
                    known[alias.asname or "import_module"] = "import_module"
                elif alias.name == "builtins":
                    known[f"{alias.asname or alias.name}.__import__"] = "__import__"
                elif alias.name in {"import_module", "__import__"}:
                    known[local] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins"}:
            for alias in node.names:
                if alias.name in {"import_module", "__import__"}:
                    known[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Assign):
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
    return known


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


def _tree_imports_banned(tree: ast.AST, path: Path, root: Path, *, redteam: bool) -> bool:
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = _from_targets(path, root, node)
        elif isinstance(node, ast.Call):
            dynamic = _dynamic_targets(node, aliases, path, root)
            if dynamic is None:
                continue
            targets = dynamic
        else:
            continue
        if "<unresolved-relative>" in targets or "<dynamic>" in targets:
            return True
        if any(_banned_module(target, redteam=redteam) for target in targets):
            return True
    return False


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


def verdict_path_import_offenders(root: Path) -> list[str]:
    """Files on the verdict path that import the pan estimator, or that cannot be parsed.

    The scan covers ``qor_receipt/`` except the vision package, every Python file
    under ``services/`` (vision imports and ``redteam`` imports), and any other
    referee path outside ``qor_receipt/``. A file that is not UTF-8 or does not
    parse is an offender. Published pages, the browser verifier, and the receipt
    schema are not scanned.
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
        if _tree_imports_banned(tree, path, root, redteam=redteam):
            offenders.append(rel)
    return offenders
