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
``__builtins__.__import__`` as loaders. A module object assigned to another
name (``il = importlib``), ``importlib.__import__``, ``vars(builtins)['__import__']``,
a ``sys.modules`` lookup, ``__import__('importlib').import_module``,
``pkgutil.resolve_name``, and ``SourceFileLoader(...).load_module()`` are
loaders too, as are ``__builtins__['__import__']``, ``builtins.__dict__['__import__']``,
``importlib.__dict__['import_module']``, and an alias of ``exec``, ``eval``, or
``compile``. A non-constant argument is an offender. A loader hidden in a C extension,
or a name built and passed through a function this scan does not recognize,
is not visible in the file. The scan is best-effort: it does not follow a walrus
or tuple alias, ``functools.partial``, ``map``, or a class-attribute holder, and it
does not see ``find_spec().loader`` / ``exec_module``, ``zipimport``, or
``sys.modules.get`` passed through another name. It reads ``*.py`` only, so a
``.pyw`` file is skipped, and it does not follow a symlinked directory or a new
top-level directory outside ``SCANNED_ROOTS``. Sidecar code a referee image
downloads is outside the tree. ``eval/`` is scanned; it is not a
verdict path. The scorebug script there imports the vendored reader, not
``qor_receipt.vision``.
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
# Roots walked for the verdict-path ban. ``eval/`` is research tooling, not a verdict path, and is still scanned.
SCANNED_ROOTS = ("qor_receipt", "services", "scripts", "eval")


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


_MODULE_ROOTS = {
    "importlib": "importlib",
    "builtins": "builtins",
    "__builtins__": "builtins",
    "importlib.util": "importlib.util",
    "runpy": "runpy",
    "pkgutil": "pkgutil",
    "importlib.machinery": "importlib.machinery",
}


def _apply_module_alias(known: dict[str, str], local: str, root: str) -> None:
    """Record loader attributes of a name that refers to one of the loader modules."""
    if root == "importlib":
        known[f"{local}.import_module"] = "import_module"
        known[f"{local}.__import__"] = "__import__"
        known[f"{local}.util.spec_from_file_location"] = "spec"
    elif root == "builtins":
        known[f"{local}.__import__"] = "__import__"
        known[f"{local}.exec"] = "exec"
        known[f"{local}.eval"] = "exec"
        known[f"{local}.compile"] = "exec"
    elif root == "importlib.util":
        known[f"{local}.spec_from_file_location"] = "spec"
    elif root == "runpy":
        known[f"{local}.run_path"] = "runpy"
        known[f"{local}.run_module"] = "runpy"
    elif root == "pkgutil":
        known[f"{local}.resolve_name"] = "resolve"
    elif root == "importlib.machinery":
        known[f"{local}.SourceFileLoader"] = "loader"


def _kind_for_loaded_module(module_name: str, attr: str) -> str | None:
    """Loader kind for ``attr`` on a module this scan treats as a loader, else None."""
    root = module_name if module_name in _MODULE_ROOTS else None
    if root is None:
        return None
    scratch: dict[str, str] = {}
    _apply_module_alias(scratch, "_m", root)
    return scratch.get(f"_m.{attr}", "<dynamic>")


def _dict_root_of(expr_name: str, modules: dict[str, str], dict_names: dict[str, str]) -> str | None:
    """Loader module whose dict ``expr_name`` refers to, if this scan treats it as one."""
    found = dict_names.get(expr_name)
    if found is not None:
        return found
    if not expr_name.endswith(".__dict__"):
        return None
    base = expr_name[: -len(".__dict__")]
    root = modules.get(base) or _MODULE_ROOTS.get(base)
    if root in {"importlib", "builtins"}:
        return root
    return None


def _vars_subscript_kind(
    node: ast.expr,
    modules: dict[str, str],
    dict_names: dict[str, str] | None = None,
) -> str | None:
    """Kind for ``vars(X)[key]``, ``X.__dict__[key]``, and ``__builtins__[key]``.

    Unrelated ``vars`` calls are ignored. A non-constant key on a loader dict is
    ``<dynamic>``.
    """
    if not isinstance(node, ast.Subscript):
        return None
    names = dict_names if dict_names is not None else {"__builtins__": "builtins"}
    value = node.value
    root: str | None = None
    if isinstance(value, ast.Call) and _expr_name(value.func) == "vars" and value.args:
        base = _expr_name(value.args[0])
        if base is not None:
            got = modules.get(base) or _MODULE_ROOTS.get(base)
            if got in {"importlib", "builtins"}:
                root = got
    else:
        expr = _expr_name(value)
        if expr is not None:
            root = _dict_root_of(expr, modules, names)
    if root not in {"importlib", "builtins"}:
        return None
    key = _const_text(node.slice)
    if key == "__import__":
        return "__import__"
    if key == "import_module":
        return "import_module"
    if key in {"exec", "eval", "compile"} and root == "builtins":
        return "exec"
    return "<dynamic>"


