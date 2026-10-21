import { isValidReceiptId, isValidReceiptSha256 } from "./receipt-id.mjs";

export const INTEGRATION_IDENTIFIER = "qortime_verified_match_hnqwlxpt";

/** Catalog price is $9.99 USD. Stripe Prices are immutable; a different amount needs a new price id and a code change. */
export const CATALOG_UNIT_AMOUNT = 999;
export const CATALOG_CURRENCY = "usd";

/** Stripe's default webhook tolerance. Reject signatures older than this. */
export const WEBHOOK_TOLERANCE_SECONDS = 300;

export const FULFILL_EVENTS = new Set([
  "checkout.session.completed",
  "checkout.session.async_payment_succeeded",
]);

const SITE_DEFAULT = "https://conwan30.github.io/qortime";

export function keyClass(key) {
  if (!key) return "missing";
  const live = "sk_" + "live_";
  const liveRestricted = "rk_" + "live_";
  const test = "sk_" + "test_";
  const testRestricted = "rk_" + "test_";
  if (key.startsWith(live) || key.startsWith(liveRestricted)) return "live";
  if (key.startsWith(test) || key.startsWith(testRestricted)) return "test";
  return "unrecognized";
}

export function siteOrigin(env) {
  const raw = (env.SITE_ORIGIN || SITE_DEFAULT).trim().replace(/\/$/, "");
  if (!/^https:\/\/[a-z0-9.-]+(?::\d+)?(?:\/[A-Za-z0-9._~/-]*)?$/i.test(raw)) {
    return SITE_DEFAULT;
  }
  return raw;
}

export function buildCheckoutParams(receiptId, { priceId, site, receiptSha256 = null }) {
  const root = site.replace(/\/$/, "");
  const meta = {
    receipt_id: receiptId,
    product: "verified_match",
    mode: "test",
  };
  if (typeof receiptSha256 === "string" && /^[0-9a-f]{64}$/i.test(receiptSha256)) {
    meta.receipt_sha256 = receiptSha256.toLowerCase();
  }
  return {
    mode: "payment",
    ui_mode: "hosted_page",
    origin_context: "web",
    adaptive_pricing: { enabled: true },
    line_items: [{ price: priceId, quantity: 1 }],
    client_reference_id: receiptId,
    metadata: meta,
    payment_intent_data: {
      metadata: {
        receipt_id: receiptId,
        product: "verified_match",
        ...(meta.receipt_sha256 ? { receipt_sha256: meta.receipt_sha256 } : {}),
      },
    },
    success_url: `${root}/verify/?checkout=success&receipt_id=${encodeURIComponent(receiptId)}&session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${root}/?checkout=cancel#verified-match`,
    integration_identifier: INTEGRATION_IDENTIFIER,
  };
}

export function sessionReceiptId(session) {
  const fromMeta = session?.metadata?.receipt_id;
  if (typeof fromMeta === "string" && fromMeta.length > 0) return fromMeta;
  const fromRef = session?.client_reference_id;
  if (typeof fromRef === "string" && fromRef.length > 0) return fromRef;
  return null;
}

export function lineItemsIncludePrice(items, priceId) {
  return items.some((item) => {
    const price = item?.price;
    const id = typeof price === "string" ? price : price?.id;
    return id === priceId;
  });
}

/**
 * The paid line must be exactly one unit of the configured price, and that Price object
 * (fetched from Stripe, not trusted from the event body) must be the $9.99 USD catalog price.
 * A string price id is not enough: amount and currency have to be on the expanded Price.
 */
export function lineItemsMatchCatalog(items, priceId) {
  if (!Array.isArray(items) || items.length !== 1) return false;
  const item = items[0];
  if (item?.quantity !== 1) return false;
  const price = item?.price;
  if (!price || typeof price !== "object") return false;
  if (price.id !== priceId) return false;
  if (price.unit_amount !== CATALOG_UNIT_AMOUNT) return false;
  if (String(price.currency || "").toLowerCase() !== CATALOG_CURRENCY) return false;
  return true;
}

export function decideFulfillment(event, session, { priceOk }) {
  const known =
    FULFILL_EVENTS.has(event.type) || event.type === "checkout.session.async_payment_failed";
  if (!known) return { action: "ignore", reason: "event" };
  if (session?.livemode === true || event.livemode === true) {
    return { action: "ignore", reason: "livemode" };
  }
  const receiptId = sessionReceiptId(session);
  if (!isValidReceiptId(receiptId)) return { action: "ignore", reason: "receipt_id" };
  if (event.type === "checkout.session.async_payment_failed") {
    return { action: "fail", reason: "async_failed", receiptId };
  }
  const metaId = session?.metadata?.receipt_id;
  const refId = session?.client_reference_id;
  if (typeof metaId === "string" && metaId.length > 0 && typeof refId === "string" && refId.length > 0 && metaId !== refId) {
    return { action: "ignore", reason: "receipt_binding", receiptId };
  }
  if (session.mode && session.mode !== "payment") {
    return { action: "ignore", reason: "mode", receiptId };
  }
  // paid is the only fulfilling status. unpaid waits. no_payment_required (a $0 session) must not grant.
  if (session.payment_status !== "paid") {
    if (session.payment_status === "unpaid") return { action: "pending", reason: "unpaid", receiptId };
    return { action: "ignore", reason: "payment_status", receiptId };
  }
  const discount = session?.total_details?.amount_discount;
  if (typeof discount === "number" && discount > 0) {
    return { action: "ignore", reason: "discount", receiptId };
  }
  const currency = typeof session.currency === "string" ? session.currency.toLowerCase() : "";
  if (currency === CATALOG_CURRENCY && typeof session.amount_total === "number" && session.amount_total !== CATALOG_UNIT_AMOUNT) {
    return { action: "ignore", reason: "amount", receiptId };
  }
  const sha = session?.metadata?.receipt_sha256;
  if (!isValidReceiptSha256(sha)) return { action: "ignore", reason: "receipt_sha256", receiptId };
  if (!priceOk) return { action: "ignore", reason: "price", receiptId };
  return { action: "record", receiptId };
}

export function decideRefund(event, charge) {
  if (event?.type !== "charge.refunded") return { action: "ignore", reason: "event" };
  if (charge?.livemode === true || event.livemode === true) return { action: "ignore", reason: "livemode" };
  const amountRefunded = charge?.amount_refunded;
  const provenZero = charge?.refunded === false
    && typeof amountRefunded === "number"
    && Number.isFinite(amountRefunded)
    && amountRefunded === 0;
  // Partial, full, or a charge.refunded whose amount we cannot read. Fail closed.
  if (provenZero) return { action: "ignore", reason: "no_refund" };
  return { action: "revoke", reason: "refunded" };
}

export function isCheckoutUrl(url) {
  if (typeof url !== "string") return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname === "checkout.stripe.com";
  } catch {
    return false;
  }
}
