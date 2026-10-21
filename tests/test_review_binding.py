"""Receipt binding against countersigned.jsonl. Fail closed. Do not loosen these to accept 021003."""
from qor_receipt.review.bundle import Bundle, check_receipt_binding


def _cp(sig: str, n: int = 0) -> dict:
    return {"seq": n, "hash": f"{n:064x}", "referee_sig": sig}


def _bundle(receipt_cps, countersigned, checkpoints=None) -> Bundle:
    b = Bundle(files={})
    b.meta = {"role": "home", "match_id": "m"}
    b.receipt = {"match": {"match_id": "m"}, "checkpoints": {"home": receipt_cps}}
    b.checkpoints = list(checkpoints if checkpoints is not None else receipt_cps)
    b.countersigned = list(countersigned)
    return b


def test_matching_countersignatures_pass():
    cps = [_cp("ab" * 64), _cp("cd" * 64, 1)]
    assert check_receipt_binding(_bundle(cps, cps)) == []


def test_checkpoint_zero_sig_mismatch_is_the_021003_rejection():
    """HTTP 422 on match 021003 is this check, not a parser bug.

    The published receipt was re-signed offline after a side correction
    (referee.key_kind local-self-attested-offline). Its checkpoint 0 no longer
    matches the live countersigned.jsonl. That must stay a rejection.
    """
    live = [_cp("aa" * 64), _cp("bb" * 64, 1)]
    resigned = [_cp("ff" * 64), _cp("bb" * 64, 1)]
    errs = check_receipt_binding(_bundle(resigned, live, checkpoints=resigned))
    assert errs == ["receipt checkpoint 0 differs from countersigned.jsonl"]


def test_truncated_or_extended_countersigned_log_fails():
    cps = [_cp("aa" * 64), _cp("bb" * 64, 1)]
    short = check_receipt_binding(_bundle(cps, cps[:1]))
    assert any("countersigned.jsonl has 1" in e for e in short)
    long = check_receipt_binding(_bundle(cps[:1], cps, checkpoints=cps[:1]))
    assert any("countersigned.jsonl has 2" in e for e in long)
