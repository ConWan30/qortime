// Canonical qor-receipt JSON. Mirrors qor_receipt/canonical.py.
// Numbers are canonical integers in [-(2^53-1), 2^53-1]: no fraction, exponent,
// leading zero, or -0. Duplicate keys and unpaired surrogates are rejected.
// JSON.parse alone cannot see these; verifyReceipt has to be given the text.

const MAX_SAFE_DIGITS = "9007199254740991";
// spec/match-receipt-v0.md §1. Same numbers in qor_receipt/canonical.py.
export const MAX_RECEIPT_BYTES = 4000000;
export const MAX_DEPTH = 64;
const FORBIDDEN = new Set(["__proto__", "constructor", "prototype"]);
const ESC = { "\"": "\"", "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
const HEXDIG = "0123456789abcdefABCDEF";

export function parseCanonical(text) {
  if (typeof text !== "string" || text.charCodeAt(0) === 0xfeff) return { ok: false, errors: ["noncanonical_json"] };
  try {
    if (new TextEncoder().encode(text).length > MAX_RECEIPT_BYTES) return { ok: false, errors: ["noncanonical_json"] };
    return { ok: true, value: new Parser(text).parse(), errors: [] };
  } catch (e) {
    if (e && e.canonical) return { ok: false, errors: [e.canonical] };
    return { ok: false, errors: ["noncanonical_json"] };
  }
}

export function structuralErrors(value) {
  const found = [];
  const add = (code) => { if (!found.includes(code)) found.push(code); };
  walk(value, add);
  signatures(value, add);
  return found;
}

function fail(code) {
  const e = new Error(code);
  e.canonical = code;
  throw e;
}

class Parser {
  constructor(text) { this.s = text; this.n = text.length; this.i = 0; this.depth = 0; }
  parse() {
    const value = this.value();
    this.ws();
    if (this.i !== this.n) fail("noncanonical_json");
    return value;
  }
  ws() {
    const s = this.s;
    let i = this.i;
    while (i < this.n) {
      const c = s.charCodeAt(i);
      if (c !== 32 && c !== 9 && c !== 10 && c !== 13) break;
      i++;
    }
    this.i = i;
  }
  peek() { return this.i < this.n ? this.s[this.i] : ""; }
  value() {
    this.ws();
    const c = this.peek();
    if (c === "{") return this.object();
    if (c === "[") return this.array();
    if (c === "\"") return this.string();
    if (c === "t") return this.lit("true", true);
    if (c === "f") return this.lit("false", false);
    if (c === "n") return this.lit("null", null);
    if (c === "-" || (c >= "0" && c <= "9")) return this.number();
    fail("noncanonical_json");
  }
  lit(word, value) {
    if (this.s.slice(this.i, this.i + word.length) !== word) fail("noncanonical_json");
    this.i += word.length;
    return value;
  }
  object() {
    this.i++;
    this.depth++;
    if (this.depth > MAX_DEPTH) fail("noncanonical_json");
    try { return this.objectBody(); }
    finally { this.depth--; }
  }
  objectBody() {
    this.ws();
    const out = Object.create(null);
    if (this.peek() === "}") { this.i++; return out; }
    for (;;) {
      this.ws();
      if (this.peek() !== "\"") fail("noncanonical_json");
      const key = this.string();
      this.ws();
      if (this.peek() !== ":") fail("noncanonical_json");
      this.i++;
      const val = this.value();
      if (FORBIDDEN.has(key)) fail("noncanonical_json");
      if (Object.prototype.hasOwnProperty.call(out, key)) fail("noncanonical_duplicate_key");
      Object.defineProperty(out, key, { value: val, writable: true, enumerable: true, configurable: true });
      this.ws();
      const c = this.peek();
      if (c === ",") { this.i++; continue; }
      if (c === "}") { this.i++; return out; }
      fail("noncanonical_json");
    }
  }
  array() {
    this.i++;
    this.depth++;
    if (this.depth > MAX_DEPTH) fail("noncanonical_json");
    try { return this.arrayBody(); }
    finally { this.depth--; }
  }
  arrayBody() {
    this.ws();
    const out = [];
    if (this.peek() === "]") { this.i++; return out; }
    for (;;) {
      out.push(this.value());
      this.ws();
      const c = this.peek();
      if (c === ",") { this.i++; continue; }
      if (c === "]") { this.i++; return out; }
      fail("noncanonical_json");
    }
  }
  string() {
    const s = this.s;
    let i = this.i + 1;
    const parts = [];
    while (i < this.n) {
      const c = s[i];
      if (c === "\"") { this.i = i + 1; return parts.join(""); }
      if (c === "\\") {
        i++;
        if (i >= this.n) fail("noncanonical_json");
        const esc = s[i++];
        if (Object.prototype.hasOwnProperty.call(ESC, esc)) { parts.push(ESC[esc]); continue; }
        if (esc !== "u" || i + 4 > this.n || !hex4(s, i)) fail("noncanonical_json");
        const unit = parseInt(s.slice(i, i + 4), 16);
        i += 4;
        if (unit >= 0xD800 && unit <= 0xDBFF) {
          if (!(i + 6 <= this.n && s[i] === "\\" && s[i + 1] === "u" && hex4(s, i + 2))) fail("noncanonical_surrogate");
          const low = parseInt(s.slice(i + 2, i + 6), 16);
          if (low < 0xDC00 || low > 0xDFFF) fail("noncanonical_surrogate");
          i += 6;
          parts.push(String.fromCodePoint(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)));
          continue;
        }
        if (unit >= 0xDC00 && unit <= 0xDFFF) fail("noncanonical_surrogate");
        parts.push(String.fromCharCode(unit));
        continue;
      }
      const code = s.charCodeAt(i);
      if (code < 0x20) fail("noncanonical_json");
      if (code >= 0xD800 && code <= 0xDFFF) fail("noncanonical_surrogate");
      parts.push(c);
      i++;
    }
    fail("noncanonical_json");
  }
  number() {
    const s = this.s;
    let i = this.i;
    const start = i;
    if (s[i] === "-") {
      i++;
      if (i >= this.n || s[i] < "0" || s[i] > "9") fail("noncanonical_json");
    }
    if (i >= this.n || s[i] < "0" || s[i] > "9") fail("noncanonical_json");
    if (s[i] === "0") {
      i++;
      if (i < this.n && s[i] >= "0" && s[i] <= "9") fail("noncanonical_number");
    } else {
      while (i < this.n && s[i] >= "0" && s[i] <= "9") i++;
    }
    if (i < this.n && (s[i] === "." || s[i] === "e" || s[i] === "E")) {
      if (s[i] === ".") {
        i++;
        if (i >= this.n || s[i] < "0" || s[i] > "9") fail("noncanonical_json");
        while (i < this.n && s[i] >= "0" && s[i] <= "9") i++;
      }
      if (i < this.n && (s[i] === "e" || s[i] === "E")) {
        i++;
        if (i < this.n && (s[i] === "+" || s[i] === "-")) i++;
        if (i >= this.n || s[i] < "0" || s[i] > "9") fail("noncanonical_json");
        while (i < this.n && s[i] >= "0" && s[i] <= "9") i++;
      }
      this.i = i;
      fail("noncanonical_number");
    }
    const lex = s.slice(start, i);
    this.i = i;
    if (lex === "-0") fail("noncanonical_number");
    const digits = lex[0] === "-" ? lex.slice(1) : lex;
    if (digits.length > MAX_SAFE_DIGITS.length || (digits.length === MAX_SAFE_DIGITS.length && digits > MAX_SAFE_DIGITS)) {
      fail("noncanonical_number");
    }
    return Number(lex);
  }
}

