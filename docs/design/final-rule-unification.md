# Final-rule unification (design only)

Status: proposal. No behaviour change in this change. `schema/**` is untouched.

Tracks [qorcap#94](https://github.com/ConWan30/qorcap/issues/94). Match `match-20261024-135645` (client v0.3.32, production submission `f26ac3935d038288`).

Sources quoted below:

| Side | Tree |
|---|---|
| QorTime referee | this repo, `393045eb14be280fb15445a5af9f55320de9c4e1` |
| QorCap client | `ConWan30/qorcap` @ `2cb66ec0b549c61814d9484be62c1faf0c640a42` (main, "Keep capturing for a short tail after the first end screen") |

The stall logger in qorcap#85 (`46d87e5`, "Keep the score reader at 4 Hz and log a stall without minting a score") does not decide a final. It does not treat unread time as clock expiry. `135645`'s last locked read is still Q4 0:11.

## What 135645 did

The client's `final.tail` rejected the end-of-game menu: `scorebug clock not 0:00 before the end-of-game menu`. The live reader had stalled at Q4 0:11.

The referee's end-menu fallback accepted 10-7 at confidence 0.912 over 485 frames, checked against the last locked read.

Those are two gates on the same pictures. The strings are not synonyms. The fallback runs only when the rejection reason is exactly `no Q4 scorebug read before the end-of-game menu`. A clock rejection is refused by the fallback. The referee accepted because its re-read never saw the Q4 0:11 lock, so the client reader (running unseeded on the referee) emitted the other reason, and the fallback then compared the menu to the bundle locks and ignored their clock.

## 1. QorTime referee

### 1.1 End-menu fallback

`qor_receipt/review/media.py`. This is the rule that accepted 135645.

Thresholds and the only reason the fallback will consider:

```204:208:qor_receipt/review/media.py
# ---------------------------------------------------------------- end-of-game menu without a tail scorebug read
END_MENU_FALLBACK_REASON = "no Q4 scorebug read before the end-of-game menu"
END_MENU_FALLBACK_MIN_CONF = 0.90
END_MENU_FALLBACK_MIN_FRAMES = 2
LEGAL_STEPS = frozenset({1, 2, 3, 6, 7, 8})
```

The contract in the docstring, including what counts as confirmed:

```236:249:qor_receipt/review/media.py
def end_menu_fallback(rejected: dict, tail_reads: list[dict] | None, timeline_locks: list[dict] | None
                      ) -> tuple[dict | None, str]:
    """Accept an end-of-game menu that QorCap's ReaderCore rejected ONLY because no Q4 scorebug was read in the tail.

    Fail closed. Every one of these must hold, otherwise ``(None, reason)``:
      * the rejection reason is exactly "no Q4 scorebug read before the end-of-game menu" (a clock, illegal-step,
        tie or any other rejection stays refused);
      * the menu read is real-template (not a partly covered "corroborated" panel), confidence >= 0.90 over >= 2
        agreeing frames, not tied;
      * there is at least one locked read to compare with (the referee's own tail reads first, else the bundle's
        committed timeline locks); none of them is above the menu score on either side (scores never go down);
      * the menu is reachable from the last locked read (each side's increase is 0 or made of legal plays).
    ``final_confirmed`` is true only when the menu equals the last locked read, or is one legal play past a last
    locked read in Q4/OT. Anything else is posted unconfirmed, which the referee reports as PARTIAL, never PASS."""
```

The implementation, verbatim. There is no clock read anywhere in this function. `clock_s` on a lock is ignored. A final of 1 is refused. A `+1` that is not itself the single legal step is refused. Any other forward delta, including `+4`, `+5`, `+9`, `+14`, or both sides scoring, is posted with `final_confirmed` false. The docstring's "made of legal plays" check is not what the code does. The note on lines 286-287 is the sentence issue 94 reports ("0.912 confidence over 485 frames, checked against the last locked read").

```250:284:qor_receipt/review/media.py
    if not isinstance(rejected, dict) or rejected.get("why") != END_MENU_FALLBACK_REASON:
        return None, f"end menu rejected for another reason: {(rejected or {}).get('why')}"
    away, home = rejected.get("away"), rejected.get("home")
    if not (_int(away) and _int(home)) or away < 0 or home < 0:
        return None, "end menu score missing"
    conf, frames = rejected.get("conf"), rejected.get("frames")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or conf < END_MENU_FALLBACK_MIN_CONF:
        return None, f"end menu confidence {conf} below {END_MENU_FALLBACK_MIN_CONF}"
    if not _int(frames) or frames < END_MENU_FALLBACK_MIN_FRAMES:
        return None, f"end menu agreed on {frames} frame(s), need {END_MENU_FALLBACK_MIN_FRAMES}"
    if "corroborated" in (rejected.get("template_sources") or []):
        return None, "end menu panel partly covered (corroboration only confirms a scorebug final)"
    if home == away:
        return None, "tied score on the end-of-game menu"
    if 1 in (home, away):
        return None, "a final of 1 point is not a legal football score here"
    tail = locked_reads(tail_reads)
    tl = locked_reads(timeline_locks)
    every = tail + tl
    if not every:
        return None, "no locked score read to check the end menu against"
    for r in every:
        if r["away"] > away or r["home"] > home:
            return None, (f"end menu AWAY {away} - HOME {home} conflicts with locked read AWAY {r['away']} - "
                          f"HOME {r['home']} (scores never go down)")
    ref = tail[-1] if tail else tl[-1]
    d = [away - ref["away"], home - ref["home"]]
    exact = d == [0, 0]
    one_step = min(d) == 0 and max(d) in LEGAL_STEPS
    # +1 alone is only a try after a touchdown already on the board; any other side total is a sum of 2/3/6/7/8.
    if 1 in d and not one_step:
        return None, f"end menu is not reachable from AWAY {ref['away']} - HOME {ref['home']}"
    q = ref.get("quarter")
    late = _int(q) and q >= 4
    confirmed = exact or (one_step and late)
```

`tests/test_referee_endmenu_fallback.py:79` pins the clock string as a refusal:

```python
({"why": "scorebug clock not 0:00 before the end-of-game menu"}, LOCKS_084523, "another reason"),
```

So the fallback does not override a clock rejection. It never sees one on the 135645 path, because the reader that produces `rejected["why"]` is not shown the lock.

### 1.2 How the re-read drops the clock

The re-read builds a fresh `ReaderCore` and does not seed the laptop timeline. Locks are used only after that reader rejects, and only by the fallback.

```179:197:qor_receipt/review/media.py
def reread_final(frames: Iterable[tuple[int, Any]], core_factory, *, corroboration: list[dict] | None = None,
                 timeline_locks: list[dict] | None = None) -> dict[str, Any]:
    """Feed the referee's decoded tail frames (t_ms = wall clock) through a fresh ReaderCore. No laptop timeline seed.

    ``timeline_locks`` (the bundle's committed ``timeline.jsonl`` score locks) never supply a score. They are only
    used by ``end_menu_fallback`` to REFUSE an end-of-game menu that conflicts with, or cannot follow, a locked read."""
    events: list[dict] = []
    core = core_factory(lambda e: events.append(e) or {})
    core.delta_corroboration = list(corroboration or [])
    for t, img in frames:
        core.on_frame(t, img)
        if getattr(core, "end_menu", None) is not None:
            break
    reads = [e for e in events if e.get("type") == "score"]
    rejected = getattr(core, "end_menu_rejected", None)
    final = core.final
    fallback = None
    if final is None and rejected:
        final, why = end_menu_fallback(rejected, reads, timeline_locks)
```

The review decodes the last 240 seconds at 2 Hz and passes `timeline.jsonl` in as those locks (`qor_receipt/review/policy.py:18` `TAIL_MS = 240_000`, `qor_receipt/review/engine.py:71` `TAIL_HZ = 2.0`, call at `engine.py:429-430`).

On 135645 the tail pictures are the menu. The unseeded reader has no Q4 row, so `rejected["why"]` is `no Q4 scorebug read before the end-of-game menu`. The fallback then accepts 0.912 / 485 frames against the last lock. The lock's `clock_s` of 11 is on the row and is not read.

### 1.3 What the review does with the result

```810:824:qor_receipt/review/engine.py
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
```

```839:844:qor_receipt/review/engine.py
    if f and f.get("basis") == "end_menu":
        res.update(method="end_menu_read", final_basis="end_menu", final_confirmed=f.get("final_confirmed") is not False)
    elif f:
        res.update(method="agreed_scorebug_final", final_basis=f.get("basis"), final_confirmed=False)
    else:
        res.update(method="last_scorebug_read_unconfirmed", final_confirmed=False)
```

A confirmed fallback is `pass`. An unconfirmed fallback is `partial`. A missing `final_confirmed` on an `end_menu` counts as confirmed (`is not False`). A scorebug basis (`clock_zero`, `scorebug_gone`, `final_screen`) is never confirmed. No read and no final is `fail` (`engine.py:803-804`). A `partial` check keeps an official receipt at PARTIAL (`qor_receipt/receipt.py:242-243`). A `fail` check is FAIL.

### 1.4 `check_final` (integrity, not the fallback)

`qor_receipt/referee_ai/integrity.py:10-13` and `:189-202`. For `end_menu_read`, the claimed score may be one legal step past the last scorebug score (`LEGAL_STEPS = {1, 2, 3, 6, 7, 8}` at line 25). Any other method must match the last scorebug score exactly. No clock. No confidence. No frame count. No corroboration. Called from `qor_receipt/referee_ai/referee.py:184` (bundle final) and `:189` (receipt result). This refuses an impossible jump. It does not confirm a final.

### 1.5 Scorebug `detect_final` (does not confirm)

`qor_receipt/final_detect.py:84-86` defaults: `need=3`, `min_span_ms=3000`, `need_red=5`, `low_clock_s=5`, `gone_ms=30000`, `gone_lead_ms=15000`, `screen_need=3`, `screen_span_ms=3000`.

`clock_zero` is quarter `>= 4`, clock `0:00`, same score on `need` reads (or `need_red` if any read is red) spanning `min_span_ms` (`final_detect.py:13-16`, `:57-64`). Ties are not final (`:96-97`). The review still sets `final_confirmed` false for every non-menu basis (`engine.py:841-842`). `clock_zero` is not a way around the menu rule.

## 2. QorCap client at `2cb66ec`

### 2.1 Menu agreement, before any final rule

`sidecar/qorcap_endmenu.py`. A digit is readable at template score `>= 0.70` and margin `>= 0.20`:

```37:38:sidecar/qorcap_endmenu.py
MIN_SCORE = 0.70   # leave-one-out (real digit vs its synthetic template): best >= 0.754
MIN_MARGIN = 0.20  # leave-one-out margin >= 0.387
```

The menu locks on 3 consecutive readable frames of the same score spanning at least 400 ms. `conf` is the stabilizing frame's minimum digit score. Later frames update `frames` and `until_ms` and do not update `conf`. 485 frames and 0.912 fit this object: 0.912 is that first stable frame, and 485 is the streak length when the gate rejected it.

```231:235:sidecar/qorcap_endmenu.py
class EndMenuAgreement:
    """Stable once ``n`` CONSECUTIVE readable end-menu frames give the same score over >= ``min_span_ms``.
    A different score or an unreadable end-menu frame restarts the streak."""

    def __init__(self, n: int = 3, min_span_ms: int = 400) -> None:
```

```251:260:sidecar/qorcap_endmenu.py
        if len(self.streak) >= self.n and self.streak[-1][0] - self.streak[0][0] >= self.min_span_ms:
            if self.stable is None or (self.stable["away"], self.stable["home"]) != (rd.away, rd.home):
                srcs = sorted({d["source"] for d in rd.digits})
                self.stable = {"away": rd.away, "home": rd.home, "t_ms": self.streak[0][0], "until_ms": t_ms,
                               "frames": len(self.streak), "conf": round(rd.conf, 3), "margin": round(rd.margin, 3),
                               "template_sources": srcs,
                               "xcheck": ("skipped" if any(d.get("xcheck") == "skipped" for d in rd.digits) else
                                         "n/a (corroboration)" if rd.reason == "corroborated" else "qortime_scorebug_pack")}
            else:
                self.stable.update(until_ms=t_ms, frames=len(self.streak))
```

There is no 0.90 gate on the client. 0.70 is enough to lock.

### 2.2 Quarter gate, clock gate, then consistency

`sidecar/qorcap_reader.py`. Red-clock window:

```48:49:sidecar/qorcap_reader.py
# Q4 end menu: the scorebug's last red clock in the final 5 seconds (110028 locked 0:03, then the menu).
END_MENU_RED_CLOCK_S = 5
```

Clock gate. OT (`quarter >= 5`) is ready with time still on the clock. A red clock counts only at `<= 5` seconds. `clock_s == 0` counts. `PeriodTrack.reached_end_of_game` counts. 0:11 matches none of these.

```1458:1479:sidecar/qorcap_reader.py
    def _clock_ready_for_end_menu(self, last: dict, t_ms: int | None = None) -> bool:
        """Q4 menu once the scorebug clock is 0:00, or already red inside the final 5 seconds.

        OT is ready whenever the quarter is (a walk-off can leave time on the clock). A red 0:14 is
        still a running quarter: match-20261017-110028's menu arrived after the last lock of 0:03 red.
        When the quarter and the clock both died at the Q1/Q2 break, a menu after three period-lengths
        of wall time (or after the fourth-quarter clock has itself run down) is the same gate.
        """
        q_last = last.get("quarter")
        # A walk-off can leave time on the clock, so OT stays clock-ready here.
        if isinstance(q_last, int) and not isinstance(q_last, bool) and q_last >= 5:
            return True
        if t_ms is not None and self.periods.reached_end_of_game(t_ms):
            return True
        if last.get("clock_s") == 0:
            return True
        rows = [r for r in self.timeline if "obs" not in r and "home" in r
                and isinstance(r.get("clock_s"), int) and not isinstance(r.get("clock_s"), bool)]
        if not rows:
            return False
        c = rows[-1]
        return bool(c.get("clock_red")) and int(c["clock_s"]) <= END_MENU_RED_CLOCK_S
```

Period track, the only non-clock way through that gate. A stall at 0:11 does not set `saw_low` (`PERIOD_END_CLOCK_S` is 5, line 31). Three completed periods plus a low clock, or the clock dying at the Q1/Q2 break and three period-lengths of wall time (`ELAPSED_PERIOD_MS` is 12 minutes, line 47):

```166:172:sidecar/qorcap_reader.py
    def reached_end_of_game(self, t_ms: int) -> bool:
        """The fourth quarter has run down, or the clock died at the Q1/Q2 break and enough time has passed."""
        if self.periods_completed >= 3 and self.saw_low:
            return True
        if self.clock_unreadable and self.periods_completed <= 1 and self.first_period_end_ms is not None:
            return int(t_ms) - self.first_period_end_ms >= 3 * ELAPSED_PERIOD_MS
        return False
```

The menu function. This is the line `final.tail` hit on 135645.

```1515:1538:sidecar/qorcap_reader.py
        if last is None or not self._quarter_ready_for_end_menu(last, t_ms):
            self._reject_end_menu(st, "no Q4 scorebug read before the end-of-game menu")
            return
        # Clock gate: the end-of-game menu is only authoritative once the scorebug has shown 0:00 in Q4.
        # Without this, a doctored end-menu-looking frame matching the current mid-Q4 score ends the match early.
        period = None
        if hasattr(self.end_menu_reader, "period"):
            try:
                period = self.end_menu_reader.period(img)
            except Exception as e:  # never silent
                print(f"end-menu period error: {type(e).__name__}: {e}", file=sys.stderr)
        # OT can end on a walk-off with time still on the clock (match-20261017-091651: 10-16 at OT 1:55).
        # A Q4 menu with the clock still running is unchanged: it is rejected.
        q_last = last.get("quarter")
        if not self._clock_ready_for_end_menu(last, t_ms):
            self._reject_end_menu(st, "scorebug clock not 0:00 before the end-of-game menu")
            return
        why = end_menu_consistency(st, last)
        if why:
            self._reject_end_menu(st, why)
            return
        if st["home"] == st["away"]:
            self._reject_end_menu(st, "tied score on the end-of-game menu")
            return
```

Consistency is exact match, or one legal step on one side. Anything else is not posted. `LEGAL_STEPS` at line 1632 is `{1, 2, 3, 6, 7, 8}`.

```1646:1657:sidecar/qorcap_reader.py
def end_menu_consistency(st: dict, last: dict | None) -> str | None:
    """None when the end-menu score equals the last scorebug read or is ONE legal scoring play past it on one side.
    A one-legal-step *delta* still needs corroboration before ``final_confirmed`` (see ``end_menu_delta_corroborated``);
    this function only rejects impossible jumps (digit-splice outside one play)."""
    if last is None:
        return None
    d = end_menu_delta(st, last)
    assert d is not None
    if d == [0, 0] or end_menu_one_legal_step(d):
        return None
    return (f"end-of-game menu AWAY {st['away']} - HOME {st['home']} is not one legal scoring play past the last "
            f"scorebug read AWAY {last['away']} - HOME {last['home']}")
```

Confirm vs post. An exact match confirms. A one-step delta confirms only with corroboration. Without it the menu is still posted, with `final_confirmed` false.

```1548:1566:sidecar/qorcap_reader.py
        corr = end_menu_delta_corroborated(
            st, last, self.timeline, self.delta_corroboration, pixel_match_ms=self._end_screen_earliest)
        confirmed = corr is not None and corr.get("kind") != "no_scorebug"
        if (sb["away"], sb["home"]) != (st["away"], st["home"]):
            notes.append(f"final play score not shown on scorebug: last scorebug read AWAY {sb['away']} - HOME {sb['home']}"
                         f", end-of-game menu AWAY {st['away']} - HOME {st['home']}")
            if confirmed and corr:
                notes.append(f"one-legal-step delta corroborated via {corr.get('kind')}")
            else:
                notes.append("one-legal-step delta NOT corroborated (no scorebug final-play read / "
                             "score-change clip / input-haptic near scoring): final_confirmed=false")
                confirmed = False
        prev = self.final
        quarter = max(4, int(q_last) if isinstance(q_last, int) else 4)
        if period == "OT" or (isinstance(q_last, int) and q_last >= 5):
            quarter = max(quarter, 5)
        self.final = {"type": "final", "basis": "end_menu", "method": "end_menu_read", "home": st["home"], "away": st["away"],
                      "quarter": quarter, "t_ms": st["t_ms"], "end_menu": st, "scorebug_last": sb,
                      "notes": notes, "final_confirmed": confirmed,
                      "delta_corroboration": corr}
```

Corroboration (`qorcap_reader.py:1660-1707`): a prior scorebug row that already equals the menu, or a `score_change_clip` with `sha256`, or `input_activity`, or `haptic`, inside the window. Evidence at or after the end screen does not count. The cutoff (`:1710-1733`) is the earliest of the first agreeing menu frame, the first pixel match, and any `final_screen` timeline row. A scorebug row still counts. A later disagreeing menu keeps the posted score and forces `final_confirmed` false (`:1576-1595`).

A covered panel (`_corroborate`, `:1597-1614`) can only echo the scorebug's own score, and only when a final is already posted and the last quarter is `>= 4`. It is not a new score. The clock gate still runs on that echoed read.

`_scorebug_for_final` (`:1426-1449`) keeps an earlier quarter when the trailing lock's quarter glyph failed, and copies that row's clock when the trailing lock has none.

### 2.3 `final.tail` (stop-time re-read)

`sidecar/qorcap_match.py`. `TAIL_SECS = 240` (`:2115`), `TAIL_HZ = 2.0` (`:2116`). The tail seeds the live timeline, then runs the same `ReaderCore` gates. It stops at the first agreeing menu. It writes `final.tail.json`. It does not have a second fallback.

```2492:2495:sidecar/qorcap_match.py
    core = (core_factory or default_core_factory)(lambda b: events.append(b) or {})
    from qorcap_reanchor_notes import append_note
    core.reanchor_note = lambda row, bundle=bundle: append_note(bundle, row)
    core.timeline = [dict(r) for r in live]
```

```2515:2526:sidecar/qorcap_match.py
    for vms, img in pts:
        t = end_ms - span + vms
        if t <= last_t:
            continue
        core.on_frame(t, img)
        if getattr(core, "end_menu", None) is not None:
            break
    fin = core.final
    out = {"final": fin, "end_menu": getattr(core, "end_menu", None), "end_menu_rejected": getattr(core, "end_menu_rejected", None),
           "frames": core.stats, "window_secs": secs, "video": vids[-1].name,
           "method": "tail: QorCap end-of-game menu score reader (authoritative) + QorTime detect_final over the recorded last minutes, seeded with the live timeline"}
    (bundle / "final.tail.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
```

That seed is why 135645's tail says `scorebug clock not 0:00`. The Q4 0:11 row is `last`. The referee's re-read does not do this seed.

The tail does not replay those rows through `PeriodTrack`. `reached_end_of_game` on a tail is false unless the tail frames themselves contain clock reads. The shared rule below replays the locks, so the tail, the offline re-read, and the referee compute the same period track.

`apply_final` (`:2062-2112`) is the receipt mapping. An end-menu final with `final_confirmed` missing defaults to true (`:2094-2097`). A scorebug basis stays unconfirmed. No final becomes `last_scorebug_read_unconfirmed`.

### 2.4 `post_final_tail`

`post_final_tail.json` records how long capture ran after a committed end screen (`post_final_tail_s`, default 10 s, clamp 0..60, `:2126-2148`). It is not a score rule.

```2474:2475:sidecar/qorcap_match.py
    The live post-final tail (``post_final_tail_s``) only keeps the recorder running before this. It does not
    change the result written here. ``hz`` stays ``TAIL_HZ``.
```

Button, haptic, and clip evidence during the tail cannot confirm a one-step (the cutoff in section 2.2). A later menu score cannot replace a posted one (`qorcap_reader.py:1508-1509`).

### 2.5 Offline finalize

`finalize` (`qorcap_match.py:2887`) re-reads the hashed video at `FIN_HZ = 4.0` (`:1717`) through one `ReaderCore`, so the same quarter, clock, consistency, and corroboration gates apply. `select_offline_final` (`:2431-2445`) prefers the end menu, and a disputed menu stays the one that is signed. `apply_offline_final` (`:2448-2454`) forces `final_confirmed` false on a dispute. There is no end-menu fallback and no 0.90 / 2-frame gate. `final.offline.json` is the sidecar (`:2973`).

## 3. Where they diverge

| Question | QorCap client @ `2cb66ec` | QorTime referee @ `393045e` |
|---|---|---|
| Clock at 0:00 | Required for a Q4 menu, unless the last clocked read is red and `<= 5` s, or `PeriodTrack.reached_end_of_game`, or the scorebug quarter is already `>= 5` (`qorcap_reader.py:1458-1479`, `:1529-1530`). 0:11 fails. The rejection string is `scorebug clock not 0:00 before the end-of-game menu`. | The fallback never reads `clock_s`. A clock rejection is refused (`media.py:241-242`, test line 79). An unseeded re-read turns "the lock exists but the tail did not show the scorebug" into `no Q4 scorebug read...`, which the fallback accepts. |
| 135645 mechanism | `tail_final_check` copies `timeline.jsonl` onto `core.timeline` (`qorcap_match.py:2495`) before the clock gate. | `reread_final` documents "No laptop timeline seed" (`media.py:181`). Locks are a post-hoc score compare (`media.py:183-184`, `:275`). |
| Confidence | Digit floor 0.70 and margin 0.20 (`qorcap_endmenu.py:37-38`). No further confirm threshold. 0.912 passes. | Fallback requires `conf >= 0.90` (`media.py:206`, `:256-257`). 0.912 passes. A covered panel is refused here (`:260-261`). |
| Frames | 3 consecutive readable frames, span `>= 400` ms (`qorcap_endmenu.py:231-235`). 485 passes. | Fallback requires `frames >= 2` (`media.py:207`, `:258-259`). Weaker than the client lock that created the object. |
| Reference read | Last timeline row with a score. A null trailing quarter does not erase an earlier quarter; a null clock is copied from that earlier row (`qorcap_reader.py:1426-1449`). | Referee tail reads if any, else the last bundle lock (`media.py:275`). Every lock must be `<=` the menu (`:271-274`). Clock and red are ignored. |
| Reachability | Exact, or one of `{1,2,3,6,7,8}` on one side and 0 on the other. Else the menu is not posted (`qorcap_reader.py:1646-1657`). | Exact confirms. One step in quarter `>= 4` confirms with no corroboration (`media.py:277-284`). Other forward deltas are posted unconfirmed. Only a non-lone `+1` is refused as unreachable (`:280-281`). A menu total of 1 is refused (`:264-265`). The client has no "total of 1" refusal. |
| One-step evidence | Confirmed only with a prior equal scorebug row, a hashed score-change clip, haptic, or input, before the end-screen cutoff (`qorcap_reader.py:1660-1707`). Otherwise posted with `final_confirmed` false. | Not required. Quarter `>= 4` plus one step sets `final_confirmed` true (`media.py:284`). |
| Tie | The reader rejects (`qorcap_reader.py:1536-1538`). `apply_final` treats a missing `final_confirmed` on an end-menu object as true (`qorcap_match.py:2094-2097`), so a hand-built tie object confirms. | Fallback refuses (`media.py:262-263`). |
| Covered panel | May echo the scorebug score and then still has to pass the clock gate (`qorcap_reader.py:1597-1614`). | Fallback refuses `template_sources` containing `corroborated` (`media.py:260-261`). |
| Stall or gap | The gap does not move `clock_s`. The last lock still gates. The tail does not replay locks into `PeriodTrack`. qorcap#85 logs the stall and does not mint a score from it. | A gap that hides the scorebug from the unseeded re-read is the fallback's entry condition, and can confirm. |
| Scorebug `detect_final` | Same function, same defaults. `apply_final` leaves `clock_zero` / `scorebug_gone` / `final_screen` unconfirmed (`qorcap_match.py:2105-2107`). | Same function. `_result_from_reread` leaves those bases unconfirmed (`engine.py:841-842`). |
| What "unconfirmed" is | Clock failure: menu not adopted; receipt stays `last_scorebug_read_unconfirmed` if nothing else posted. One-step without corroboration: `end_menu_read` and `final_confirmed` false. | Confirmed fallback: review `pass`. Unconfirmed fallback: `partial`. No score at all: `fail`. |
| `post_final_tail` / offline finalize | Tail duration is `post_final_tail.json` only (`qorcap_match.py:2474-2475`). Offline finalize uses the same reader gates at 4 Hz and has no fallback. | No `post_final_tail`. The counterpart is the 240 s / 2 Hz re-read plus the fallback. |

## 4. The one rule: `final-rule/v1`

Both sides call one function, `decide(locks, menu, corroboration) -> Decision`. It lives in QorTime as `qor_receipt/final_rule.py`. QorCap already imports `qor_receipt`. `ReaderCore._end_menu`, `tail_final_check`, offline `finalize`, and the referee's `reread_final` all call it. `end_menu_fallback` stops being a second rule. The rejection string is an output, not a switch.

`PeriodTrack` stays the clock-history implementation. `decide` replays `locks` through it in order. The tail's missing replay, and the referee's missing seed, go away because neither caller gets to skip that replay. Thresholds stay the ones quoted above (`PERIOD_END_CLOCK_S = 5`, `ELAPSED_PERIOD_MS = 720_000`, `reached_end_of_game` at `qorcap_reader.py:166-172`). Do not fork that arithmetic.

### 4.1 Inputs

* `locks`: time-ordered agreed scorebug rows. Fields used: `away`, `home`, `quarter`, `clock_s`, `clock_red`, `t_ms`. Source is the union of committed `timeline.jsonl` locks and agreed scorebug reads from this session, deduped by `(t_ms, away, home, quarter)`. The referee's own tail reads are included when they agreed. They do not replace an earlier lock except by being later.
* `menu`: the `EndMenuAgreement` object, or null. Fields: `away`, `home`, `t_ms`, `until_ms`, `frames`, `conf`, `margin`, `template_sources`. `period` is informational. OT paint on the menu does not open the clock gate.
* `corroboration`: the list `end_menu_delta_corroborated` already accepts, plus the cutoff inputs (pixel-match time, `final_screen` rows).

A bool is not an int. A missing menu is "no menu".

### 4.2 Reference lock

The latest lock with integer `away` and `home`, both `>= 0`. If that row's quarter is null, copy the latest earlier quarter. If that row's `clock_s` is null, copy `clock_s` and `clock_red` from the row that supplied the quarter. This is `_scorebug_for_final` (`qorcap_reader.py:1426-1449`). No reference lock: do not adopt a menu.

### 4.3 Preconditions, all required before a menu is adopted

Quarter ready: reference quarter `>= 4`, or `PeriodTrack.reached_end_of_game(menu.t_ms)` after the replay. Otherwise reason `no Q4 scorebug read before the end-of-game menu`. Do not adopt.

Clock ready, same predicate as `_clock_ready_for_end_menu`:

* reference quarter `>= 5`, or
* `reached_end_of_game(menu.t_ms)`, or
* reference `clock_s == 0`, or
* the last lock with an integer `clock_s` has `clock_red` true and `clock_s <= 5`.

Otherwise reason `scorebug clock not 0:00 before the end-of-game menu`. Do not adopt. This is the fail-closed default. Section 4.5 is the stall case. Section 4.6 is the one exception that already exists.

Menu readable, all of:

* `frames >= 3`
* `until_ms - t_ms >= 400`
* real-template `conf >= 0.90` and `margin >= 0.20`
* `template_sources` does not contain `corroborated`, except the narrow clause in section 4.4
* both scores are integers `>= 0`
* the two scores differ
* neither score is `1`

Failing any of these: do not adopt. 2 frames is not enough. Confidence 0.89 is not enough. The glyph floor of 0.70 still decides whether a frame is readable. 0.90 is the confirm gate on the agreed object.

Agreement with the locks:

* every lock has `away <= menu.away` and `home <= menu.home` (scores never go down)
* the delta from the reference lock is `[0, 0]`, or one value in `{1, 2, 3, 6, 7, 8}` and the other value `0`

Any other delta, including both sides scoring, a `+4` / `+5` / `+9` / `+14`, or a `+1` beside another change: do not adopt. The referee's "post the jump unconfirmed" path is dropped. The last locked score stays the receipt score.

### 4.4 What a passing menu produces

| Shape | Receipt `result` | Referee `final_reread` |
|---|---|---|
| Clock ready, exact match, real template, gates passed | `method=end_menu_read`, `final_basis=end_menu`, `final_confirmed=true`, menu score, quarter `max(4, reference quarter)` (5 if the scorebug quarter is `>= 5`) | `pass` when the claim equals that score |
| Clock ready, one legal step, corroboration before the cutoff | same, `final_confirmed=true`, notes name the kind | `pass` when the claim equals the menu score |
| Clock ready, one legal step, no corroboration | `method=end_menu_read`, `final_basis=end_menu`, `final_confirmed=false`, menu score. This is the only time an unconfirmed menu score is written onto the receipt. | `partial` |
| Anything else in section 4.3 | menu not adopted. `method=last_scorebug_read_unconfirmed`, `final_confirmed=false`, no `final_basis`. Scores, quarter, and winner come from the reference lock. Quarter is not forced to 4. No lock: scores null, winner `unknown`. | `partial` when a lock exists. `fail` when no lock and no readable score. |

Corroboration is the client function quoted in section 2.2, including the end-screen cutoff. The referee does not confirm a one-step from quarter alone.

Covered panel, kept narrow: a `corroborated` read may be adopted only as an exact match to the reference lock, and only when the clock gate passes. Its threshold stays the corroboration floor (`CORROB_MIN = 0.55`, `CORROB_MARGIN = 0.15` in `qorcap_endmenu.py:272-273`), because that number is not a digit-template `conf`. It cannot carry a one-step delta. A covered panel at Q4 0:11 does not confirm. `check_final` stays a refusal of impossible jumps. A clean `check_final` is not a confirm. `detect_final`'s `clock_zero` / `scorebug_gone` / `final_screen` may still be posted and stay `final_confirmed` false until this rule confirms a menu.

A later menu with a different score keeps the first adopted score and sets `final_confirmed` false (`qorcap_reader.py:1576-1595`). Both sides do that. `apply_final` and `_result_from_reread` must stop treating a missing `final_confirmed` as true. `decide` always sets the boolean.

`post_final_tail` still only extends capture. It does not relax a gate. Offline finalize and `final.tail` call `decide` on the same locks. Sample rate does not change the thresholds: a 2 Hz tail that cannot show 3 frames over 400 ms does not confirm. 2 Hz can show 3 frames over 1000 ms. 485 frames passes the frame gate and still fails the clock gate on 135645.

### 4.5 Stall or gap

A stall is an agreed-read gap: the menu time is after the reference lock, and no later agreed scorebug row exists. qorcap#85 logs that gap and does not mint a score from it. `decide` uses the same definition for a referee tail that never shows the scorebug.

* The unread interval is not subtracted from `clock_s`.
* The unread interval is not `clock_s == 0`.
* "No scorebug in this tail" is not `no Q4 scorebug read...` when an earlier lock has a quarter. That string remains only for "quarter is not ready".
* Both callers pass the committed locks, including `clock_s` and `clock_red`, into `decide`. An empty reader timeline is not an input that selects a looser rule.

135645 under this rule: reference lock Q4, `clock_s` 11, not red, menu 10-7 equal to that lock, `conf` 0.912, `frames` 485, real template. Quarter ready. Clock not ready. Menu not adopted. Receipt: `last_scorebug_read_unconfirmed`, `final_confirmed` false, the locked score, quarter 4. Review: `partial`. Not VERIFIED. The client's rejection stands. The referee's `pass` does not.

### 4.6 Exception that stays, and the one that does not

**Kept.** Scorebug quarter `>= 5` may confirm with time left on the clock (walk-off). This is `_clock_ready_for_end_menu` (`qorcap_reader.py:1467-1469`) and the 091651 test: OT 1:55, menu equal to the lock, confirms. OT paint on the menu does not open the gate. A Q4 clock of 0:30 with an OT label still rejects (`test_qorcap_endmenu.py` at the 0:30 case).

Risk: a quarter glyph that reads 5 or higher during a running quarter, plus a menu that matches the current score, confirms early. The existing limits stay: quarters do not go backwards, and the menu's own "OT" pixels are not enough. This exception does not cover 135645. Quarter 4 at 0:11 is not OT.

**Not adopted.** Do not confirm a menu when the clock was not seen at 0:00, or red inside 5 seconds, or by the period track, because the reader stalled or the tail has a gap. 135645 is this case: Q4 0:11, menu equal to the last lock, 0.912 over 485 frames.

Risk of allowing it: eleven seconds is a field goal, a safety, or a touchdown. A stable menu shows that the panel did not flicker. It does not show that the clock expired, and it does not show that those eleven seconds scored nothing. A lookalike panel that matches the score on the bug ends the match while the real clock is still running. That is the case the client comment at `qorcap_reader.py:1518-1519` refuses. 485 frames and 0.912 do not close it. qorcap#85 makes a later match more likely to see 0:00. It is not evidence that 135645's clock hit 0:00.

If that exception were taken anyway, it would have to be this and nothing wider: the menu equals the last lock (not one step past it), the lock is quarter 4 with an integer `clock_s` of at most 15, `conf >= 0.90`, `frames >= 3`, span `>= 400` ms, real template, and the review state would still be `partial` rather than `pass`. This design does not take it. Section 7 is the decision.

## 5. Shared test vectors

Canonical JSON is in this section. When the function is implemented, the same bytes go to:

| Repo | File | Runner |
|---|---|---|
| QorTime | `tests/vectors/final_rule/v1.json` | `tests/test_final_rule.py`, calling `qor_receipt.final_rule.decide`. Run by the existing `pytest -q` job. |
| QorCap | `sidecar/fixtures/final_rule/v1.json` | `sidecar/test_final_rule.py`, importing the same `decide`. Run by `python -m pytest sidecar`. |

The two files are byte-identical. A hash mismatch fails the test. This design change does not add the runner or the copy. `final.tail`, `post_final_tail`, and offline finalize are covered because they call `decide` rather than a private gate: the 135645 vector is the tail case (seeded locks, clock 11), and `post_final_tail` has no vector because it does not change `decide`'s inputs.

`today` records the drift. `expect` is `final-rule/v1`. Sides on 135645: issue 94 says "10-7" and does not assign home and away. The vector uses away 10, home 7, which is left-to-right on the scorebug. Swapping the sides does not change the clock result. `margin` was not in the issue; `0.215` is a passing margin so the vector fails on the clock alone. `clock_red` false: a red 0:11 would still fail the 5-second window, and that case is the next vector.

```json
{
  "rule": "final-rule/v1",
  "legal_steps": [1, 2, 3, 6, 7, 8],
  "agree_frames": 3,
  "agree_span_ms": 400,
  "confirm_conf": 0.90,
  "confirm_margin": 0.20,
  "red_clock_s": 5,
  "cases": [
    {
      "id": "135645-stall-q4-0-11",
      "source": "match-20261024-135645 / qorcap#94",
      "note": "Away 10, home 7 stands in for the issue's '10-7'. Margin 0.215 is a stand-in so only the clock fails.",
      "locks": [
        {"t_ms": 800000, "away": 10, "home": 7, "quarter": 4, "clock_s": 11, "clock_red": false}
      ],
      "menu": {
        "away": 10, "home": 7, "t_ms": 900000, "until_ms": 1021000,
        "frames": 485, "conf": 0.912, "margin": 0.215,
        "template_sources": ["real"]
      },
      "corroboration": [],
      "today": {
        "qorcap_2cb66ec": {"adopt_menu": false, "reason": "scorebug clock not 0:00 before the end-of-game menu"},
        "qortime_referee_unseeded": {
          "reader_why": "no Q4 scorebug read before the end-of-game menu",
          "adopt_menu": true, "confirmed": true,
          "fallback": "end_menu_without_tail_scorebug", "review_state": "pass"
        }
      },
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "red-0-11-still-running",
      "locks": [{"t_ms": 1, "away": 30, "home": 6, "quarter": 4, "clock_s": 11, "clock_red": true}],
      "menu": {"away": 30, "home": 6, "t_ms": 10000, "until_ms": 12500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 30, "home": 6, "quarter": 4, "winner": "away",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "clock-zero-exact",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 10, "home": 7, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": true, "confirmed": true,
        "method": "end_menu_read", "final_basis": "end_menu",
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "exact_match", "review_state": "pass"
      }
    },
    {
      "id": "110028-red-0-03-exact",
      "source": "match-20261017-110028",
      "locks": [{"t_ms": 1, "away": 30, "home": 6, "quarter": 4, "clock_s": 3, "clock_red": true}],
      "menu": {"away": 30, "home": 6, "t_ms": 10000, "until_ms": 12500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": true, "confirmed": true,
        "method": "end_menu_read", "final_basis": "end_menu",
        "away": 30, "home": 6, "quarter": 4, "winner": "away",
        "reason": "exact_match", "review_state": "pass"
      }
    },
    {
      "id": "red-0-14-rejected",
      "locks": [{"t_ms": 1, "away": 30, "home": 6, "quarter": 4, "clock_s": 14, "clock_red": true}],
      "menu": {"away": 30, "home": 6, "t_ms": 10000, "until_ms": 12500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 30, "home": 6, "quarter": 4, "winner": "away",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "q4-clock-30-exact-rejected",
      "note": "Doctored menu matching a mid-Q4 score. Client test_core_rejects_end_menu_before_clock_zero.",
      "locks": [{"t_ms": 1, "away": 0, "home": 20, "quarter": 4, "clock_s": 30, "clock_red": false}],
      "menu": {"away": 0, "home": 20, "t_ms": 10000, "until_ms": 12500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 0, "home": 20, "quarter": 4, "winner": "home",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "091651-ot-walkoff",
      "source": "match-20261017-091651",
      "note": "Kept exception: scorebug quarter >= 5, time left, exact match.",
      "locks": [{"t_ms": 1, "away": 10, "home": 16, "quarter": 5, "clock_s": 115, "clock_red": false}],
      "menu": {"away": 10, "home": 16, "t_ms": 10000, "until_ms": 12500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": true, "confirmed": true,
        "method": "end_menu_read", "final_basis": "end_menu",
        "away": 10, "home": 16, "quarter": 5, "winner": "home",
        "reason": "ot_walkoff_exact", "review_state": "pass"
      }
    },
    {
      "id": "ot-paint-does-not-open-q4",
      "note": "Menu period paint is ignored. Quarter on the lock is 4 and the clock is 0:30.",
      "locks": [{"t_ms": 1, "away": 10, "home": 16, "quarter": 4, "clock_s": 30, "clock_red": false}],
      "menu": {"away": 10, "home": 16, "t_ms": 10000, "until_ms": 12500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"], "period": "OT"},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 10, "home": 16, "quarter": 4, "winner": "home",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "one-step-haptic-before-cutoff",
      "locks": [{"t_ms": 3000, "away": 0, "home": 17, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 0, "home": 20, "t_ms": 8000, "until_ms": 10500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [{"kind": "haptic", "t_ms": 3500}],
      "cutoff_ms": 8000,
      "expect": {
        "adopt_menu": true, "confirmed": true,
        "method": "end_menu_read", "final_basis": "end_menu",
        "away": 0, "home": 20, "quarter": 4, "winner": "home",
        "reason": "one_step_corroborated", "review_state": "pass"
      }
    },
    {
      "id": "one-step-haptic-after-cutoff",
      "locks": [{"t_ms": 3000, "away": 0, "home": 17, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 0, "home": 20, "t_ms": 8000, "until_ms": 10500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [{"kind": "haptic", "t_ms": 8000}],
      "cutoff_ms": 8000,
      "expect": {
        "adopt_menu": true, "confirmed": false,
        "method": "end_menu_read", "final_basis": "end_menu",
        "away": 0, "home": 20, "quarter": 4, "winner": "home",
        "reason": "one_step_not_corroborated", "review_state": "partial"
      }
    },
    {
      "id": "one-step-during-stall-not-adopted",
      "note": "Clock failed, so the menu score is not written onto the receipt even as unconfirmed.",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": 11, "clock_red": false}],
      "menu": {"away": 10, "home": 14, "t_ms": 900000, "until_ms": 1021000, "frames": 485, "conf": 0.912, "margin": 0.215, "template_sources": ["real"]},
      "corroboration": [{"kind": "haptic", "t_ms": 1000}],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "multi-play-not-posted",
      "note": "Referee today posts this unconfirmed. v1 does not adopt it.",
      "locks": [{"t_ms": 1, "away": 0, "home": 7, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 6, "home": 14, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 0, "home": 7, "quarter": 4, "winner": "home",
        "reason": "not_one_legal_play", "review_state": "partial"
      }
    },
    {
      "id": "plus-one-beside-another-play",
      "locks": [{"t_ms": 1, "away": 0, "home": 13, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 6, "home": 14, "t_ms": 5000, "until_ms": 7500, "frames": 31, "conf": 0.953, "margin": 0.215, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 0, "home": 13, "quarter": 4, "winner": "home",
        "reason": "not_one_legal_play", "review_state": "partial"
      }
    },
    {
      "id": "menu-below-a-lock",
      "locks": [
        {"t_ms": 1, "away": 6, "home": 14, "quarter": 4, "clock_s": 0, "clock_red": true},
        {"t_ms": 2, "away": 7, "home": 14, "quarter": 4, "clock_s": 0, "clock_red": true}
      ],
      "menu": {"away": 6, "home": 14, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 7, "home": 14, "quarter": 4, "winner": "home",
        "reason": "scores_went_down", "review_state": "partial"
      }
    },
    {
      "id": "final-of-1-refused",
      "locks": [{"t_ms": 1, "away": 0, "home": 0, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 1, "home": 0, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 0, "home": 0, "quarter": 4, "winner": "tie",
        "reason": "final_of_1", "review_state": "partial"
      }
    },
    {
      "id": "tie-refused",
      "locks": [{"t_ms": 1, "away": 14, "home": 14, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 14, "home": 14, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 14, "home": 14, "quarter": 4, "winner": "tie",
        "reason": "tied", "review_state": "partial"
      }
    },
    {
      "id": "conf-below-0-90",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 10, "home": 7, "t_ms": 5000, "until_ms": 20000, "frames": 31, "conf": 0.89, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "confidence", "review_state": "partial"
      }
    },
    {
      "id": "two-frames-not-enough",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 10, "home": 7, "t_ms": 5000, "until_ms": 5400, "frames": 2, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "frames", "review_state": "partial"
      }
    },
    {
      "id": "no-lock",
      "locks": [],
      "menu": {"away": 10, "home": 7, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": null, "home": null, "quarter": null, "winner": "unknown",
        "reason": "no_locked_read", "review_state": "fail"
      }
    },
    {
      "id": "covered-exact-at-clock-zero",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 10, "home": 7, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.60, "margin": 0.16, "template_sources": ["corroborated"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": true, "confirmed": true,
        "method": "end_menu_read", "final_basis": "end_menu",
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "covered_exact_match", "review_state": "pass"
      }
    },
    {
      "id": "covered-exact-during-stall",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": 11, "clock_red": false}],
      "menu": {"away": 10, "home": 7, "t_ms": 900000, "until_ms": 1021000, "frames": 485, "conf": 0.60, "margin": 0.16, "template_sources": ["corroborated"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "covered-one-step-refused",
      "locks": [{"t_ms": 1, "away": 0, "home": 17, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 0, "home": 20, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.60, "margin": 0.16, "template_sources": ["corroborated"]},
      "corroboration": [{"kind": "haptic", "t_ms": 2000}],
      "cutoff_ms": 5000,
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 0, "home": 17, "quarter": 4, "winner": "home",
        "reason": "covered_panel_not_exact", "review_state": "partial"
      }
    },
    {
      "id": "null-clock-on-q4-lock",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": null, "clock_red": false}],
      "menu": {"away": 10, "home": 7, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "corroboration": [],
      "expect": {
        "adopt_menu": false, "confirmed": false,
        "method": "last_scorebug_read_unconfirmed", "final_basis": null,
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "scorebug clock not 0:00 before the end-of-game menu",
        "review_state": "partial"
      }
    },
    {
      "id": "later-menu-disagrees",
      "note": "First adopted score is kept. Confirmation drops. The second score is not written.",
      "locks": [{"t_ms": 1, "away": 10, "home": 7, "quarter": 4, "clock_s": 0, "clock_red": true}],
      "menu": {"away": 10, "home": 7, "t_ms": 5000, "until_ms": 7500, "frames": 6, "conf": 0.95, "margin": 0.25, "template_sources": ["real"]},
      "later_menu": {"away": 10, "home": 14, "t_ms": 9000, "frames": 3},
      "corroboration": [],
      "expect": {
        "adopt_menu": true, "confirmed": false,
        "method": "end_menu_read", "final_basis": "end_menu",
        "away": 10, "home": 7, "quarter": 4, "winner": "away",
        "reason": "end_menu_disputed", "review_state": "partial"
      }
    }
  ]
}
```

`review_state` assumes the signed claim equals `expect`'s score. A claim of a menu score that `decide` did not adopt is `fail` (score mismatch), which is the current `_final_check` comparison (`engine.py:805-809`).

## 6. Schema and contract

Nothing in `schema/**` changes. `receipt.v0` stays frozen.

The verdict uses fields that are already there (`schema/receipt.v0.json:62-68`):

* `result.method`: `end_menu_read` or `last_scorebug_read_unconfirmed`
* `result.final_basis`: `end_menu`, or absent when the menu is not adopted
* `result.final_confirmed`: boolean
* `result.notes`: optional strings, already allowed
* `result.scorebug_last`: already allowed on an end-menu result

No new method. No new basis. `schema/contracts/compat.json` `result_methods` and `final_basis` stay as they are. `check_final` does not gain a mode. `review.checks.final_reread.state` stays `pass` | `partial` | `fail`.

The trace that is not a verdict goes in a sidecar, `final.rule.json`, next to the bundle. Same family as `final.tail.json`, `final.offline.json`, and `post_final_tail.json`. It is not a `bundle.json` field and not a receipt property. Suggested body, all of it optional for verifiers:

* `rule`: `final-rule/v1`
* `reason`, `adopt_menu`, `confirmed`
* the reference lock (`away`, `home`, `quarter`, `clock_s`, `clock_red`, `t_ms`)
* the menu object (`frames`, `conf`, `margin`, `template_sources`, `t_ms`, `until_ms`)
* `clock_ready`, `quarter_ready`
* `stall`: true when the menu time is after the reference lock and no later agreed scorebug row exists

The referee writes the same object beside the review bundle. The check detail can be one sentence. The structured trace stays in the sidecar so the signed receipt does not grow a field. One note string in `result.notes` is already legal and is not required.

`post_final_tail.json` stays a capture record. It does not gain the score decision.

## 7. Decision for Con

Adopt `final-rule/v1` as written. 135645 stays unconfirmed: last locked score, `final_confirmed` false, official review `partial`, not VERIFIED.

The stall exception in section 4.6 is not part of the rule. Taking it would confirm a score with eleven seconds of play that nobody read. That is the decision.
