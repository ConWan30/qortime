"""score_timeline.json is the timeline when the receipt has no signals.score_timeline."""
from __future__ import annotations

import base64
import copy
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from qor_receipt import receipt as R
from qor_receipt.demo import run_match
from qor_receipt.keys import SoftwareKey
from qor_receipt.review.corroboration import (
    SCORE_EVENT_TOLERANCE_MS,
    build_corroboration,
    corroboration_view,
    side_from_receipt,
)
from qor_receipt.review.device_attest import DEVICE_ATTEST_SCHEMA, device_signed_bytes
from qor_receipt.review.score_timeline import SCHEMA, chain_events, events_from_sidecar, signing_message
from tests.test_corroboration import _challenge, _events, _signed_receipt


def _plain_receipt(role, device, capture, *, final=(14, 7), challenges=None, head="ab" * 32):
    """A player-signed receipt with no signals.score_timeline."""
    home, away = final
    challenges = challenges if challenges is not None else [_challenge(role, "11" * 16, "aa" * 32)]
    rec = R.build(
        match={"match_id": "pair-test", "game": "madden27", "platform": "ps5",
               "scorebug_profile": "madden27_standard", "mode": "single", "started_ms": 1_000, "ended_ms": 130_000},
        result={"home_score": home, "away_score": away, "quarter": 4, "final_t_ms": 130_000,
                "winner": R.winner_for(home, away), "method": "end_menu_read", "final_basis": "end_menu",
                "final_confirmed": True},
        players=[{"role": role, "player_id": f"local:{role}", "pubkey": device.pubkey_hex, "key_kind": "software"}],
        referee={"pubkey": capture.pubkey_hex, "key_id": capture.key_id, "key_kind": "local-self-attested"},
        challenges=challenges,
        checkpoints={role: [{"seq": 0, "t_ms": 1_000, "frames_hash": "cd" * 32, "input_hash": "ef" * 32,
                             "prev": "0" * 64, "hash": head, "referee_ts_ms": 1_030, "referee_sig": "0" * 128}]},
        agreement={"timelines_agree": None, "mismatches": []},
        signals={"motion_permille": {role: None}},
    )
    assert "score_timeline" not in rec["signals"]
    return R.sign_player(R.sign_referee(rec, capture), role, device)


def _sidecar(events, *, checkpoint_head, signer=None, pub=None, sig=None, chain=None, chain_head=None):
    chained, head = chain_events(events)
    body = {
        "schema": SCHEMA,
        "signals": {"score_timeline": {"events": events}},
        "chain": chain if chain is not None else chained,
        "chain_head": chain_head if chain_head is not None else head,
        "checkpoint_head": checkpoint_head,
    }
    message = signing_message(signals=body["signals"], chain=body["chain"], chain_head=body["chain_head"],
                              checkpoint_head=checkpoint_head)
    if signer is not None:
        body["signature"] = {"pub": signer.pubkey_hex, "sig": signer.sign(message)}
    else:
        body["signature"] = {"pub": pub, "sig": sig}
    return json.dumps(body).encode(), message


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _spki(priv) -> bytes:
    return priv.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def _attest(priv, algorithm: str, head: str, *, provider="platform-crypto", signature: bytes | None = None) -> bytes:
    message = device_signed_bytes(head)
    if signature is None:
        if algorithm == "RSA_2048":
            signature = priv.sign(message, padding.PKCS1v15(), hashes.SHA256())
        else:
            signature = priv.sign(message, ec.ECDSA(hashes.SHA256()))
    body = {
        "schema": DEVICE_ATTEST_SCHEMA,
        "key_provider": provider,
        "algorithm": algorithm,
        "public_key_spki_b64": _b64(_spki(priv)),
        "non_exportable": True,
        "signature_b64": _b64(signature),
    }
    return json.dumps(body).encode()


def test_ed25519_sidecar_corroborates_when_receipt_has_no_timeline():
    home_dev, away_dev = SoftwareKey(), SoftwareKey()
    home_head, away_head = "ab" * 32, "cd" * 32
    home = _plain_receipt("home", home_dev, SoftwareKey(), head=home_head,
                          challenges=[_challenge("home", "11" * 16, "aa" * 32)])
    away = _plain_receipt("away", away_dev, SoftwareKey(), head=away_head, final=(14, 7),
                          challenges=[_challenge("away", "22" * 16, "bb" * 32)])
    home_file, _ = _sidecar(_events(), checkpoint_head=home_head, signer=home_dev)
    away_file, _ = _sidecar(_events(SCORE_EVENT_TOLERANCE_MS), checkpoint_head=away_head, signer=away_dev)
    home_side = side_from_receipt(home, own_checks="pass", score_timeline_file=home_file)
    away_side = side_from_receipt(away, own_checks="pass", score_timeline_file=away_file)
    assert home_side is not None and away_side is not None
    out = build_corroboration(home_side, away_side, home_present=True, away_present=True)
    assert out["status"] == "corroborated", out["reasons"]
    assert out["affects_verdict"] is False
    assert out["finals"] == {"home": [14, 7], "away": [14, 7]}
    view = corroboration_view({"corroboration": dict(out, finals={"home": [14, 7], "away": [14, 3], "bad": [1]})})
    assert view["finals"] == {"home": [14, 7], "away": [14, 3]}
    assert view["affects_verdict"] is False


