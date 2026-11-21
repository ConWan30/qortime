import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import Stripe from "stripe";
import { clientIp, createApp } from "../src/app.mjs";
import {
  WEBHOOK_TOLERANCE_SECONDS,
  buildCheckoutParams,
  decideFulfillment,
  decideRefund,
  keyClass,
  lineItemsIncludePrice,
  lineItemsMatchCatalog,
} from "../src/fulfill.mjs";
import { receiptIdFrom } from "../src/receipt-id.mjs";
import { fileStore, memoryStore } from "../src/store.mjs";

const PRICE = "price_1UQruXCepyIJsHZulKEdcVkX";
const SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "../../..");

function listen(server) {
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

function close(server) {
  return new Promise((resolve, reject) => {
    server.close((err) => (err ? reject(err) : resolve()));
  });
}

test("receipt id comes from receipt_id or match.match_id", () => {
  assert.equal(receiptIdFrom({ receipt_id: "abc_1", match: { match_id: "other" } }), "abc_1");
  assert.equal(receiptIdFrom({ match: { match_id: "32b29a8a383c4849" } }), "32b29a8a383c4849");
  assert.equal(receiptIdFrom({ match: { match_id: "has space" } }), null);
  assert.equal(receiptIdFrom(null), null);
});

test("checkout params tie the receipt and omit payment method allowlists and tax", () => {
  const params = buildCheckoutParams("match_42", {
    priceId: PRICE,
    site: "https://conwan30.github.io/qortime",
  });
  assert.equal(params.mode, "payment");
  assert.equal(params.ui_mode, "hosted_page");
  assert.equal(params.origin_context, "web");
  assert.equal(params.client_reference_id, "match_42");
  assert.equal(params.metadata.receipt_id, "match_42");
  assert.equal(params.line_items[0].price, PRICE);
  assert.equal(params.line_items[0].quantity, 1);
  assert.equal(params.adaptive_pricing.enabled, true);
  assert.equal("payment_method_types" in params, false);
  assert.equal("automatic_tax" in params, false);
  assert.match(params.success_url, /^https:\/\/conwan30\.github\.io\/qortime\/verify\/\?checkout=success/);
  assert.match(params.success_url, /session_id=\{CHECKOUT_SESSION_ID\}$/);
  assert.match(params.cancel_url, /checkout=cancel#verified-match$/);
  assert.match(params.integration_identifier, /[a-z]{8}$/);
});

test("key class refuses live prefixes and accepts test prefixes", () => {
  assert.equal(keyClass(""), "missing");
  assert.equal(keyClass("rk_" + "live_" + "example"), "live");
  assert.equal(keyClass("sk_" + "live_" + "example"), "live");
  assert.equal(keyClass("rk_" + "test_" + "example"), "test");
  assert.equal(keyClass("not-a-key"), "unrecognized");
});

test("fulfillment records a paid test session only when the price matches", () => {
  const session = {
    id: "cs_test_123",
    livemode: false,
    mode: "payment",
    payment_status: "paid",
    currency: "usd",
    amount_total: 999,
    metadata: { receipt_id: "match_42", receipt_sha256: SHA },
    client_reference_id: "match_42",
  };
  const paid = decideFulfillment(
    { type: "checkout.session.completed", livemode: false },
    session,
    { priceOk: true },
  );
  assert.deepEqual(paid, { action: "record", receiptId: "match_42" });

  const unpaid = decideFulfillment(
    { type: "checkout.session.completed", livemode: false },
    { ...session, payment_status: "unpaid" },
    { priceOk: true },
  );
  assert.equal(unpaid.action, "pending");

  const wrongPrice = decideFulfillment(
    { type: "checkout.session.async_payment_succeeded", livemode: false },
    session,
    { priceOk: false },
  );
  assert.equal(wrongPrice.action, "ignore");
  assert.equal(wrongPrice.reason, "price");

  const live = decideFulfillment(
    { type: "checkout.session.completed", livemode: true },
    { ...session, livemode: true },
    { priceOk: true },
  );
  assert.equal(live.reason, "livemode");

  const zero = decideFulfillment(
    { type: "checkout.session.completed", livemode: false },
    { ...session, payment_status: "no_payment_required", amount_total: 0 },
    { priceOk: true },
  );
  assert.equal(zero.action, "ignore");
  assert.equal(zero.reason, "payment_status");

  const cheap = decideFulfillment(
    { type: "checkout.session.completed", livemode: false },
    { ...session, amount_total: 50 },
    { priceOk: true },
  );
  assert.equal(cheap.reason, "amount");

  const unbound = decideFulfillment(
    { type: "checkout.session.completed", livemode: false },
    { ...session, metadata: { receipt_id: "match_42" } },
    { priceOk: true },
  );
  assert.equal(unbound.reason, "receipt_sha256");

  const crossed = decideFulfillment(
    { type: "checkout.session.completed", livemode: false },
    { ...session, client_reference_id: "other_match" },
    { priceOk: true },
  );
  assert.equal(crossed.reason, "receipt_binding");

  assert.equal(lineItemsIncludePrice([{ price: { id: PRICE } }], PRICE), true);
  assert.equal(lineItemsIncludePrice([{ price: "price_other" }], PRICE), false);
  const catalog = { id: PRICE, unit_amount: 999, currency: "usd" };
  assert.equal(lineItemsMatchCatalog([{ quantity: 1, price: catalog }], PRICE), true);
  assert.equal(lineItemsMatchCatalog([{ quantity: 1, price: { ...catalog, unit_amount: 0 } }], PRICE), false);
  assert.equal(lineItemsMatchCatalog([{ quantity: 1, price: { ...catalog, currency: "eur" } }], PRICE), false);
  assert.equal(lineItemsMatchCatalog([{ quantity: 2, price: catalog }], PRICE), false);
  assert.equal(lineItemsMatchCatalog([{ quantity: 1, price: PRICE }], PRICE), false);

  assert.equal(decideRefund({ type: "charge.refunded", livemode: false }, { livemode: false, refunded: true, amount: 999, amount_refunded: 999 }).action, "revoke");
  assert.equal(decideRefund({ type: "charge.refunded", livemode: false }, { livemode: false, refunded: false, amount: 999, amount_refunded: 100 }).action, "revoke");
  assert.equal(decideRefund({ type: "charge.refunded", livemode: false }, { livemode: false, refunded: false, amount: 999, amount_refunded: 0 }).reason, "no_refund");
  assert.equal(decideRefund({ type: "charge.refunded", livemode: false }, { livemode: false }).action, "revoke");
  assert.equal(decideRefund({ type: "charge.refunded", livemode: true }, { livemode: true, refunded: true }).reason, "livemode");
});

test("file store keeps the first paid record", async () => {
  const dir = await mkdtemp(join(tmpdir(), "qortime-vm-"));
  const store = fileStore(join(dir, "receipts.json"));
  const first = await store.save({
    receipt_id: "match_42",
    paid: true,
    recorded_at: "2026-10-15T00:00:00.000Z",
  });
  const second = await store.save({
    receipt_id: "match_42",
    paid: true,
    recorded_at: "2026-10-15T01:00:00.000Z",
  });
  assert.equal(second.recorded_at, first.recorded_at);
  const raw = JSON.parse(await readFile(join(dir, "receipts.json"), "utf8"));
  assert.equal(raw.match_42.paid, true);
});

function fakeStripe(overrides = {}) {
  const created = [];
  return {
    created,
    checkout: {
      sessions: {
        async create(params) {
          created.push(params);
          return { id: "cs_test_mock", url: "https://checkout.stripe.com/c/pay/cs_test_mock" };
        },
        async listLineItems() {
          return { data: [{ quantity: 1, price: { id: PRICE, unit_amount: 999, currency: "usd" } }] };
        },
      },
    },
    webhooks: {
      constructEvent(raw, signature, secret) {
        if (signature !== "ok" || secret !== "webhook-secret") {
          throw new Error("bad signature");
        }
        return JSON.parse(raw.toString("utf8"));
      },
    },
    ...overrides,
  };
}

async function withServer(ctx, fn) {
  const server = createApp(ctx);
  const port = await listen(server);
  try {
    await fn(`http://127.0.0.1:${port}`);
  } finally {
    await close(server);
  }
}

test("checkout and receipt routes stay in test mode", async () => {
  const store = memoryStore();
  const stripe = fakeStripe();
  const env = {
    STRIPE_PRICE_ID: PRICE,
    STRIPE_WEBHOOK_SECRET: "webhook-secret",
    SITE_ORIGIN: "https://conwan30.github.io/qortime",
  };
  await withServer({ env, store, stripe, keyClass: "test", now: () => 1_700_000_000_000 }, async (base) => {
    const health = await fetch(`${base}/health`);
    assert.equal(health.status, 200);
    const healthBody = await health.json();
    assert.equal(healthBody.mode, "test");
    assert.equal(healthBody.livemode, false);
    assert.equal(healthBody.stripe_key, "test");

    const missing = await fetch(`${base}/v1/receipts/match_42`);
    assert.equal((await missing.json()).paid, false);

    const bad = await fetch(`${base}/v1/checkout`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: "https://conwan30.github.io" },
      body: JSON.stringify({ receipt_id: "bad id", receipt_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }),
    });
    assert.equal(bad.status, 400);
    const missingSha = await fetch(`${base}/v1/checkout`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: "https://conwan30.github.io" },
      body: JSON.stringify({ receipt_id: "match_42" }),
    });
    assert.equal(missingSha.status, 400);


    const created = await fetch(`${base}/v1/checkout`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: "https://conwan30.github.io" },
      body: JSON.stringify({ receipt_id: "match_42", receipt_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }),
    });
    assert.equal(created.status, 200);
    assert.equal(created.headers.get("access-control-allow-origin"), "https://conwan30.github.io");
    for (const origin of ["https://qortime.com", "https://www.qortime.com"]) {
      const pre = await fetch(`${base}/v1/checkout`, { method: "OPTIONS", headers: { origin } });
      assert.equal(pre.headers.get("access-control-allow-origin"), origin);
    }
    const evil = await fetch(`${base}/v1/checkout`, { method: "OPTIONS", headers: { origin: "https://evil.example" } });
    assert.equal(evil.headers.get("access-control-allow-origin"), null);
    const createdBody = await created.json();
    assert.equal(createdBody.url, "https://checkout.stripe.com/c/pay/cs_test_mock");
    assert.equal(stripe.created[0].metadata.receipt_id, "match_42");
    assert.equal(stripe.created[0].metadata.receipt_sha256, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    assert.equal(stripe.created[0].client_reference_id, "match_42");

    const unpaid = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": "ok" },
      body: JSON.stringify({
        type: "checkout.session.completed",
        livemode: false,
        data: {
          object: {
            id: "cs_test_unpaid",
            livemode: false,
            mode: "payment",
            payment_status: "unpaid",
            metadata: { receipt_id: "match_42" },
          },
        },
      }),
    });
    assert.equal((await unpaid.json()).action, "pending");
    assert.equal((await (await fetch(`${base}/v1/receipts/match_42`)).json()).paid, false);

    const paid = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": "ok" },
      body: JSON.stringify({
        type: "checkout.session.completed",
        livemode: false,
        data: {
          object: {
            id: "cs_test_paid",
            livemode: false,
            mode: "payment",
            payment_status: "paid",
            metadata: { receipt_id: "match_42", receipt_sha256: SHA },
            client_reference_id: "match_42",
            payment_intent: "pi_test_paid",
            currency: "usd",
            amount_total: 999,
          },
        },
      }),
    });
    assert.equal((await paid.json()).action, "record");
    const again = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": "ok" },
      body: JSON.stringify({
        type: "checkout.session.completed",
        livemode: false,
        data: {
          object: {
            id: "cs_test_paid",
            livemode: false,
            mode: "payment",
            payment_status: "paid",
            metadata: { receipt_id: "match_42", receipt_sha256: SHA },
            client_reference_id: "match_42",
            payment_intent: "pi_test_paid",
            currency: "usd",
            amount_total: 999,
          },
        },
      }),
    });
    assert.equal((await again.json()).action, "record");
    const badge = await (await fetch(`${base}/v1/receipts/match_42`)).json();
    assert.equal(badge.paid, true);
    assert.equal(badge.tier, "verified-match");
    assert.equal(badge.mode, "test");
    assert.equal(badge.livemode, false);
    assert.equal(badge.receipt_sha256, SHA);
    assert.equal("session_id" in badge, false);
    assert.equal("payment_intent" in badge, false);
    assert.equal("refunded_payment_intents" in badge, false);

    const rejected = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": "nope" },
      body: "{}",
    });
    assert.equal(rejected.status, 400);
  });
});

