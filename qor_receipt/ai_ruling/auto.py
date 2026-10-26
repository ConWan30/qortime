"""HACKATHON (Meta AI Developer Hackathon, Oct 26 - Nov 4 2026): automatic Muse advisory files for new referee receipts.

After the official referee has finished a submission (the player signature is in and the receipt is final), a
background worker produces two ADVISORY sidecars, both signed with the AI advisory key (never a referee key) and bound
to SHA-256(JCS(receipt)):

* ``ai_ruling.json``        Muse Spark plain-English explanation of the verdict (``affects_verdict: false``).
* ``score_crosscheck.json`` Muse Spark re-read of the scorebug just after each locked score the referee holds video for,
                            plus the final score in the recorded tail (``lower_only``, max cap PARTIAL).

They are published at ``GET /v1/advisory/{receipt_jcs_sha256}/{name}`` on the referee; the verifier page falls back to
that URL when a receipt has no sibling file.

Hard rules (tests: tests/test_auto_advisory.py):
* It runs strictly after the verdict. It gets deep copies of the receipt and the verify result, never writes to the
  submission, and the HTTP response has already been built. It cannot delay, block or alter the verdict.
* Any failure, timeout, cap or missing secret = no file. The verifier then shows "Muse unchecked", no penalty.
* Kill switch ``QORTIME_AI_ADVISORY_ENABLED`` (must be "1"; anything else is off). Per-match cap
  ``QORTIME_AI_MATCH_USD_CAP`` (default $0.05, enforced by a worst-case estimate before any call). Weekly cap
  ``QORTIME_AI_WEEKLY_USD_CAP`` (default $5, rolling 7 days, ledger in the state dir).
* Secrets come only from env: ``META_API_KEY`` (or ``MODEL_API_KEY``) and ``QORTIME_AI_ADVISORY_SEED``. They are never
  logged or written to disk.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from qor_receipt.jcs import canonicalize

ENV_ENABLED = "QORTIME_AI_ADVISORY_ENABLED"
ENV_SEED = "QORTIME_AI_ADVISORY_SEED"
ENV_MATCH_USD = "QORTIME_AI_MATCH_USD_CAP"
ENV_WEEKLY_USD = "QORTIME_AI_WEEKLY_USD_CAP"
ENV_TIMEOUT_S = "QORTIME_AI_TIMEOUT_S"
ENV_ADMIN = "QORTIME_AI_ADMIN_TOKEN"

NAMES = ("ai_ruling.json", "score_crosscheck.json")
WEEK_MS = 7 * 86_400_000
# Worst-case cost estimates in micro-USD, used to reserve budget BEFORE any call.
# Explanation: ~2k input tokens at $1.25/M + max 2000 output tokens at $4.25/M ~= $0.011.
EXPLAIN_EST_MICRO = 11_000
# One scorebug read: one small image + short JSON reply; measured ~$0.0015, reserve $0.0025.
READ_EST_MICRO = 2_500
MAX_READS = 12
LOCK_OFFSETS_MS = (1_000, 2_500)
MAX_LOCKS = 4
FINAL_FRAMES = 3
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def receipt_hash(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(canonicalize(receipt)).hexdigest()


def _usd_env(env: dict[str, str], name: str, default: float) -> int:
    try:
        v = float(env.get(name) or default)
    except ValueError:
        v = default
    return max(0, int(round(v * 1_000_000)))


class Caps:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.enabled = (env.get(ENV_ENABLED) or "").strip() == "1"
        self.match_micro = _usd_env(env, ENV_MATCH_USD, 0.05)
        self.weekly_micro = _usd_env(env, ENV_WEEKLY_USD, 5.0)
        try:
            self.timeout_s = max(10.0, float(env.get(ENV_TIMEOUT_S) or 240))
        except ValueError:
            self.timeout_s = 240.0

    def max_reads(self) -> int:
        left = self.match_micro - EXPLAIN_EST_MICRO
        return max(0, min(MAX_READS, left // READ_EST_MICRO))

    def public(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "match_usd_cap": self.match_micro / 1e6, "weekly_usd_cap": self.weekly_micro / 1e6,
                "timeout_s": self.timeout_s, "max_reads": self.max_reads()}


class Ledger:
    """Rolling spend ledger (micro-USD) in the state dir. Reservations are held in memory while a job runs."""

    def __init__(self, path: Path, now: Callable[[], int] | None = None) -> None:
        self.path = Path(path)
        self.now = now or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._pending = 0

    def week_spent(self) -> int:
        cut = self.now() - WEEK_MS
        total = 0
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and int(o.get("ts_ms", 0)) >= cut:
                    total += max(0, int(o.get("micro_usd", 0)))
        except OSError:
            pass
        return total

    def reserve(self, micro: int, weekly_cap: int) -> bool:
        with self._lock:
            if self.week_spent() + self._pending + micro > weekly_cap:
                return False
            self._pending += micro
            return True

    def settle(self, reserved: int, actual: int, ref: str, kind: str) -> None:
        with self._lock:
            self._pending = max(0, self._pending - reserved)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts_ms": self.now(), "micro_usd": int(actual), "receipt": ref[:16], "kind": kind}) + "\n")


class Store:
    """advisory/<receipt_jcs_sha256>/{ai_ruling.json, score_crosscheck.json, status.json}."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, sha: str) -> Path:
        if not _SHA_RE.match(sha or ""):
            raise ValueError("bad receipt hash")
        return self.root / sha

    def write(self, sha: str, name: str, doc: dict[str, Any]) -> None:
        if name not in NAMES + ("status.json",):
            raise ValueError("bad name")
        d = self._dir(sha)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, d / name)

    def read(self, sha: str, name: str) -> bytes | None:
        if name not in NAMES + ("status.json",):
            return None
        try:
            return (self._dir(sha) / name).read_bytes()
        except (OSError, ValueError):
            return None

    def status(self, sha: str) -> dict[str, Any] | None:
        raw = self.read(sha, "status.json")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None


