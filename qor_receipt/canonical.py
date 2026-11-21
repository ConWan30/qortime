"""Canonical qor-receipt JSON.

The signed bytes are JCS of an already-parsed value, so a lenient parser can
turn ``10.0``, ``10e0``, ``-0``, duplicate keys, or a trailing signature nibble
into a value that still verifies. Both verifiers read the receipt text and
reject anything that is not the canonical integer profile before they will
return VERIFIED.

Numbers must be spelled as integers in [-(2**53 - 1), 2**53 - 1]: no fraction,
exponent, leading zero, or negative zero. Object keys must be unique after
escape decoding. Strings must not contain unpaired UTF-16 surrogates.
"""
from __future__ import annotations

from typing import Any

MAX_SAFE = 2**53 - 1
_SAFE_DIGITS = "9007199254740991"
# spec/match-receipt-v0.md §1. Same numbers in verify/canonical.js.
MAX_RECEIPT_BYTES = 4000000
MAX_DEPTH = 64
_FORBIDDEN_KEYS = frozenset({"__proto__", "constructor", "prototype"})
_WS = frozenset(" \t\r\n")
_ESC = {"\"": "\"", "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEXDIG = frozenset("0123456789abcdefABCDEF")


class CanonicalError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_canonical(text: str) -> tuple[Any, list[str]]:
    """Parse receipt text. On failure the value is None and errors has one code."""
    if not isinstance(text, str) or text.startswith("\ufeff"):
        return None, ["noncanonical_json"]
    try:
        if len(text.encode("utf-8")) > MAX_RECEIPT_BYTES:
            return None, ["noncanonical_json"]
        return _Parser(text).parse(), []
    except CanonicalError as e:
        return None, [e.code]
    except Exception:
        # ValueError, RecursionError, MemoryError, UnicodeEncodeError: FAIL, never raise.
        return None, ["noncanonical_json"]


def structural_errors(value: Any) -> list[str]:
    """Rules that survive a lenient parse: hex shape, signature keys, floats, surrogates."""
    found: list[str] = []

    def add(code: str) -> None:
        if code not in found:
            found.append(code)

    _walk(value, add)
    _signatures(value, add)
    return found


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.n = len(text)
        self.i = 0
        self.depth = 0

    def parse(self) -> Any:
        value = self._value()
        self._ws()
        if self.i != self.n:
            raise CanonicalError("noncanonical_json")
        return value

    def _ws(self) -> None:
        s, n = self.s, self.n
        i = self.i
        while i < n and s[i] in _WS:
            i += 1
        self.i = i

    def _peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def _value(self) -> Any:
        self._ws()
        c = self._peek()
        if c == "{":
            return self._object()
        if c == "[":
            return self._array()
        if c == '"':
            return self._string()
        if c == "t":
            return self._lit("true", True)
        if c == "f":
            return self._lit("false", False)
        if c == "n":
            return self._lit("null", None)
        if c == "-" or _ascii_digit(c):
            return self._number()
        raise CanonicalError("noncanonical_json")

    def _lit(self, word: str, value: Any) -> Any:
        if self.s[self.i : self.i + len(word)] != word:
            raise CanonicalError("noncanonical_json")
        self.i += len(word)
        return value

    def _object(self) -> dict[str, Any]:
        self.i += 1
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CanonicalError("noncanonical_json")
        try:
            return self._object_body()
        finally:
            self.depth -= 1

    def _object_body(self) -> dict[str, Any]:
        self._ws()
        out: dict[str, Any] = {}
        if self._peek() == "}":
            self.i += 1
            return out
        while True:
            self._ws()
            if self._peek() != '"':
                raise CanonicalError("noncanonical_json")
            key = self._string()
            self._ws()
            if self._peek() != ":":
                raise CanonicalError("noncanonical_json")
            self.i += 1
            val = self._value()
            if key in _FORBIDDEN_KEYS:
                raise CanonicalError("noncanonical_json")
            if key in out:
                raise CanonicalError("noncanonical_duplicate_key")
            out[key] = val
            self._ws()
            c = self._peek()
            if c == ",":
                self.i += 1
                continue
            if c == "}":
                self.i += 1
                return out
            raise CanonicalError("noncanonical_json")

    def _array(self) -> list[Any]:
        self.i += 1
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CanonicalError("noncanonical_json")
        try:
            return self._array_body()
        finally:
            self.depth -= 1

    def _array_body(self) -> list[Any]:
        self._ws()
        out: list[Any] = []
        if self._peek() == "]":
            self.i += 1
            return out
        while True:
            out.append(self._value())
            self._ws()
            c = self._peek()
            if c == ",":
                self.i += 1
                continue
            if c == "]":
                self.i += 1
                return out
            raise CanonicalError("noncanonical_json")

    def _string(self) -> str:
        s, n = self.s, self.n
        i = self.i + 1
        parts: list[str] = []
        while i < n:
            c = s[i]
            if c == '"':
                self.i = i + 1
                return "".join(parts)
            if c == "\\":
                i += 1
                if i >= n:
                    raise CanonicalError("noncanonical_json")
                esc = s[i]
                i += 1
                if esc in _ESC:
                    parts.append(_ESC[esc])
                    continue
                if esc != "u" or i + 4 > n or any(s[i + k] not in _HEXDIG for k in range(4)):
                    raise CanonicalError("noncanonical_json")
                unit = int(s[i : i + 4], 16)
                i += 4
                if 0xD800 <= unit <= 0xDBFF:
                    if not (i + 6 <= n and s[i] == "\\" and s[i + 1] == "u" and all(ch in _HEXDIG for ch in s[i + 2 : i + 6])):
                        raise CanonicalError("noncanonical_surrogate")
                    low = int(s[i + 2 : i + 6], 16)
                    if not 0xDC00 <= low <= 0xDFFF:
                        raise CanonicalError("noncanonical_surrogate")
                    i += 6
                    parts.append(chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)))
                    continue
                if 0xDC00 <= unit <= 0xDFFF:
                    raise CanonicalError("noncanonical_surrogate")
                parts.append(chr(unit))
                continue
            if ord(c) < 0x20 or 0xD800 <= ord(c) <= 0xDFFF:
                raise CanonicalError("noncanonical_surrogate" if ord(c) >= 0xD800 else "noncanonical_json")
            parts.append(c)
            i += 1
        raise CanonicalError("noncanonical_json")

    def _number(self) -> int:
        s, n = self.s, self.n
        i = self.i
        start = i
        if s[i] == "-":
            i += 1
            if i >= n or not _ascii_digit(s[i]):
                raise CanonicalError("noncanonical_json")
        if i >= n or not _ascii_digit(s[i]):
            raise CanonicalError("noncanonical_json")
        if s[i] == "0":
            i += 1
            if i < n and _ascii_digit(s[i]):
                raise CanonicalError("noncanonical_number")
        else:
            while i < n and _ascii_digit(s[i]):
                i += 1
        if i < n and (s[i] == "." or s[i] in "eE"):
            if s[i] == ".":
                i += 1
                if i >= n or not _ascii_digit(s[i]):
                    raise CanonicalError("noncanonical_json")
                while i < n and _ascii_digit(s[i]):
                    i += 1
            if i < n and s[i] in "eE":
                i += 1
                if i < n and s[i] in "+-":
                    i += 1
                if i >= n or not _ascii_digit(s[i]):
                    raise CanonicalError("noncanonical_json")
                while i < n and _ascii_digit(s[i]):
                    i += 1
            self.i = i
            raise CanonicalError("noncanonical_number")
        lex = s[start:i]
        self.i = i
        if lex == "-0":
            raise CanonicalError("noncanonical_number")
        digits = lex[1:] if lex[0] == "-" else lex
        if len(digits) > len(_SAFE_DIGITS) or (len(digits) == len(_SAFE_DIGITS) and digits > _SAFE_DIGITS):
            raise CanonicalError("noncanonical_number")
        return int(lex)