def _sys_modules_names(tree: ast.AST) -> set[str]:
    """Names that refer to ``sys.modules``, including ``import sys as s`` and assignments."""
    sys_aliases = {"sys"}
    assigns: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    sys_aliases.add(alias.asname or "sys")
        elif isinstance(node, ast.Assign):
            src = _expr_name(node.value)
            if src is None:
                continue
            for target in node.targets:
                name = _expr_name(target)
                if name:
                    assigns.append((name, src))
    names = {f"{alias}.modules" for alias in sys_aliases}
    changed = True
    while changed:
        changed = False
        for name, src in assigns:
            if src in names and name not in names:
                names.add(name)
                changed = True
    return names


def _modules_lookup_key(node: ast.expr, sys_modules: set[str]) -> ast.expr | None:
    """The key of ``sys.modules[key]`` or ``sys.modules.get(key)``."""
    if isinstance(node, ast.Subscript) and _expr_name(node.value) in sys_modules:
        return node.slice
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _expr_name(node.func.value) in sys_modules
    ):
        if node.args:
            return node.args[0]
        for kw in node.keywords:
            if kw.arg == "key":
                return kw.value
        return ast.Constant(value=None)
    return None


def _import_aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Map local names to a loader kind, following aliases, assignments, and ``getattr``.

    The second dict maps a local name to the loader module it refers to
    (``il = importlib`` records ``il`` as ``importlib``). The third maps a name
    to a loader module when the name is that module's dict (``__builtins__``,
    ``builtins.__dict__``, ``importlib.__dict__``, and aliases of those).
    """
    known = {
        "import_module": "import_module",
        "importlib.import_module": "import_module",
        "importlib.__import__": "__import__",
        "__import__": "__import__",
        "builtins.__import__": "__import__",
        "__builtins__.__import__": "__import__",
        "exec": "exec",
        "eval": "exec",
        "compile": "exec",
        "builtins.exec": "exec",
        "builtins.eval": "exec",
        "builtins.compile": "exec",
        "__builtins__.exec": "exec",
        "__builtins__.eval": "exec",
        "__builtins__.compile": "exec",
        "importlib.util.spec_from_file_location": "spec",
        "runpy.run_path": "runpy",
        "runpy.run_module": "runpy",
        "pkgutil.resolve_name": "resolve",
        "resolve_name": "resolve",
        "importlib.machinery.SourceFileLoader": "loader",
        "SourceFileLoader": "loader",
    }
    modules: dict[str, str] = {}
    dict_names: dict[str, str] = {"__builtins__": "builtins"}
    assigns: list[tuple[str, str]] = []
    getattr_assigns: list[tuple[str, str, str | None]] = []
    vars_assigns: list[tuple[str, ast.Subscript]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[-1]
                root = _MODULE_ROOTS.get(alias.name)
                if root is not None:
                    modules[local] = root
                    if alias.name == "importlib":
                        modules[alias.asname or alias.name] = root
                    _apply_module_alias(known, alias.asname or alias.name if alias.name == "importlib" else local, root)
                elif alias.name == "importlib.import_module":
                    known[alias.asname or "import_module"] = "import_module"
                elif alias.name in {"import_module", "__import__", "resolve_name", "SourceFileLoader"}:
                    known[alias.asname or alias.name] = {
                        "import_module": "import_module",
                        "__import__": "__import__",
                        "resolve_name": "resolve",
                        "SourceFileLoader": "loader",
                    }[alias.name]
        elif isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins", "runpy", "pkgutil", "importlib.machinery"}:
            for alias in node.names:
                if alias.name in {"import_module", "__import__"}:
                    known[alias.asname or alias.name] = alias.name
                elif node.module == "builtins" and alias.name in {"exec", "eval", "compile"}:
                    known[alias.asname or alias.name] = "exec"
                elif node.module == "importlib" and alias.name == "util":
                    modules[alias.asname or alias.name] = "importlib.util"
                    _apply_module_alias(known, alias.asname or alias.name, "importlib.util")
                elif node.module == "runpy" and alias.name in {"run_path", "run_module"}:
                    known[alias.asname or alias.name] = "runpy"
                elif node.module == "pkgutil" and alias.name == "resolve_name":
                    known[alias.asname or alias.name] = "resolve"
                elif node.module == "importlib.machinery" and alias.name == "SourceFileLoader":
                    known[alias.asname or alias.name] = "loader"
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib.util":
            for alias in node.names:
                if alias.name == "spec_from_file_location":
                    known[alias.asname or alias.name] = "spec"
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Subscript):
                for target in node.targets:
                    name = _expr_name(target)
                    if name:
                        vars_assigns.append((name, node.value))
                continue
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
            root = modules.get(src) or _MODULE_ROOTS.get(src)
            if root is not None and modules.get(name) != root:
                modules[name] = root
                _apply_module_alias(known, name, root)
                changed = True
            if src in known and known.get(name) != known[src]:
                known[name] = known[src]
                changed = True
            dict_root = _dict_root_of(src, modules, dict_names)
            if dict_root is not None and dict_names.get(name) != dict_root:
                dict_names[name] = dict_root
                changed = True
        for name, base, attr in getattr_assigns:
            if not _is_loader_base(base, known) and modules.get(base) not in {"importlib", "builtins"} and base not in _MODULE_ROOTS:
                continue
            kind = _loader_attr_kind(attr)
            if known.get(name) != kind:
                known[name] = kind
                changed = True
        for name, sub in vars_assigns:
            kind = _vars_subscript_kind(sub, modules, dict_names)
            if kind is not None and known.get(name) != kind:
                known[name] = kind
                changed = True
    return known, modules, dict_names


def _getattr_parts(node: ast.expr) -> tuple[str, str | None] | None:
    """``(base, attr)`` for ``getattr(base, attr)``. ``attr`` is None when it is not a string."""
    if not isinstance(node, ast.Call) or _expr_name(node.func) != "getattr" or len(node.args) < 2:
        return None
    base = _expr_name(node.args[0])
    if base is None:
        return None
    return base, _const_text(node.args[1])


_LOADER_KINDS = frozenset({"import_module", "__import__", "spec", "runpy", "resolve", "loader", "exec", "<dynamic>"})


def _is_loader_base(base: str, known: dict[str, str]) -> bool:
    if base in _MODULE_ROOTS or base in {"__builtins__"}:
        return True
    return any(name.startswith(base + ".") and known[name] in _LOADER_KINDS for name in known)


def _loader_attr_kind(attr: str | None) -> str:
    if attr == "import_module":
        return "import_module"
    if attr == "__import__":
        return "__import__"
    if attr == "spec_from_file_location":
        return "spec"
    if attr in {"run_path", "run_module"}:
        return "runpy"
    if attr == "resolve_name":
        return "resolve"
    if attr == "SourceFileLoader":
        return "loader"
    if attr in {"exec", "eval", "compile"}:
        return "exec"
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


def _import_call_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    """Constant module name for a direct ``import_module`` / ``__import__`` call."""
    kind = aliases.get(_expr_name(node.func) or "")
    if kind not in {"import_module", "__import__"}:
        return None
    name = _const_text(node.args[0]) if node.args else None
    for kw in node.keywords:
        if kw.arg == "name":
            name = _const_text(kw.value)
    return name


def _attr_on_call_kind(func: ast.expr, aliases: dict[str, str]) -> str | None:
    """``__import__('importlib').import_module`` and ``SourceFileLoader(...).load_module()``."""
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Call):
        return None
    inner = func.value
    inner_name = _expr_name(inner.func) or ""
    inner_kind = aliases.get(inner_name)
    if inner_kind == "loader" or inner_name == "SourceFileLoader" or inner_name.endswith(".SourceFileLoader"):
        return "loader"
    if inner_kind not in {"import_module", "__import__"}:
        return None
    loaded = _import_call_name(inner, aliases)
    if loaded is None:
        return "<dynamic>"
    root = loaded if loaded in _MODULE_ROOTS else loaded.split(".", 1)[0]
    if root not in _MODULE_ROOTS:
        return None
    return _kind_for_loaded_module(loaded if loaded in _MODULE_ROOTS else root, func.attr)


def _sys_modules_attr_kind(func: ast.expr, sys_modules: set[str]) -> str | None:
    """``sys.modules['importlib'].import_module``."""
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Subscript):
        return None
    if _expr_name(func.value.value) not in sys_modules:
        return None
    text = _const_text(func.value.slice)
    if text is None:
        return "<dynamic>"
    root = text if text in _MODULE_ROOTS else text.split(".", 1)[0]
    if root not in _MODULE_ROOTS:
        return None
    return _kind_for_loaded_module(text if text in _MODULE_ROOTS else root, func.attr)


def _call_loader(
    node: ast.Call,
    aliases: dict[str, str],
    modules: dict[str, str] | None = None,
    sys_modules: set[str] | None = None,
    dict_names: dict[str, str] | None = None,
) -> str | None:
    kind = aliases.get(_expr_name(node.func) or "")
    if kind in _LOADER_KINDS:
        return kind
    vars_kind = _vars_subscript_kind(node.func, modules or {}, dict_names)
    if vars_kind is not None:
        return vars_kind
    attr_kind = _attr_on_call_kind(node.func, aliases)
    if attr_kind is not None:
        return attr_kind
    modules_kind = _sys_modules_attr_kind(node.func, sys_modules or set())
    if modules_kind is not None:
        return modules_kind
    got = _getattr_loader(node.func, aliases)
    if got is not None:
        return got
    name = _expr_name(node.func) or ""
    if name in {
        "exec", "eval", "compile",
        "builtins.exec", "builtins.eval", "builtins.compile",
        "__builtins__.exec", "__builtins__.eval", "__builtins__.compile",
    }:
        return "exec"
    if name == "spec_from_file_location" or name.endswith(".spec_from_file_location"):
        return "spec"
    if name in {"runpy.run_path", "runpy.run_module"} or name.endswith(".run_path") or name.endswith(".run_module"):
        if name.startswith("runpy.") or aliases.get(name) == "runpy" or "." not in name:
            return "runpy"
    if name == "resolve_name" or name.endswith(".resolve_name"):
        if name.startswith("pkgutil.") or aliases.get(name) == "resolve" or "." not in name:
            return "resolve"
    return None


def _resolve_targets(node: ast.Call) -> list[str]:
    """Module part of ``pkgutil.resolve_name``. A non-literal name is ``<dynamic>``."""
    name = _const_text(node.args[0]) if node.args else None
    for kw in node.keywords:
        if kw.arg == "name":
            name = _const_text(kw.value)
        elif kw.arg is None:
            return ["<dynamic>"]
    if name is None:
        return ["<dynamic>"]
    return [name.split(":", 1)[0]]


def _lookup_targets(key: ast.expr) -> list[str]:
    text = _const_text(key)
    return ["<dynamic>"] if text is None else [text]


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
    aliases, modules, dict_names = _import_aliases(tree)
    sys_modules = _sys_modules_names(tree)
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = _from_targets(path, root, node)
        elif isinstance(node, ast.Subscript):
            key = _modules_lookup_key(node, sys_modules)
            if key is None:
                if _vars_subscript_kind(node, modules, dict_names) == "<dynamic>":
                    targets = ["<dynamic>"]
                else:
                    continue
            else:
                targets = _lookup_targets(key)
        elif isinstance(node, ast.Call):
            loader = _call_loader(node, aliases, modules, sys_modules, dict_names)
            if loader == "exec":
                targets = _exec_targets(node, path, root, redteam=redteam, allow_vision=allow_vision)
            elif loader == "resolve":
                targets = _resolve_targets(node)
            elif loader in {"spec", "runpy", "loader", "<dynamic>"}:
                targets = ["<dynamic>"]
            elif loader in {"import_module", "__import__"}:
                dynamic = _dynamic_targets(node, aliases, path, root)
                if dynamic is None:
                    dynamic = _dynamic_targets_from_args(node, loader, path, root)
                targets = dynamic
            else:
                key = _modules_lookup_key(node, sys_modules)
                if key is not None:
                    targets = _lookup_targets(key)
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

    The scan covers the roots in ``SCANNED_ROOTS``: ``qor_receipt/`` except the
    vision package, every Python file under ``services/``, ``scripts/``, and
    ``eval/`` (vision imports and ``redteam`` imports), and any other referee
    path outside ``qor_receipt/``. ``eval/`` is research tooling, not a verdict
    path. A file that is not UTF-8 or does not parse is an offender. A
    non-literal ``import_module`` or ``__import__``, ``exec`` / ``eval`` /
    ``compile`` (including an alias), ``runpy``, ``spec_from_file_location``,
    ``getattr`` on a loader module, a module alias of ``importlib``,
    ``importlib.__import__``, ``vars`` on ``builtins``, a ``__dict__`` or
    ``__builtins__`` subscript, a ``sys.modules`` lookup,
    ``__import__('importlib').import_module``, ``pkgutil.resolve_name``, and
    ``SourceFileLoader`` are offenders. The walk uses ``SCANNED_ROOTS``. A loader
    hidden in a C extension, or a name passed through a function this scan does
    not recognize, is not visible. ``scripts/pan_estimate_clips.py`` may import
    the vision package; a dynamic import or a ``redteam`` import there is still
    an offender. Published pages, the browser verifier, and the receipt schema
    are not scanned.
    """
    paths: list[tuple[Path, bool]] = []
    for folder in SCANNED_ROOTS:
        if folder == "qor_receipt":
            package = root / "qor_receipt"
            if not package.is_dir():
                continue
            for path in _python_under(root, "qor_receipt"):
                if path.relative_to(package).parts[:1] == ("vision",):
                    continue
                paths.append((path, False))
            continue
        for path in _python_under(root, folder):
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
