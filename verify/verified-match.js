const RECEIPT_ID = /^[A-Za-z0-9._:-]{1,128}$/;
const RECEIPT_SHA = /^[0-9a-f]{64}$/i;

export const VERIFIED_MATCH = {
  mode: "test",
  productId: "prod_VRlRQfOgasBJJj",
  priceId: "price_1UQruXCepyIJsHZulKEdcVkX",
  paymentLinkId: "plink_1UQrueCepyIJsHZuXcqSnjnH",
  // Payment Link paused until KYC; not rendered on the site. Test link lives in docs/STRIPE_VERIFIED_MATCH_TEST.md.
  paymentLinkUrl: "",
  apiBase: "https://qortime-verified-match-production.up.railway.app",
};

export function isValidReceiptId(value) {
  return typeof value === "string" && RECEIPT_ID.test(value);
}

export function isValidReceiptSha256(value) {
  return typeof value === "string" && RECEIPT_SHA.test(value);
}

export function receiptIdFrom(receipt) {
  if (!receipt || typeof receipt !== "object") return null;
  const explicit = receipt.receipt_id;
  const matchId = receipt.match && typeof receipt.match === "object" ? receipt.match.match_id : null;
  const id = typeof explicit === "string" && explicit.length > 0 ? explicit : matchId;
  return isValidReceiptId(id) ? id : null;
}

export function apiBase() {
  const configured = (VERIFIED_MATCH.apiBase || "").replace(/\/$/, "");
  const override = new URLSearchParams(location.search).get("api");
  const host = location.hostname;
  const local = host === "localhost" || host === "127.0.0.1";
  if (local && override && /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(override)) {
    return override.replace(/\/$/, "");
  }
  return configured;
}

/** True when a paid record is bound to this exact receipt body (or legacy unpaid-hash records). */
export function badgeMatchesReceipt(record, receiptSha256) {
  if (!record || record.paid !== true) return false;
  if (!record.receipt_sha256) return true; // legacy test payments before content binding
  return isValidReceiptSha256(receiptSha256) && record.receipt_sha256.toLowerCase() === receiptSha256.toLowerCase();
}
