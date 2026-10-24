"""Referee review engine: submissions, spot-check plan, segment verification, review, official countersign.

HTTP-free so tests can drive it directly; ``qor_receipt.referee_service.app`` wraps it in FastAPI.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import shutil
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from qor_receipt import receipt as R
from qor_receipt.agent.pause_detect import ONSET_HORIZON_MS, ONSET_SLACK_MS, detector_for_challenge_rule
from qor_receipt.challenge import (
    DEFAULT_DEADLINE_MS,
    PHASES,
    RULE_V2,
    apply_consistency,
    challenge_rule_of,
    latency_ceiling_ms,
    menu_safe_nonce,
    pattern_for,
    response_press_ms,
    verify_response,
)
from qor_receipt.jcs import canonicalize, sha256_hex
from qor_receipt.keys import KeyProvider, kid, verify
from qor_receipt.review import bundle as B
from qor_receipt.review import media as M
from qor_receipt.review.clock import capture_clock, fragment_pts0_ms, frame_mono_ms, motion_mono_ms
from qor_receipt.review.corroboration import (
    SCORE_EVENT_TOLERANCE_MS,
    accept_pairing_token,
    build_corroboration,
    own_checks_label,
    read_pairing_file,
    side_from_bundle,
)
from qor_receipt.review.device_attest import DEVICE_ATTEST_SCHEMA, device_key_for_bundle
from qor_receipt.review.motion import (
    MOTION_BASIS_V3,
    MOTION_HZ,
    MOTION_RULE,
    MOTION_SAMPLE_MAX_SEGMENTS,
    MOTION_SIZE,
    motion_sample_seqs,
    score_motion,
)
from qor_receipt.review.policy import (
    ANCHOR_TYPE,
    CHALLENGE_TOKEN_TYPE,
    FAIL_CHECK,
    MAX_SEGMENT_BYTES,
    NOT_PROVEN,
    PAIRING_TYPE,
    PARTIAL_CHECK,
    PASS,
    RANDOM_SEGMENTS,
    REVIEW_KEY_KIND,
    REVIEW_TYPE,
    TAIL_MS,
)

SPOT, FULL = "spot-check", "full"
TAIL_HZ = 2.0
ANCHOR_SLACK_MS = 5_000


def _token_payload(tok: dict[str, Any]) -> bytes:
    return canonicalize({k: v for k, v in tok.items() if k != "sig"})


def verify_token(tok: Any, pubkey_hex: str, typ: str) -> bool:
    """Signed challenge / anchor token issued by the referee key ``pubkey_hex``."""
    if not isinstance(tok, dict) or tok.get("type") != typ or not isinstance(tok.get("sig"), str):
        return False
    if tok.get("key_id") != kid(pubkey_hex):
        return False
    return verify(pubkey_hex, tok["sig"], _token_payload(tok))


@dataclass
class Submission:
    id: str
    dir: Path
    mode: str
    created_ms: int
    upload_token: str
    bundle: B.Bundle
    log_entry: dict | None = None
    state: str = "new"  # rejected | awaiting_segments | refused | awaiting_player_signature | complete
    errors: list[str] = field(default_factory=list)
    commit: str | None = None
    required: list[int] = field(default_factory=list)
    decode: set[int] = field(default_factory=set)       # segments kept on disk for decoding
    random_sample: list[int] = field(default_factory=list)
    motion_sample: list[int] = field(default_factory=list)
    received: dict[int, int] = field(default_factory=dict)  # seq -> bytes verified
    bad: dict[int, str] = field(default_factory=dict)
    review: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    verify: dict[str, Any] | None = None
    corroboration: dict[str, Any] | None = None
    pairing_id: str | None = None

    def public(self) -> dict[str, Any]:
        missing = [s for s in self.required if s not in self.received]
        out = {"id": self.id, "state": self.state, "mode": self.mode, "errors": self.errors, "commit": self.commit,
               "segments_total": len(self.bundle.segments), "required": self.required, "missing": missing[:50],
               "missing_count": len(missing), "received": len(self.received), "bad_segments": self.bad,
               "bytes_required": sum(int(self.bundle.segments[s]["bytes"]) for s in self.required),
               "review": self.review, "verify": self.verify}
        reason = self.review.get("reason") if isinstance(self.review, dict) else None
        checks = self.review.get("checks") if isinstance(self.review, dict) else None
        blocked = isinstance(checks, dict) and any(
            isinstance(c, dict) and c.get("state") == FAIL_CHECK for c in checks.values())
        if isinstance(reason, str) and reason and blocked:
            out["reason"] = reason
        if self.corroboration:
            out["corroboration"] = self.corroboration
        return out


class Referee:
    def __init__(self, key: KeyProvider, *, workdir: str | Path, now: Callable[[], int] | None = None,
                 rng: Any = None, core_factory: Any = "sidecar", sidecar_dir: str | None = None,
                 random_segments: int = RANDOM_SEGMENTS, tail_ms: int = TAIL_MS, referee_id: str = "qortime-v1",
                 delegation: dict[str, Any] | None = None, log: Any = None) -> None:
        self.key = key
        self.delegation = delegation  # root-signed qortime-referee-delegation/v1 cert for this (online) key, if any
        self.log = log  # qor_receipt.translog.TransparencyLog (every signed receipt is appended before release)
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.now = now or (lambda: int(time.time() * 1000))
        self.rng = rng or secrets.SystemRandom()
        self.sidecar_dir = sidecar_dir
        self.core_factory = M.sidecar_core_factory(sidecar_dir) if core_factory == "sidecar" else core_factory
        self.random_segments = random_segments
        self.tail_ms = tail_ms
        self.referee_id = referee_id
        self.subs: dict[str, Submission] = {}
        # pairing_id -> {nonce, roles: {home|away: submission id}, conflicts: [str]}
        self.pairs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ info / live tokens
    def info(self) -> dict[str, Any]:
        return {"id": self.referee_id, "pubkey": self.key.pubkey_hex, "key_id": self.key.key_id, "key_kind": REVIEW_KEY_KIND,
                "review_type": REVIEW_TYPE, "final_reader": self.core_factory is not None,
                "ffmpeg": M.ffmpeg_bin() is not None,
                "scorebug_profiles": M.sidecar_profiles(self.sidecar_dir) if self.core_factory is not None else [],
                "policy": {"random_segments": self.random_segments, "tail_ms": self.tail_ms, "modes": [SPOT, FULL],
                           "pairing_type": PAIRING_TYPE,
                           "corroboration_tolerance_ms": SCORE_EVENT_TOLERANCE_MS,
                           "device_attest": DEVICE_ATTEST_SCHEMA},
                "delegation": self.delegation,
                "log": ({"size": len(self.log.entries), "head": self.log.head} if self.log is not None else None)}

    def _sign_token(self, tok: dict[str, Any]) -> dict[str, Any]:
        tok = dict(tok, key_id=self.key.key_id)
        tok["sig"] = self.key.sign(_token_payload(tok))
        return tok

    def issue_challenge(self, phase: str, role: str = "home", match_id: str = "", anchor: dict | None = None,
                        nonce: str | None = None) -> dict[str, Any]:
        if phase not in PHASES or role not in ("home", "away"):
            raise ValueError("bad phase/role")
        t = self.now()
        n = nonce or menu_safe_nonce()
        return self._sign_token({"type": CHALLENGE_TOKEN_TYPE, "nonce": n, "pattern": pattern_for(n), "phase": phase,
                                 "role": role, "match_id": str(match_id)[:128], "issued_ms": t,
                                 "deadline_ms": t + DEFAULT_DEADLINE_MS, "anchor": _clean_anchor(anchor)})

    def issue_pairing(self) -> dict[str, Any]:
        """A shared match-pairing token. The id and nonce are chosen here and signed.

        Callers cannot supply them. Both players put this exact token in ``pairing.json``.
        """
        return self._sign_token({"type": PAIRING_TYPE, "pairing_id": secrets.token_hex(16),
                                 "nonce": secrets.token_hex(16), "issued_ms": self.now()})

    def _corroboration(self, sub: Submission, checks: dict[str, Any]) -> dict[str, Any]:
        """Link this submission by a referee-signed pairing token and cross-check the partner, if any.

        A missing or forged token stays ``single_sided``. It is not a review check, so it cannot
        change pass / partial / fail.
        """
        raw = read_pairing_file(sub.bundle.files)
        if raw is None:
            return build_corroboration(None, None, home_present=False, away_present=False)
        accepted = None if raw.get("_malformed") else accept_pairing_token(raw, self.key.pubkey_hex)
        if accepted is None:
            return build_corroboration(None, None, home_present=False, away_present=False,
                                       extra_reasons=["pairing_unverified"])
        pid, nonce = accepted["pairing_id"], accepted["nonce"]
        slot = self.pairs.setdefault(pid, {"nonce": nonce, "roles": {}, "conflicts": []})
        extra: list[str] = []
        if slot["nonce"] != nonce:
            extra.append("pairing_nonce_mismatch")
            return build_corroboration(None, None, home_present=False, away_present=False,
                                       extra_reasons=extra, pairing_id=pid)
        role = sub.bundle.role if sub.bundle.role in ("home", "away") else ""
        prev = slot["roles"].get(role)
        if role and prev and prev != sub.id:
            msg = f"{role}:{prev}:{sub.id}"
            if msg not in slot["conflicts"]:
                slot["conflicts"].append(msg)
            extra.append("role_conflict")
        elif role:
            slot["roles"][role] = sub.id
        sub.pairing_id = pid
        if slot["conflicts"]:
            extra.append("role_conflict")
        home_id, away_id = slot["roles"].get("home"), slot["roles"].get("away")
        home_sub = self.subs.get(home_id) if home_id else None
        away_sub = self.subs.get(away_id) if away_id else None

        def label(other: Submission | None) -> str:
            if other is None:
                return "absent"
            if other is sub:
                return own_checks_label(checks)
            return own_checks_label((other.review or {}).get("checks"))

        home = side_from_bundle(home_sub.bundle, label(home_sub)) if home_sub is not None else None
        away = side_from_bundle(away_sub.bundle, label(away_sub)) if away_sub is not None else None
        return build_corroboration(home, away, home_present=home_sub is not None, away_present=away_sub is not None,
                                   extra_reasons=extra, pairing_id=pid)

    def _publish_pair(self, sub: Submission, view: dict[str, Any]) -> None:
        """Copy the live corroboration onto the partner. Re-sign only while they still owe a player signature."""
        pid = view.get("pairing_id")
        slot = self.pairs.get(pid) if isinstance(pid, str) else None
        if not slot:
            return
        for sid in (slot.get("roles") or {}).values():
            other = self.subs.get(sid)
            if other is None or other is sub:
                continue
            other.corroboration = view
            if isinstance(other.review, dict):
                other.review["corroboration"] = view
            if other.state == "awaiting_player_signature" and isinstance(other.receipt, dict):
                review = other.receipt.get("review")
                if isinstance(review, dict):
                    review["corroboration"] = view
                    other.receipt = R.sign_referee(other.receipt, self.key)

    def issue_anchor(self, match_id: str, anchor: dict) -> dict[str, Any]:
        a = _clean_anchor(anchor)
        if not a:
            raise ValueError("anchor needs chain_head")
        return self._sign_token({"type": ANCHOR_TYPE, "match_id": str(match_id)[:128], **a, "referee_ts_ms": self.now()})

    # ------------------------------------------------------------ submissions
    def create(self, files: dict[str, bytes], mode: str = SPOT) -> Submission:
        if mode not in (SPOT, FULL):
            raise ValueError("mode must be spot-check or full")
        sid = secrets.token_hex(8)
        d = self.workdir / sid
        (d / "meta").mkdir(parents=True)
        (d / "segs").mkdir()
        for name, data in files.items():
            (d / "meta" / name).write_bytes(data)
        sub = Submission(id=sid, dir=d, mode=mode, created_ms=self.now(), upload_token=secrets.token_urlsafe(24),
                         bundle=B.load(files))
        self.subs[sid] = sub
        errs = self._precheck(sub)
        if errs:
            sub.state, sub.errors = "rejected", errs
            return sub
        self._plan(sub)
        sub.state = "awaiting_segments"
        return sub

    def _precheck(self, sub: Submission) -> list[str]:
        b = sub.bundle
        errs = list(b.errors)
        if errs:
            return errs
        errs += B.check_manifest(b)
        by_seg, e = B.check_inputs(b)
        errs += e
        errs += B.check_checkpoints(b, by_seg)
        errs += B.check_receipt_binding(b)
        _, e = B.check_input_files(b)
        errs += e
        r = b.receipt
        ref = r.get("referee") or {}
        if str(ref.get("pubkey", "")).lower() == self.key.pubkey_hex.lower():
            errs.append("receipt is already signed by the official referee")
        players = r.get("players") or []
        if len(players) != 1 or (r.get("match") or {}).get("mode") != "single":
            errs.append("only single-console bundles are supported by the review service")
        # The laptop's own receipt must be internally sound under its own keys (signatures, chain timing, result).
        v = R.verify_receipt(r, referee_pub=ref.get("pubkey"))
        local_errors = [x for x in (v.get("errors") or []) if isinstance(x, str)]
        # The referee re-runs every challenge from the raw log. A laptop that already stored
        # challenge_failed is not a reason to refuse the upload. Any other local error still is.
        if not _only_challenge_failed(local_errors):
            errs += [f"local_receipt:{x}" for x in local_errors]
        return errs

    def _wall_minus_mono(self, b: B.Bundle) -> int:
        diffs = [int(m["wall_ms"]) - int(m["mono_ms"]) for m in b.segments if "wall_ms" in m and "mono_ms" in m]
        return int(statistics.median(diffs)) if diffs else (B.wall_anchor(b) or 0)

    def _plan(self, sub: Submission) -> None:
        b = sub.bundle
        segs = b.segments
        sub.commit = sha256_hex({"chain_head": b.chain_head, "checkpoint_head": b.checkpoints[-1]["hash"],
                                 "input_head": b.input_head, "files": {k: B.file_sha(v) for k, v in sorted(b.files.items())},
                                 "mode": sub.mode, "submission": sub.id})
        inits = {int(m["seq"]) for m in segs if int(m.get("offset", 0)) == 0}
        last_file = segs[-1]["file"]
        end_mono = int(segs[-1]["mono_ms"])
        tail = {int(m["seq"]) for m in segs if m["file"] == last_file and int(m["mono_ms"]) >= end_mono - self.tail_ms}
        wmm = self._wall_minus_mono(b)
        chal: set[int] = set()
        presses = B.press_events(b)
        for c in b.receipt.get("challenges") or []:
            chal |= set(_challenge_seqs(b, c, presses, wmm))
        # Both draws happen only now, after the manifest/checkpoints/inputs are committed (sub.commit),
        # from the referee's RNG. The motion sample is one segment per time stratum, so the client
        # cannot know which fragments will be scored before this commit.
        sub.motion_sample = motion_sample_seqs(segs, self.rng)
        motion = sub.motion_sample
        fixed = inits | tail | chal | set(motion)
        pool = [int(m["seq"]) for m in segs if int(m["seq"]) not in fixed]
        sub.random_sample = sorted(self.rng.sample(pool, min(self.random_segments, len(pool))))
        sub.decode = fixed | set(sub.random_sample)
        sub.required = sorted(range(len(segs))) if sub.mode == FULL else sorted(sub.decode)
        (sub.dir / "plan.json").write_text(json.dumps({"commit": sub.commit, "required": sub.required,
                                                       "random": sub.random_sample, "tail": sorted(tail),
                                                       "challenge": sorted(chal), "init": sorted(inits),
                                                       "motion": motion}))

    def put_segment(self, sub: Submission, seq: int, chunks) -> dict[str, Any]:
        """Stream one segment's bytes; hash against the committed manifest. ``chunks`` is an iterable of bytes."""
        if sub.state != "awaiting_segments":
            raise ValueError(f"submission is {sub.state}")
        if seq < 0 or seq >= len(sub.bundle.segments):
            raise ValueError("no such segment")
        if sub.mode == SPOT and seq not in sub.decode:
            raise ValueError("segment not requested")
        m = sub.bundle.segments[seq]
        want_len, want_sha = int(m["bytes"]), str(m["sha256"])
        h, n = hashlib.sha256(), 0
        keep = seq in sub.decode
        tmp = sub.dir / "segs" / f"{seq}.part"
        fh = open(tmp, "wb") if keep else None
        try:
            for c in chunks:
                n += len(c)
                if n > min(want_len, MAX_SEGMENT_BYTES):
                    raise ValueError("segment larger than the manifest says")
                h.update(c)
                if fh:
                    fh.write(c)
        finally:
            if fh:
                fh.close()
        ok = n == want_len and h.hexdigest() == want_sha
        if not ok:
            sub.bad[seq] = "sha256/length mismatch: bytes differ from the committed manifest"
            tmp.unlink(missing_ok=True)
            return {"seq": seq, "ok": False, "error": sub.bad[seq]}
        sub.bad.pop(seq, None)
        if keep:
            tmp.rename(sub.dir / "segs" / f"{seq}.bin")
        sub.received[seq] = n
        return {"seq": seq, "ok": True, "bytes": n, "remaining": len([s for s in sub.required if s not in sub.received])}

    def _read_seg(self, sub: Submission):
        def rd(seq: int) -> bytes | None:
            p = sub.dir / "segs" / f"{seq}.bin"
            return p.read_bytes() if p.exists() else None
        return rd

    # ------------------------------------------------------------ review
    def review(self, sub: Submission) -> dict[str, Any]:
        if sub.state == "awaiting_player_signature" or sub.state == "complete":
            return sub.public()
        if sub.state != "awaiting_segments":
            raise ValueError(f"submission is {sub.state}")
        b = sub.bundle
        checks: dict[str, dict[str, Any]] = {}
        wmm = self._wall_minus_mono(b)
        missing = [s for s in sub.required if s not in sub.received]
        total_bytes = sum(int(m["bytes"]) for m in b.segments)
        verified_bytes = sum(sub.received.values())
        k = len(sub.random_sample)
        checks["bundle_structure"] = {"state": PASS, "detail": "manifest chain, checkpoint chain, inputs chain and "
                                      "receipt binding recomputed by the referee"}
        checks["capture_chain"] = {"state": PASS, "detail": "laptop checkpoint countersignatures valid under the capture "
                                   "key; gaps/skew within limits (timing is the laptop's own clock)"}
        vb: dict[str, Any] = {"method": sub.mode, "segments_total": len(b.segments), "segments_verified": len(sub.received),
                              "bytes_total": total_bytes, "bytes_verified": verified_bytes, "random_segments": k,
                              "random_sample": sub.random_sample, "commit": sub.commit}
        if sub.mode == SPOT:
            vb["p_undetected_if_missing_10pct"] = f"{0.9 ** k:.3g}"
            vb["p_undetected_if_missing_50pct"] = f"{0.5 ** k:.3g}"
        if missing or sub.bad:
            vb["missing"] = missing[:50]
            vb["bad"] = sub.bad
            checks["video_bytes"] = {"state": FAIL_CHECK, "detail": vb}
        else:
            checks["video_bytes"] = {"state": PASS, "detail": vb}

        rd = self._read_seg(sub)
        # ---- final re-read from the referee's own decode of the recorded tail
        final = None
        if checks["video_bytes"]["state"] != PASS:
            checks["final_reread"] = {"state": FAIL_CHECK, "detail": "video bytes not verified"}
        elif self.core_factory is None or M.ffmpeg_bin() is None:
            checks["final_reread"] = {"state": FAIL_CHECK, "detail": "referee final reader unavailable (fail-closed)"}
        else:
            last_file = b.segments[-1]["file"]
            end_mono = int(b.segments[-1]["mono_ms"])
            tail = [int(m["seq"]) for m in b.segments
                    if m["file"] == last_file and int(m["mono_ms"]) >= end_mono - self.tail_ms and int(m.get("offset", 0)) > 0]

            def frames():
                for run in M.runs(tail, b):
                    for t, img in M.decode_run(b, run, rd, hz=TAIL_HZ):
                        yield t + wmm, img
            corr = M.sidecar_corroboration(str(sub.dir / "meta"), self.sidecar_dir)
            try:
                challenge_rule = challenge_rule_of(b.meta)

                def factory(post, _base=self.core_factory, _rule=challenge_rule):
                    return M.install_rule_pause(_base(post), _rule)

                final = M.reread_final(frames(), factory, corroboration=corr,
                                       timeline_locks=M.timeline_locks_from_bytes(b.files.get("timeline.jsonl")))
                checks["final_reread"] = _final_check(final, b.receipt.get("result") or {})
            except Exception as e:  # undecodable video is a failed check, never a crash or a silent pass
                final = None
                checks["final_reread"] = {"state": FAIL_CHECK, "detail": f"decode/read error: {type(e).__name__}: {e}"}
        # ---- challenges (official nonce tokens, raw HID re-run, pause seen on the referee's decode)
        ch_out, ch_check, pause_spans, challenge_reason, challenge_blocking = self._challenges(sub, rd, wmm)
        checks["challenges"] = ch_check
        # ---- motion rule v3: a spread across the match, not the final-reread tail
        try:
            mp, mdetail = self._motion(sub, rd, pause_spans, wmm)
        except Exception as e:
            mp, mdetail = None, {"note": f"motion not computed: {type(e).__name__}: {e}"}
        checks["motion"] = {"state": PASS if mp is not None and mp >= R.MOTION_MIN_PERMILLE else PARTIAL_CHECK,
                            "detail": mdetail}
        # ---- input proof integrity
        isum, ierrs = B.check_input_files(b)
        if not isum.get("present"):
            checks["input_integrity"] = {"state": PARTIAL_CHECK, "detail": "no controller log in this bundle"}
        else:
            checks["input_integrity"] = {"state": FAIL_CHECK if ierrs else PASS,
                                         "detail": {**isum, "errors": ierrs,
                                                    "note": "hashes/chains/counts recomputed; QorCap's human-likeness and "
                                                            "alignment model is not re-run (advisory only)"}}
        anchors = self._anchors(sub)
        corroboration = self._corroboration(sub, checks)
        review = {"type": REVIEW_TYPE, "reviewed_ms": self.now(), "submission": sub.id, "commit": sub.commit,
                  "capture_referee": {k2: (b.receipt.get("referee") or {}).get(k2) for k2 in ("pubkey", "key_id", "key_kind")},
                  "bundle": {"schema": b.meta.get("schema"), "qorcap_version": b.meta.get("qorcap_version"),
                             "chain_head": b.chain_head, "checkpoint_head": b.checkpoints[-1]["hash"],
                             "inputs_chain_head": b.input_head, "segments": len(b.segments), "bytes": total_bytes,
                             "local_receipt_sha256": sha256_hex({k2: v for k2, v in b.receipt.items() if k2 != "signatures"})},
                  "checks": checks, "anchors": anchors, "corroboration": corroboration,
                  "final_reread": _final_summary(final),
                  "scope": ("post-hoc review of an uploaded QorCap bundle by the QorTime official referee: "
                            + ("every video byte hashed against the committed manifest" if sub.mode == FULL else
                               f"spot-check of {len(sub.received)}/{len(b.segments)} segments (init, recorded tail, "
                               f"challenge windows, a stratified random spread of at most {MOTION_SAMPLE_MAX_SEGMENTS} "
                               f"media segments for motion drawn after commit, {k} random segments drawn after commit)")),
                  "not_proven": NOT_PROVEN}
        if challenge_reason:
            # First field a reader sees. A refusal must not live only under post_sign_errors.
            review = {"reason": challenge_reason, **review}
        device_key = device_key_for_bundle(b)
        if device_key is not None:
            # Informational. Not a check, so it cannot pass or fail the review.
            review["device_key"] = device_key
        sub.review = review
        sub.corroboration = corroboration
        if any(c["state"] == FAIL_CHECK for c in checks.values()):
            # Nothing was signed. qorcap submit prints refusal lines from post_sign_errors, so the
            # same blocking list that built review.reason is copied there. A video or final failure
            # with no challenge block leaves the list empty.
            if challenge_blocking:
                sub.review["post_sign_errors"] = list(challenge_blocking)
            elif challenge_reason:
                sub.review["post_sign_errors"] = [challenge_reason]
            sub.state = "refused"
            self._publish_pair(sub, corroboration)
            return sub.public()
        if self.delegation and not (int(self.delegation.get("not_before_ms", 0)) <= self.now()
                                    <= int(self.delegation.get("not_after_ms", 0))):
            sub.review["post_sign_errors"] = ["service delegation certificate is not valid now (rotate the key)"]
            sub.state = "refused"
            self._publish_pair(sub, corroboration)
            return sub.public()
        rec = self._build(sub, final, ch_out, mp, mdetail, review)
        v = R.verify_receipt(rec, referee_pub=self.key.pubkey_hex)
        blocking = [e for e in v["errors"] if not e.startswith("missing_player_signature")]
        if blocking:
            sub.review["post_sign_errors"] = blocking
            sub.state = "refused"
            self._publish_pair(sub, corroboration)
            return sub.public()
        if self.log is not None:
            from qor_receipt.translog import body_sha256

            le = self.log.append("receipt", body_sha256(rec), submission=sub.id, match_id=str(rec["match"].get("match_id")))
            sub.log_entry = le  # not part of the signed receipt; returned to the client
        sub.receipt = rec
        sub.state = "awaiting_player_signature"
        self._publish_pair(sub, corroboration)
        out = sub.public()
        out["receipt"] = rec
        out["signing_bytes_sha256"] = hashlib.sha256(R.signing_bytes(rec)).hexdigest()
        if sub.log_entry:
            out["log_entry"] = sub.log_entry
        return out

    def _challenges(self, sub: Submission, rd, wmm: int
                    ) -> tuple[list[dict], dict[str, Any], list[tuple[int, int]], str | None, list[str]]:
        b = sub.bundle
        toks = {t.get("nonce"): t for t in b.jsonl("official_challenges.jsonl")}
        presses = B.press_events(b)
        rule = challenge_rule_of(b.meta)
        clock = capture_clock(b.segments) if rule == RULE_V2 else None
        ceiling = latency_ceiling_ms(rule)
        hz = 30.0
        out, notes, bad = [], [], False
        pause_spans: list[tuple[int, int]] = []
        for c in b.receipt.get("challenges") or []:
            c2 = {k: c.get(k) for k in ("id", "role", "phase", "nonce", "pattern", "issued_ms", "deadline_ms")}
            tok = toks.get(c.get("nonce"))
            official = (verify_token(tok, self.key.pubkey_hex, CHALLENGE_TOKEN_TYPE)
                        and tok["issued_ms"] == c.get("issued_ms") and tok["deadline_ms"] == c.get("deadline_ms")
                        and tok.get("phase") == c.get("phase"))
            if official:
                c2["issuer"] = {k: tok[k] for k in tok}
                a = tok.get("anchor") or {}
                if a.get("chain_head"):
                    seq = b.heads.get(a["chain_head"])
                    if seq is None or seq < 0 or int(b.segments[seq]["wall_ms"]) > tok["issued_ms"] + ANCHOR_SLACK_MS:
                        notes.append(f"{c.get('phase')}: challenge anchor chain head not in manifest before issue time")
                        bad = True
            else:
                notes.append(f"{c.get('phase')}: nonce was not issued by the official referee")
            try:
                issued, deadline = int(c["issued_ms"]), int(c["deadline_ms"])
            except (KeyError, TypeError, ValueError):
                out.append(dict(c2, result=None))
                continue
            win = [p for p in presses if issued <= p["t_ms"] <= deadline]
            seqs = _challenge_seqs(b, c, presses, wmm)
            pp = response_press_ms(c2["pattern"] or [], win, issued_ms=issued, deadline_ms=deadline)
            # Rule v2 only. Historical caps stay on rule v1. Ceiling and ±300 ms are untouched.
            det = detector_for_challenge_rule(rule)
            media_seqs = [s for s in seqs if int(b.segments[s].get("offset", 0)) > 0]
            missing = [s for s in media_seqs if s not in sub.received]
            present = [s for s in media_seqs if s not in set(missing)]
            for run in M.runs(present, b):
                init = M.init_seq_for(b, run[0])
                if init is not None and init not in sub.received:
                    missing.append(init)
            missing = sorted(set(missing))
            scan = None
            if M.ffmpeg_bin() and not missing:
                def frames(seqs=list(present)):
                    for run in M.runs(seqs, b):
                        pts0 = None
                        if rule == RULE_V2:
                            init = M.init_seq_for(b, run[0])
                            if init is not None:
                                pts0 = fragment_pts0_ms(rd(init) or b"", rd(run[0]) or b"")
                        for n, (t, img) in enumerate(M.decode_run(b, run, rd, hz=hz, size=(640, 360))):
                            # No tfdt: keep the segment_ranges time. That stamp is late, so it fails the ceiling.
                            mono = t if pts0 is None else frame_mono_ms(clock, pts0, n, hz)
                            yield mono + wmm, img
                scan = M.scan_pause(frames() if present else iter(()), det)
            if scan is not None and scan.error is None:
                pause_spans.extend(scan.spans)
            pause_seen = pause_seen_for(pp, scan.onsets) if scan is not None and scan.error is None else None
            res = verify_response(c2["pattern"] or [], [{"t_ms": p["t_ms"], "button": p["button"], "down": True} for p in win],
                                  issued_ms=issued, deadline_ms=deadline, pause_seen_ms=pause_seen,
                                  max_latency_ms=ceiling).to_dict()
            res["rule"] = rule
            res["hid_sha256"] = sha256_hex(win)
            res["reverified_by"] = "official-referee"
            # Same gate as the recorder. A mismatch the controller log cannot support is unanswered,
            # not a player failure. Old bundles without chunk times withhold and stay partial.
            if res.get("ok") is False and res.get("reason") == "pattern_mismatch":
                from qor_receipt.review.local import mismatch_log_trustworthy

                anchor = B.wall_anchor(b) or 0
                if not mismatch_log_trustworthy(
                        sub.dir / "meta", b.inputs + b.input_chunks,
                        issued_ms=issued, deadline_ms=deadline, anchor_ms=anchor):
                    out.append(dict(c2, result=None))
                    notes.append(f"{c.get('phase')}: pattern_mismatch withheld; controller log cannot support it")
                    continue
            out.append(dict(c2, result=res))
            if not res["ok"] and res["reason"] == "pause_not_seen_on_video":
                notes.append(M.pause_not_seen_note(str(c.get("phase")), scan, pp, missing=missing or None,
                                                   thresholds=det.th, rule=rule))
            elif scan is not None and scan.error:
                notes.append(f"{c.get('phase')}: pause detector error {scan.error}")
            if not res["ok"] and res["reason"] != "pause_not_seen_on_video":
                notes.append(f"{c.get('phase')}: {res['reason']}")
        if rule == RULE_V2:
            for i in apply_consistency(out):
                notes.append(f"{out[i].get('phase')}: latency_inconsistent")
        phases = {c.get("phase") for c in out if c.get("issuer") and (c.get("result") or {}).get("ok")}
        blocking: list[str] = []
        for role in ("home", "away"):
            if any(isinstance(c, dict) and c.get("role") == role for c in out):
                blocking.extend(R.blocking_challenge_errors(out, role))
        state, reason = challenge_check_state(
            blocking=blocking, anchor_bad=bad, phases_passed=all(p in phases for p in PHASES))
        if state != FAIL_CHECK:
            # A partial or a pass never carries a reason. A reason on those states would
            # headline a signed receipt as FAIL.
            reason = None
        if state == PARTIAL_CHECK:
            missing = [p for p in PHASES if p not in phases]
            notes.append(f"official challenges passed for {sorted(phases) or 'none'}; missing/failed: {missing}")
        return out, {"state": state, "detail": {"count": len(out), "rule": rule, "notes": notes}}, pause_spans, reason, blocking

    def _motion(self, sub: Submission, rd, pause_spans_wall: list[tuple[int, int]],
                wmm: int) -> tuple[int | None, dict[str, Any]]:
        """Motion rule v3 over the stratified sample drawn in ``_plan``.

        Decode budget: at most ``MOTION_SAMPLE_MAX_SEGMENTS`` media fragments (about 48 s when a
        fragment is ~2 s), at ``MOTION_HZ`` and ``MOTION_SIZE``. That is the whole budget for this
        check. The recorded tail is decoded separately for the final re-read and is not added here.
        Frame time is the fragment PTS clock. A fragment with no ``tfdt`` keeps ``segment_ranges``.
        The sample is the one already drawn from the referee RNG after commit. It is not redrawn.
        """
        b = sub.bundle
        if not M.ffmpeg_bin() or not b.inputs:
            return None, {"rule": MOTION_RULE, "note": "not computed (no ffmpeg or no controller log)"}
        sample = list(sub.motion_sample)
        present = [s for s in sample if s in sub.received]
        if not present:
            return None, {"rule": MOTION_RULE, "basis": MOTION_BASIS_V3, "bins": 0, "sample_segments": sample,
                          "note": "motion sample was not received"}
        energy = M.input_energy_series(b)
        clock = capture_clock(b.segments)
        scorebug = M.sidecar_standard_reader(self.sidecar_dir)()
        end_reader, final_fn = M.motion_screen_detectors(self.sidecar_dir)
        reasons: list[tuple[int, str]] = []
        end_hits: list[tuple[int, bool]] = []
        final_hits: list[tuple[int, bool]] = []
        motion: dict[int, int] = {}
        fallbacks = 0
        for run in M.runs(present, b):
            pts0 = None
            init = M.init_seq_for(b, run[0])
            if init is not None:
                pts0 = fragment_pts0_ms(rd(init) or b"", rd(run[0]) or b"")
            if pts0 is None:
                fallbacks += 1

            def frames(run=run, pts0=pts0):
                for n, (t, img) in enumerate(M.decode_run(b, run, rd, hz=MOTION_HZ, size=MOTION_SIZE)):
                    mono = t if pts0 is None else motion_mono_ms(clock, pts0, n, MOTION_HZ)
                    reasons.append((mono, _scorebug_reason(scorebug, img)))
                    if end_reader is not None:
                        end_hits.append((mono, _end_menu_hit(end_reader, img)))
                    if final_fn is not None:
                        final_hits.append((mono, _final_screen_hit(final_fn, img)))
                    yield mono, img

            motion.update(M.motion_series(frames()))
        spans_mono = [(a - wmm, z - wmm) for a, z in pause_spans_wall]
        return score_motion(energy, motion, scorebug_reasons=reasons, end_menu=end_hits, final_screen=final_hits,
                            pause_spans_mono=spans_mono, sample_segments=sample, clock_fallbacks=fallbacks,
                            screen_readers=end_reader is not None)

    def _anchors(self, sub: Submission) -> dict[str, Any]:
        b = sub.bundle
        ok, bad = [], 0
        for a in b.jsonl("anchors.jsonl"):
            seq = b.heads.get(a.get("chain_head")) if isinstance(a, dict) else None
            if (verify_token(a, self.key.pubkey_hex, ANCHOR_TYPE) and seq is not None and seq >= 0
                    and int(b.segments[seq]["wall_ms"]) <= int(a["referee_ts_ms"]) + ANCHOR_SLACK_MS):
                ok.append({"segment_seq": seq, "referee_ts_ms": a["referee_ts_ms"]})
            else:
                bad += 1
        return {"valid": len(ok), "invalid": bad, "first": ok[0] if ok else None, "last": ok[-1] if ok else None,
                "meaning": "each valid anchor proves the video chain up to that segment existed when the official "
                           "referee stamped it"}

    def _referee_block(self) -> dict[str, Any]:
        blk = {"pubkey": self.key.pubkey_hex, "key_id": self.key.key_id, "key_kind": REVIEW_KEY_KIND}
        if self.delegation:
            blk["delegation"] = copy.deepcopy(self.delegation)
        return blk

    def _build(self, sub: Submission, final: dict | None, challenges: list[dict], mp: int | None, mdetail: dict,
               review: dict) -> dict[str, Any]:
        b = sub.bundle
        lr = b.receipt
        role = b.role
        res = _result_from_reread(final, lr.get("result") or {})
        signals = {"motion_permille": {role: mp}, "motion_basis": mdetail.get("basis", mdetail.get("note"))}
        if (lr.get("signals") or {}).get("input"):
            signals["input"] = lr["signals"]["input"]
        rec = R.build(match=dict(lr["match"]), result=res, players=copy.deepcopy(lr["players"]),
                      referee=self._referee_block(),
                      challenges=challenges, checkpoints={role: copy.deepcopy(lr["checkpoints"][role])},
                      agreement=lr.get("agreement") or {"timelines_agree": None, "mismatches": []}, signals=signals)
        rec["review"] = review
        return R.sign_referee(rec, self.key)

    def add_player_signature(self, sub: Submission, sig: str) -> dict[str, Any]:
        if sub.state != "awaiting_player_signature" or sub.receipt is None:
            raise ValueError(f"submission is {sub.state}")
        p = sub.receipt["players"][0]
        if not verify(p["pubkey"], sig, R.signing_bytes(sub.receipt)):
            raise PermissionError("bad player signature")
        sub.receipt["signatures"]["players"][p["role"]] = {"key_id": kid(p["pubkey"]), "sig": sig}
        sub.verify = R.verify_receipt(sub.receipt, referee_pub=self.key.pubkey_hex)
        sub.state = "complete"
        self.cleanup_segments(sub)
        return {"receipt": sub.receipt, "verify": sub.verify}

    def cleanup_segments(self, sub: Submission) -> None:
        shutil.rmtree(sub.dir / "segs", ignore_errors=True)

    def expire(self, max_age_ms: int = 3_600_000) -> int:
        n = 0
        for sid, sub in list(self.subs.items()):
            if self.now() - sub.created_ms > max_age_ms:
                shutil.rmtree(sub.dir, ignore_errors=True)
                del self.subs[sid]
                n += 1
        return n


