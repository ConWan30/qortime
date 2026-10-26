// node verify/ai-ruling.test.mjs : advisory AI explanation panel checks (Meta AI Developer Hackathon; see HACKATHON.md).
import { readFileSync } from "node:fs";
import * as ed from "./vendor/noble-ed25519.js";
import { canonicalize, sha256Hex, verifyReceipt } from "./verify.js";
import { checkAiRuling } from "./ai-ruling-view.js";

let fails = 0;
const ok = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
const read = (p) => JSON.parse(readFileSync(new URL(p, import.meta.url)));
const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const unhex = (s) => Uint8Array.from(s.match(/../g).map((x) => parseInt(x, 16)));
const kidOf = async (pub) => hex(new Uint8Array(await crypto.subtle.digest("SHA-256", unhex(pub)))).slice(0, 16);
const without = (o, k) => Object.fromEntries(Object.entries(o).filter(([x]) => x !== k));
async function resign(doc, priv) {
  const pub = hex(await ed.getPublicKeyAsync(priv));
  const d = { ...without(doc, "advisory_signature"), advisory_key: { ...doc.advisory_key, pubkey: pub, key_id: await kidOf(pub) } };
  d.advisory_signature = { alg: "Ed25519", key_id: await kidOf(pub), sig: hex(await ed.signAsync(new TextEncoder().encode(canonicalize(d)), priv)) };
  return { d, pub };
}

const receipt = read("../site/matches/170123/receipt.json");
const ruling = read("../site/matches/170123/ai_ruling.json");
const v = await verifyReceipt(receipt, receipt.referee.pubkey);
const vBefore = JSON.stringify(v);

let r = await checkAiRuling(ruling, receipt, v.status);
ok(r.ok && r.text.startsWith("STATUS") && r.text.includes("VERIFIED"), "published 170123 ai_ruling.json shows for the VERIFIED result");
ok(JSON.stringify(await verifyReceipt(receipt, receipt.referee.pubkey)) === vBefore, "checking the AI explanation does not change the verdict");
ok(!(await checkAiRuling(ruling, receipt, "PARTIAL")).ok, "hidden when the live verdict differs from the echoed one (stale)");
ok((await checkAiRuling(ruling, receipt, "PARTIAL")).problems.join() === "verdict_echo_stale", "stale is the only problem for an unpinned check");

const t1 = structuredClone(ruling); t1.explanation += " Extra words.";
ok((await checkAiRuling(t1, receipt, "VERIFIED")).problems.includes("bad_advisory_signature"), "edited explanation => hidden (bad signature)");
const t2 = structuredClone(receipt); t2.result.home_score = 3;
ok((await checkAiRuling(ruling, t2, "VERIFIED")).problems.includes("receipt_mismatch"), "explanation for another receipt => hidden");
const t3 = structuredClone(ruling); t3.signature = t3.advisory_signature; delete t3.advisory_signature;
ok(!(await checkAiRuling(t3, receipt, "VERIFIED")).ok, "a 'signature' field (referee-style) is never accepted");
const t4 = structuredClone(ruling); t4.affects_verdict = true;
ok((await checkAiRuling(t4, receipt, "VERIFIED")).problems.includes("affects_verdict_not_false"), "affects_verdict must be false");

const priv = ed.utils.randomPrivateKey();
const { d: other, pub: otherPub } = await resign(ruling, priv);
ok((await checkAiRuling(other, receipt, "VERIFIED")).problems.join() === "advisory_key_not_allowlisted", "validly signed by an unlisted key => hidden");
const keys = { keys: [{ id: "t", pubkey: otherPub, key_kind: "qortime-ai-advisory", status: "active" }] };
ok((await checkAiRuling(other, receipt, "VERIFIED", { keys })).ok, "same doc with that key allowlisted => shown (signature path works)");
ok(!(await checkAiRuling(other, receipt, "VERIFIED", { keys: { keys: [{ ...keys.keys[0], status: "revoked" }] } })).ok, "revoked advisory key => hidden");
const asRef = structuredClone(receipt); asRef.referee.pubkey = otherPub;
ok((await checkAiRuling(other, asRef, "VERIFIED", { keys })).problems.includes("advisory_key_is_a_referee_key"), "the receipt's referee key can't double as an advisory key");
for (const bad of [null, 1, "x", [], {}]) ok(!(await checkAiRuling(bad, receipt, "VERIFIED")).ok, `malformed ${JSON.stringify(bad)} => hidden`);
ok((await sha256Hex(receipt)) === ruling.receipt_jcs_sha256, "browser JCS hash == Python receipt_jcs_sha256");

const src = readFileSync(new URL("./ai-ruling-view.js", import.meta.url), "utf8");
ok(!/gold/i.test(src.replace(/never uses gold|no gold/gi, "")) && !/innerHTML/.test(src), "panel code has no gold styling and no innerHTML");
const css = readFileSync(new URL("./viewer.css", import.meta.url), "utf8");
const aiCss = css.slice(css.indexOf("/* Hackathon: advisory AI explanation panel"));
ok(aiCss.length > 0 && !/gold|--gold|#d4af37|#ffd/i.test(aiCss.replace("never gold", "")), "panel CSS uses no gold");
process.exit(fails ? 1 : 0);
