// node verify/hands.test.mjs : Show Your Hands panel (Meta AI Developer Hackathon; see HACKATHON.md).
import { readFileSync } from "node:fs";
import * as ed from "./vendor/noble-ed25519.js";
import { canonicalize, verifyReceipt } from "./verify.js";
import { checkHands, applyHands, HANDS_CODE } from "./hands-view.js";

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
const hdoc = read("../site/matches/170123/hands.json");
const v = await verifyReceipt(receipt, receipt.referee.pubkey);

// 1. Published 170123: valid, inputs verified, nothing scored, status stays VERIFIED.
const h = await checkHands(hdoc, receipt);
ok(h.ok && h.inputsVerified && h.cap === null && h.flags === 0, "published 170123 hands.json is valid, no flags");
ok(h.reports === 31184 && h.chunks === 471, "inputs: 31,184 reports in 471 committed chunks");
ok(applyHands(v, h).status === "VERIFIED", "170123 stays VERIFIED");
ok(!/HID\\|VID_|PID_|instance|Users/i.test(JSON.stringify(hdoc)), "no device path / user path in the published file");
ok(!hdoc.clips.some((c) => "track" in c), "published file carries no per-frame controller track");

// 2. A signed flagged file lowers VERIFIED to PARTIAL only.
const fl = structuredClone(hdoc);
fl.clips[0] = { ...fl.clips[0], scorable: true, flags: [{ kind: "no_press", t_ms: 1 }, { kind: "no_press", t_ms: 2 }] }; delete fl.clips[0].why; fl.cap = "PARTIAL";
const hf = await checkHands(await resign(fl), receipt, { keys });
ok(hf.ok && hf.cap === "PARTIAL", "signed flagged file accepted (test key)");
const a = applyHands(v, hf);
ok(a.status === "PARTIAL" && a.partial.includes(HANDS_CODE), "flags lower VERIFIED to PARTIAL");
for (const st of ["PARTIAL", "SELF_ATTESTED", "FAIL"]) ok(applyHands({ ...v, status: st }, hf).status === st, `flags leave ${st} unchanged`);

// 3. Invalid => hidden, status untouched.
ok(!(await checkHands(fl, receipt)).ok, "edited file => bad signature => hidden");
ok(applyHands(v, null).status === "VERIFIED" && !("show_your_hands" in applyHands(v, null)), "no file => nothing changes");
const one = structuredClone(fl); one.clips[0].flags = [{ kind: "no_press", t_ms: 1 }];
ok((await checkHands(await resign(one), receipt, { keys })).problems.includes("cap_inconsistent"), "one flag can't cap");
const unv = structuredClone(fl); unv.inputs_verified = false;
ok((await checkHands(await resign(unv), receipt, { keys })).problems.includes("cap_inconsistent"), "unverified inputs can't cap");
const failCap = structuredClone(fl); failCap.cap = "FAIL";
ok((await checkHands(await resign(failCap), receipt, { keys })).problems.includes("bad_cap"), "FAIL cap rejected");
const leak = structuredClone(hdoc); leak.note = "HID\\VID_054C&PID_0DF2";
ok((await checkHands(await resign(leak), receipt, { keys })).problems.includes("identifying_data"), "device path => rejected");
const leak2 = structuredClone(hdoc); leak2.device_instance_path = "x";
ok((await checkHands(await resign(leak2), receipt, { keys })).problems.includes("identifying_data"), "device path key => rejected");
const other = structuredClone(receipt); other.result.home_score = 3;
ok((await checkHands(hdoc, other)).problems.includes("receipt_mismatch"), "other receipt => hidden");
ok(!(await checkHands(await resign(fl), receipt)).ok, "unlisted key => hidden");
for (const bad of [null, 3, "x", [], {}]) ok(!(await checkHands(bad, receipt)).ok, `malformed ${JSON.stringify(bad)} => hidden`);
ok(!/innerHTML/.test(readFileSync(new URL("./hands-view.js", import.meta.url), "utf8")), "panel: no innerHTML");
process.exit(fails ? 1 : 0);
