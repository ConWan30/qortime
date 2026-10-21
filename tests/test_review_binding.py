"""Receipt binding against countersigned.jsonl. Fail closed. Do not loosen these to accept 021003."""
from qor_receipt.jcs import sha256_hex
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
    assert any("ends before the receipt chain" in e and "countersigned.jsonl has 1" in e for e in short)
    long = check_receipt_binding(_bundle(cps[:1], cps, checkpoints=cps[:1]))
    assert any("continues past the receipt chain" in e and "countersigned.jsonl has 2" in e for e in long)


def _sealed_chain():
    """v0.3.28 checkpoint objects: JCS body hash, qorcap evidence, stop seals the chain.

    The recorder writes this shape (seq, t_ms, frames_hash, input_hash, prev, hash, qorcap)
    and the sidecar appends referee_ts_ms and referee_sig. Nothing is appended after stop.
    """
    prev = "0" * 64
    out = []
    for seq, trigger, t_ms in ((0, "start", 1_000), (1, "segment", 5_000), (2, "stop", 9_000)):
        ext = {
            "trigger": trigger,
            "segment_seq": max(seq - 1, 0),
            "segment_hash": None,
            "chain_head": "cd" * 32,
            "score": None,
            "mono_ms": t_ms,
            "match_id": "m",
            "role": "home",
        }
        body = {
            "seq": seq,
            "t_ms": t_ms,
            "frames_hash": sha256_hex(ext),
            "input_hash": sha256_hex([]),
            "prev": prev,
        }
        body["hash"] = sha256_hex(body)
        out.append({**body, "qorcap": ext, "referee_ts_ms": t_ms + 80, "referee_sig": f"{seq + 1:02x}" * 64})
        prev = body["hash"]
    return out


def _linked_after(chain, trigger):
    """One more countersigned object, prev-linked, as finalize leaves a longer live file."""
    prev = chain[-1]
    seq = len(chain)
    ext = {
        "trigger": trigger,
        "segment_seq": seq,
        "segment_hash": None,
        "chain_head": "cd" * 32,
        "score": {"home": 7, "away": 0},
        "mono_ms": prev["t_ms"] + 4_000,
        "match_id": "m",
        "role": "home",
    }
    body = {
        "seq": seq,
        "t_ms": prev["t_ms"] + 4_000,
        "frames_hash": sha256_hex(ext),
        "input_hash": sha256_hex([]),
        "prev": prev["hash"],
    }
    body["hash"] = sha256_hex(body)
    return {**body, "qorcap": ext, "referee_ts_ms": body["t_ms"] + 80, "referee_sig": "ee" * 64}


def test_v0328_stop_chain_matches_and_a_longer_live_log_is_rejected():
    """QorCap v0.3.28 does not record events after the stop checkpoint.

    Start Match puts the full countersigned list in the receipt. Finalize, since v0.3.0,
    will still sign live[:len(checkpoints.jsonl)] when the file is longer and will not
    truncate countersigned.jsonl. That bundle is not an honest sealed match: the extra
    line is a signed checkpoint the receipt dropped. It must 422, including when the
    extra line is a valid hash-chain continuation (the shape that prefix check would miss).
    """
    chain = _sealed_chain()
    assert chain[-1]["qorcap"]["trigger"] == "stop"
    assert check_receipt_binding(_bundle(chain, chain)) == []

    extra = _linked_after(chain, "late")
    assert extra["prev"] == chain[-1]["hash"] and extra["seq"] == len(chain)
    longer = check_receipt_binding(_bundle(chain, chain + [extra], checkpoints=chain))
    assert longer == [
        "countersigned.jsonl continues past the receipt chain "
        "(receipt has 3 checkpoints for home, countersigned.jsonl has 4); "
        "a signed checkpoint the receipt does not carry"
    ]

    broken = dict(extra, prev="ab" * 32, hash="11" * 32)
    garbage = check_receipt_binding(_bundle(chain, chain + [broken], checkpoints=chain))
    assert any("continues past the receipt chain" in e for e in garbage)

    short = check_receipt_binding(_bundle(chain, chain[:-1], checkpoints=chain))
    assert short == [
        "countersigned.jsonl ends before the receipt chain "
        "(receipt has 3 checkpoints for home, countersigned.jsonl has 2)"
    ]