function hex4(s, i) {
  for (let k = 0; k < 4; k++) if (!HEXDIG.includes(s[i + k])) return false;
  return true;
}

function loneSurrogate(text) {
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c >= 0xD800 && c <= 0xDBFF) {
      const n = text.charCodeAt(i + 1);
      if (!(n >= 0xDC00 && n <= 0xDFFF)) return true;
      i++;
    } else if (c >= 0xDC00 && c <= 0xDFFF) return true;
  }
  return false;
}

function isLowerHex(text, length) {
  if (typeof text !== "string" || text.length !== length) return false;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (!((c >= 48 && c <= 57) || (c >= 97 && c <= 102))) return false;
  }
  return true;
}

// Lengths taken from schema/receipt.v0.json hex patterns. A key named hash or sig
// somewhere else (review, signals, delegation) is not checked here.
function hexLen(path) {
  const n = path.length;
  const a = path[0], b = path[1], c = path[2], d = path[3];
  if (n === 3 && a === "players" && c === "pubkey") return 64;
  if (n === 2 && a === "referee" && b === "pubkey") return 64;
  if (n === 2 && a === "referee" && b === "key_id") return 16;
  if (n === 3 && a === "challenges" && c === "nonce") return 32;
  if (n === 4 && a === "challenges" && c === "result" && d === "hid_sha256") return 64;
  if (n === 4 && a === "checkpoints" && (b === "home" || b === "away")) {
    if (d === "frames_hash" || d === "input_hash" || d === "prev" || d === "hash") return 64;
    if (d === "referee_sig") return 128;
  }
  if (n === 4 && a === "signatures" && b === "players") {
    if (d === "key_id") return 16;
    if (d === "sig") return 128;
  }
  if (n === 3 && a === "signatures" && b === "referee") {
    if (c === "key_id") return 16;
    if (c === "sig") return 128;
  }
  return 0;
}