# ------------------------------------------------------------------ frames from the referee's own segments
def _jpeg(img) -> bytes | None:
    import cv2

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return buf.tobytes() if ok else None


def _grab(b, rd, seq: int, target_mono: int, *, hz: float = 4.0, tol_ms: int = 600) -> bytes | None:
    """JPEG of the frame nearest ``target_mono`` inside one stored segment, or None."""
    from qor_receipt.review import media as M

    best = None
    for t, img in M.decode_run(b, [seq], rd, hz=hz):
        d = abs(t - target_mono)
        if d <= tol_ms and (best is None or d < best[0]):
            best = (d, img.copy())
    return _jpeg(best[1]) if best else None


def locks_from_bundle(b, rd, have: set[int], receipt: dict[str, Any], *, wmm: int, tail_ms: int,
                      offsets_ms=LOCK_OFFSETS_MS, max_locks: int = MAX_LOCKS, final_frames: int = FINAL_FRAMES,
                      deadline: float | None = None) -> list[dict[str, Any]]:
    """Cross-check inputs built only from segments the referee itself holds (``have``). Never raises.

    * one row per laptop score lock (timeline.jsonl, wall clock) whose +1.0 s / +2.5 s frames fall in a stored segment
      (most recent ``max_locks``);
    * one ``final`` row: the receipt's final score vs frames in the recorded tail after the last lock.
    Neighbouring lock scores are passed as ``tolerate`` so a timing slip can never become a disagreement.
    """
    from qor_receipt.review import bundle as Bm
    from qor_receipt.review import media as M

    def late() -> bool:
        return deadline is not None and time.monotonic() > deadline

    out: list[dict[str, Any]] = []
    try:
        ranges = Bm.segment_ranges(b)
        media = {int(m["seq"]) for m in b.segments if int(m.get("offset", 0)) > 0}

        def seq_at(mono: int) -> int | None:
            for s, (a, z) in ranges.items():
                if s in media and s in have and a <= mono < z:
                    return s
            return None

        locks = M.timeline_locks_from_bytes(b.files.get("timeline.jsonl"))
        scores = [{"away": int(lk["away"]), "home": int(lk["home"])} for lk in locks]
        rows = []
        for i, lk in enumerate(locks):
            t = lk.get("t_ms")
            if not isinstance(t, int) or isinstance(t, bool):
                continue
            targets = [(t + off - wmm) for off in offsets_ms]
            seqs = [seq_at(m) for m in targets]
            if not any(s is not None for s in seqs):
                continue
            tol = [scores[j] for j in (i - 1, i + 1) if 0 <= j < len(scores) and scores[j] != scores[i]]
            rows.append((i, targets, seqs, tol))
        for i, targets, seqs, tol in rows[-max_locks:]:
            if late():
                return out
            frames = [(_grab(b, rd, s, m) if s is not None else None) for m, s in zip(targets, seqs)]
            out.append({"id": f"lock{i:02d}-a{scores[i]['away']}-h{scores[i]['home']}", "expected": scores[i],
                        "frames": frames, "tolerate": tol})
        res = receipt.get("result") or {}
        exp = {"away": res.get("away_score"), "home": res.get("home_score")}
        segs = b.segments
        by_seq = {int(m["seq"]): m for m in segs}
        if segs and not late():
            last_file, end_mono = segs[-1]["file"], int(segs[-1]["mono_ms"])
            lo = end_mono - tail_ms
            last_lock = max((lk["t_ms"] - wmm for lk in locks if isinstance(lk.get("t_ms"), int)), default=None)
            if last_lock is not None:
                lo = max(lo, last_lock + 3_000)
            tail = sorted(s for s in have if s in media and s in ranges and by_seq[s]["file"] == last_file
                          and ranges[s][0] >= lo)
            frames = []
            if tail:
                picks = sorted({tail[min(len(tail) - 1, (k * len(tail)) // (final_frames + 1))]
                                for k in range(1, final_frames + 1)})
                for s in picks:
                    if late():
                        break
                    a, z = ranges[s]
                    frames.append(_grab(b, rd, s, (a + z) // 2, tol_ms=max(600, z - a)))
            tol = [sc for sc in scores[-1:] if sc != exp]
            out.append({"id": "final", "expected": exp, "frames": frames, "tolerate": tol})
    except Exception:  # noqa: BLE001  advisory layer: no frames means "Muse unchecked"
        return out
    return out


# ------------------------------------------------------------------ one job
def run_job(receipt: dict[str, Any], verify_result: dict[str, Any], locks: list[dict[str, Any]], *, signer, store: Store,
            ledger: Ledger, caps: Caps, explain_fn: Callable | None = None, crosscheck_fn: Callable | None = None,
            deadline: float | None = None, publish: bool = True, kind: str = "auto") -> dict[str, Any]:
    """Explain + cross-check one final receipt. Returns a status dict. Never raises. Inputs are deep-copied."""
    from qor_receipt.ai_ruling.signing import sign_ruling

    receipt = copy.deepcopy(receipt)
    verify_result = copy.deepcopy(verify_result)
    sha = receipt_hash(receipt)
    st: dict[str, Any] = {"receipt_jcs_sha256": sha, "kind": kind, "files": [], "label": "HACKATHON advisory only",
                          "verdict_echo": verify_result.get("status")}

    cost = [0]

    def finish(reason: str) -> dict[str, Any]:
        st["reason"] = reason
        st["cost_micro_usd"] = cost[0]
        st["done_ms"] = int(time.time() * 1000)
        if publish:
            try:
                store.write(sha, "status.json", st)
            except Exception:  # noqa: BLE001
                pass
        return st

    def late() -> bool:
        return deadline is not None and time.monotonic() > deadline

    if not caps.enabled:
        return finish("disabled")
    if signer is None:
        return finish("advisory_key_missing")
    reserve = min(caps.match_micro, EXPLAIN_EST_MICRO + caps.max_reads() * READ_EST_MICRO)
    if reserve < EXPLAIN_EST_MICRO:
        return finish("match_cap_too_low")
    if not ledger.reserve(reserve, caps.weekly_micro):
        return finish("weekly_cap")
    try:
        if explain_fn is None:
            from qor_receipt.ai_ruling.explainer import explain as explain_fn
        if crosscheck_fn is None:
            from qor_receipt.ai_ruling.score_crosscheck import crosscheck as crosscheck_fn
        er = explain_fn(receipt, verify_result)
        usage = (getattr(er, "sidecar", None) or {}).get("usage") or {}
        cost[0] += _usage_micro(usage) if er.ok else 0
        st["explain"] = "ok" if er.ok else f"none:{er.reason}"
        if er.ok and not late():
            doc = sign_ruling(er.sidecar, signer)
            if publish:
                store.write(sha, "ai_ruling.json", doc)
                st["files"].append("ai_ruling.json")
            else:
                st["preview_ai_ruling"] = doc
        if late():
            return finish("timeout")
        mr = caps.max_reads()
        if locks and mr > 0:
            xc = crosscheck_fn(receipt, locks, max_reads=mr)
            cost[0] += int(xc.get("cost_micro_usd") or 0)
            st["crosscheck"] = {"checked": xc.get("checked"), "agree": xc.get("agree"), "disagree": xc.get("disagree"),
                                "reads": xc.get("reads")}
            if int(xc.get("checked") or 0) > 0 and not late() and publish:
                store.write(sha, "score_crosscheck.json", sign_ruling(xc, signer))
                st["files"].append("score_crosscheck.json")
            elif late():
                return finish("timeout")
        else:
            st["crosscheck"] = {"checked": 0, "reason": "no_frames" if not locks else "no_read_budget"}
        return finish("ok")
    except Exception as e:  # noqa: BLE001  never take anything down
        return finish(f"error:{type(e).__name__}")
    finally:
        ledger.settle(reserve, cost[0], sha, kind)


def _usage_micro(usage: dict[str, Any]) -> int:
    from qor_receipt.ai_ruling.score_crosscheck import _usd_micro

    try:
        return int(_usd_micro(usage))
    except Exception:  # noqa: BLE001
        return EXPLAIN_EST_MICRO


def signer_from_env(env: dict[str, str] | None = None):
    """Advisory signer from QORTIME_AI_ADVISORY_SEED, or None. Refuses a referee key (sign_ruling also refuses)."""
    from qor_receipt.keys import SoftwareKey
    from qor_receipt.trusted_referees import lookup

    env = os.environ if env is None else env
    seed = (env.get(ENV_SEED) or "").strip()
    if not seed:
        return None
    try:
        k = SoftwareKey.from_seed_hex(seed)
    except Exception:  # noqa: BLE001
        return None
    return None if lookup(k.pubkey_hex) is not None else k


# ------------------------------------------------------------------ background worker wired into the referee
class AutoAdvisor:
    """One daemon worker, small bounded queue. ``hook(sub)`` is called by Referee.add_player_signature after the
    receipt is final; it returns True only when it took ownership of deleting the submission's segments."""

    def __init__(self, referee, state_dir: str | Path, *, env: dict[str, str] | None = None, signer=None,
                 explain_fn=None, crosscheck_fn=None, locks_fn=None, maxsize: int = 4) -> None:
        self.ref = referee
        self.env = os.environ if env is None else env
        self.caps = Caps(self.env)
        self.signer = signer if signer is not None else signer_from_env(self.env)
        root = Path(state_dir) / "advisory"
        self.store = Store(root)
        self.ledger = Ledger(root / "spend.jsonl")
        self.explain_fn, self.crosscheck_fn = explain_fn, crosscheck_fn
        self.locks_fn = locks_fn or locks_from_bundle
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.last: dict[str, Any] | None = None
        self._t: threading.Thread | None = None

    def public(self) -> dict[str, Any]:
        return {**self.caps.public(), "advisory_key_id": getattr(self.signer, "key_id", None),
                "queue": self.q.qsize(), "week_spent_usd": self.ledger.week_spent() / 1e6,
                "label": "HACKATHON: advisory only, never changes a verdict"}

    def hook(self, sub) -> bool:
        if not self.caps.enabled or self.signer is None or sub.receipt is None:
            return False
        try:
            job = {"receipt": copy.deepcopy(sub.receipt), "verify": copy.deepcopy(sub.verify or {}), "sub": sub}
            self.q.put_nowait(job)
        except queue.Full:
            return False
        except Exception:  # noqa: BLE001
            return False
        self._ensure_thread()
        return True

    def _ensure_thread(self) -> None:
        if self._t is None or not self._t.is_alive():
            self._t = threading.Thread(target=self._loop, name="qortime-ai-advisory", daemon=True)
            self._t.start()

    def _loop(self) -> None:
        while True:
            job = self.q.get()
            try:
                self.process(job)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.q.task_done()

    def process(self, job: dict[str, Any]) -> dict[str, Any]:
        sub = job["sub"]
        deadline = time.monotonic() + self.caps.timeout_s
        locks: list[dict[str, Any]] = []
        try:
            sha = receipt_hash(job["receipt"])
            prev = self.store.status(sha)
            if prev and prev.get("reason") == "ok":
                self.last = prev
                return prev  # idempotent: already done for this exact receipt, no new spend
            try:
                b = sub.bundle
                have = set(int(k) for k in (sub.received or {}).keys())
                locks = self.locks_fn(b, self.ref._read_seg(sub), have, job["receipt"], wmm=self.ref._wall_minus_mono(b),
                                      tail_ms=self.ref.tail_ms, deadline=deadline)
            except Exception:  # noqa: BLE001
                locks = []
        finally:
            try:
                self.ref.cleanup_segments(sub)
            except Exception:  # noqa: BLE001
                pass
        self.last = run_job(job["receipt"], job["verify"], locks, signer=self.signer, store=self.store, ledger=self.ledger,
                            caps=self.caps, explain_fn=self.explain_fn, crosscheck_fn=self.crosscheck_fn,
                            deadline=deadline)
        return self.last

    def dry_run(self, receipt: dict[str, Any], verify_result: dict[str, Any], *, publish: bool) -> dict[str, Any]:
        """Explanation only (no video on this path). publish=False writes nothing (ledger still records spend).
        Idempotent: a receipt that already has an ok status is returned as-is with no new call."""
        sha = receipt_hash(receipt)
        prev = self.store.status(sha)
        if prev and prev.get("reason") == "ok" and "ai_ruling.json" in (prev.get("files") or []):
            return {**prev, "idempotent": True}
        deadline = time.monotonic() + self.caps.timeout_s
        return run_job(receipt, verify_result, [], signer=self.signer, store=self.store, ledger=self.ledger, caps=self.caps,
                       explain_fn=self.explain_fn, crosscheck_fn=self.crosscheck_fn, deadline=deadline, publish=publish,
                       kind="dry-run" if not publish else "reprocess")
