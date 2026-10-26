"""Advisory AI receipt explainer (Meta Muse Spark). Built during the Meta AI Developer Hackathon, Oct 26 - Nov 4 2026.

ADVISORY ONLY. The verdict comes from ``qor_receipt.receipt.verify_receipt`` (or ``verify/verify.js``) and nothing here
can change it. See README.md in this folder.
"""
from qor_receipt.ai_ruling.explainer import FILENAME, SCHEMA, ExplainResult, explain, facts_for
from qor_receipt.ai_ruling.guardrail import check_text
from qor_receipt.ai_ruling.muse import MuseClient, MuseError
from qor_receipt.ai_ruling.signing import check_ruling, receipt_jcs_sha256, sign_ruling

__all__ = ["FILENAME", "SCHEMA", "ExplainResult", "explain", "facts_for", "check_text", "MuseClient", "MuseError", "check_ruling", "receipt_jcs_sha256", "sign_ruling"]