test("live keys and live events never mark a receipt paid", async () => {
  const store = memoryStore();
  const stripe = fakeStripe();
  const env = { STRIPE_PRICE_ID: PRICE, STRIPE_WEBHOOK_SECRET: "webhook-secret" };
  await withServer({ env, store, stripe, keyClass: "live" }, async (base) => {
    const checkout = await fetch(`${base}/v1/checkout`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ receipt_id: "match_42", receipt_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }),
    });
    assert.equal(checkout.status, 403);
    assert.equal(stripe.created.length, 0);
    const unsigned = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": "nope" },
      body: "{}",
    });
    assert.equal(unsigned.status, 400);
    const signed = await unsigned.json();
    assert.equal(signed.error, "Invalid webhook signature");
    assert.equal(JSON.stringify(signed).includes("webhook-secret"), false);
  });

  await withServer({ env, store, stripe, keyClass: "test" }, async (base) => {
    const liveEvent = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": "ok" },
      body: JSON.stringify({
        type: "checkout.session.completed",
        livemode: true,
        data: {
          object: {
            id: "cs_live_nope",
            livemode: true,
            mode: "payment",
            payment_status: "paid",
            metadata: { receipt_id: "match_42" },
          },
        },
      }),
    });
    assert.equal((await liveEvent.json()).reason, "livemode");
    assert.equal((await (await fetch(`${base}/v1/receipts/match_42`)).json()).paid, false);
  });
});