def _scorebug_reason(reader, img) -> str:
    read = reader.read(img)
    if getattr(read, "ok", False):
        return "ok"
    return str(getattr(read, "reason", "") or "")


def _end_menu_hit(reader, img) -> bool:
    read = reader.read(img)
    return bool(getattr(read, "ok", False))


def _final_screen_hit(fn, img) -> bool:
    return bool(fn(img))


def _only_challenge_failed(errors: list[str]) -> bool:
    """True when every local-receipt error is a challenge_failed and there is at least one.

    ``home:kickoff:challenge_failed:pattern_mismatch`` is that shape. A pattern problem, a chain
    error, or an empty list is not. Empty means the receipt had no errors, which is not this case.
    """
    if not errors:
        return False
    for err in errors:
        parts = err.split(":")
        if len(parts) < 3 or parts[2] != "challenge_failed":
            return False
    return True


def challenge_check_state(*, blocking: list[str], anchor_bad: bool, phases_passed: bool) -> tuple[str, str | None]:
    """Map a challenge re-run onto pass / partial / fail.

    A failure that blocks signing (``blocking``) is fail, and the refusal reason is returned with it.
    Missing or unofficial phases stay partial. An anchor that is not in the manifest is fail on its own.
    """
    if anchor_bad or blocking:
        if len(blocking) == 1:
            reason = blocking[0]
        elif blocking:
            reason = "; ".join(blocking)
        else:
            reason = "challenge_anchor_not_in_manifest"
        return FAIL_CHECK, reason
    if phases_passed:
        return PASS, None
    return PARTIAL_CHECK, None


