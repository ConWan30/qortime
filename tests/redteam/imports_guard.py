"""Import and process guard for the test-only red-team tree.

The check is static. It does not open a socket. ``urllib.request`` is covered
because the root of a dotted name is what matters, not only the exact string
``urllib``. Subprocess is allowed for local tools; network clients are not.
"""
from __future__ import annotations

import ast

# Roots, so ``urllib.request`` and ``http.client`` match.
FORBIDDEN_IMPORT_ROOTS = frozenset({"urllib", "http", "socket", "requests", "httpx", "aiohttp"})

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
    """Return one finding per forbidden import or network-tool subprocess."""
    tree = ast.parse(source, filename=filename)
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_import(alias.name):
                    findings.append(f"{filename}:{node.lineno}: forbidden import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and _forbidden_import(node.module):
            findings.append(f"{filename}:{node.lineno}: forbidden import {node.module}")
        elif isinstance(node, ast.Call) and node.args:
            name = _call_name(node.func)
            if name not in NETWORK_CALLS:
                continue
            tokens = _argv(node.args[0])
            if tokens and _tool_name(tokens[0]) in NETWORK_TOOLS:
                findings.append(f"{filename}:{node.lineno}: network tool {tokens[0]} via {name}")
    return findings