test("public pages show Verified Match as planned, with no checkout until KYC", async () => {
  const home = await readFile(join(ROOT, "site/index.html"), "utf8");
  const verify = await readFile(join(ROOT, "verify/index.html"), "utf8");
  const config = await readFile(join(ROOT, "verify/verified-match.js"), "utf8");
  const pages = await Promise.all(["site/pricing/index.html", "site/how-it-works/index.html", "site/early-access/index.html"]
    .map((p) => readFile(join(ROOT, p), "utf8")));
  for (const page of [home, verify, config, ...pages]) {
    assert.doesNotMatch(page, /buy\.stripe\.com|4242 4242|Buy Verified Match|test checkout/i);
  }
  assert.match(home, /Verified Match, a hosted referee countersign, is planned at \$9\.99\. Self-verify is free today\./);
  assert.match(verify, /const VM_CHECKOUT_LIVE = false;/);
  assert.match(verify, /Verified Match/);
  assert.match(verify, /verified-match\.js/);
  assert.doesNotMatch(home + verify + config, /sk_live_|sk_test_|whsec_/);
  assert.match(verify, /const gold = paid && sigStatus === "VERIFIED"/);
  assert.match(verify, /was refunded/);
});

test("client ip ignores a spoofed forwarding header unless the peer is the proxy", () => {
  assert.equal(clientIp({
    headers: { "x-forwarded-for": "1.1.1.1, 203.0.113.9" },
    socket: { remoteAddress: "10.1.2.3" },
  }), "203.0.113.9");
  assert.equal(clientIp({
    headers: { "x-forwarded-for": "203.0.113.9" },
    socket: { remoteAddress: "203.0.113.50" },
  }), "203.0.113.50");
});