def pause_seen_for(press_ms: int | None, onsets: list[int]) -> int | None:
    """The pause-menu onset that belongs to the answering OPTIONS press (latency is judged by verify_response).

    Lookback is 300 ms and the horizon is 5 s. Those bounds are not the latency ceiling.
    """
    if press_ms is None:
        return None
    return next((o for o in sorted(onsets) if press_ms - ONSET_SLACK_MS <= o <= press_ms + ONSET_HORIZON_MS), None)


def _challenge_seqs(b: B.Bundle, c: dict[str, Any], presses: list[dict[str, Any]], wmm: int) -> list[int]:
    """Segments the referee needs to see the pause menu for one challenge: around the answering OPTIONS press (the
    first one followed by the pattern, else the first) inside the challenge window (from the committed controller log), or the whole window when there is no press."""
    try:
        issued, deadline = int(c["issued_ms"]), int(c["deadline_ms"])
    except (KeyError, TypeError, ValueError):
        return []
    pp = response_press_ms(c.get("pattern") or [], presses, issued_ms=issued, deadline_ms=deadline)
    lo, hi = (pp - 1_000, pp + 5_000) if pp is not None else (issued, deadline)
    lo, hi = lo - wmm, hi - wmm
    # a segment covers (prev mono_ms, mono_ms]; include every segment overlapping [lo, hi]
    rng = B.segment_ranges(b)
    return [s for s, (a, z) in rng.items() if z >= lo and a <= hi]


