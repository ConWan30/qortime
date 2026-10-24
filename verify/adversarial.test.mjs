// Shared with tests/test_adversarial_parity.py. Both read tests/fixtures/adversarial/manifest.json.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { verifyReceipt } from "./verify.js";

const root = new URL("../", import.meta.url);

function loadBase() {
  const manifest = JSON.parse(readFileSync(new URL("tests/fixtures/adversarial/manifest.json", root), "utf8"));
  const base = readFileSync(new URL(manifest.base, root), "utf8").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const pin = JSON.parse(base).referee.pubkey;
  return { manifest, base, pin };
}

function apply(base, cse) {
  if (!cse.replace) return base;
  const [old, neu] = cse.replace;
  const found = base.split(old).length - 1;
  if (found !== 1) throw new Error(`${cse.id}: needle count ${found}`);
  return base.replace(old, neu);
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
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  let fails = 0;
  const ok = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
  await runAdversarial(ok);
  process.exit(fails ? 1 : 0);
}