def test_bad_or_missing_sidecar_stays_unsigned_or_single_sided():
    device = SoftwareKey()
    head = "ab" * 32
    home = _plain_receipt("home", device, SoftwareKey(), head=head)
    good, _ = _sidecar(_events(), checkpoint_head=head, signer=device)
    tampered = json.loads(good)
    tampered["signals"]["score_timeline"]["events"][0]["home"] = 9
    wrong_head, _ = _sidecar(_events(), checkpoint_head="ff" * 32, signer=device)
    bad_sig = json.loads(good)
    bad_sig["signature"]["sig"] = "00" * 64
    cases = (None, json.dumps(tampered).encode(), wrong_head, json.dumps(bad_sig).encode(), b"not json")
    for raw in cases:
        assert side_from_receipt(home, own_checks="pass", score_timeline_file=raw) is None
        both = build_corroboration(None, None, home_present=True, away_present=True)
        assert both["status"] == "disagreed"
        assert both["reasons"] == ["unsigned_score_timeline"]
    one = build_corroboration(None, None, home_present=True, away_present=False)
    assert one["status"] == "single_sided"
    assert "unsigned_score_timeline" not in one["reasons"]


def test_receipt_timeline_wins_over_sidecar():
    device = SoftwareKey()
    head = "ab" * 32
    rec = _signed_receipt("home", device, SoftwareKey(), head=head, events=_events())
    other, _ = _sidecar(_events(away=3), checkpoint_head=head, signer=device)
    side = side_from_receipt(rec, own_checks="pass", score_timeline_file=other)
    assert side is not None
    assert side["events"][-1]["away"] == 7


def test_present_but_malformed_receipt_timeline_does_not_fall_back():
    device = SoftwareKey()
    head = "ab" * 32
    rec = _signed_receipt("home", device, SoftwareKey(), head=head, events=_events())
    rec = copy.deepcopy(rec)
    rec["signals"]["score_timeline"] = {"events": "nope"}
    rec = R.sign_player(R.sign_referee(rec, SoftwareKey()), "home", device)
    good, _ = _sidecar(_events(), checkpoint_head=head, signer=device)
    assert side_from_receipt(rec, own_checks="pass", score_timeline_file=good) is None


def test_device_attest_rsa_and_p256_sign_the_same_bytes():
    events = _events()
    head = "ab" * 32
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p256 = ec.generate_private_key(ec.SECP256R1())
    for priv, algorithm in ((rsa_key, "RSA_2048"), (p256, "ECDSA_P256")):
        attest = _attest(priv, algorithm, head)
        chained, chain_head = chain_events(events)
        message = signing_message(
            signals={"score_timeline": {"events": events}}, chain=chained, chain_head=chain_head,
            checkpoint_head=head)
        if algorithm == "RSA_2048":
            raw_sig = priv.sign(message, padding.PKCS1v15(), hashes.SHA256())
        else:
            raw_sig = priv.sign(message, ec.ECDSA(hashes.SHA256()))
        spki = _spki(priv)
        for pub in (_b64(spki), spki.hex()):
            raw, _ = _sidecar(events, checkpoint_head=head, pub=pub, sig=raw_sig.hex())
            got = events_from_sidecar(raw, player_pub="00" * 32, checkpoint_head=head, device_attest=attest)
            assert got == events, algorithm
        b64_sig, _ = _sidecar(events, checkpoint_head=head, pub=_b64(spki), sig=_b64(raw_sig))
        assert events_from_sidecar(b64_sig, player_pub="00" * 32, checkpoint_head=head, device_attest=attest) == events


def test_pss_and_unauthenticated_device_key_are_not_a_timeline():
    events = _events()
    head = "ab" * 32
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attest = _attest(priv, "RSA_2048", head)
    chained, chain_head = chain_events(events)
    message = signing_message(
        signals={"score_timeline": {"events": events}}, chain=chained, chain_head=chain_head, checkpoint_head=head)
    pss = priv.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                    hashes.SHA256())
    raw, _ = _sidecar(events, checkpoint_head=head, pub=_b64(_spki(priv)), sig=pss.hex())
    assert events_from_sidecar(raw, player_pub="00" * 32, checkpoint_head=head, device_attest=attest) is None
    swapped = _attest(priv, "RSA_2048", head, signature=b"\x00" * 256)
    good, _ = _sidecar(events, checkpoint_head=head, pub=_b64(_spki(priv)),
                       sig=priv.sign(message, padding.PKCS1v15(), hashes.SHA256()).hex())
    assert events_from_sidecar(good, player_pub="00" * 32, checkpoint_head=head, device_attest=swapped) is None


def test_player_key_sidecar_ignores_a_bad_device_attest():
    device = SoftwareKey()
    head = "ab" * 32
    home = _plain_receipt("home", device, SoftwareKey(), head=head)
    raw, _ = _sidecar(_events(), checkpoint_head=head, signer=device)
    side = side_from_receipt(home, own_checks="pass", score_timeline_file=raw, device_attest_file=b"{")
    assert side is not None
    assert side["events"][-1]["home"] == 14


def test_sidecar_corroboration_does_not_upgrade_a_failing_receipt(ref):
    client, clock, key = ref
    rec, pkeys = run_match(client, clock, "single")
    edited = copy.deepcopy(rec)
    edited["result"]["winner"] = "away" if rec["result"]["winner"] != "away" else "home"
    edited["review"] = {"corroboration": {
        "status": "corroborated", "reasons": [], "affects_verdict": True,
        "finals": {"home": [14, 7], "away": [14, 7]},
    }}
    edited = R.sign_referee(edited, key)
    for role, player in pkeys.items():
        edited = R.sign_player(edited, role, player)
    verdict = R.verify_receipt(edited, referee_pub=key.pubkey_hex)
    assert verdict["status"] == "FAIL"
    assert "winner_inconsistent" in verdict["errors"]
    assert verdict["corroboration"]["status"] == "corroborated"
    assert verdict["corroboration"]["finals"]["home"] == [14, 7]
    assert verdict["corroboration"]["affects_verdict"] is False