test("checkout rate limit follows the proxy's last hop", async () => {
  const store = memoryStore();
  const stripe = fakeStripe();
  const env = {
    STRIPE_PRICE_ID: PRICE,
    STRIPE_WEBHOOK_SECRET: "webhook-secret",
    SITE_ORIGIN: "https://conwan30.github.io/qortime",
  };
  await withServer({ env, store, stripe, keyClass: "test", now: () => 1_700_000_000_000 }, async (base) => {
    const body = JSON.stringify({ receipt_id: "match_42", receipt_sha256: SHA });
    let status = 0;
    for (let i = 0; i < 21; i += 1) {
      const res = await fetch(`${base}/v1/checkout`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-forwarded-for": `198.51.100.${i}, 203.0.113.9`,
        },
        body,
      });
      status = res.status;
    }
    assert.equal(status, 429);
    const other = await fetch(`${base}/v1/checkout`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-forwarded-for": "198.51.100.9, 203.0.113.10",
      },
      body,
    });
    assert.equal(other.status, 200);
    const evil = await fetch(`${base}/v1/receipts/match_42`, { headers: { origin: "https://evil.example" } });
    assert.equal(evil.headers.get("access-control-allow-origin"), null);
  });
});

test("a refund clears that payment and a replay cannot grant it again", async () => {
  const store = memoryStore();
  const stripe = fakeStripe();
  stripe.paymentIntents = {
    async retrieve(id) {
      assert.equal(id, "pi_test_paid");
      return { id, metadata: { receipt_id: "match_42" } };
    },
  };
  const env = { STRIPE_PRICE_ID: PRICE, STRIPE_WEBHOOK_SECRET: "webhook-secret" };
  const paidEvent = {
    type: "checkout.session.completed",
    livemode: false,
    data: {
      object: {
        id: "cs_test_paid",
        livemode: false,
        mode: "payment",
        payment_status: "paid",
        metadata: { receipt_id: "match_42", receipt_sha256: SHA },
        client_reference_id: "match_42",
        payment_intent: "pi_test_paid",
        currency: "usd",
        amount_total: 999,
      },
    },
  };
  await withServer({ env, store, stripe, keyClass: "test", now: () => 1_700_000_000_000 }, async (base) => {
    const post = (body) => fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": "ok" },
      body: JSON.stringify(body),
    });
    assert.equal((await (await post(paidEvent)).json()).action, "record");
    const zero = await post({
      type: "charge.refunded",
      livemode: false,
      data: { object: { id: "ch_test", livemode: false, refunded: false, amount: 999, amount_refunded: 0, payment_intent: "pi_test_paid" } },
    });
    assert.equal((await zero.json()).reason, "no_refund");
    assert.equal((await (await fetch(`${base}/v1/receipts/match_42`)).json()).paid, true);

    const partial = await post({
      type: "charge.refunded",
      livemode: false,
      data: { object: { id: "ch_test", livemode: false, refunded: false, amount: 999, amount_refunded: 100, payment_intent: "pi_test_paid" } },
    });
    assert.equal((await partial.json()).action, "revoke");
    assert.equal((await (await fetch(`${base}/v1/receipts/match_42`)).json()).paid, false);

    const refunded = await post({
      type: "charge.refunded",
      livemode: false,
      data: { object: { id: "ch_test", livemode: false, refunded: true, amount: 999, amount_refunded: 999, payment_intent: "pi_test_paid" } },
    });
    assert.equal((await refunded.json()).action, "revoke");
    const cleared = await (await fetch(`${base}/v1/receipts/match_42`)).json();
    assert.equal(cleared.paid, false);
    assert.equal(cleared.tier, null);
    assert.equal(cleared.payment_status, "refunded");

    assert.equal((await (await post(paidEvent)).json()).action, "record");
    assert.equal((await (await fetch(`${base}/v1/receipts/match_42`)).json()).paid, false);

    const again = structuredClone(paidEvent);
    again.data.object.id = "cs_test_again";
    again.data.object.payment_intent = "pi_test_again";
    assert.equal((await (await post(again)).json()).action, "record");
    assert.equal((await (await fetch(`${base}/v1/receipts/match_42`)).json()).paid, true);

    const redirect = await fetch(`${base}/v1/receipts/other_match?checkout=success&session_id=cs_test_paid`);
    assert.equal((await redirect.json()).paid, false);
    const missing = await fetch(`${base}/checkout/success?session_id=cs_test_paid`);
    assert.equal(missing.status, 404);
  });
});