def _clean_anchor(a: dict | None) -> dict | None:
    if not isinstance(a, dict) or not isinstance(a.get("chain_head"), str) or len(a["chain_head"]) != 64:
        return None
    out = {"chain_head": a["chain_head"].lower()}
    for k in ("segment_seq", "checkpoint_seq"):
        if isinstance(a.get(k), int) and not isinstance(a.get(k), bool):
            out[k] = a[k]
    if isinstance(a.get("checkpoint_head"), str) and len(a["checkpoint_head"]) == 64:
        out["checkpoint_head"] = a["checkpoint_head"].lower()
    return out


def _final_summary(final: dict | None) -> dict[str, Any] | None:
    if final is None:
        return None
    f = final.get("final") or {}
    lr = final.get("last_read") or {}
    return {"basis": f.get("basis"), "home": f.get("home", lr.get("home")), "away": f.get("away", lr.get("away")),
            "quarter": f.get("quarter", lr.get("quarter")), "final_confirmed": f.get("final_confirmed"),
            "notes": f.get("notes"), "scorebug_reads": final.get("reads"), "stats": final.get("stats"),
            "end_menu_rejected": (final.get("end_menu_rejected") or {}).get("why"),
            "end_menu_fallback": final.get("end_menu_fallback")}


def _score_atom(n: Any) -> int | None:
    """Non-negative integer the learning loop can compare. Booleans are not scores."""
    if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 2**53 - 1:
        return n
    return None


