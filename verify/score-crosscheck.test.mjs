// node verify/score-crosscheck.test.mjs : Muse Spark score cross-check (Meta AI Developer Hackathon; see HACKATHON.md).
import { readFileSync } from "node:fs";
import * as ed from "./vendor/noble-ed25519.js";
import { canonicalize, verifyReceipt } from "./verify.js";
import { checkCrosscheck, applyCrosscheck, XC_CODE } from "./score-crosscheck-view.js";
import { acceptLowerOnly } from "./lower-only.js";

let fails = 0;
const ok = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
const read = (p) => JSON.parse(readFileSync(new URL(p, import.meta.url)));
const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const unhex = (s) => Uint8Array.from(s.match(/../g).map((x) => parseInt(x, 16)));
const kidOf = async (pub) => hex(new Uint8Array(await crypto.subtle.digest("SHA-256", unhex(pub)))).slice(0, 16);
const without = (o, k) => Object.fromEntries(Object.entries(o).filter(([x]) => x !== k));
const priv = ed.utils.randomPrivateKey(), pub = hex(await ed.getPublicKeyAsync(priv));
const keys = { keys: [{ id: "t", pubkey: pub, key_kind: "qortime-ai-advisory", status: "active" }] };
async function resign(doc) {
  const d = { ...without(doc, "advisory_signature"), advisory_key: { ...doc.advisory_key, pubkey: pub, key_id: await kidOf(pub) } };
  d.advisory_signature = { alg: "Ed25519", key_id: await kidOf(pub), sig: hex(await ed.signAsync(new TextEncoder().encode(canonicalize(d)), priv)) };
  return d;
}

const receipt = read("../site/matches/170123/receipt.json");
const xcDoc = read("../site/matches/170123/score_crosscheck.json");
const v = await verifyReceipt(receipt, receipt.referee.pubkey);
const vJson = JSON.stringify(v);

// 1. Published 170123 cross-check: valid, Muse agrees, status stays VERIFIED.
const xc = await checkCrosscheck(xcDoc, receipt);
ok(xc.ok && xc.cap === null && xc.agree >= 1 && xc.disagree === 0, "published 170123 cross-check is valid and agrees");
const a = applyCrosscheck(v, xc);
ok(a.status === "VERIFIED" && a.muse_crosscheck === "agree" && JSON.stringify(v) === vJson, "agreement keeps VERIFIED; verifier output not mutated");
ok(xcDoc.cost_micro_usd < 500000, "per-match cost recorded and well under $0.50");

// 2. A signed disagreement lowers VERIFIED to PARTIAL, and only that.
const dis = structuredClone(xcDoc); dis.locks[0].result = "disagree"; dis.cap = "PARTIAL";
const xcd = await checkCrosscheck(await resign(dis), receipt, { keys });
ok(xcd.ok && xcd.cap === "PARTIAL", "signed disagreement is accepted (test key)");
const ad = applyCrosscheck(v, xcd);
ok(ad.status === "PARTIAL" && ad.partial.includes(XC_CODE) && ad.muse_crosscheck === "disagree", "disagreement lowers VERIFIED to PARTIAL");
for (const st of ["PARTIAL", "SELF_ATTESTED", "FAIL"]) ok(applyCrosscheck({ ...v, status: st }, xcd).status === st, `disagreement leaves ${st} unchanged (never raises, never FAIL)`);

// 3. Invalid sidecars are ignored: status exactly as verify.js computed ("Muse unchecked").
ok((await checkCrosscheck(dis, receipt)).problems.includes("bad_advisory_signature"), "edited sidecar => bad signature => ignored");
ok(applyCrosscheck(v, await checkCrosscheck(dis, receipt)).status === "VERIFIED", "ignored sidecar => VERIFIED unchanged");
ok(applyCrosscheck(v, null).status === "VERIFIED" && applyCrosscheck(v, null).muse_crosscheck === "unchecked", "no sidecar => Muse unchecked, no penalty");
const other = structuredClone(receipt); other.result.home_score = 3;
ok((await checkCrosscheck(xcDoc, other)).problems.includes("receipt_mismatch"), "sidecar for another receipt => ignored");
const failCap = structuredClone(dis); failCap.cap = "FAIL";
ok((await checkCrosscheck(await resign(failCap), receipt, { keys })).problems.includes("bad_cap"), "a FAIL cap is never accepted");
const incons = structuredClone(xcDoc); incons.cap = "PARTIAL";
ok((await checkCrosscheck(await resign(incons), receipt, { keys })).problems.includes("cap_inconsistent"), "cap without a disagreeing lock is rejected");
const notLower = structuredClone(xcDoc); notLower.affects_verdict = true;
ok((await checkCrosscheck(await resign(notLower), receipt, { keys })).problems.includes("not_lower_only"), "must declare lower_only");
ok(!(await checkCrosscheck(await resign(dis), receipt)).ok, "validly signed by an unlisted key => ignored");
const asRef = structuredClone(receipt); asRef.referee.pubkey = pub;
ok((await checkCrosscheck(await resign(dis), asRef, { keys })).problems.includes("advisory_key_is_a_referee_key"), "referee key can't sign a cross-check");
for (const bad of [null, 3, "x", [], {}]) ok(!(await checkCrosscheck(bad, receipt)).ok, `malformed ${JSON.stringify(bad)} => ignored`);

// 4. Page guard: adjustments can only keep the status or lower VERIFIED to PARTIAL.
const S = ["VERIFIED", "PARTIAL", "SELF_ATTESTED", "FAIL"];
for (const from of S) for (const to of S) {
  const got = acceptLowerOnly({ status: from }, { status: to }).status;
  const want = to === from || (from === "VERIFIED" && to === "PARTIAL") ? to : from;
  ok(got === want, `guard ${from} -> ${to} gives ${want}`);
}
ok(acceptLowerOnly({ status: "PARTIAL" }, null).status === "PARTIAL", "guard ignores null");
ok(!/innerHTML|gold/i.test(readFileSync(new URL("./score-crosscheck-view.js", import.meta.url), "utf8").replace(/gold still requires|Never gold/g, "")), "panel: no innerHTML, no gold");
// 220105: the referee's signed cross-check, published byte for byte next to the receipt. Agreement keeps VERIFIED.
{
  const r2 = read("../site/matches/220105/receipt.json"), x2 = read("../site/matches/220105/score_crosscheck.json");
  const v2 = await verifyReceipt(r2, r2.referee.pubkey);
  const c2 = await checkCrosscheck(x2, r2);
  ok(c2.ok && c2.cap === null && c2.agree >= 1 && c2.disagree === 0, "published 220105 cross-check is valid and agrees");
  ok(applyCrosscheck(v2, c2).status === "VERIFIED", "220105 stays VERIFIED with its cross-check");
  ok(!(await checkCrosscheck(x2, receipt)).ok, "220105 cross-check does not apply to the 170123 receipt");
}
process.exit(fails ? 1 : 0);