test("stripe webhook signatures expire outside the tolerance window", async () => {
  const secret = `whsec_${"a".repeat(32)}`;
  const real = new Stripe(`rk_test_${"b".repeat(24)}`, { apiVersion: "2026-09-30.endive" });
  const payload = JSON.stringify({
    id: "evt_test_sig",
    type: "checkout.session.async_payment_failed",
    livemode: false,
    data: {
      object: {
        id: "cs_test_sig",
        livemode: false,
        mode: "payment",
        payment_status: "unpaid",
        metadata: { receipt_id: "match_42" },
      },
    },
  });
  const header = Stripe.webhooks.generateTestHeaderString({ payload, secret });
  const store = memoryStore();
  const env = { STRIPE_PRICE_ID: PRICE, STRIPE_WEBHOOK_SECRET: secret };
  await withServer({ env, store, stripe: real, keyClass: "test" }, async (base) => {
    const ok = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": header },
      body: payload,
    });
    assert.equal(ok.status, 200);
    assert.equal((await ok.json()).action, "fail");
    assert.equal((await (await fetch(`${base}/v1/receipts/match_42`)).json()).paid, false);

    const tampered = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": header },
      body: payload.replace("match_42", "match_99"),
    });
    assert.equal(tampered.status, 400);

    const stale = Stripe.webhooks.generateTestHeaderString({
      payload,
      secret,
      timestamp: Math.floor(Date.now() / 1000) - (WEBHOOK_TOLERANCE_SECONDS + 30),
    });
    const old = await fetch(`${base}/v1/webhook`, {
      method: "POST",
      headers: { "stripe-signature": stale },
      body: payload,
    });
    assert.equal(old.status, 400);
    const oldBody = await old.json();
    assert.equal(oldBody.error, "Invalid webhook signature");
    assert.equal(JSON.stringify(oldBody).includes(secret), false);
  });
  assert.equal(WEBHOOK_TOLERANCE_SECONDS, 300);
});

