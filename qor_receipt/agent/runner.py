"""Agent runner: one core, two drivers (live hardware / offline replay).

Core flow (per role):
  start match with referee -> kickoff challenge -> rolling checkpoints every ``cp_interval_ms`` of
  (frame hashes, HID events) -> halftime challenge when agreed scorebug reaches Q3 -> end challenge
  when ``final_detect`` says FINAL -> final checkpoint, timeline + motion bins, finalize, player
  co-sign -> receipt JSON.
Challenges: the prompt shows OPTIONS + 4 buttons; the agent submits its HID button edges in the
challenge window plus ``pause_seen_ms`` (pause-menu onset from ``pause_detect``) and the referee
checks the 0..600 ms latency.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from qor_receipt.challenge import PAUSE
from qor_receipt.checkpoints import Chain, hid_events_hash, window_hash
from qor_receipt.final_detect import detect_final
from qor_receipt.jcs import canonicalize, sha256_hex
from qor_receipt.receipt import signing_bytes

from .capture import Frame
from .pause_detect import PauseDetector, PauseThresholds

# reader refusals that mean "no scorebug strip on screen" (vs. a strip that is there but unreadable)
NOT_VISIBLE = frozenset({"layout_unknown"})


# ---------------------------------------------------------------- clocks
class AgentClock:
    """Epoch-ms timestamps derived from a monotonic counter (immune to wall-clock steps mid-match)."""

    def __init__(self) -> None:
        self._wall0 = time.time_ns()
        self._mono0 = time.perf_counter_ns()

    def now_ms(self) -> int:
        return (self._wall0 + (time.perf_counter_ns() - self._mono0)) // 1_000_000


class VirtualClock:
    def __init__(self, t: int = 0) -> None:
        self.t = t

    def now_ms(self) -> int:
        return self.t

    def __call__(self) -> int:
        return self.t


# ---------------------------------------------------------------- prompt
class Prompt:
    """Console always; optional OBS text file and a small always-on-top tk window."""

    def __init__(self, obs_text: str | None = None, window: bool = False, quiet: bool = False) -> None:
        self.obs = Path(obs_text) if obs_text else None
        self.quiet = quiet
        self.root = self.label = None
        if window:
            try:
                import tkinter as tk

                self.root = tk.Tk()
                self.root.title("QorTime")
                self.root.attributes("-topmost", True)
                self.label = tk.Label(self.root, text="", font=("Consolas", 22, "bold"), padx=16, pady=12)
                self.label.pack()
            except Exception as e:  # headless / no tk
                print(f"[qor] tk window unavailable ({e}); console only", file=sys.stderr)
                self.root = None

    def show(self, text: str) -> None:
        if not self.quiet:
            print(f"[qor] {text}", flush=True)
        if self.obs:
            self.obs.write_text(text, encoding="utf-8")
        if self.label is not None:
            self.label.config(text=text)

    def pump(self) -> None:
        if self.root is not None:
            try:
                self.root.update()
            except Exception:
                self.root = None


# ---------------------------------------------------------------- referee
class RefereeError(RuntimeError):
    pass


@dataclass
class AgentConfig:
    role: str = "home"
    player_id: str = "local:player"
    cp_interval_ms: int = 4_000
    kickoff_delay_ms: int = 1_000
    scorebug_interval_ms: int = 200     # ~5 reads/s (Qoresence sampler cadence)
    challenge_settle_ms: int = 800       # wait after the last button so the pause onset is captured
    bin_ms: int = 1_000
    pause: PauseThresholds | None = None
    health_interval_ms: int = 30_000     # periodic scorebug read-health line on the prompt


class AgentCore:
    def __init__(self, client, key, cfg: AgentConfig, prompt: Prompt | None = None,
                 session_log: Callable[[dict], None] | None = None, scorebug=None) -> None:
        self.client, self.key, self.cfg = client, key, cfg
        self.prompt = prompt or Prompt(quiet=True)
        self.log = session_log or (lambda rec: None)
        self.pause = PauseDetector(cfg.pause)
        self.chain = Chain()
        self.mid: str | None = None
        self.referee_pub: str | None = None
        self.start_ms = 0
        self.frames_win: list[str] = []
        self.hid_win: list[dict] = []
        self.buttons: list[dict] = []
        self.last_cp_ms = 0
        self.challenges: dict[str, dict] = {}   # phase -> issued challenge (+ "result")
        self.pending: dict | None = None
        self.reads: list[dict] = []
        self.timeline: list[dict] = []
        self.final = None
        self.receipt: dict | None = None
        self.done = False
        self._scorebug = scorebug
        self._agree = None
        self._last_sb_ms = -10**12
        self._prev_thumb = None
        self.motion_bins: dict[int, list[float]] = {}
        self.energy_bins: dict[int, list[float]] = {}
        self.sb_reasons: dict[str, int] = {}
        self._last_health_ms: int | None = None
        self._last_agreed_key: tuple | None = None
        self._absent: dict | None = None   # open "scorebug_absent" obs span (lives inside self.timeline)
        self._screen: dict | None = None   # open "final_screen" obs span
        self.final_screen_detector: Callable[[Any], bool] | None = None  # none shipped yet (no real frames)
        # Observation-only hook (e.g. Jev shadow detector): called with (image, t_ms) while the scorebug is
        # absent; its return value is ignored, so it can never open a final_screen span or set the basis.
        self.final_screen_shadow: Callable[[Any, int], Any] | None = None
        self.stats = {"frames": 0, "size_resets": 0, "hid_events": 0, "checkpoints": 0, "scorebug_reads": 0, "scorebug_ok": 0,
                      "scorebug_agreed": 0, "scorebug_errors": 0, "scorebug_skipped_paused": 0}

    # ---- referee plumbing
    def _post(self, path: str, body: dict) -> dict:
        r = self.client.post(path, json=body)
        if r.status_code != 200:
            raise RefereeError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _signed(self, payload: dict) -> dict:
        return {"role": self.cfg.role, "payload": payload, "sig": self.key.sign(canonicalize(payload))}

    # ---- lifecycle
    def start(self, t_ms: int, match_id: str | None = None) -> str:
        if match_id:
            self.mid = match_id
            self.referee_pub = self.client.get("/referee").json()["pubkey"]
        else:
            r = self._post("/matches", {"players": [{"role": self.cfg.role, "player_id": self.cfg.player_id,
                                                     "pubkey": self.key.pubkey_hex, "key_kind": self.key.kind}]})
            self.mid, self.referee_pub = r["match_id"], r["referee_pubkey"]
        self.start_ms = self.last_cp_ms = t_ms
        self.log({"session": {"match_id": self.mid, "role": self.cfg.role, "start_ms": t_ms, "referee_pub": self.referee_pub}})
        self.prompt.show(f"match {self.mid} started ({self.cfg.role})")
        return self.mid

    # ---- inputs
    def on_hid(self, events: Iterable[dict]) -> None:
        for e in events:
            self.stats["hid_events"] += 1
            self.hid_win.append(e)
            self.log(e)
            if "button" in e:
                self.buttons.append(e)
            elif "sticks" in e:
                b = (e["t_ms"] - self.start_ms) // self.cfg.bin_ms
                self.energy_bins.setdefault(b, []).append(sum(abs(v - 128) for v in e["sticks"]))

    def on_frame(self, f: Frame) -> None:
        import cv2
        import numpy as np

        self.stats["frames"] += 1
        if f.reset:
            self.stats["size_resets"] += 1
            self._prev_thumb = None
            if self._agree is not None:
                self._agree.reset()
        self.frames_win.append(f.sha256)
        self.pause.update(f.t_ms, f.image, reset=f.reset)
        thumb = cv2.resize(cv2.cvtColor(f.image, cv2.COLOR_BGR2GRAY), (160, 90), interpolation=cv2.INTER_AREA).astype(np.int16)
        if self._prev_thumb is not None:
            b = (f.t_ms - self.start_ms) // self.cfg.bin_ms
            self.motion_bins.setdefault(b, []).append(float(np.abs(thumb - self._prev_thumb).mean()))
        self._prev_thumb = thumb
        if f.t_ms - self._last_sb_ms >= self.cfg.scorebug_interval_ms:
            self._last_sb_ms = f.t_ms
            if self.pause.paused:
                self.stats["scorebug_skipped_paused"] += 1
                self._absent = self._screen = None   # a pause menu breaks any absence / final-screen span
            else:
                self._read_scorebug(f)
        self._health(f.t_ms)

    def health_line(self) -> str:
        st = self.stats
        top = sorted(self.sb_reasons.items(), key=lambda kv: -kv[1])[:4]
        refused = ", ".join(f"{k}={v}" for k, v in top) or "none"
        return (f"scorebug health: reads={st['scorebug_reads']} accepted={st['scorebug_ok']} "
                f"agreed={st['scorebug_agreed']} errors={st['scorebug_errors']} "
                f"skipped_paused={st['scorebug_skipped_paused']} refused[{refused}] "
                f"last_agreed={self._last_agreed_key}")

    def _health(self, t_ms: int) -> None:
        if self._last_health_ms is None:
            self._last_health_ms = t_ms
        elif t_ms - self._last_health_ms >= self.cfg.health_interval_ms:
            self._last_health_ms = t_ms
            self.prompt.show(self.health_line())
            self.log({"scorebug_health": {"t_ms": t_ms, **{k: v for k, v in self.stats.items() if k.startswith("scorebug")},
                                          "refused": dict(self.sb_reasons)}})

    def _read_scorebug(self, f: Frame) -> None:
        if self._scorebug is None:
            from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
            from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader

            self._scorebug, self._agree = ScorebugReader(), AgreementWindow()
        elif self._agree is None:
            from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow

            self._agree = AgreementWindow()
        self.stats["scorebug_reads"] += 1
        try:
            fr = self._scorebug.read(f.image)
            band, _ = self._scorebug.reference_band(f.image)
        except Exception as e:  # never silent: count it, say it (first few), keep capturing
            self.stats["scorebug_errors"] += 1
            self.sb_reasons["exception"] = self.sb_reasons.get("exception", 0) + 1
            if self.stats["scorebug_errors"] <= 3:
                self.prompt.show(f"scorebug reader error: {type(e).__name__}: {e}")
            return
        ch = hashlib.sha256(band.tobytes()).hexdigest()[:16] if band is not None else ""
        if fr.ok:
            self.stats["scorebug_ok"] += 1
        else:
            self.sb_reasons[fr.reason] = self.sb_reasons.get(fr.reason, 0) + 1
        self._observe(f, absent=(not fr.ok and fr.reason in NOT_VISIBLE))
        res = self._agree.offer(fr, clock_ns=f.t_ms * 1_000_000, crop_hash=ch, session_id=self.mid or "m")
        if not (res.agreed and res.read is not None):
            return
        rd = {"t_ms": f.t_ms, "away": res.read.left_score, "home": res.read.right_score,  # Madden: away team on the LEFT
              "quarter": res.read.quarter, "clock_s": res.read.clock_seconds,
              "clock_red": bool(getattr(res.read, "clock_red", False))}
        self.stats["scorebug_agreed"] += 1
        akey = (rd["home"], rd["away"], rd["quarter"])
        if akey != self._last_agreed_key:
            self._last_agreed_key = akey
            clk = res.read.clock_text or "--:--"
            self.prompt.show(f"scorebug agreed: {rd['away']}-{rd['home']} (away-home) Q{rd['quarter']} {clk} (run {res.run})")
            self.log({"scorebug_agreed": rd})
        prev = next((r for r in reversed(self.timeline) if "obs" not in r), None)
        key = lambda r: (r["home"], r["away"], r["quarter"], r["clock_s"])  # noqa: E731
        if prev is None or key(prev) != key(rd) or rd["t_ms"] - prev["t_ms"] >= 1_000:
            self.timeline.append(rd)
        self.reads.append(rd)
        self._absent = self._screen = None  # a fresh score read ends any absence span
        if rd["quarter"] >= 3 and "halftime" not in self.challenges:
            self._issue("halftime", f.t_ms)
        self._check_final(f.t_ms)

    def _observe(self, f: Frame, *, absent: bool) -> None:
        """Track continuous scorebug-absent / final-screen spans as timeline obs entries."""
        def span(cur, name, on):
            if not on:
                return None
            if cur is None:
                cur = {"t_ms": f.t_ms, "until_ms": f.t_ms, "obs": name, "n": 0}
                self.timeline.append(cur)
            cur["until_ms"], cur["n"] = f.t_ms, cur["n"] + 1
            return cur

        self._absent = span(self._absent, "scorebug_absent", absent)
        screen = bool(absent and self.final_screen_detector and self.final_screen_detector(f.image))
        self._screen = span(self._screen, "final_screen", screen)
        if absent and self.final_screen_shadow is not None:
            try:
                self.final_screen_shadow(f.image, f.t_ms)
            except Exception:  # noqa: BLE001 -- a shadow observer must never disturb capture
                pass
        if absent:
            self._check_final(f.t_ms)

    def _check_final(self, t_ms: int) -> None:
        if self.final is not None:
            return
        fin = detect_final(self.timeline)
        if fin.final:
            self.final = fin
            self.log({"final": {"home": fin.home, "away": fin.away, "quarter": fin.quarter, "t_ms": fin.t_ms,
                                "final_basis": fin.basis}})
            self.prompt.show(f"FINAL detected {fin.away}-{fin.home} (away-home, Q{fin.quarter}, basis {fin.basis})")
            self._issue("end", t_ms)

    # ---- challenges
    def _issue(self, phase: str, t_ms: int) -> None:
        if phase in self.challenges:
            return
        c = self._post(f"/matches/{self.mid}/challenges", {"phase": phase, "role": self.cfg.role})
        self.challenges[phase] = c
        self.log({"challenge": {"phase": phase, "role": self.cfg.role, "nonce": c["nonce"], "id": c["id"],
                                "issued_ms": c["issued_ms"]}})
        if self.pending is None:
            self._activate(c)

    def _activate(self, c: dict) -> None:
        self.pending = c
        self.prompt.show(f"CHALLENGE ({c['phase']}): from gameplay press  " + "  ".join(c["pattern"]))

    def _check_pending(self, t: int) -> None:
        c = self.pending
        if c is None:
            return
        win = [e for e in self.buttons if c["issued_ms"] <= e["t_ms"] <= c["deadline_ms"]]
        downs = [e for e in win if e["down"]]
        i = next((k for k, e in enumerate(downs) if e["button"] == PAUSE), None)
        complete = i is not None and len(downs) - i >= len(c["pattern"])
        if complete:
            done = downs[i + len(c["pattern"]) - 1]["t_ms"]
            if t < done + self.cfg.challenge_settle_ms:
                return
        elif t <= c["deadline_ms"] + 500:
            return
        press = downs[i]["t_ms"] if i is not None else None
        seen = self.pause.onset_after(press) if press is not None else None
        res = self._post(f"/matches/{self.mid}/challenges/{c['id']}/response",
                         self._signed({"hid_events": win, "pause_seen_ms": seen}))
        c["result"] = res
        self.log({"challenge_result": {"phase": c["phase"], **res}})
        lat = f" latency {res.get('latency_ms')} ms" if res.get("latency_ms") is not None else ""
        self.prompt.show(f"challenge {c['phase']}: {'OK' if res['ok'] else 'FAILED ' + res['reason']}{lat}")
        self.pending = next((x for x in self.challenges.values() if "result" not in x and x is not c), None)
        if self.pending:
            self._activate(self.pending)

    # ---- clock-driven work
    def _checkpoint(self, t: int) -> None:
        fh = window_hash(bytes.fromhex(h) for h in self.frames_win)
        ih = hid_events_hash(self.hid_win)
        cp = self.chain.next(t, fh, ih)
        self._post(f"/matches/{self.mid}/checkpoints", self._signed(cp))
        self.log({"checkpoint": {"seq": cp["seq"], "t_ms": t, "frames": len(self.frames_win), "hid": len(self.hid_win)}})
        self.frames_win, self.hid_win = [], []
        self.last_cp_ms = t
        self.stats["checkpoints"] += 1

    def tick(self, t: int) -> None:
        if self.done or self.mid is None:
            return
        if "kickoff" not in self.challenges and t - self.start_ms >= self.cfg.kickoff_delay_ms:
            self._issue("kickoff", t)
        if t - self.last_cp_ms >= self.cfg.cp_interval_ms:
            self._checkpoint(t)
        self._check_pending(t)
        end = self.challenges.get("end")
        if self.final is not None and end is not None and "result" in end and self.pending is None:
            self.finish(t)

    def _bins(self) -> tuple[list[int], list[int]]:
        n = max([*self.motion_bins, *self.energy_bins, 0]) + 1
        energy = [int(round(sum(v) / len(v))) if (v := self.energy_bins.get(i)) else 0 for i in range(n)]
        motion = [int(round(10 * sum(v) / len(v))) if (v := self.motion_bins.get(i)) else 0 for i in range(n)]
        return energy, motion

    def finish(self, t: int) -> dict:
        self._checkpoint(t)
        energy, motion = self._bins()
        self._post(f"/matches/{self.mid}/timeline", self._signed({"reads": self.timeline, "input_energy": energy,
                                                                  "video_motion": motion}))
        rec = self._post(f"/matches/{self.mid}/finalize", {})
        body_hash = sha256_hex({k: v for k, v in rec.items() if k != "signatures"})
        rec = self._post(f"/matches/{self.mid}/sign", self._signed({"receipt_sha256": body_hash,
                                                                    "sig": self.key.sign(signing_bytes(rec))}))
        self.receipt, self.done = rec, True
        self.prompt.show(f"receipt finalized: away {rec['result']['away_score']} @ home {rec['result']['home_score']}")
        return rec


# ---------------------------------------------------------------- session log
class SessionLog:
    def __init__(self, path: Path) -> None:
        import threading

        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()  # the Jev shadow worker logs from its own thread

    def __call__(self, rec: dict) -> None:
        with self._lock:
            if not self.fh.closed:
                self.fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def close(self) -> None:
        with self._lock:
            self.fh.close()


def load_hid_log(path: str | Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------- replay driver
def run_replay(frames_dir: str | Path, hid_log: str | Path, *, cfg: AgentConfig | None = None, key=None,
               quiet: bool = True, tail_ms: int = 25_000) -> dict[str, Any]:
    """Whole pipeline, no hardware: recorded frames + HID log -> in-process referee (virtual clock) -> receipt.

    Challenge nonces come from the log's ``challenge`` records so a recorded session's button
    patterns match; this proves the pipeline, not freshness (a live referee issues random nonces).
    """
    from fastapi.testclient import TestClient

    from qor_receipt.challenge import menu_safe_nonce
    from qor_receipt.keys import SoftwareKey
    from qor_receipt.receipt import verify_receipt
    from qor_receipt.referee.app import create_app

    from .capture import replay_frames
    from .hid import HidDecoder

    cfg = cfg or AgentConfig()
    recs = load_hid_log(hid_log)
    nonces = {(c["phase"], c.get("role", cfg.role)): c["nonce"] for r in recs if (c := r.get("challenge"))}
    vclock = VirtualClock()
    ref_key = SoftwareKey()
    app = create_app(ref_key, vclock, nonce_fn=lambda ph, ro: nonces.get((ph, ro)) or menu_safe_nonce())
    core = AgentCore(TestClient(app), key or SoftwareKey(), cfg, Prompt(quiet=quiet))
    dec = HidDecoder()
    hid = sorted(((r["t_ms"], i, r) for i, r in enumerate(recs) if "t_ms" in r), key=lambda x: (x[0], x[1]))
    frames = replay_frames(frames_dir)
    hi = 0
    t = 0
    for f in frames:
        if core.mid is None:
            vclock.t = f.t_ms
            core.start(f.t_ms)
        while hi < len(hid) and hid[hi][0] <= f.t_ms:
            t, _, r = hid[hi]
            hi += 1
            vclock.t = max(vclock.t, t)
            core.on_hid(dec.feed(bytes.fromhex(r["report"]), t) if "report" in r else [r])
            core.tick(vclock.t)
        vclock.t = max(vclock.t, f.t_ms)
        core.on_frame(f)
        core.tick(vclock.t)
        if core.done:
            break
    t = vclock.t
    while not core.done and vclock.t - t < tail_ms:  # let a pending end challenge time out
        vclock.t += 200
        core.tick(vclock.t)
    if core.receipt is None:
        raise RuntimeError(f"replay ended without a receipt (final={core.final}, challenges="
                           f"{ {k: v.get('result') for k, v in core.challenges.items()} }, stats={core.stats})")
    return {"receipt": core.receipt, "referee_pub": ref_key.pubkey_hex, "stats": core.stats,
            "hid_decoder": {"reports_in": dec.reports_in, "events_out": dec.events_out},
            "verify": verify_receipt(core.receipt, referee_pub=ref_key.pubkey_hex)}


# ---------------------------------------------------------------- live driver
def run_live(*, device: str, referee: str, cfg: AgentConfig, out: Path, key, match_id: str | None = None,
             obs_text: str | None = None, window: bool = False, record_dir: Path | None = None) -> dict | None:
    import cv2
    import httpx

    from qor_receipt.receipt import verify_receipt

    from .capture import DShowCapture
    from .hid import DualSenseReader

    clock = AgentClock()
    prompt = Prompt(obs_text, window)
    session_dir = out.parent / f"qor-session-{time.strftime('%Y%m%d-%H%M%S')}"
    log = SessionLog(session_dir / "hid.jsonl")
    frames_dir = record_dir or (session_dir / "frames")
    frames_dir.mkdir(parents=True, exist_ok=True)
    cap = DShowCapture(device, clock)
    cap.open()
    hid = DualSenseReader(clock)
    hid.start()
    prompt.show(f"capture {device!r} + {hid.device_name} on USB; referee {referee}")
    client = httpx.Client(base_url=referee.rstrip("/"), timeout=10.0)
    core = AgentCore(client, key, cfg, prompt, log)
    from .final_screen_jev import from_env as jev_from_env

    jev = jev_from_env(log, last_clock=lambda: core.reads[-1] if core.reads else None)  # QORTIME_JEV_FINAL_SCREEN=shadow|off; shadow only with TYPESAFE_API_KEY
    if jev is not None:
        core.final_screen_shadow = jev.submit
        prompt.show(f"Jev final-screen detector: shadow mode ({jev.model}), log only")
    core.start(clock.now_ms(), match_id)
    last_saved = 0
    try:
        while not core.done:
            f = cap.read()
            core.on_hid(hid.drain())
            if hid.error:
                raise RuntimeError(f"HID reader stopped: {hid.error}")
            if f is not None:
                core.on_frame(f)
                # Recording for replay/tuning: every frame around challenges, ~5/s otherwise.
                if core.pending is not None or core.pause.paused or f.t_ms - last_saved >= 200:
                    cv2.imwrite(str(frames_dir / f"{f.t_ms}.jpg"), f.image, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    last_saved = f.t_ms
            core.tick(clock.now_ms())
            prompt.pump()
    except KeyboardInterrupt:
        prompt.show("stopped by user (no receipt)")
        return None
    finally:
        hid.stop()
        cap.close()
        if jev is not None:
            jev.close()
        log.close()
    out.write_text(json.dumps(core.receipt, indent=2), encoding="utf-8")
    v = verify_receipt(core.receipt, referee_pub=core.referee_pub)
    prompt.show(f"wrote {out}: {v['status']} {v['trust_tier']} {v['errors'] or ''} {v['partial'] or ''}")
    return {"receipt": core.receipt, "verify": v, "session_dir": str(session_dir)}
