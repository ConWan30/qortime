// Shared with tests/test_adversarial_parity.py. Both read tests/fixtures/adversarial/manifest.json.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { MAX_DEPTH, MAX_RECEIPT_BYTES, parseCanonical, structuralErrors, verifyReceipt } from "./verify.js";

const root = new URL("../", import.meta.url);

function loadBase() {
  const manifest = JSON.parse(readFileSync(new URL("tests/fixtures/adversarial/manifest.json", root), "utf8"));
  const base = readFileSync(new URL(manifest.base, root), "utf8").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const pin = JSON.parse(base).referee.pubkey;
  return { manifest, base, pin };
}

function apply(base, cse) {
  if (cse.generate === "depth") return "[".repeat(MAX_DEPTH + 1) + "]".repeat(MAX_DEPTH + 1);
  if (cse.generate === "size") return "[" + " ".repeat(MAX_RECEIPT_BYTES) + "]";
  let text = base;
  if (cse.replace) {
    const [old, neu] = cse.replace;
    const found = text.split(old).length - 1;
    if (found !== 1) throw new Error(`${cse.id}: needle count ${found}`);
    text = text.replace(old, neu);
  }
  if (cse.prefix) text = cse.prefix + text;
  if (cse.suffix) text = text + cse.suffix;
  return text;
}

export async function runAdversarial(ok) {
  const { manifest, base, pin } = loadBase();
  ok(manifest.cases[0].id === "baseline", "adversarial manifest starts from the published receipt");
  for (const cse of manifest.cases) {
    const text = apply(base, cse);
    const verdict = await verifyReceipt(text, pin);
    ok(verdict.status === cse.expect, `${cse.id} => ${cse.expect} (got ${verdict.status})`);
    if (cse.error) ok(verdict.errors.includes(cse.error), `${cse.id} error ${cse.error}`);
  }
  const expected = JSON.parse(readFileSync(new URL("tests/fixtures/adversarial/published_verdicts.json", root), "utf8"));
  for (const [matchId, modes] of Object.entries(expected)) {
    const raw = readFileSync(new URL(`site/matches/${matchId}/receipt.json`, root), "utf8");
    const pin = JSON.parse(raw).referee.pubkey;
    for (const [mode, want] of Object.entries(modes)) {
      const verdict = await verifyReceipt(raw, mode === "pinned" ? pin : null);
      ok(verdict.status === want.status && JSON.stringify(verdict.errors) === JSON.stringify(want.errors)
        && JSON.stringify(verdict.partial) === JSON.stringify(want.partial)
        && verdict.trust_tier === want.trust_tier && verdict.referee_trust === want.referee_trust
        && (verdict.referee_id ?? null) === want.referee_id,
        `published ${matchId} ${mode} stays ${want.status}`);
    }
  }
  const spec = readFileSync(new URL("spec/match-receipt-v0.md", root), "utf8");
  const src = readFileSync(new URL("verify/canonical.js", root), "utf8");
  const py = readFileSync(new URL("qor_receipt/canonical.py", root), "utf8");
  ok(spec.includes(`at most ${MAX_RECEIPT_BYTES} UTF-8 bytes`) && spec.includes(`at most ${MAX_DEPTH} nested`),
    "spec states the shared size and depth caps");
  ok(src.includes(`MAX_RECEIPT_BYTES = ${MAX_RECEIPT_BYTES}`) && src.includes(`MAX_DEPTH = ${MAX_DEPTH}`),
    "JS caps match the spec");
  ok(py.includes(`MAX_RECEIPT_BYTES = ${MAX_RECEIPT_BYTES}`) && py.includes(`MAX_DEPTH = ${MAX_DEPTH}`),
    "Python caps match the spec");
  const exact = "[]" + " ".repeat(MAX_RECEIPT_BYTES - 2);
  const atSize = parseCanonical(exact);
  ok(atSize.ok && Array.isArray(atSize.value) && atSize.value.length === 0, "a receipt at the size cap still parses");
  const atDepth = parseCanonical("[".repeat(MAX_DEPTH) + "]".repeat(MAX_DEPTH));
  ok(atDepth.ok, "nesting at the depth cap still parses");
  ok(!structuralErrors({ review: { bundle: { hash: "NOT-HEX" } } }).includes("noncanonical_hex"),
    "a hash outside the schema hex paths is not a hex failure");
  ok(structuralErrors({ players: [{ pubkey: "ABCD" }] }).includes("noncanonical_hex"),
    "player pubkey stays on the hex rule");
  const proto = JSON.parse(readFileSync(new URL("tests/fixtures/adversarial/proto_motion.json", root), "utf8"));
  const clean = await verifyReceipt(proto.clean, proto.pub, { allowlist: proto.allowlist });
  const injected = await verifyReceipt(proto.injected, proto.pub, { allowlist: proto.allowlist });
  ok(clean.status === "PARTIAL" && clean.errors.length === 0 && JSON.stringify(clean.partial) === JSON.stringify(["home:motion_signal_missing"]),
    "proto fixture without motion stays PARTIAL");
  ok(injected.status === "FAIL" && injected.errors.includes("noncanonical_json") && injected.status !== "VERIFIED",
    "signals.__proto__.motion_permille cannot upgrade PARTIAL to VERIFIED");
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  let fails = 0;
  const ok = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
  await runAdversarial(ok);
  process.exit(fails ? 1 : 0);
}
