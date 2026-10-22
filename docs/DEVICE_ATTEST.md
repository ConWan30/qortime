# Device attestation sidecar (`device_attest.v0.json`)

Status: informational. The referee reports `device_key` as `tpm_claimed`, `software`, or
`invalid`. That status is not a review check. It does not change `VERIFIED`, `PARTIAL`,
`SELF_ATTESTED`, or `FAIL`. An `invalid` signature is shown on the verify page in the failure
color so it is not quiet. It still does not change the verdict. Gold on the page stays reserved
for a `VERIFIED` receipt.

`tpm_attested` is not a status this referee emits. A software keypair can produce a sidecar and
a self-signed blob. That is not evidence the key lives in a TPM. `tpm_claimed` is the strongest
status, and the verify page says the TPM claim is unverified.

A future `tpm_attested` is allowed only when the attestation chains to a TPM manufacturer EK
root, or to a Microsoft AIK / attestation-service certificate, and this referee has validated
that chain against a pinned root set. That check is not implemented. See [PHASE2.md](PHASE2.md).
Until it is, do not add the status back on the strength of a signature this process can mint.
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

### TPM claim

When the device signature verifies and `key_provider` is a TPM claim, the status is
`tpm_claimed`. `non_exportable: false` adds the flag `exportable_key` and does not raise the
status.

Optional object `tpm_attestation` is ignored for the status. A signature over any statement,
including one produced by another software key, an EK-shaped certificate chain, or a
self-signature, cannot raise the status above `tpm_claimed`. There is no signed-bytes
definition for that object in this referee, because this referee does not treat the object as
evidence.

## Parser

The parser caps the file at 64 KiB, rejects unexpected types, and does not raise into the
review. A broken sidecar cannot change the receipt verdict and cannot crash the referee.