test("checkout errors do not echo secret keys", async () => {
  const stripe = fakeStripe();
  stripe.checkout.sessions.create = async () => {
    throw new Error("Stripe said sk_test_SECRETVALUE and whsec_SECRETVALUE");
  };
  const env = { STRIPE_PRICE_ID: PRICE, STRIPE_WEBHOOK_SECRET: "webhook-secret" };
  await withServer({ env, store: memoryStore(), stripe, keyClass: "test", now: () => 1_700_000_000_000 }, async (base) => {
    const res = await fetch(`${base}/v1/checkout`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ receipt_id: "match_42", receipt_sha256: SHA }),
    });
    const text = await res.text();
    assert.equal(res.status, 502);
    assert.doesNotMatch(text, /sk_test_|whsec_|SECRETVALUE/);
  });
});

test("file store does not let a refunded payment intent grant again", async () => {
  const dir = await mkdtemp(join(tmpdir(), "qortime-vm-"));
  const store = fileStore(join(dir, "receipts.json"));
  await store.save({
    receipt_id: "match_42",
    paid: true,
    payment_intent: "pi_1",
    payment_status: "paid",
    recorded_at: "2026-10-15T00:00:00.000Z",
  });
  const cleared = await store.save({ op: "revoke", receipt_id: "match_42", payment_intent: "pi_1" });
  assert.equal(cleared.paid, false);
  assert.equal(cleared.payment_status, "refunded");
  const replay = await store.save({
    receipt_id: "match_42",
    paid: true,
    payment_intent: "pi_1",
    payment_status: "paid",
    recorded_at: "2026-10-15T01:00:00.000Z",
  });
  assert.equal(replay.paid, false);
  const next = await store.save({
    receipt_id: "match_42",
    paid: true,
    payment_intent: "pi_2",
    payment_status: "paid",
    mode: "test",
    livemode: false,
    tier: "verified-match",
    recorded_at: "2026-10-15T02:00:00.000Z",
  });
  assert.equal(next.paid, true);
  assert.equal(next.payment_intent, "pi_2");
  assert.deepEqual(next.refunded_payment_intents, ["pi_1"]);
});