function ownValue(obj, key) {
  const desc = Object.getOwnPropertyDescriptor(obj, key);
  return desc && "value" in desc ? desc.value : undefined;
}

function walk(value, add, path = [], depth = 0) {
  if (depth > MAX_DEPTH) { add("noncanonical_json"); return; }
  if (typeof value === "number") {
    if (Object.is(value, -0) || !Number.isSafeInteger(value)) add("noncanonical_number");
    return;
  }
  if (typeof value === "string") {
    if (loneSurrogate(value)) add("noncanonical_surrogate");
    return;
  }
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) walk(value[i], add, path.concat(i), depth + 1);
    return;
  }
  if (value && typeof value === "object") {
    for (const key of Object.keys(value)) {
      if (FORBIDDEN.has(key)) add("noncanonical_json");
      if (loneSurrogate(key)) add("noncanonical_surrogate");
      const item = ownValue(value, key);
      const child = path.concat(key);
      const length = hexLen(child);
      if (length && typeof item === "string" && !isLowerHex(item, length)) add("noncanonical_hex");
      walk(item, add, child, depth + 1);
    }
  }
}

function sigShape(entry) {
  return !!entry && typeof entry === "object" && !Array.isArray(entry)
    && Object.keys(entry).length === 2 && Object.prototype.hasOwnProperty.call(entry, "key_id")
    && Object.prototype.hasOwnProperty.call(entry, "sig");
}

function signatures(value, add) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const sigs = value.signatures;
  if (!sigs || typeof sigs !== "object" || Array.isArray(sigs)) return;
  const keys = Object.keys(sigs);
  if (keys.length !== 2 || !Object.prototype.hasOwnProperty.call(sigs, "players") || !Object.prototype.hasOwnProperty.call(sigs, "referee")) {
    add("noncanonical_signatures");
  }
  let players = sigs.players;
  if (!players || typeof players !== "object" || Array.isArray(players)) {
    add("noncanonical_signatures");
    players = {};
  } else {
    for (const entry of Object.values(players)) {
      if (!sigShape(entry)) { add("noncanonical_signatures"); break; }
    }
  }
  if (Object.prototype.hasOwnProperty.call(sigs, "referee") && sigs.referee != null && !sigShape(sigs.referee)) {
    add("noncanonical_signatures");
  }
  const roles = new Set();
  if (Array.isArray(value.players)) {
    for (const player of value.players) {
      if (player && typeof player.role === "string") roles.add(player.role);
    }
  }
  for (const role of Object.keys(players)) if (!roles.has(role)) add("noncanonical_signatures");
}
