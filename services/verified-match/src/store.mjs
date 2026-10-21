import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

/**
 * Paid sticks until a full refund of that same PaymentIntent.
 * A replay of a refunded PaymentIntent cannot grant again.
 * A different PaymentIntent can pay after a refund.
 * Failed writes never clear a paid or refunded row.
 */
export function applyRecord(existing, record) {
  const blocked = new Set(existing?.refunded_payment_intents || []);
  if (record.op === "revoke") {
    const pi = record.payment_intent || null;
    if (pi) blocked.add(pi);
    const list = [...blocked];
    if (existing?.paid && pi && existing.payment_intent === pi) {
      return {
        ...existing,
        paid: false,
        tier: null,
        payment_status: "refunded",
        refunded_payment_intents: list,
      };
    }
    if (existing) return { ...existing, refunded_payment_intents: list };
    return {
      receipt_id: record.receipt_id,
      paid: false,
      mode: "test",
      livemode: false,
      tier: null,
      payment_status: "refunded",
      payment_intent: pi,
      refunded_payment_intents: list,
      recorded_at: record.recorded_at ?? null,
    };
  }
  if (existing?.paid) return existing;
  if (existing?.payment_status === "refunded" && record.paid !== true) return existing;
  if (record.payment_intent && blocked.has(record.payment_intent)) return existing ?? null;
  return { ...record, refunded_payment_intents: [...blocked] };
}

function chainLock() {
  let tail = Promise.resolve();
  return function lock(fn) {
    const run = tail.then(fn, fn);
    tail = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  };
}

export function memoryStore() {
  const rows = new Map();
  const lock = chainLock();
  return {
    async get(id) {
      return rows.get(id) ?? null;
    },
    async save(record) {
      return lock(async () => {
        const existing = rows.get(record.receipt_id) ?? null;
        const next = applyRecord(existing, record);
        if (!next) return existing;
        rows.set(record.receipt_id, next);
        return next;
      });
    },
  };
}

export function fileStore(filePath) {
  const lock = chainLock();

  async function readAll() {
    try {
      const text = await readFile(filePath, "utf8");
      const data = JSON.parse(text);
      if (!data || typeof data !== "object" || Array.isArray(data)) return {};
      return data;
    } catch (err) {
      if (err && err.code === "ENOENT") return {};
      throw err;
    }
  }

  return {
    async get(id) {
      const all = await readAll();
      return all[id] ?? null;
    },
    async save(record) {
      return lock(async () => {
        const all = await readAll();
        const existing = all[record.receipt_id] ?? null;
        const next = applyRecord(existing, record);
        if (!next) return existing;
        all[record.receipt_id] = next;
        await mkdir(dirname(filePath), { recursive: true });
        const tmp = `${filePath}.tmp`;
        await writeFile(tmp, `${JSON.stringify(all, null, 2)}\n`);
        await rename(tmp, filePath);
        return next;
      });
    },
  };
}

export function toPublic(record, receiptId) {
  if (!record) {
    return {
      receipt_id: receiptId,
      paid: false,
      tier: null,
      mode: "test",
      livemode: false,
      payment_status: null,
      recorded_at: null,
      receipt_sha256: null,
    };
  }
  const paid = record.paid === true;
  return {
    receipt_id: record.receipt_id,
    paid,
    tier: paid ? "verified-match" : null,
    mode: "test",
    livemode: false,
    payment_status: record.payment_status ?? null,
    recorded_at: record.recorded_at ?? null,
    receipt_sha256: typeof record.receipt_sha256 === "string" ? record.receipt_sha256 : null,
  };
}
