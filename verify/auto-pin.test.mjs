// node verify/auto-pin.test.mjs : verifier auto-pin (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026; see verify/AUTO_PIN.md).
import { readFileSync } from "node:fs";
import * as ed from "./vendor/noble-ed25519.js";
import { canonicalize, sha256Hex, verifyReceipt, loadAllowlist } from "./verify.js";
import { autoPinFor } from "./auto-pin.js";

let fails = 0;
const ok = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const unhex = (s) => Uint8Array.from(s.match(/../g).map((x) => parseInt(x, 16)));
const enc = new TextEncoder();
const kidOf = async (pub) => hex(new Uint8Array(await crypto.subtle.digest("SHA-256", unhex(pub)))).slice(0, 16);
const signDoc = async (doc, priv) => ({ ...doc, sig: hex(await ed.signAsync(enc.encode(canonicalize(doc)), priv)) });
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const read = (p) => JSON.parse(readFileSync(new URL(p, import.meta.url)));

// 1. Match 170123 (delegated online key, serial 2 from root qortime-v1): auto-pinned => VERIFIED, identical to a manual pin.
const live = read("../site/matches/170123/receipt.json");
const before = JSON.stringify(live);
const ap = await autoPinFor(live);
ok(ap.pin === live.referee.pubkey.toLowerCase() && ap.source === "delegation", "170123: auto-pin picks its delegated referee key");
const vAuto = await verifyReceipt(live, ap.pin);
const vManual = await verifyReceipt(live, live.referee.pubkey);
ok(vAuto.status === "VERIFIED" && vAuto.partial.length === 0 && vAuto.errors.length === 0, "170123: auto-pinned result is VERIFIED");
ok(same(vAuto, vManual), "170123: auto-pinned result is identical to the manual pin");
ok(JSON.stringify(live) === before, "170123: auto-pin does not modify the receipt");

// 2. Auto-pin never rescues a tampered receipt.
const tampered = structuredClone(live); tampered.result.home_score = 99;
const apT = await autoPinFor(tampered);
ok((await verifyReceipt(tampered, apT.pin)).status === "FAIL", "tampered 170123 is still FAIL with auto-pin");

// 3. Self-signed / ephemeral demo referee: never auto-pinned, result unchanged vs. no pin.
const d = read("../tests/vectors/demo_receipt_dual.json");
const apD = await autoPinFor(d.receipt);
ok(apD.pin === null && apD.reason === "not_on_allowlist", "ephemeral demo referee is never auto-pinned");
ok(same(await verifyReceipt(d.receipt, apD.pin), await verifyReceipt(d.receipt, null)), "unknown referee: result identical to before");
const sample = read("./sample-receipt.json");
const apS = await autoPinFor(sample);
ok(apS.pin === null || loadAllowlist().referees.some((r) => r.status === "active" && r.pubkey.toLowerCase() === apS.pin) || apS.source === "delegation",
   "sample receipt is auto-pinned only if its key is trusted");

// 4. An attacker key that copies 170123's delegation cert is not pinned (delegation_key_mismatch).
const attackerPriv = ed.utils.randomPrivateKey();
const attackerPub = hex(await ed.getPublicKeyAsync(attackerPriv));
const forged = structuredClone(live); forged.referee.pubkey = attackerPub;
const apF = await autoPinFor(forged);
ok(apF.pin === null && apF.reason.includes("delegation_key_mismatch"), "copied delegation on an attacker key is not auto-pinned");
ok(same(await verifyReceipt(forged, apF.pin), await verifyReceipt(forged, null)), "attacker key: result identical to before");

// 5. Referee equals a player, local self-attested, malformed inputs.
const selfRef = structuredClone(live); selfRef.players[0].pubkey = selfRef.referee.pubkey;
ok((await autoPinFor(selfRef)).pin === null, "referee key equal to a player key is never auto-pinned");
const local = structuredClone(d.receipt); local.referee.key_kind = "local-self-attested-start-match";
ok((await autoPinFor(local)).pin === null, "local self-attested referee is never auto-pinned");
for (const bad of [null, 7, "x", [], {}, { referee: {} }, { referee: { pubkey: "zz" } }]) {
  ok((await autoPinFor(bad)).pin === null, `malformed input ${JSON.stringify(bad)} => no pin`);
}

