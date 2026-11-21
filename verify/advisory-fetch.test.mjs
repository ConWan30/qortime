// node verify/advisory-fetch.test.mjs : verifier fallback to the referee's automatic advisory files (hackathon).
import { readFileSync } from "node:fs";
import { sha256Hex } from "./verify.js";
import { advisoryUrl, fetchAdvisoryText, isOfficialReview, refereeApi } from "./advisory-fetch.js";

let fails = 0;
const ok = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
const receipt = JSON.parse(readFileSync(new URL("../site/matches/170123/receipt.json", import.meta.url)));
const sha = await sha256Hex(receipt);
const API = "https://qortime-referee-production.up.railway.app";

ok(isOfficialReview(receipt), "170123 is an official-review receipt");
ok(await advisoryUrl(receipt, "ai_ruling.json") === `${API}/v1/advisory/${sha}/ai_ruling.json`, "referee URL is bound to SHA-256(JCS(receipt))");
ok(await advisoryUrl(receipt, "hands.json") === null, "only the two Muse files are fetched from the referee");
const self = { ...receipt, referee: { ...receipt.referee, key_kind: "software" } };
ok(await advisoryUrl(self, "ai_ruling.json") === null, "self-signed receipts never trigger a referee fetch");
ok(refereeApi("?referee_api=https://evil.example") === API, "referee_api override only for localhost");
ok(refereeApi("?referee_api=http://127.0.0.1:8080") === "http://127.0.0.1:8080", "localhost override for staging");

const calls = [];
const fake = (map) => async (u) => { calls.push(u); return u in map ? { ok: true, text: async () => map[u] } : { ok: false }; };
ok(await fetchAdvisoryText("https://x/ai_ruling.json", receipt, "ai_ruling.json", { fetchFn: fake({ "https://x/ai_ruling.json": "S" }) }) === "S"
  && calls.length === 1, "sibling file wins, no referee request");
calls.length = 0;
ok(await fetchAdvisoryText("https://x/ai_ruling.json", receipt, "ai_ruling.json", { fetchFn: fake({ [`${API}/v1/advisory/${sha}/ai_ruling.json`]: "R" }) }) === "R"
  && calls.length === 2, "missing sibling falls back to the referee copy");
ok(await fetchAdvisoryText(null, receipt, "score_crosscheck.json", { fetchFn: fake({}) }) === null, "nothing anywhere = null (Muse unchecked)");
ok(await fetchAdvisoryText(null, receipt, "ai_ruling.json", { fetchFn: async () => { throw new Error("net"); } }) === null, "network error = null, never throws");
process.exit(fails ? 1 : 0);
