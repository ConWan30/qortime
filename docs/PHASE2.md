# Phase 2 roadmap

## TPM key attestation
* The informational `device_key` status in [DEVICE_ATTEST.md](DEVICE_ATTEST.md) is not this work. It checks a sidecar signature and an optional attestation blob. It does not verify a manufacturer EK chain, and it does not set `key_kind` or change VERIFIED / FAIL.
* Make `keys.TpmKey` real: an Ed25519 key isn't available in most TPM 2.0 parts, so either (a) use ECDSA P-256 in the TPM via
  Windows Platform Crypto Provider (NCrypt) and add `alg` to signatures (`ed25519` or `p256`), or (b) keep Ed25519 but
  seal the seed to the TPM (weaker: the seed is in memory while signing).
* At enrollment the agent sends a TPM **key attestation** (AK-certified key + EK certificate chain to the vendor CA).
  The referee records `key_kind: "tpm"` only after verifying it. Add `players[].attestation_ref` to the schema
  (v0 verifiers ignore unknown fields only after a schema bump to v1).
* Optional: PCR policy / measured boot so that the key only signs while the agent binary is the expected build.

## Transparency log
* The referee appends every finalized receipt hash (and checkpoint heads) to an append-only Merkle log. The log could be
  Trillian/Tessera-style or anchored periodically (cf. QorTroller PoSP anchor).
* Receipts gain `log: {index, inclusion_proof, signed_tree_head}`. Verifiers check inclusion. Platforms monitor
  for split views. This limits damage from a compromised referee key (RISKS #8).

## Sealed dongle
* A small USB device between controller and PC (or an HDMI passthrough) with its own secure element. It signs HID report
  hashes and/or frame hashes at the source. That removes the PC from the trust path for input and video (RISKS #4, #5).

## C2PA
* Have the agent emit a C2PA manifest for match highlight clips. The manifest binds the clip to the receipt
  hash, so disputes can be reviewed with provenance. Longer term: capture hardware that signs C2PA natively.

## Other
* Persistent referee store (Postgres) and auth tokens for platforms. Webhooks to escrow platforms.
* The Windows agent: capture card → vendored `ScorebugReader` → `AgreementWindow`, a DualSense Edge HID reader,
  and a pause-menu template detector. Plus more titles and scorebug profiles.