def _ascii_digit(c: str) -> bool:
    return len(c) == 1 and "0" <= c <= "9"


def _hex_len(path: tuple) -> int | None:
    """Lowercase-hex lengths from schema/receipt.v0.json, by path, not by key name."""
    n = len(path)
    if n == 3 and path[0] == "players" and path[2] == "pubkey":
        return 64
    if n == 2 and path[0] == "referee" and path[1] == "pubkey":
        return 64
    if n == 2 and path[0] == "referee" and path[1] == "key_id":
        return 16
    if n == 3 and path[0] == "challenges" and path[2] == "nonce":
        return 32
    if n == 4 and path[0] == "challenges" and path[2] == "result" and path[3] == "hid_sha256":
        return 64
    if n == 4 and path[0] == "checkpoints" and path[1] in ("home", "away"):
        if path[3] in ("frames_hash", "input_hash", "prev", "hash"):
            return 64
        if path[3] == "referee_sig":
            return 128
    if n == 4 and path[0] == "signatures" and path[1] == "players":
        if path[3] == "key_id":
            return 16
        if path[3] == "sig":
            return 128
    if n == 3 and path[0] == "signatures" and path[1] == "referee":
        if path[2] == "key_id":
            return 16
        if path[2] == "sig":
            return 128
    return None


def _lone_surrogate(text: str) -> bool:
    i = 0
    n = len(text)
    while i < n:
        o = ord(text[i])
        if 0xD800 <= o <= 0xDBFF:
            if i + 1 < n and 0xDC00 <= ord(text[i + 1]) <= 0xDFFF:
                i += 2
                continue
            return True
        if 0xDC00 <= o <= 0xDFFF:
            return True
        i += 1
    return False


def _walk(value: Any, add, path: tuple = (), depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        add("noncanonical_json")
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float) or (isinstance(value, int) and abs(value) > MAX_SAFE):
        add("noncanonical_number")
        return
    if isinstance(value, str):
        if _lone_surrogate(value):
            add("noncanonical_surrogate")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _walk(item, add, path + (i,), depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_KEYS:
                add("noncanonical_json")
            if not isinstance(key, str) or _lone_surrogate(key):
                add("noncanonical_surrogate")
            child = path + (key,)
            length = _hex_len(child)
            if length is not None and isinstance(item, str) and (
                len(item) != length or any(ch not in "0123456789abcdef" for ch in item)
            ):
                add("noncanonical_hex")
            _walk(item, add, child, depth + 1)


def _signatures(value: Any, add) -> None:
    if not isinstance(value, dict):
        return
    sigs = value.get("signatures")
    if not isinstance(sigs, dict):
        return
    if set(sigs) != {"players", "referee"}:
        add("noncanonical_signatures")
    players = sigs.get("players")
    if not isinstance(players, dict):
        add("noncanonical_signatures")
        players = {}
    else:
        for entry in players.values():
            if not isinstance(entry, dict) or set(entry) != {"key_id", "sig"}:
                add("noncanonical_signatures")
                break
    ref = sigs.get("referee", None)
    if "referee" in sigs and ref is not None and (not isinstance(ref, dict) or set(ref) != {"key_id", "sig"}):
        add("noncanonical_signatures")
    roles: set[str] = set()
    listed = value.get("players")
    if isinstance(listed, list):
        for player in listed:
            if isinstance(player, dict) and isinstance(player.get("role"), str):
                roles.add(player["role"])
    if set(players) - roles:
        add("noncanonical_signatures")