// 6. Synthetic root + delegation: valid => pinned; revoked => not; root not allowlisted => not; revoked root row => not.
const rootPriv = ed.utils.randomPrivateKey(), rootPub = hex(await ed.getPublicKeyAsync(rootPriv)), rootKid = await kidOf(rootPub);
const delPriv = ed.utils.randomPrivateKey(), delPub = hex(await ed.getPublicKeyAsync(delPriv));
const allow = { referees: [{ id: "test-root", pubkey: rootPub, status: "active", can_delegate: true }] };
const cert = await signDoc({ type: "qortime-referee-delegation/v1", root_key_id: rootKid, delegate_pubkey: delPub, delegate_key_id: await kidOf(delPub),
  delegate_id: "test-online-s7", serial: 7, scope: ["official-review"], allowed_statuses: ["VERIFIED", "PARTIAL"],
  not_before_ms: 1000, not_after_ms: 1000 + 86400000 }, rootPriv);
const revNone = await signDoc({ type: "qortime-referee-revocations/v1", root_key_id: rootKid, revoked: [] }, rootPriv);
const revSeven = await signDoc({ type: "qortime-referee-revocations/v1", root_key_id: rootKid, revoked: [{ serial: 7 }] }, rootPriv);
const synth = { referee: { pubkey: delPub, key_kind: "qortime-official-review", delegation: cert }, players: [], review: { reviewed_ms: 5000 } };
const okDel = await autoPinFor(synth, { allowlist: allow, revocations: revNone, nowMs: 10000 });
ok(okDel.pin === delPub && okDel.source === "delegation", "valid delegation from an allowlisted root => auto-pinned");
const revoked = await autoPinFor(synth, { allowlist: allow, revocations: revSeven, nowMs: 10000 });
ok(revoked.pin === null && revoked.reason.includes("delegation_revoked:7"), "revoked delegation is never auto-pinned");
ok((await autoPinFor(synth, { allowlist: { referees: [] }, revocations: revNone, nowMs: 10000 })).pin === null, "delegation from a non-allowlisted root is never auto-pinned");
const late = structuredClone(synth); late.review.reviewed_ms = 1000 + 2 * 86400000;
ok((await autoPinFor(late, { allowlist: allow, revocations: revNone, nowMs: 1e13 })).pin === null, "expired delegation is never auto-pinned");
const wrongKind = structuredClone(synth); wrongKind.referee.key_kind = "something-else";
ok((await autoPinFor(wrongKind, { allowlist: allow, revocations: revNone, nowMs: 10000 })).pin === null, "delegation outside official-review scope is never auto-pinned");
ok((await autoPinFor({ referee: { pubkey: rootPub }, players: [] }, { allowlist: allow })).source === "allowlist", "active allowlisted root key => auto-pinned");
const allowRevoked = { referees: [{ id: "test-root", pubkey: rootPub, status: "revoked", can_delegate: true }] };
ok((await autoPinFor({ referee: { pubkey: rootPub }, players: [] }, { allowlist: allowRevoked })).pin === null, "revoked allowlist row is never auto-pinned");
ok((await autoPinFor(synth, { allowlist: allowRevoked, revocations: revNone, nowMs: 10000 })).pin === null, "delegation from a revoked root is never auto-pinned");

// 7. The page wires it only for an empty pin box and still sends every verdict through verify.js.
const page = readFileSync(new URL("./index.html", import.meta.url), "utf8");
ok(/if \(!pin\) \{ auto = await autoPinFor\(currentReceipt\(\)\); pin = auto\.pin; \}/.test(page), "index.html auto-pins only when the pin box is empty");
ok(!/auto-pin|autoPin/.test(readFileSync(new URL("./verify.js", import.meta.url), "utf8")), "verify.js has no auto-pin logic (library semantics unchanged)");
ok((await sha256Hex(live)).length === 64, "sanity");

process.exit(fails ? 1 : 0);
