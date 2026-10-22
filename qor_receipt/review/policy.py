"""Constants shared by the referee service, the review builder and both verifiers (Py + JS)."""
from __future__ import annotations

REVIEW_TYPE = "qortime-bundle-review/v1"
REVIEW_KEY_KIND = "qortime-official-review"
CHALLENGE_TOKEN_TYPE = "qortime-challenge/v1"
ANCHOR_TYPE = "qortime-anchor/v1"
# Referee-issued link for two single-console submissions of the same match.
# The pairing id and nonce are signed here. Clients do not choose them.
PAIRING_TYPE = "qortime-pairing/v1"

# Check states inside review.checks: pass | partial | fail. A "fail" is never signed by the service; the verifier
# also turns it into FAIL if it ever appears. "partial" keeps the receipt at PARTIAL.
PASS, PARTIAL_CHECK, FAIL_CHECK = "pass", "partial", "fail"
CHECK_STATES = (PASS, PARTIAL_CHECK, FAIL_CHECK)

# Spot-check defaults (all overridable via env on the service).
TAIL_MS = 240_000              # recorded tail the referee decodes itself for the end-of-game re-read (= QorCap TAIL_SECS)
RANDOM_SEGMENTS = 16           # random media segments drawn after the manifest is committed
CHALLENGE_PAD_MS = 4_000       # segments around each challenge window
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
MAX_META_BYTES = 96 * 1024 * 1024

NOT_PROVEN = [
    "who was holding the controller (identity)",
    "that the laptop's local countersignatures were made live: they come from a key on the player's own PC",
    "that the footage was not fabricated or edited before QorCap hashed it, except where an official challenge "
    "or anchor timestamps the chain",
    "content of video segments the referee did not receive (spot-check mode)",
]
