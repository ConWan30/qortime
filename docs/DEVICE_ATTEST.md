# Device attestation sidecar (`device_attest.v0.json`)

Status: informational. The referee reports `device_key` as `tpm_attested`, `tpm_claimed`,
`software`, or `invalid`. That status is not a review check. It does not change
`VERIFIED`, `PARTIAL`, `SELF_ATTESTED`, or `FAIL`. An `invalid` signature is shown on the
verify page in the failure color so it is not quiet. It still does not change the verdict.
Gold on the page stays reserved for a `VERIFIED` receipt.

This is not the Phase 2 manufacturer EK chain in [PHASE2.md](PHASE2.md). `tpm_attested` means
the attestation blob defined below verifies. It does not mean a TPM vendor certified the key.
`players[].key_kind: "tpm"` remains an unverified claim.

## Assumption — align this with the QorCap PR

QorCap is adding this sidecar. The bytes below are the contract this referee verifies.
If the QorCap PR signs different bytes, change this document and `qor_receipt/review/device_attest.py`
together. Do not loosen a failing signature to make a bundle pass.

### Sidecar

File name: `device_attest.v0.json` (optional bundle meta file). Absent means no `device_key`
field. Present and unreadable means `invalid`.

```json
{
  "schema": "qorcap-device-attest/v0",
  "key_provider": "software",
  "algorithm": "ed25519",
  "public_key_spki_b64": "<standard or url-safe base64 of the SPKI DER>",
  "non_exportable": false,
  "signature_b64": "<base64 signature>",
  "tpm_attestation": null
}
```

`schema_id` is accepted as an alias of `schema` only. Any other schema id is `invalid`.

`key_provider` (case-insensitive): `software` or `software-file` are software. `tpm`,
`windows-tpm`, `platform-crypto`, and `ncrypt` are TPM claims. Anything else with a valid
signature is `invalid` (`key_provider_unknown`).

`algorithm`: `ed25519`, or `ecdsa-p256-sha256` (aliases `ecdsa-p256` and `p256`). The SPKI
key type must match the algorithm.

`non_exportable` is a JSON boolean. Missing is not treated as true.

### Device signature

The device key signs the ASCII bytes

```
qorcap-device-attest/v0\n
<64 lowercase hex characters of the final checkpoint hash>\n
```

The final checkpoint hash is `receipt.checkpoints[<role>][-1].hash` on the player-signed
receipt in the bundle: the head of that role's checkpoint chain. It is the hex text, not the
raw 32-byte digest, and it is not `bundle.json` `chain_head` (the video segment chain).

Ed25519 signatures are 64 raw bytes. P-256 signatures are DER ECDSA over SHA-256.

A signature that does not verify is `invalid` with flag `device_signature_invalid`.

### TPM attestation material

Optional object `tpm_attestation`. When the device signature is valid and the provider is a
TPM claim:

- material absent, malformed, or signature-invalid → `tpm_claimed`
- material verifies, and `non_exportable` is true → `tpm_attested`
- material verifies, but `non_exportable` is not true → `tpm_claimed` with flag `exportable_key`

The attestation key signs the ASCII bytes

```
qorcap-tpm-key-attest/v0\n
spki_sha256=<lowercase hex SHA-256 of the device SPKI DER>\n
non_exportable=true\n
algorithm=<ed25519 or ecdsa-p256-sha256>\n
key_provider=tpm\n
```

```json
{
  "schema": "qorcap-tpm-key-attest/v0",
  "ak_algorithm": "ecdsa-p256-sha256",
  "ak_public_key_spki_b64": "<SPKI DER of the attestation key>",
  "signature_b64": "<signature by that key over the bytes above>"
}
```

The attestation key must be a different key from the device key. A self-signature is not
attestation (`attestation_key_is_device_key`) and stays `tpm_claimed`.

No EK certificate, no PCR quote, and no vendor CA is checked. A software keypair can produce
a blob that reaches `tpm_attested` under this definition. The verify page says so.

## Parser

The parser caps the file at 64 KiB, rejects unexpected types, and does not raise into the
review. A broken sidecar cannot change the receipt verdict and cannot crash the referee.
