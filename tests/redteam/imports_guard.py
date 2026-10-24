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