def _reread_value(src: dict[str, Any] | None) -> dict[str, int] | None:
    """Optional structured score beside final_reread. Absent when away/home are not integers.

    Learning may compare this object. It is not a verdict, and a missing value does not change ``state``.
    """
    if not isinstance(src, dict):
        return None
    away, home = _score_atom(src.get("away")), _score_atom(src.get("home"))
    if away is None or home is None:
        return None
    out: dict[str, int] = {"away": away, "home": home}
    quarter = _score_atom(src.get("quarter"))
    if quarter is not None:
        out["quarter"] = quarter
    return out


def _final_check(final: dict, claimed: dict) -> dict[str, Any]:
    f = final.get("final")
    lr = final.get("last_read")
    src = f or lr
    if src is None:
        return {"state": FAIL_CHECK, "detail": "referee could not read any score from the recorded tail"}
    mine = (src["home"], src["away"])
    theirs = (claimed.get("home_score"), claimed.get("away_score"))
    if mine != theirs:
        row: dict[str, Any] = {"state": FAIL_CHECK, "detail": f"referee read HOME {mine[0]} - AWAY {mine[1]} but the laptop claimed "
                                                              f"HOME {theirs[0]} - AWAY {theirs[1]}"}
    elif f and f.get("fallback") == "end_menu_without_tail_scorebug":
        if f.get("final_confirmed") is True:
            row = {"state": PASS, "detail": "end-of-game menu re-read by the referee matches the claimed final (no Q4 "
                                            "scorebug read in the tail; menu checked against the last locked read)"}
        else:
            row = {"state": PARTIAL_CHECK, "detail": "end-of-game menu read with no Q4 scorebug in the tail; not confirmed "
                                                     "against a locked read"}
    elif f and f.get("basis") == "end_menu" and f.get("final_confirmed") is not False:
        row = {"state": PASS, "detail": "end-of-game menu re-read by the referee matches the claimed final"}
    elif f and f.get("basis") == "end_menu":
        row = {"state": PARTIAL_CHECK, "detail": "end-of-game menu read but one-legal-step delta not corroborated"}
    elif f:
        row = {"state": PARTIAL_CHECK, "detail": f"scorebug final ({f.get('basis')}) only; end-of-game menu not read"}
    else:
        row = {"state": PARTIAL_CHECK, "detail": "last scorebug read only; no final detected in the recorded tail"}
    value = _reread_value(src)
    if value is not None:
        row["value"] = value
    return row


def _result_from_reread(final: dict | None, claimed: dict) -> dict[str, Any]:
    f = (final or {}).get("final")
    lr = (final or {}).get("last_read")
    src = f or lr
    q_last = src.get("quarter") if src.get("quarter") is not None else claimed.get("quarter")
    res = {"home_score": src["home"], "away_score": src["away"], "quarter": max(4, int(src.get("quarter") or 4)) if f else
           (int(q_last) if q_last is not None and int(q_last) >= 1 else None), "final_t_ms": int(src["t_ms"]),
           "winner": R.winner_for(src["home"], src["away"])}
    if f and f.get("basis") == "end_menu":
        res.update(method="end_menu_read", final_basis="end_menu", final_confirmed=f.get("final_confirmed") is not False)
    elif f:
        res.update(method="agreed_scorebug_final", final_basis=f.get("basis"), final_confirmed=False)
    else:
        res.update(method="last_scorebug_read_unconfirmed", final_confirmed=False)
    if f and f.get("notes"):
        res["notes"] = list(f["notes"])
    return res
