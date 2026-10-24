# Final-rule unification — `final-rule/v1`

Status: design only. Con decided. No behaviour change in this change. `schema/**` is untouched. `receipt.v0` stays frozen.

Con's decision on PR #79, in his words: "The final menu screen should be the determination."

Menu-as-determination. A confidently read end-of-game menu confirms the final, including when the clock was never seen at 0:00. Match 135645 confirms at away 10, home 7. The clock-seen precondition is removed. The previous draft left the stall as an open question and kept overtime as a clock exception. That question is closed.

Tracks [qorcap#94](https://github.com/ConWan30/qorcap/issues/94). Match `match-20261024-135645` (client v0.3.32, production submission `f26ac3935d038288`).

Sections 1–3 quote the code that diverges today. Section 4 is the rule.

Sources quoted below:

| Side | Tree |
|---|---|
| QorTime referee | this repo, `393045eb14be280fb15445a5af9f55320de9c4e1` |
| QorCap client | `ConWan30/qorcap` @ `2cb66ec0b549c61814d9484be62c1faf0c640a42` (main, "Keep capturing for a short tail after the first end screen") |

The stall logger in qorcap#85 (`46d87e5`, "Keep the score reader at 4 Hz and log a stall without minting a score") does not decide a final. It does not treat unread time as clock expiry. `135645`'s last locked read is still Q4 0:11. Under §4 that lock is the stall note, and the end menu is the score.

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

This table is the code at the commits above. The rule is §4.

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

## 4. The rule: `final-rule/v1`

Con's decision on PR #79, in his words: "The final menu screen should be the determination."

That choice is menu-as-determination. A confidently read end-of-game menu is the final score. It confirms the final when the clock was never seen at 0:00: a stall, or a tail with no scorebug. Under this rule match 135645 confirms at away 10, home 7.

The clock-seen precondition is removed. The previous draft left the stall undecided and kept an overtime walk-off as a clock exception. There is no clock exception left to apply, because the clock is not an input to confirmation. Overtime confirms the same way a stalled Q4 confirms: the end menu passed the gates below.

`135645` sides: issue 94 says "10-7" and does not assign home and away. This rule uses away 10, home 7, left to right on the scorebug.

### 4.1 What confirms

All of these hold:

1. The screen is the end-of-game menu, by the discriminator in §4.2.
2. The score is stably read: 3 menu frames, spanning at least 400 ms, confidence at least 0.90 on every frame, every frame the same score (§4.3). A lock clears an incomplete streak. A run that already met this gate is kept.
3. Two stable reads whose scores differ are a dispute. The receipt keeps the first score, unconfirmed, with `end_menu_disputed` true, however many non-end frames separate the pictures (§4.4). A later picture with that same stable score confirms. A later picture with no stable run of its own cancels an earlier agreement. A later readable end-menu frame at or above the 0.70 glyph floor, with a different score, blocks the confirm when that frame is not itself a stable run. A scorebug return clears an unstable or unreadable picture. It does not clear a stably agreed score. The evidence cutoff is re-derived from the end screens that remain (§4.6).

The menu score is then the receipt score. `method` is `end_menu_read`, `final_basis` is `end_menu`, `final_confirmed` is true. The official `final_reread` check is `pass` when the claim equals that score. A pass can still become VERIFIED when the other checks pass. The note in §4.5 does not change this.

A tail with no scorebug lock still confirms, when the three gates pass. There is no "no Q4 scorebug read before the end-of-game menu" adoption block.

### 4.2 End menu, not a pause, halftime, or replay

The discriminator is the picture plus the OPTIONS hold. It does not read the game clock and it does not read the quarter.

`madden_final_screen` (`sidecar/qorcap_reader.py:819-855`, QorCap `2cb66ec`) is the picture test, on 640×360 coordinates at any input size:

* Bottom band y 200–345, skipping gamertag rows 295–328, is near-black and flat: mean &lt; 25, fraction of pixels brighter than 60 &lt; 0.02, standard deviation &lt; 14. A Thursday-night stadium (mean ~19, std ~12) passes. A faded field (std ~18, or a flat grey of 40) fails.
* Menu column x 20–150, y 15–130 contains a white highlight of at least 8 rows that are &gt;70% bright, and text density above 0.05.
* The column under the menu, y 132–175, is empty (fraction brighter than 90 &lt; 0.004). The end menu has 6 items and, even with the highlight expanded, ends by y ~126. The pause menu has 8 items, with QUIT GAME at y ~140, so it fills that band and fails.

Anything `madden_final_screen` rejects is not an end menu. That covers the 8-item pause, a halftime menu over the faded field, and a replay or highlights browser that is not the FINISH GAME column.

OPTIONS is the second half, for a picture the pixel test accepts. DualSense OPTIONS is button bit 9 (`OPTIONS_BIT`, `qorcap_reader.py:55-63`). `_opened_by_options` (`:977-997`) is true when the dark picture started within 4 s after OPTIONS (`OPTIONS_MENU_LEAD_MS = 4000`) while the scorebug was still up. `on_frame` (`:1025-1036`) returns before `_end_menu` when that is true and the picture has been held less than 12 s (`OPTIONS_COINCIDENT_HOLD_MS = 12000`). That return stays. Match `match-20261020-171032` is the case: a Practice / Play Now pause can share the 6-item shape for a moment after OPTIONS. A hold of 12 s or more may proceed to the score gates. The existing `test_final_screen_pause.py` helper `_menu(items=6)` is a picture `madden_final_screen` accepts; the pause fixture that must not confirm is the 8-item menu, the faded field, or that 6-item picture held under 12 s after OPTIONS.

`_end_context` (`:970-975`) and `_accept_final_screen` (`:999-1010`) currently require a finished clock before a pixel match may be labeled `final_screen`. They stop being a precondition for adopting the menu. The docstring at `:833` says the detector is only used behind the Q4-low-clock gate so a halftime menu cannot end a game. That sentence goes. The geometry and the OPTIONS hold are the discriminator.

The discriminator runs inside `decide`, once per frame. A recording can show a pause, a halftime menu, and the end menu at different times. A single `screen` label for the whole call is not an input.

Each frame carries `pixel` (`end_menu_6`, `pause_8`, `faded_field`, `replay`, or `other`) and, when the hold has to be measured, `picture_since_ms` (the first frame of the current dark picture). `options_presses_ms` is the list of OPTIONS rising edges, or null when that list was not measured. OPTIONS is DualSense button bit 9 (`OPTIONS_BIT = 1 << 9`, `qorcap_reader.py:55-63`; the same bit is `BUTTON_BITS` name `OPTIONS` bit 9 in `qor_receipt/review/bundle.py:306-307`). A press time is wall milliseconds: `inputs.jsonl` header `wall_anchor_ms` plus `t_us // 1000` on a rising edge of field `b` (`bundle.py:316-329`).

`picture_since_ms` is required only when a measured press could still label this frame a pause. For a frame at `t` and a press at `p`, that window is `t - 16000 < p <= t + 500`.

The binding picture is the latest one the press can open, not the earliest. The opened-by-OPTIONS test allows `picture_since_ms` from `p - 500` through `p + OPTIONS_MENU_LEAD_MS` (`OPTIONS_MENU_LEAD_MS = 4000`, `qorcap_reader.py:977-997`). The earliest picture (`p - 500`) has the longest hold. The latest picture (`p + 4000`) has the shortest hold, and that is the one that can still be under 12 s after the others are not.

* The frame is a pause only when the hold is under `OPTIONS_COINCIDENT_HOLD_MS` (12000).
* The shortest hold for a press is `t - (p + 4000)`. It is still under 12000 when `p > t - 16000`.
* A press at exactly `t - 16000` has shortest hold 12000. That is not under 12000. The press is outside the window. `options-press-at-t-minus-16000-is-outside` confirms.
* A press at `t - 15999` with the picture 4000 ms later has hold 11999. It is inside the window and it is a pause. `options-press-at-t-minus-15999-is-pause` does not adopt.
* If `p > t + 500`, the earliest picture for that press is after the frame. A press at exactly `t + 500` is inside the window. `options-press-at-t-plus-500-is-pause` does not adopt.

A window of `(t - 11500, t + 500]` is the earliest picture, and it lets a pause through. At the old boundary the picture is still a pause after 11.2 s when it appears 300 ms after the press, after 9.5 s with a 2 s lag, and after 7.5 s with the 4 s lag this rule allows.

Scorebug still up is a lock. The picture was opened by OPTIONS when some press in the window has `-500 <= picture_since_ms - p <= 4000` and at least one lock has `t_ms <= p`. The hold is `t - picture_since_ms`. A hold under 12000 labels the frame pause.

`options_presses_ms` for one frame is the list that loader returns for that frame. `[]` means that frame's own window was measured, the pad was connected through it, and OPTIONS did not rise. A confirming vector that omits both `inputs` and `options_presses_ms` is that `[]` on every frame. It is not a missing log.

The referee re-read today does not pass those presses. `reread_final` (`media.py:179-191`) feeds frames to a fresh `ReaderCore` and does not call `note_options_from_inputs`. The bundle does contain `inputs.jsonl` (`bundle.py` `META_FILES`). v1 has both the client tail and the re-read call `load_options_presses_ms` on those rows. They do not call `note_options_from_inputs` or `press_events` for the pause gate. The tail window is 240 s at 2 Hz (`policy.py` `TAIL_MS`, `engine.py` `TAIL_HZ`). A tail can start after the dark picture is already up, so `picture_since_ms` can fall outside the decoded frames. The loader's window is that frame's OPTIONS window, not the union of the tail.

`bundle.press_events` (`bundle.py:316-329`) cannot return null. It returns `[]` for an empty log, for an input row with no integer `b`, and for an input row with no integer `t_us`, and with no header it uses anchor 0. A connected status with no later status runs to the end of the log, so a hung reader or a truncated file also comes back as `[]`. Each of those is "measured, not pressed", and the pause gate never runs. v1 does not call `press_events` for this list.

Both sides call `load_options_presses_ms`. For each frame at `t` it reuses qorcap #97's Final rule (`docs/design/challenge-failed-result.md`, heading "Final rule") with `issued_ms = t - 16000` and `deadline_ms = t + 500`. Those two bindings are the frame's OPTIONS window, checked as #97's closed span. The gate text, with only those names substituted, is:

> Every non-empty line of `inputs.jsonl` must parse as a JSON object. A JSON value that is not an object, such as `42` or `[]`, is malformed, and so is a second `header` row. Coverage opens at the latest `chunk` row stamped at or before `issued_ms`; there must be one, and a chunk after `issued_ms` does not open coverage. From it, each next chunk must be at most 3000 ms after the previous one until a chunk stamped at or after `deadline_ms` is reached; a last chunk before `deadline_ms` withholds even when the deadline is within 3000 ms of it. Gaps before the opening chunk and after the closing chunk are not checked. Only `type: "chunk"` rows with an integer `t_us` count; others are ignored. Chunk rows, including empty ones, are written every segment from 0.3.34. That bound is the 2 s fMP4 fragment (`min_frag_duration`) plus 1 s of slack for the one-second GOP. A single chunk after the deadline does not cover the window, and neither does a crash gap followed by rows after the deadline. Wall time is the header `wall_anchor_ms` plus `t_us // 1000`. The pad's status must be `connected` at `issued_ms` and remain `connected` through `deadline_ms + 2500` ms. That 2500 ms is the stall-detection lag: a `stalled` row is written only after 2 s with no HID reports, on the next 100 ms read timeout. A `stalled`, `unplugged`, or `missing` row anywhere in that span withholds the result, including a drop that reconnects before the deadline. A status row whose `t_us` is not an integer, or whose `state` is not a string, withholds it too.

`pad_connected_through` withholds on any state other than `connected` in that span, so `off` and `searching` withhold too. The named states are the ones #97 calls out. A bool is not an integer, for `wall_anchor_ms`, for `b`, and for every `t_us`. The header count is exactly one: zero headers is null, and so is a second header. Status rows are checked in log order. A status wall time strictly earlier than the previous status row is null, and that log is not sorted into order. Rows that pass that check are then sorted by `(wall, state)`, the way qorcap #97 sorts its `events`. At one millisecond `"unplugged"` sorts after `"connected"`, so the unplugged row is the state at that time and the pad is not connected, including when the connected row is written last in the file. `inputs-same-ms-unplugged-and-connected` is that pair.

The return is null when the log itself cannot be measured. Every frame then fails closed. Otherwise the return has one entry per frame. A frame entry is null when that frame's own window fails the gate. A frame entry is a list of OPTIONS rising edges when that frame's window passes. `[]` is that list when OPTIONS did not rise. A connected status does not run to infinity: without a chunk at or after `deadline_ms`, the frame is null. `inputs-log-ends-before-window-end` is a log whose last chunk is 2500 ms before the window end, inside the 3000 ms step, with the 19.8 s OPTIONS press not in the file. The menu is not adopted. `inputs-well-formed-no-options-press` steps chunks by exactly 3000 ms and confirms. `inputs-chunk-step-3001` does not. `inputs-stall-at-window-end-plus-2500` stalls on the last frame's `deadline_ms + 2500` and does not confirm. The well-formed log stalls 1 ms later and does confirm. `inputs-unplug-reconnects` drops and reconnects inside the window and is null. `inputs-unplug-minutes-earlier-later-frame-confirms` puts that drop inside an early frame's window only. The later frame's own window passes, and that menu confirms. One unplug does not fail the rest of the tail.

`inputs-no-wall-anchor`, `inputs-wall-anchor-not-integer`, `inputs-wall-anchor-bool`, `inputs-non-object-line`, `inputs-second-header`, `inputs-malformed-row`, `inputs-b-true`, `inputs-status-t-us-true`, and `inputs-status-backwards` are null for the log. `inputs-pad-not-connected` parses, and every frame's entry is null because the pad is not `connected` through the window. The same 13–6 frames would confirm on `[]`. They do not.

```text
load_options_presses_ms(rows, frame_times_ms) -> list | None:
    # None: the log cannot be measured. Every frame fails closed.
    # Otherwise one entry per frame time: a list of press times, or None
    # when that frame's window fails qorcap #97's Final rule.
    if frame_times_ms is empty or rows is not a list:
        return None
    if any row is not a JSON object:
        return None
    if the number of rows with type "header" is not 1:
        return None
    if wall_anchor_ms is not an integer, or it is a bool:
        return None
    anchor = wall_anchor_ms
    for each type "in" row:
        if b is not an integer, or b is a bool, or t_us is not an integer, or t_us is a bool:
            return None
    events = []
    prev_wall = none
    for each type "status" row, in log order, not sorted:
        if t_us is not an integer, or t_us is a bool, or state is not a string:
            return None
        wall = anchor + t_us // 1000
        if prev_wall is set and wall < prev_wall:
            return None
        prev_wall = wall
        events.append((wall, state))
    presses = []
    prev = 0
    for each type "in" row, in log order:
        rise = b & ~prev
        if rise has bit 9 (OPTIONS, 1 << 9):
            presses.append(anchor + t_us // 1000)
        prev = b
    out = []
    for t in frame_times_ms:
        issued_ms = t - 16000
        deadline_ms = t + 500
        if chunks_cover and pad_connected_through for that pair:
            out.append(presses)
        else:
            out.append(None)
    return out

chunks_cover(rows, anchor, issued_ms, deadline_ms):  # qorcap #97 Final rule
    times = sorted unique wall times of type "chunk" rows
            whose t_us is an integer and not a bool
            (any other chunk row is ignored)
    prev = the latest time <= issued_ms
    if there is no such time:
        fail
    if prev >= deadline_ms:
        cover
    while a next time remains:
        if that time - prev > 3000:
            fail
        prev = that time
        if prev >= deadline_ms:
            cover
    fail
    # the times ran out before deadline_ms, even when the gap would be <= 3000

pad_connected_through(events, issued_ms, deadline_ms):  # qorcap #97 Final rule
    state = the latest event with wall <= issued_ms
    if state is not "connected":
        fail
    if any event with issued_ms < wall <= deadline_ms + 2500 has state other than "connected":
        fail
    pass
```

A frame whose entry is null is not an end menu. A press in that frame's window with no `picture_since_ms` is not an end menu either. The final stays unconfirmed. Official review is `partial` when a chain posts a score, and `fail` when it does not. It is not `pass`. A 6-item frame with no `picture_since_ms` and no press in its window is still an end menu.

Accepted risks, all of which need the recording to end on the picture. A later agreed scorebug read clears an incomplete or unreadable streak. A run that already met the stable gate is kept, so these still survive a scorebug that returns after that run:

* A halftime screen that is pixel-identical to the six-item end menu, and was not opened by OPTIONS, passes the geometry. v1 does not put a clock gate behind it.
* Three forged end-menu frames over at least 400 ms at the very end of the recording, with confidence at least 0.90 and the same score, confirm. That is the case the clock-gate comment at `qorcap_reader.py:1518-1519` refused.
* A 6-item Practice / Play Now pause held at least 12 s, with the recording stopped on that picture (`match-20261020-171032`), confirms. The short hold still refuses it. The long hold at the end of the file does not.

### 4.3 Stably read

A menu is stably read when `decide` has 3 menu frames that agree, spanning at least 400 ms, each with `conf` ≥ 0.90. A lock clears an incomplete streak: the frames gathered so far are dropped when they have not yet met that gate, and the next run is built from frames strictly after that lock. A frame whose `t_ms` equals a lock's `t_ms` is not counted. The lock is ordered before the frame at that timestamp. A run that already met the gate is kept. A later lock does not drop it, and the frames do not have to sit strictly after the latest lock in the capture. Latest, when a caller still needs it for the evidence cutoff, means the maximum `t_ms` among locks, not the last row in list order. With no scorebug read, every menu frame can join a run.

`decide` requires each of `frames` and `locks` to be listed in non-decreasing `t_ms`. A row with `t_ms` less than the previous row fails the decision closed: the menu is not adopted. A duplicate `t_ms` stays in the list and does not add a distinct instant or any span. The run needs 3 distinct, strictly increasing timestamps. Rows `5000, 5000, 5400` are two instants. Latest lock is the maximum `t_ms`. On a non-decreasing list that is the last lock. An `ok: false` frame resets the streak: the frames before it are dropped, and the frames after it must form a new run. A readable frame with confidence from 0.70 through 0.89 resets the streak the same way. The bad frame is not bridged. Two frames after the reset do not confirm. Three fresh frames after the reset can. A backwards timestamp is the case that fails the whole decision closed.

On the fresh, strictly increasing frames, all of these hold:

* at least 3 distinct timestamps
* the last timestamp minus the first is at least 400 ms
* every frame has `conf` ≥ 0.90
* every frame agrees on `(away, home)`
* every frame's `pixel` is `end_menu_6` and the OPTIONS hold does not label it `pause`
* the object carries `min_conf`, the minimum across the run

`EndMenuAgreement` (`sidecar/qorcap_endmenu.py:231-261`) already requires 3 consecutive readable frames and a span of at least 400 ms, and it already clears the run when the score changes or a frame is unreadable. It does not know about the scorebug. `offer` stores `conf` from the frame that first completes the run (`:251-256`) and later frames update only `until_ms` and `frames` (`:259-260`). A readable frame under 0.90 clears the run, the same way an unreadable frame does.

A scorebug return clears an unstable or unreadable earlier picture. It does not clear a score that was already stably read. `_post_agreed` clears `_end_screen_earliest` (`qorcap_reader.py:1343-1344`) and does not clear `EndMenuAgreement`. The keep-or-clear rule is in `decide`, so the tail and the referee re-read do the same thing. A completed run stays in the list of stable runs. The next frames start a new run.

The glyph floor stays what makes a frame readable: `MIN_SCORE = 0.70` and `MIN_MARGIN = 0.20` (`qorcap_endmenu.py:37-38`). 0.90 is the confirm gate on top of a readable frame. A covered-panel corroboration (`CORROB_MIN = 0.55`, `qorcap_endmenu.py:272`) fails 0.90 on every frame, so a covered panel does not confirm.

The referee fallback's `frames >= 2` (`media.py:207`, `:258-259`) is replaced by this 3-frame rule.

Issue 94 reported one confidence, 0.912, over 485 frames. That number is the completing frame in today's reader, not a per-frame minimum. The stall fixture is synthetic: three frames at confidence 0.94, with invented timestamps. It does not reuse 0.912. A 2 Hz tail spaces frames by 500 ms, so 3 frames span about 1000 ms and clear 400 ms.

### 4.4 Last menu, and what a failed gate produces

`decide` derives the last menu and later live play from `frames` and `locks`. There is no caller-supplied `later` flag.

`decide` collects every stable run. A lock clears an incomplete streak and leaves a completed run in that collection. A later stable run whose score differs from the first stable run is a dispute, not a new candidate. The gap between them does not matter: gameplay, a replay, one stray non-end frame, or no frame at all. The receipt keeps the first stable score, unconfirmed, with `end_menu_disputed` true. The later score is not posted. `dispute-later-menu-after-gameplay` is 7–0, then gameplay, then 10–0, and the receipt stays 7–0. `dispute-later-menu-reverse-order` is that pair reversed, and the receipt stays 10–0, the first score. `dispute-one-stray-frame` is 7–0, one non-end frame, then 10–0. Treating the later picture as the candidate would confirm 10–0. It is a dispute, and the receipt stays 7–0. `same-menu-after-gameplay-confirms` is 7–0, then gameplay, then 7–0, and it confirms 7–0.

A later frame blocks that confirm even when it is not itself stable. After the candidate run, any readable end-menu frame in that same picture whose score differs from the agreed score fails the decision closed. Readable here means `ok` is true, `pixel` is `end_menu_6`, the OPTIONS hold does not label the frame pause, and `conf` is at least the glyph floor `MIN_SCORE` (0.70, `qorcap_endmenu.py:37-38`). 0.70 stays that constant. One frame at 0.85 is enough. The candidate is not posted. A frame under 0.70 does not block. An `ok: false` frame does not block. A frame that only resets the streak, and is followed by a later stable run of the same score in the same picture, is earlier than that run and does not block it. `later-readable-frame-blocks-confirm` is the 0.85 frame with no lock: posted score null, neither menu, `final_reread` fail. `later-frame-below-readability-floor` is the same shape at 0.69 and still confirms.

An end-menu picture that starts after a non-end-screen frame is a new picture. A new picture whose own stable score differs from an earlier stable score is the dispute above, not a fresh candidate. A new picture with the same stable score confirms that score. A new picture that never gets a stable run cancels the earlier agreement: the earlier menu is not posted. Two stable scores in one picture, with no non-end frame between them, are the same dispute and keep the first score. Unreadable flicker inside the picture that already agreed neither blocks nor starts a new picture. `new-picture-cancels-earlier-agreement` is a stable 6–3, then a replay frame, then the online menu unreadable (`ok` false, 4–29) with no scorebug lock. The later picture has no stable run. The 6–3 is not posted. `same-picture-unreadable-flicker-keeps-agreement` is that 6–3 with the unreadable frame still inside the same picture, and it confirms.

Before the implementation PR merges, measure blocking-flicker and streak-reset rates on the real recordings 135645, 143212, 091651, 110028, and 162055. Those five are not the held-out set, and they are not vector templates. 135645's vector stays synthetic. The held-out matches (170123, 021003, 125430, 084523, 193534) are not the measurement set. A synthetic fixture pass is not that measurement: 0 wrong of 615 on 143212, and 2 of 972 reading 4–29 on the online menu without the scorebug cross-check, only land on partial. Partial is the safe side. The implementation PR records the five rates next to this paragraph.

An agreed scorebug read with `t_ms` after menu frames is later live play. It is a lock. It clears an incomplete or unreadable streak, including when `clock_s` is 0. It does not drop a run that already met the stable gate, and it does not turn two different stable menus into a confirm of the later one. When no second stable menu follows and the longest chains agree, the kept score stands. `later-live-play` confirms 13–6. The clock-45 lock still closes the episode, and the stall note names Q4 0:45. `cutoff-falls-back-to-earliest-end-screen` confirms 10–7, and the cutoff stays 2000 because the lock is 0:00 and no end screen follows it. When a different stable menu follows, the result is the dispute, and the note names the scorebug return when a lock sits strictly between the two runs. `scorebug-return-resets-agreed-score` keeps 3–0. When the same stable menu follows, the score confirms. `scorebug-return-same-score-confirms` is that pair at 10–7. A later lock that is a stale decrease, so the longest chains disagree, does not confirm the kept menu. `replay-lock-lower-after-menu` stays a null score and `final_reread` fail.

Failed gates produce these results, on both sides:

| Case | Posted score | `method` | `final_basis` | `final_confirmed` | `final_reread` |
|---|---|---|---|---|---|
| Gate failed, one longest chain | terminal score of that chain | `last_scorebug_read_unconfirmed` | absent | false | `partial` |
| Several longest chains, same terminal score | that score | `last_scorebug_read_unconfirmed` | absent | false | `partial` |
| Several longest chains, terminal scores differ | null. Neither chain end is posted | `last_scorebug_read_unconfirmed` | absent | false | `fail` |
| No lock, and no adopted menu | null | `last_scorebug_read_unconfirmed` | absent | false | `fail` |
| Two stable reads whose scores differ, in one picture or after any number of non-end frames | the first stable score. `end_menu_disputed` true. The later score is not posted | `end_menu_read` | `end_menu` | false | `partial` |
| No lock, stable read, then a later readable frame in that picture with a different score, and that frame is not itself a stable run | null. Neither score is posted | `last_scorebug_read_unconfirmed` | absent | false | `fail` |
| Stable read, then a new end-menu picture with no stable run of its own | the chain score when one exists, otherwise null. The earlier menu is not posted | `last_scorebug_read_unconfirmed` | absent | false | `partial` when a chain posts a score, otherwise `fail` |
| Stable read kept across a later lock, and the longest chains disagree | null | `last_scorebug_read_unconfirmed` | absent | false | `fail` |

The no-score fail detail stays "referee could not read any score from the recorded tail" (`engine.py:804`).

`partial` on an official review check stays PARTIAL (`qor_receipt/receipt.py:242-243`). It does not become VERIFIED.

qorcap live `_note_end_menu_disagreement` (`qorcap_reader.py:1576-1595`) and qorcap #102 keep the first menu score, set `final_confirmed` false, and set `end_menu_disputed` true. v1 does the same when two stable reads disagree. The receipt score is that first score, not the chain score. The note is `later end-of-game menu read AWAY {later} - HOME {later} disagreed with the posted final AWAY {first} - HOME {first}` plus, when a lock's `t_ms` is strictly after the first run's last frame and strictly before the later run's first frame, ` after a scorebug read came back between the two menus`, then `; score not replaced; final_confirmed=false`. A lock on either bound does not add the phrase. The note names the first later run whose score differs. `select_offline_final` (`qorcap_match.py:2431-2445`) and `_disputed_final_result` (`:2457-2464`) sign that first score, `final_confirmed` false. The review is `partial`. It is not VERIFIED, including when the claim equals the first score. A new picture that never becomes a stable run still does not post the earlier menu. When the longest chains disagree and no dispute posts a menu, the receipt score is null.

`tail_final_check` stops at the first end-menu agreement (`qorcap_match.py:2520-2521`). `reread_final` does the same (`media.py:190-191`). Both loops run to the end of the decoded window, then `decide` sees the last menu. The comment at `qorcap_match.py:2477-2478` is the bug those breaks leave in place.

`apply_final` treats a missing `final_confirmed` on an end-menu object as true (`qorcap_match.py:2094-2096`). `_final_check` and `_result_from_reread` treat "not false" as confirmed (`engine.py:817`, `:840`). `decide` always writes the boolean. A missing boolean is false.

`detect_final` bases `clock_zero`, `scorebug_gone`, and `final_screen` stay unconfirmed (`engine.py:841-842`, `qorcap_match.py:2105-2107`). They do not confirm a final. `post_final_tail.json` remains capture duration only (`qorcap_match.py:2474-2475`).

### 4.5 Disagreement and a stall: the menu still wins

When the gates in §4.1 pass, the menu score is the final even if it disagrees with the last locked live read.

A disagreement that must be recorded is either of:

* the menu is more than one legal scoring play past the last lock
* a stall gap

One legal scoring play is a delta in `{1, 2, 3, 6, 7, 8}` on one side and 0 on the other (`LEGAL_STEPS`, `qorcap_reader.py:1632`, `integrity.py` uses the same set). That delta is an ordinary final play. It confirms with no disagreement note, unless there is also a stall gap.

More than one legal scoring play includes both sides changing, a second play on one side, and any other forward delta (`+4`, `+5`, `+9`, `+14`, a `+1` beside another change). The menu still confirms.

A stall gap is recorded from the raw `clock_s` on the lock that posts the chain score. `decide` does not copy a clock from an earlier row. `_scorebug_for_final` (`qorcap_reader.py:1426-1449`) does that copy today. Callers pass the lock as it was read. A null `clock_s` stays null. When the chains disagree there is no posted lock, and the stall note does not fire.

The note fires when either of these is true:

* raw `clock_s` is an integer greater than 0. Unread seconds are that `clock_s`. Issue 94's lock is 11.
* raw `clock_s` is null and `wall_gap_ms` is at least `STALL_WALL_GAP_MS` (10000). `wall_gap_ms` is the first frame of the adopted run minus that posted lock's `t_ms`. Unread seconds are null. The note names the wall gap. Example: "End menu confirmed 6–3. Scorebug last locked at Q4 with no clock read (19000 ms unread on the wall clock)."

`wall_gap_ms` is stored in both cases. When a stable menu is kept across a later scorebug, the adopted run can start before the posted lock, and `wall_gap_ms` is negative. The clock note still fires when raw `clock_s` is greater than 0. `later-live-play` confirms 13–6 with that note for Q4 0:45. The menu frames start at 5000 and the posted lock is at 9000, so the wall gap is −4000. Unread seconds are not subtracted from the clock, and they are not treated as 0:00. qorcap#85 logs a stall and does not mint a score from it. The menu mints the score. The gap is recorded. The note does not downgrade the verdict.

The menu must be greater than or equal to every lock on both sides, not only the locks on the chain. A menu below any lock is unconfirmed. The posted unconfirmed score is not a greedy running maximum. It is the terminal score of the longest componentwise non-decreasing chain of locks. A later lock follows an earlier one only when its away is at least the earlier away and its home is at least the earlier home. `side-swap-keeps-componentwise-chain` is 14–7, then 7–14, then 21–7. The chain is 14–7 then 21–7. The posted score is 21–7. The 7–14 lock is not on that chain.

One longest chain posts its last lock, and every lock on that chain may close an episode. Several longest chains with the same terminal score post that score, and only a lock that sits on every such chain may close. Several longest chains whose terminal scores differ fail closed: the receipt score is null, neither chain end is posted, and only a lock on every such chain may close. `backwards_lock` is the first lock, in the order given, that sits on no longest chain. `check_final` (`integrity.py:195-202`) compares the receipt score to `last_state`. The caller passes the posted lock as `last_state` when there is one. A tie has no `last_state` score.

`stale-decrease-keeps-later-lock` is the early-pause locks plus a 3–3. The 6–3 chain and the 3–3 chain are the same length and both end 13–6, so the posted score is 13–6. The 3–3 lock is not on every chain, so it does not close. The 13–6 lock at 15000 does. `spurious-high-lock-does-not-poison` puts 77–6 in that slot. The chain that skips it is longer, the posted score is 13–6, and 77–6 does not close. `longest-chains-tie-score-unconfirmed` is 6–3, then 77–6, then 13–6. The two chains have length 2 and end on different scores. The receipt score is null. Only the 6–3 lock may close. `replay-lock-lower-after-menu` and `menu-below-an-earlier-lock` are the same kind of tie: each lock is its own chain, the scores differ, and the receipt score is null. One real lock plus one stale lower lock moves from partial to fail. The old running-maximum walk posted the higher lock and the review was partial. Under the longest chain those two locks are each length 1, the terminal scores differ, and `final_reread` is fail. The stable 14–7 menu in `replay-lock-lower-after-menu` is kept across the later 3–0 lock and is still not adopted, because the chains do not agree on a score.

A tie, and a menu with either side equal to 1, stay refused. See §4.7. That refusal is not a sign-off to delete them.

The record goes in the bundle sidecar `final.rule.json`. It is not a `receipt.v0` field, not a `result.notes` entry, and not a `bundle.json` field. `schema/**` stays untouched. Absence of the file does not fail verification, the same way `device_attest.v0.json` is optional. The publisher reads the file when it is present.

Shape:

```json
{
  "schema": "final-rule/v1",
  "rule": "final-rule/v1",
  "final_confirmed": true,
  "score": {"away": 10, "home": 7},
  "method": "end_menu_read",
  "agreement": {"frames": 3, "span_ms": 1000, "min_conf": 0.94},
  "last_locked": {"away": 10, "home": 7, "quarter": 4, "clock_s": 11, "clock_source": "raw", "t_ms": 800000},
  "gap": {"seconds_unread": 11, "wall_gap_ms": 100000},
  "disagreement": null,
  "note": "End menu confirmed 10–7. Scorebug last locked at Q4 0:11 (11 seconds unread).",
  "downgrades_verdict": false,
  "writer": "referee",
  "cutoff_ms": 900000,
  "cutoff_reason": "open_episode",
  "episode_e0_ms": 900000,
  "end_menu_disputed": false
}
```

When the delta is more than one legal play, `disagreement` is:

```json
{
  "last_locked": {"away": 7, "home": 0},
  "menu": {"away": 17, "home": 14},
  "delta": [10, 14],
  "more_than_one_legal_play": true
}
```

and the note names both the last lock and the menu. `downgrades_verdict` stays false.

`writer` is `client` when the capture tail wrote the file and `referee` when the official re-read wrote it. `cutoff_reason` on this example is `open_episode`: the lock is before the menu, so that menu's episode is still open. A null `cutoff_ms` is not one meaning. The three reasons are in §4.6.

The public note is that `note` string, shown on the match page and on the matches index. It is rendered from the sidecar at publish time:

* `qor_receipt/match_index/build.py` copies `final.rule.json` onto the page index as `result.final_rule` (the result object is built near `:421`).
* `qor_receipt/match_index/viewer_template.html` `paintFinal` (`:984-995`) appends `result.final_rule.note` to `#finalWhy` when the final is confirmed. The gold class stays on `#finalSum` when the receipt is VERIFIED.
* `qor_receipt/match_index/publish.py` `_card` (`:452-474`) adds the same sentence under the score. The gold class stays when `final_confirmed` is true and `receipt_status` is `VERIFIED` (`:459`).

The note does not downgrade the verdict. `final_reread` stays `pass`. A VERIFIED receipt stays VERIFIED. The score on the page stays the menu score.

Issue 94's shape, under this rule, with a synthetic per-frame read: end menu away 10, home 7, every frame at 0.94, three timestamps strictly after the lock, span 1000 ms, pixels `end_menu_6`, no later menu in `frames`, no lock after those frames. Last raw lock Q4 0:11, same score. Confirmed. `final_reread` pass when a review is run. The sidecar note is the 11 unread seconds and that last lock. The client's current rejection does not stand. The referee's current pass stays a pass, with the note added. The 0.912 figure in sections 1–3 is the issue's single reported confidence. The vector does not copy it.

### 4.6 One function, and the clock leaves both callers

Both sides call `qor_receipt.final_rule.decide`. QorCap already imports `qor_receipt`. This is not `qor_receipt.referee_ai.referee.decide`, which turns integrity errors into a verdict (`referee.py:194`).

Removing the clock gate changes which frames count as end screens. A pixel match that `_accept_final_screen` used to reject for the clock (`qorcap_reader.py:999-1010`) can now be an end screen, and a `final_screen` timeline row from that match now feeds the cutoff. The cutoff is re-derived here, per episode. It is not the first end screen of the capture.

```text
decide(frames, locks, evidence, options_presses_ms) -> Decision
```

* `frames`: reads `{t_ms, away, home, conf, ok, pixel, picture_since_ms}`, listed in non-decreasing `t_ms`. A backwards step fails the decision closed. A duplicate timestamp does not add a distinct instant or any span. A frame counts toward a stable run when `ok` is true, `conf` ≥ 0.90, §4.2 labels `pixel` as the end menu, and `t_ms` is strictly greater than the previous counted frame. A frame whose `t_ms` equals a lock's `t_ms` is not counted. A lock clears an incomplete streak. A run that already met the stable gate is kept. Two stable runs whose scores differ are a dispute: the first score is posted, unconfirmed, with `end_menu_disputed` true, however many non-end frames separate them, including one stray frame and including none. Stable runs that share one score confirm when the §4.4 gates pass, including a later picture of that same score. A later picture with no stable run of its own cancels an earlier agreement. A later readable frame in the confirming picture at or above 0.70 with a different score, when that frame is not itself a stable run, blocks the confirm (§4.4).
* `locks`: agreed scorebug rows, possibly empty, each with raw `clock_s` (integer or null). Latest is `max t_ms`. Used to clear an incomplete streak, for the chain score, the stall gap, the scorebug phrase on a dispute note, and the disagreement note. `clock_s` is not a confirm precondition. `decide` does not require a Q4 row and does not copy a clock onto a null.
* `evidence`: clip, haptic, and input rows `{t_ms, kind}`. A scorebug row is a lock, not evidence.
* `options_presses_ms`: null, one shared list, or a per-frame list. Null means the presses were not measured, and every 6-item frame fails closed. A list of JSON integers, including `[]`, is one shared press list for every frame. A bool is not an integer. A list whose length is `len(frames)`, and whose elements are each null or a list of JSON integers, is per-frame: a null element fails that frame only, and a list element is that frame's presses. An empty list is the shared shape, not the per-frame shape. Any other length or type fails closed, and every 6-item frame is not an end menu. `options-presses-wrong-shape` passes `[null]` beside three frames. A reader that treated that value as `[]` would confirm. `decide` does not. `picture_since_ms` is required only when a press falls in the §4.2 window `(t - 16000, t + 500]`.

End-screen timestamps are derived inside `decide`. `decide` has no `end_screens` argument. An end screen is a frame `decide` labels by picture and hold: `pixel` `end_menu_6`, and the OPTIONS hold does not label the frame pause. `ok` and `conf` are not part of that label. An `ok: false` frame whose picture is the six-item menu is an end screen, including when name tags cover the away score. Readability is the stable-run gate and the later-frame blocker only. A frame under 0.70 can still be an end-screen timestamp, and it does not block a confirm. A 6-item frame the hold labels pause is not an end screen. A 6-item frame whose hold was not measured is not an end screen either: that frame's press entry is null, or a press in the §4.2 window has no `picture_since_ms`. A 6-item frame with no `picture_since_ms` and no press in the window is still an end screen. `picture_since_ms` on a frame that remains an end menu is included. A vector is reproducible from `frames`, `locks`, `evidence`, and `options_presses_ms`. When the vector carries `inputs`, the harness calls `load_options_presses_ms` on those rows and the frame times before `decide`. That result is not a second field on the vector.

`Decision` carries `rule` = `final-rule/v1`, `cutoff_ms`, `cutoff_reason`, `episode_e0_ms`, `evidence_counted`, `posted_score`, `end_menu_disputed`, and `writer`. `end_menu_disputed` is true when two stable end-menu scores disagree. `cutoff_ms` is not a receipt field. `end_menu_disputed` is not a `receipt.v0` field. `cutoff_reason` is one of `no_end_screen`, `closed_episodes_only`, or `open_episode`.

The evidence cutoff does not grant or withhold `final_confirmed`. Its only consumer is the sidecar writer: `tail_final_check`, offline `finalize`, and `reread_final` copy `evidence_counted`, `cutoff_ms`, `cutoff_reason`, `episode_e0_ms`, and `writer` onto `final.rule.json`. The client tail writes `writer` `client`. The referee re-read writes `writer` `referee`. The publisher does not render the evidence list. No confirm path reads it. A boundary bug in the list changes no verdict.

Today the first end-screen time can outlive the picture it came from. `_end_screen_earliest` is the first pixel match (`qorcap_reader.py:1028-1029` and `:1368-1369`). A non-matching frame clears `_pixel_match_since` only (`:1042-1043`). The comment at `:919-920` keeps that earliest match until the next scorebug read, so a one-frame gap cannot move the cutoff forward. `_post_agreed` sets the pointer to null (`:1343-1344`). `end_screen_evidence_cutoff_ms` (`:1710-1733`) then takes the earliest of the menu frame, that pointer, and every `final_screen` row on the timeline. Clearing the pointer does not remove those rows. An early pause or flash stays the cutoff for the rest of the match. A one-step that needed input, a haptic, or a clip after that old time fails the filter at `:1703-1704` and stays unconfirmed. That is an extra PARTIAL.

v1 keeps the one-frame gap inside an episode. It does not keep the whole-capture minimum.

An episode opens at an end screen. It stays open across later end screens of the same absence, including a one-frame hole and an `ok: false` frame. The confirm streak can reset. `E0` does not move forward. A non-end-screen frame between two end menus starts a new picture for the agreement rule in §4.4. It does not by itself close the episode. Only a lock in the closing set can close an episode. That set is the longest chain when there is one, and the locks that sit on every longest chain when there are several (§4.5). A lock outside that set cannot close. A lock in the set closes when its raw `clock_s` is an integer greater than 0 and at least one end screen in the open episode has `t_ms` strictly less than the lock. `decide` reads that raw clock, not a clock copied from an earlier row. A lock with `clock_s` 0 or null does not close the episode. Screens strictly before the lock belong to the closed episode. A screen whose `t_ms` equals the lock opens the new episode and is that episode's `E0`. `equal-ts-opens-new-episode` is a flash at 4000, a touchdown lock at 8000 with raw clock 0:08, and a menu frame at that same 8000. The cutoff stays 8000, and evidence at 8250 does not count. `qorcap-98-touchdown-lock-same-ts` has no screen strictly before the lock, so nothing closes and the shared timestamp stays the open episode.

`episode_e0_ms` is the earliest end screen of the episode that is still open, including a screen at the closing lock's timestamp. When the close has no end screen at or after it, `episode_e0_ms` is null and `cutoff_ms` is null. The cutoff does not fall back to the previous episode's first screen. `cutoff_reason` is then `closed_episodes_only` if any end screen existed, and `no_end_screen` if none did.

Cutoff, computed by `decide`:

* Let `E0` be `episode_e0_ms`. Let `S` be the latest lock's `t_ms`, including a lock that was not allowed to close. When `locks` is empty there is no `S`, and `cutoff_ms` is `E0`.
* When the open episode has an `E0` and `S` is present, the cutoff is the earliest end screen of that episode strictly after `S`. When none of that episode's screens are strictly after `S`, the cutoff falls back to that episode's `E0`, not to an earlier episode. `cutoff_reason` is `open_episode`, `cutoff_ms` is an integer, and `episode_e0_ms` is that same integer `E0`.
* `cutoff_reason` `no_end_screen`: the capture has no end screen. `cutoff_ms` is null and no evidence row is counted. Today's `end_screen_evidence_cutoff_ms` (`qorcap_reader.py:1710-1733`) returns null whenever its inputs are missing, and the filter then skips (`:1703`), so late evidence counts. v1 does not use that skip.
* `cutoff_reason` `closed_episodes_only`: every end-screen episode was closed. `cutoff_ms` is null and `episode_e0_ms` is null. A row outside every closed band counts. A row inside one does not. The null does not mean "count nothing".
* When `cutoff_ms` is an integer, a row is counted only when `t_ms < cutoff_ms`. `t_ms >= cutoff_ms` is not counted. The comparison is inclusive of the equal timestamp. The code already uses `>=` at `:1704`. The contract keeps that operator.
* A row with `E0 <= t_ms <= S` is never counted, when both exist, including when that timestamp is below `cutoff_ms`. That interval is the open episode's first end screen through the latest scorebug read, both ends included.
* A closed episode adds its own band: its first end screen (strictly before the closing lock) through that lock, both ends included. A row in that band does not count. The screen at the lock's timestamp is not in that episode. It is the next episode's `E0`, and a row at that timestamp is still inside the closed band. A row before every episode counts when the reason is `closed_episodes_only`, or when it is also below the open cutoff.

`ReaderCore._end_menu`, `tail_final_check`, offline `finalize`, and `reread_final` all call it. `end_menu_fallback` stops being a second rule. The rejection string is an output of a failed gate, not a switch that selects a looser rule.

The qorcap client tail and the qortime referee re-read both stop seeding or requiring the live-timeline clock.

The client tail seeds it today. `tail_final_check` copies `timeline.jsonl` onto `core.timeline` (`qorcap_match.py:2495`) before `on_frame`. That copy is why `_clock_ready_for_end_menu` sees Q4 0:11 on 135645. The assignment goes away. The lock rows are passed to `decide` as `locks`. They are not written onto the reader timeline first.

The referee re-read already refuses the seed (`reread_final`, `media.py:181`, "No laptop timeline seed") and then requires the clock by another door. An unseeded `ReaderCore` emits `no Q4 scorebug read before the end-of-game menu` (`qorcap_reader.py:1515-1517`), and `end_menu_fallback` accepts only that exact string (`media.py:205`, `:250-251`). A clock rejection is refused. Locks are applied after the fact and `clock_s` is ignored (`media.py:275-284`). v1 does not keep that door. `reread_final` still does not seed `core.timeline`. It also stops treating "no Q4 row in this tail" as the condition that confirms. Locks from `timeline.jsonl` go to `decide` as `locks` only.

Removing the seed and leaving the quarter block in `_end_menu` would still reject an unseeded 135645. Both the clock block and the prior-Q4 block leave the confirm path.

### 4.7 Open decision: ties and a final of 1

The owner has not signed off on removing these guards. They stay fail-closed, inside `decide`.

* A tied menu score (`away == home`) is not adopted. The check at `qorcap_reader.py:1536-1538` moves into `decide`.
* A menu with either side equal to 1 is not adopted. The check at `media.py:264-265` moves into `decide`.

`_end_menu` and the removed `end_menu_fallback` do not keep a second copy. The posted result is the unconfirmed chain score, `final_reread` `partial`, or `fail` when no chain posts a score. Deleting either guard is a separate decision. This design does not delete them.

## 5. Code changes

Plan only. This change does not edit these files.

| Where | What changes |
|---|---|
| New `qor_receipt/final_rule.py` | `decide` as in §4.6. QorCap imports it. One copy. |
| `sidecar/qorcap_reader.py` `madden_final_screen` `:819-855` | Keep the geometry. Delete the `:833` sentence that the detector only runs behind the Q4-low-clock gate. |
| `sidecar/qorcap_reader.py` `on_frame` `:1025-1036` | Keep the OPTIONS short-hold return that happens before `_end_menu`. |
| `sidecar/qorcap_reader.py` `_end_context` `:970-975`, `_accept_final_screen` `:999-1010` | Stop using them as a precondition for `_end_menu`. They require quarter and clock today. |
| `sidecar/qorcap_reader.py` `_end_menu` `:1511-1538` | Remove the quarter rejection (`:1515-1517`), the clock rejection (`:1529-1530`), and the `end_menu_consistency` refusal (`:1532-1535`). The tie refusal (`:1536-1538`) and the final-of-1 refusal (`media.py:264-265`) move into `decide`. `_end_menu` does not keep a second copy. §4.7 is still an open decision; the guards stay. Call `decide`. |
| `sidecar/qorcap_endmenu.py` `EndMenuAgreement.offer` `:242-261` | Track `min_conf`. Every frame in the run has `conf` ≥ 0.90. A lower frame clears the run. `n` stays 3. `min_span_ms` stays 400. A scorebug clears an incomplete streak. A run that already met the stable gate is kept and is still offered. A frame at a lock's timestamp is not offered. |
| `sidecar/qorcap_reader.py` `end_menu_consistency` `:1646-1657` | No longer a refusal. The delta feeds the sidecar note. `end_menu_delta_corroborated` (`:1660-1707`) is not required to confirm. |
| `sidecar/qorcap_reader.py` `_note_end_menu_disagreement` `:1576-1595` | Two stable scores disagree: keep the first score, `end_menu_disputed` true, `final_confirmed` false, review `partial`, never VERIFIED. The note matches qorcap #102, including the #100 phrase when a scorebug row lies strictly between the two runs. A later readable frame at or above 0.70 that is not itself a stable run, and a new picture with no stable run, still post neither menu. A tie of longest chains with no dispute posts null. |
| `sidecar/qorcap_match.py` `tail_final_check` `:2495` | Stop `core.timeline = live`. Pass those rows to `decide` as locks. Load OPTIONS with `load_options_presses_ms`, not `press_events`. Do not break at the first `end_menu` (`:2520-2521`); scan the window. |
| `sidecar/qorcap_match.py` `apply_final` `:2094-2096` | A missing `final_confirmed` is false. |
| `sidecar/qorcap_match.py` `select_offline_final` `:2431-2445`, `_disputed_final_result` `:2457-2464`, `finalize` `:2887` | Same `decide`. A dispute signs the first stable menu, unconfirmed, with `end_menu_disputed` true. A tie of longest chains with no dispute signs null. Offline finalize does not reintroduce a timeline clock seed. |
| `qor_receipt/review/media.py` `reread_final` `:179-201` | Keep "no laptop timeline seed". Do not break at the first `end_menu` (`:190-191`). Delete the `end_menu_fallback` call. Load OPTIONS with `load_options_presses_ms`, not `bundle.press_events`. Null fails a 6-item frame closed. `picture_since_ms` is required only when a measured press falls in `(t - 16000, t + 500]`. |
| `qor_receipt/review/media.py` `end_menu_fallback` `:236-298` | Remove. The reason switch at `:250` and the thresholds at `:205-207` are not the rule. `clock_s` is not read. A prior Q4 row is not required. |
| `qor_receipt/review/engine.py` `_final_check` `:799-828` | A confirmed menu is `pass` when the claim equals the menu score, including a stall and a multi-play delta. A failed gate with a chain score is `partial` at that score. `end_menu_disputed` true is `partial` even when the claim equals the first score, and that review cannot become VERIFIED. A tie of longest chains is `fail`. A note does not flip `pass` to `partial`. Score mismatch stays `fail` (`:807-809`). |
| `qor_receipt/review/engine.py` `_result_from_reread` `:831-847` | Write `decide`'s boolean. The `:840` test `is not False` becomes that boolean. Missing is false. |
| `qor_receipt/referee_ai/integrity.py` `check_final` `:189-202` | An adopted end menu more than one legal step past the last scorebug is not an error. `:201` is that error today, and `referee.py:184` and `:189` both append it. Leaving it in place would FAIL a multi-play menu and downgrade the verdict. `decide` does not adopt a menu below any lock. A dispute is not an adoption: the posted score is the first menu, and that comparison is not an integrity error that flips the review to fail. The review stays `partial`. An unconfirmed chain posts that chain's lock, and that lock is the `last_state` `check_final` sees. A tie posts no score, so there is no `last_state` to mismatch. A backward menu posted outside a dispute still errors. |
| `sidecar/qorcap_reader.py` `end_screen_evidence_cutoff_ms` `:1710-1733`, filter `:1703-1704` | Replaced by `decide`'s cutoff. The minimum is no longer the first end screen of the whole timeline. Only a lock in the closing set with a running clock closes an episode, and only the screens strictly before it. A screen at the lock's timestamp opens the next episode. An `ok: false` six-item picture is still an end screen. The cutoff does not fall back to an earlier episode. `cutoff_reason` distinguishes a null that counts nothing from a null that counts outside closed episodes. The test stays `t_ms >= cutoff_ms`. | 
| `sidecar/qorcap_reader.py` `_post_agreed` `:1343-1344` | Still clears the running `_end_screen_earliest` pointer. A later scorebug clears an incomplete streak. A completed stable run is kept. Clearing the pointer does not remove `final_screen` rows, so it is not the episode rule. `decide` derives the episode from locks and end screens. | 
| `qor_receipt/match_index/build.py`, `viewer_template.html` `paintFinal`, `publish.py` `_card` | Read `final.rule.json` and show `note` as in §4.5. Gold stays. |

`post_final_tail` is unchanged.

## 6. Published matches

The QorTime site lists 3 published matches (`site/matches/index.html`, "3 published"; pages `170123`, `021003`, `125430`). This design does not re-review them. A frozen receipt stays the page until a new official review is signed. Merging the rule does not upgrade any of them to a new status and does not rewrite the page.

Stored facts, and what v1 can and cannot conclude from them:

| Match | What is stored | What v1 can conclude | Page after this design |
|---|---|---|---|
| `local-20261023-170123` | VERIFIED. Final away 10, home 0, `end_menu_read`, `final_confirmed` true, `final_reread` pass. One end-menu object: 3 frames, span 838 ms (`1792793932982 − 1792793932144`), one `conf_permille` 912, margin 229, template `real`, delta `exact_match`. `scorebug_last.clock_s` is null. Lock `t_ms` `1792793907322`, menu `t_ms` `1792793932144`, wall gap 24822 ms. No second menu object and no post-menu scorebug row are in the page payload. | The every-frame confidence gate is unverifiable: 912 is one stored number (§2.1, the stabilizing frame). The other two frames are not stored and could sit between the 0.70 glyph floor and 0.90. A whole-window scan for a later menu or a later scorebug is also unverifiable: the page does not contain that scan. A null raw clock and a 24822 ms gap would fire the stall note if a re-review ran. | Unchanged. The frozen VERIFIED 10–0 page stays. No stall note is added, because there is no re-review. |
| `local-20261017-021003` | SELF_ATTESTED. Final away 22, home 41, `end_menu_read`, `final_confirmed` true. Partials: `referee_key_not_pinned`, `referee_not_on_allowlist`, `referee_local_self_attested`, missing kickoff, halftime, and end challenges, `away:motion_signal_missing`. The page says the v0.3.10 receipt read 0–0 and the end-menu frame plus the v0.3.11 replay reads 22–41. | Those partials are outside this rule. The final-score rule does not produce SELF_ATTESTED. | Unchanged. |
| `local-20261016-125430` | FAIL. Errors: `home:cp[0]:gap_too_long`, `home:cp[591]:gap_too_long`, `home:cp[591]:clock_skew`, `home:cp[592]:clock_skew`, plus the local-key and missing-challenge partials. Score away 7, home 14, `last_scorebug_read_unconfirmed`, `final` null. End basis stored on the page: "scorebug disappeared after last read; end-of-game menu NOT read by the engine (bundle.final is null)". | The FAIL is the checkpoint chain. v1 does not invent a menu from a null `bundle.final`. | Unchanged. |

One real lock plus one stale lower lock moves from partial to fail. The old running-maximum walk posted the higher lock and the review was partial. `replay-lock-lower-after-menu` is 14–7 and then 3–0. `menu-below-an-earlier-lock` is 21–0 and then 14–0. Each lock is its own longest chain, the terminal scores differ, the receipt score is null, and `final_reread` is fail. The stable 14–7 menu is kept across the later lock and is still not adopted, because those chains do not agree on a score.

`match-20261024-135645` (qorcap#94, submission `f26ac3935d038288`) is a production submission. It is not on the site. The owner's held-out set is 170123, 021003, 125430, 084523, and 193534. The issue says the client tail rejected the menu because the clock was not 0:00, the last lock was Q4 0:11, and the referee fallback accepted 10–7 from one reported confidence of 0.912 over 485 frames. Per-frame confidences and the three frame timestamps are not in that report. The vector in §7 is labeled synthetic and uses 0.94. Under the rule, a stable end menu after that lock confirms away 10, home 7, with a stall note for 11 unread seconds, and the note does not downgrade a review that is actually run. This design does not re-review the submission.

## 7. Fixtures and tests (plan only)

This change adds no fixture file and no test. When `decide` is implemented, the same JSON bytes go to:

| Repo | File | Runner |
|---|---|---|
| QorTime | `tests/vectors/final_rule/v1.json` | `tests/test_final_rule.py`, calling `qor_receipt.final_rule.decide`, on the existing `pytest -q` job |
| QorCap | `sidecar/fixtures/final_rule/v1.json` | `sidecar/test_final_rule.py`, importing that same function, on `python -m pytest sidecar` |

A hash mismatch fails the test. The two pause cases also have pixel fixtures beside that JSON: `_menu(items=8)` and the OPTIONS short-hold on `_menu(items=6)` from `sidecar/test_final_screen_pause.py`. The 6-item picture alone must not be the pause case. `madden_final_screen` accepts it. In the JSON, `screen` is expanded onto each frame as `pixel` before `decide`. `pause-options-short-hold` keeps `pixel` `end_menu_6` and lists the OPTIONS press. `decide` labels the frame pause because the hold is under 12 s. A vector with no `options_presses_ms` and no `inputs` is the well-formed `[]` on every frame: that frame's window passed qorcap #97's Final rule, and OPTIONS was not pressed. `options_presses_ms: null` fails every frame closed. A list of JSON integers, including `[]`, is one shared list for every frame. A list of length `len(frames)` whose elements are each null or a list of integers is per-frame. A bool is not an integer. Any other length or type fails closed. A vector that carries `inputs` derives the per-frame result with `load_options_presses_ms` before `decide`, and `options_presses_ms` is not a second field. There is no `options_hold_ms` field.

`screen` is test sugar. `decide` does not take it. The expansion is:

| JSON `screen` | `pixel` |
|---|---|
| `end_menu` | `end_menu_6` |
| `pause` | `pause_8` |
| `halftime` | `faded_field` |
| `replay` | `replay` |

A frame that already has `pixel` keeps it. `pixel_on_frames`, when a vector sets it, wins over `screen`. `decide` does not take `screen`, `later`, `later_menu`, `later_live_play`, or `end_screens`.

`today` on the stall case is the drift at the commits in the header. `expect` is `final-rule/v1`. The stall case is synthetic (`"synthetic": true`): issue 94 did not publish per-frame confidence or the three frame timestamps, so the vector uses 0.94 and invented times. It does not copy 170123's 10–0 or its stored 0.912. v1 vectors do not carry `LOCKS_084523` or any row from the owner's held-out set (170123, 021003, 125430, 084523, 193534). The `LOCKS_084523` citation in §1.1 is the current referee test, not a v1 input. Review `pass` means the claim equals `decide`'s score and the check does not drop to `partial` because of the note. Every end screen in a vector is a frame. A listed `end_screens` time is not an input.

```json
{
  "rule": "final-rule/v1",
  "agree_frames": 3,
  "agree_span_ms": 400,
  "min_conf_each_frame": 0.9,
  "readable_floor": 0.7,
  "legal_steps": [
    1,
    2,
    3,
    6,
    7,
    8
  ],
  "cases": [
    {
      "id": "135645-stall",
      "source": "match-20261024-135645 / qorcap#94",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 800000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 11,
          "clock_red": false
        }
      ],
      "frames": [
        {
          "t_ms": 900000,
          "away": 10,
          "home": 7,
          "conf": 0.94,
          "ok": true
        },
        {
          "t_ms": 900500,
          "away": 10,
          "home": 7,
          "conf": 0.94,
          "ok": true
        },
        {
          "t_ms": 901000,
          "away": 10,
          "home": 7,
          "conf": 0.94,
          "ok": true
        }
      ],
      "reported_frames": 485,
      "today": {
        "qorcap_tail_seeded": "scorebug clock not 0:00 before the end-of-game menu",
        "qortime_referee_unseeded_fallback": "accepted 10-7, final_confirmed true, review pass"
      },
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 10,
        "home": 7,
        "method": "end_menu_read",
        "review": "pass",
        "note": "End menu confirmed 10–7. Scorebug last locked at Q4 0:11 (11 seconds unread).",
        "seconds_unread": 11,
        "last_locked": {
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 11
        },
        "downgrades_verdict": false
      }
    },
    {
      "id": "pause-8-item",
      "screen": "pause",
      "pixel_fixture": "test_final_screen_pause._menu(items=8)",
      "locks": [
        {
          "t_ms": 1000,
          "away": 7,
          "home": 0,
          "quarter": 2,
          "clock_s": 120
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 7,
          "home": 0,
          "conf": 0.99,
          "ok": true
        },
        {
          "t_ms": 5500,
          "away": 7,
          "home": 0,
          "conf": 0.99,
          "ok": true
        },
        {
          "t_ms": 6000,
          "away": 7,
          "home": 0,
          "conf": 0.99,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 7,
        "home": 0,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "pause-options-short-hold",
      "screen": "end_menu",
      "pixel_fixture": "test_final_screen_pause._menu(items=6), pixel end_menu_6, picture_since_ms 1000, OPTIONS press at 800, hold 4000 ms",
      "locks": [
        {
          "t_ms": 500,
          "away": 14,
          "home": 7,
          "quarter": 4,
          "clock_s": 40
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 14,
          "home": 7,
          "conf": 0.99,
          "ok": true,
          "pixel": "end_menu_6",
          "picture_since_ms": 1000
        },
        {
          "t_ms": 5500,
          "away": 14,
          "home": 7,
          "conf": 0.99,
          "ok": true,
          "pixel": "end_menu_6",
          "picture_since_ms": 1000
        },
        {
          "t_ms": 6000,
          "away": 14,
          "home": 7,
          "conf": 0.99,
          "ok": true,
          "pixel": "end_menu_6",
          "picture_since_ms": 1000
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 14,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      },
      "options_presses_ms": [
        800
      ],
      "illustrates": "The picture is the six-item shape. The lock at 500 is still up at the OPTIONS press at 800. picture_since_ms is 1000, so the hold at 5000 ms is 4000 ms, under 12000. decide labels the frame pause."
    },
    {
      "id": "halftime-faded-field",
      "screen": "halftime",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 2,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "replay-not-end-menu",
      "screen": "replay",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 40
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "exact-at-0",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 6,
        "home": 3,
        "method": "end_menu_read",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false
      }
    },
    {
      "id": "one-legal-step",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 7,
          "home": 0,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 14,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 14,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 14,
          "home": 0,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 14,
        "home": 0,
        "method": "end_menu_read",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false
      }
    },
    {
      "id": "multi-play-menu-wins",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 7,
          "home": 0,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 17,
          "home": 14,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 17,
          "home": 14,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 17,
          "home": 14,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 17,
        "home": 14,
        "method": "end_menu_read",
        "review": "pass",
        "disagreement": {
          "delta": [
            10,
            14
          ],
          "more_than_one_legal_play": true
        },
        "note": "End menu confirmed 17–14. Last locked read was 7–0, more than one scoring play apart.",
        "downgrades_verdict": false
      }
    },
    {
      "id": "no-scorebug-in-tail",
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 10,
        "home": 7,
        "method": "end_menu_read",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false
      }
    },
    {
      "id": "conf-below-0-90-on-one-frame",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 7,
          "conf": 0.89,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "two-frames",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5600,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "span-under-400",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5100,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5399,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "frames-disagree",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 10,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "later-contradicting-menu",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9000,
          "away": 17,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9400,
          "away": 17,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9800,
          "away": 17,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "end_menu_read",
        "review": "partial",
        "note": "later end-of-game menu read AWAY 17 - HOME 7 disagreed with the posted final AWAY 10 - HOME 7; score not replaced; final_confirmed=false",
        "final_basis": "end_menu",
        "end_menu_disputed": true,
        "downgrades_verdict": false
      },
      "illustrates": "The second score is in frames, after the first stable run. The two stable reads disagree, so the receipt keeps 10–7, the first menu. end_menu_disputed is true. method is end_menu_read. final_basis is end_menu. The lock is before both runs, so the note does not add the scorebug phrase."
    },
    {
      "id": "later-live-play",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        },
        {
          "t_ms": 9000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 45
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 13,
        "home": 6,
        "method": "end_menu_read",
        "review": "pass",
        "note": "End menu confirmed 13–6. Scorebug last locked at Q4 0:45 (45 seconds unread).",
        "cutoff_ms": null,
        "final_basis": "end_menu",
        "episode_e0_ms": null,
        "evidence_counts": [
          "before-episode",
          "after-close"
        ],
        "cutoff_reason": "closed_episodes_only",
        "end_menu_disputed": false,
        "downgrades_verdict": false,
        "seconds_unread": 45,
        "wall_gap_ms": -4000
      },
      "illustrates": "The 13–6 run already met the stable gate, so the clock-45 lock at 9000 does not clear it. The menu confirms 13–6 and the stall note names Q4 0:45. That lock still closes the episode. cutoff_ms is null and cutoff_reason is closed_episodes_only. Evidence before the episode and evidence after the closing lock count. Evidence inside the closed band does not.",
      "evidence": [
        {
          "id": "before-episode",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "inside-closed",
          "t_ms": 5500,
          "kind": "input_activity"
        },
        {
          "id": "after-close",
          "t_ms": 9500,
          "kind": "input_activity"
        }
      ]
    },
    {
      "id": "score-went-down",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 14,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 14,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "covered-panel",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.55,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 7,
          "conf": 0.55,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 7,
          "conf": 0.55,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "gates-fail-no-lock",
      "screen": "pause",
      "locks": [],
      "frames": [],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": null,
        "home": null,
        "method": "last_scorebug_read_unconfirmed",
        "review": "fail",
        "note": null,
        "cutoff_ms": null,
        "evidence_counts": [],
        "cutoff_reason": "no_end_screen"
      },
      "evidence": [
        {
          "id": "during-pause",
          "t_ms": 5000,
          "kind": "input_activity"
        }
      ],
      "illustrates": "No end screen. cutoff_ms is null and cutoff_reason is no_end_screen. The evidence row does not count. That null is not the closed-episodes null."
    },
    {
      "id": "scorebug-return-resets-agreed-score",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 10000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 2000,
          "away": 3,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 3000,
          "away": 3,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 3500,
          "away": 3,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 4000,
          "away": 3,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 21000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 3,
        "home": 0,
        "method": "end_menu_read",
        "review": "partial",
        "note": "later end-of-game menu read AWAY 10 - HOME 7 disagreed with the posted final AWAY 3 - HOME 0 after a scorebug read came back between the two menus; score not replaced; final_confirmed=false",
        "cutoff_ms": 20000,
        "downgrades_verdict": false,
        "cutoff_reason": "open_episode",
        "final_basis": "end_menu",
        "end_menu_disputed": true,
        "episode_e0_ms": 2000
      },
      "illustrates": "A stable 3–0, then a 0:00 lock at 10–7, then a stable 10–7. The lock sits strictly between the two runs, so this is a dispute that keeps 3–0. The note says a scorebug read came back between the two menus. The cutoff is the first frame of the later run."
    },
    {
      "id": "menu-frame-equal-to-scorebug-not-fresh",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null,
        "cutoff_ms": 5400,
        "evidence_counts": [
          "between-lock-and-next-screen"
        ],
        "final_basis": "absent",
        "cutoff_reason": "open_episode"
      },
      "evidence": [
        {
          "id": "at-lock-and-menu",
          "t_ms": 5000,
          "kind": "input_activity"
        },
        {
          "id": "between-lock-and-next-screen",
          "t_ms": 5200,
          "kind": "input_activity"
        },
        {
          "id": "at-cutoff",
          "t_ms": 5400,
          "kind": "input_activity"
        }
      ],
      "illustrates": "Every readable menu frame is an end screen, including 5400 and 5800. The lock is at 5000, so the cutoff is 5400. The frame at 5000 is not fresh. Evidence at 5200 counts. Evidence at 5000 and at 5400 does not."
    },
    {
      "id": "cutoff-dead-zone-and-inclusive-equal",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 10000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 2000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 21000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-first-screen",
          "t_ms": 1500,
          "kind": "input_activity"
        },
        {
          "id": "at-first-screen",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "between-screen-and-scorebug",
          "t_ms": 5000,
          "kind": "haptic"
        },
        {
          "id": "at-scorebug",
          "t_ms": 10000,
          "kind": "score_change_clip"
        },
        {
          "id": "after-scorebug-before-menu",
          "t_ms": 12000,
          "kind": "input_activity"
        },
        {
          "id": "at-fresh-cutoff",
          "t_ms": 20000,
          "kind": "input_activity"
        },
        {
          "id": "after-cutoff",
          "t_ms": 21000,
          "kind": "haptic"
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 10,
        "home": 7,
        "method": "end_menu_read",
        "review": "pass",
        "note": null,
        "cutoff_ms": 20000,
        "evidence_counts": [
          "before-first-screen",
          "after-scorebug-before-menu"
        ],
        "downgrades_verdict": false,
        "cutoff_reason": "open_episode"
      },
      "illustrates": "The screen at 2000 is a frame, same score as the final menu, and the 0:00 lock does not close the episode. Evidence from that frame through the lock does not count. Evidence before it, and evidence strictly between the lock and the fresh menu, does."
    },
    {
      "id": "cutoff-falls-back-to-earliest-end-screen",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 10000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 2000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 3000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 3500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 4000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-first-screen",
          "t_ms": 1500,
          "kind": "input_activity"
        },
        {
          "id": "at-first-screen",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "after-scorebug",
          "t_ms": 12000,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 10,
        "home": 7,
        "method": "end_menu_read",
        "review": "pass",
        "note": null,
        "cutoff_ms": 2000,
        "evidence_counts": [
          "before-first-screen"
        ],
        "cutoff_reason": "open_episode",
        "final_basis": "end_menu",
        "end_menu_disputed": false,
        "downgrades_verdict": false,
        "episode_e0_ms": 2000
      },
      "illustrates": "The screen at 2000 is a stable 10–7 run, and the 0:00 lock matches it, so the menu confirms. The lock does not close the episode. No end screen is after the lock, so the cutoff stays 2000."
    },
    {
      "id": "qorcap-98-touchdown-lock-same-ts",
      "source": "qorcap#98",
      "synthetic": true,
      "illustrates": "A touchdown lock is flushed at the same timestamp as a menu frame. Evidence is at that timestamp and 250 ms later. No end screen is strictly after the lock, so the cutoff falls back to 8000. Neither evidence row counts. The frame is not fresh. The case does not confirm.",
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 8000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 8
        }
      ],
      "frames": [
        {
          "t_ms": 8000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "at-T",
          "t_ms": 8000,
          "kind": "input_activity"
        },
        {
          "id": "T-plus-250",
          "t_ms": 8250,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": 8000,
        "evidence_counts": [],
        "posted_score": {
          "away": 13,
          "home": 6
        },
        "cutoff_reason": "open_episode"
      }
    },
    {
      "id": "duplicate-timestamps",
      "screen": "end_menu",
      "illustrates": "Rows 5000, 5000, 5400 are two instants. A duplicate adds no span and no frame.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 9,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5000,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 9,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "backwards-timestamp",
      "screen": "end_menu",
      "illustrates": "Frames are listed 5000, 5800, 5400. Sorting them into 5000, 5400, 5800 would be a stable run. A backwards step fails the decision closed.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 9,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 9,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "unreadable-frame-resets-run",
      "screen": "end_menu",
      "illustrates": "ok false resets the streak. The frame before it is dropped. Two frames after it do not confirm. Bridging across the unreadable frame would be three frames and must not confirm.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 8,
          "home": 5,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": false
        },
        {
          "t_ms": 5800,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6200,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 8,
        "home": 5,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "unreadable-then-new-run-confirms",
      "screen": "end_menu",
      "illustrates": "The same reset leaves a new run. Three fresh frames after ok false confirm. An unreadable frame does not fail the rest of the decision.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 8,
          "home": 5,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": false
        },
        {
          "t_ms": 6000,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6400,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6800,
          "away": 8,
          "home": 5,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 8,
        "home": 5,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false
      }
    },
    {
      "id": "conf-0-75-resets-run",
      "screen": "end_menu",
      "illustrates": "Confidence 0.75 is readable and below 0.90. It resets the streak. Skipping it and keeping the earlier frame would be three frames and must not confirm.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 4,
          "home": 3,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 4,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 4,
          "home": 3,
          "conf": 0.75,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 4,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6200,
          "away": 4,
          "home": 3,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 4,
        "home": 3,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "conf-0-75-then-new-run-confirms",
      "screen": "end_menu",
      "illustrates": "Three frames at 0.95 after the 0.75 frame are a new run and confirm.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 4,
          "home": 3,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 4,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 4,
          "home": 3,
          "conf": 0.75,
          "ok": true
        },
        {
          "t_ms": 6000,
          "away": 4,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6400,
          "away": 4,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6800,
          "away": 4,
          "home": 3,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 4,
        "home": 3,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false
      }
    },
    {
      "id": "replay-lock-lower-after-menu",
      "screen": "end_menu",
      "illustrates": "14–7 and then 3–0. Each lock is a longest chain of length 1 and the terminal scores differ, so the receipt score is null. Neither lock sits on both chains, so the running clock does not close the episode. The episode stays open at the menu's first frame, 5000. Evidence after the stale lock does not count.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 14,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        },
        {
          "t_ms": 12000,
          "away": 3,
          "home": 0,
          "quarter": 4,
          "clock_s": 30
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 14,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 14,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 14,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": null,
        "home": null,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "fail",
        "note": null,
        "posted_score": null,
        "backwards_lock": null,
        "cutoff_ms": 5000,
        "episode_e0_ms": 5000,
        "evidence_counts": [
          "before-menu"
        ],
        "cutoff_reason": "open_episode"
      },
      "evidence": [
        {
          "id": "before-menu",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "after-stale-lock",
          "t_ms": 13000,
          "kind": "input_activity"
        }
      ]
    },
    {
      "id": "menu-below-an-earlier-lock",
      "screen": "end_menu",
      "illustrates": "The menu equals the later lock and is below the earlier lock, so it is not adopted. 21–0 and 14–0 are each a longest chain of length 1. The terminal scores differ, so the receipt score is null, not 21–0 and not 14–0.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 21,
          "home": 0,
          "quarter": 4,
          "clock_s": 0
        },
        {
          "t_ms": 2000,
          "away": 14,
          "home": 0,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 14,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 14,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 14,
          "home": 0,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": null,
        "home": null,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "fail",
        "note": null,
        "posted_score": null,
        "backwards_lock": null
      }
    },
    {
      "id": "tie-menu-stays-unconfirmed",
      "screen": "end_menu",
      "illustrates": "A tied menu stays refused. Open decision, §4.7. The owner has not signed off on removing the guard.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 6,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 6,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 6,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 6,
        "home": 3,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "final-of-1-stays-unconfirmed",
      "screen": "end_menu",
      "illustrates": "A menu with either side equal to 1 stays refused. Open decision, §4.7.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 0,
          "home": 0,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 1,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 1,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 1,
          "home": 0,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 0,
        "home": 0,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null
      }
    },
    {
      "id": "null-clock-long-gap",
      "synthetic": true,
      "screen": "end_menu",
      "illustrates": "decide reads the raw clock_s on the lock. Null stays null. A null clock and a wall gap of 19000 ms, at least 10000, fires the stall note. The note does not downgrade the verdict.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 4,
          "clock_s": null
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20500,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 21000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 6,
        "home": 3,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": "End menu confirmed 6–3. Scorebug last locked at Q4 with no clock read (19000 ms unread on the wall clock).",
        "seconds_unread": null,
        "wall_gap_ms": 19000,
        "downgrades_verdict": false
      }
    },
    {
      "id": "no-lock-contradicting-menus",
      "screen": "end_menu",
      "illustrates": "No lock, and two stable menus that disagree. The receipt keeps 9–6, the first menu, with end_menu_disputed true. method is end_menu_read. final_basis is end_menu. final_confirmed is false. final_reread is partial.",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9000,
          "away": 16,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9400,
          "away": 16,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9800,
          "away": 16,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 9,
        "home": 6,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "partial",
        "note": "later end-of-game menu read AWAY 16 - HOME 6 disagreed with the posted final AWAY 9 - HOME 6; score not replaced; final_confirmed=false",
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000,
        "end_menu_disputed": true,
        "downgrades_verdict": false
      }
    },
    {
      "id": "referee-cannot-measure-hold",
      "screen": "end_menu",
      "options_presses_ms": null,
      "illustrates": "options_presses_ms is null: inputs.jsonl was not read. decide fails the 6-item frames closed. Official review is partial at the lock. Null presses are what fail them. A missing picture_since_ms does not, unless a measured press falls in the window.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 9,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 5000,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "pixel": "end_menu_6"
        },
        {
          "t_ms": 5400,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "pixel": "end_menu_6"
        },
        {
          "t_ms": 5800,
          "away": 9,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "pixel": "end_menu_6"
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 9,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen"
      }
    },
    {
      "id": "early-pause-then-final-menu",
      "synthetic": true,
      "screen": "end_menu",
      "illustrates": "A pause-like flash at 4000 is one end-screen frame, not a stable read. Live play resumes at 15000 with a running clock, which closes that episode. The real menu is at 22000. Evidence after the resume counts. Evidence from the flash through the resume lock does not. A cutoff stuck at 4000 would drop the later evidence and is the whole-capture bug.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 2,
          "clock_s": 120
        },
        {
          "t_ms": 15000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 35
        },
        {
          "t_ms": 18000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 4000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 22000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 22500,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 23000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-pause",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "at-pause",
          "t_ms": 4000,
          "kind": "input_activity"
        },
        {
          "id": "between-pause-and-resume",
          "t_ms": 8000,
          "kind": "haptic"
        },
        {
          "id": "at-resume",
          "t_ms": 15000,
          "kind": "input_activity"
        },
        {
          "id": "after-resume-before-zero",
          "t_ms": 16000,
          "kind": "input_activity"
        },
        {
          "id": "after-zero-before-menu",
          "t_ms": 19000,
          "kind": "haptic"
        },
        {
          "id": "at-menu",
          "t_ms": 22000,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 13,
        "home": 6,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false,
        "cutoff_ms": 22000,
        "episode_e0_ms": 22000,
        "evidence_counts": [
          "before-pause",
          "after-resume-before-zero",
          "after-zero-before-menu"
        ],
        "cutoff_reason": "open_episode"
      }
    },
    {
      "id": "equal-ts-opens-new-episode",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 8000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 8
        }
      ],
      "frames": [
        {
          "t_ms": 4000,
          "away": 4,
          "home": 2,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 8000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-flash",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "at-flash",
          "t_ms": 4000,
          "kind": "input_activity"
        },
        {
          "id": "at-lock",
          "t_ms": 8000,
          "kind": "input_activity"
        },
        {
          "id": "while-menu-up",
          "t_ms": 8250,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "cutoff_ms": 8000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 8000,
        "evidence_counts": [
          "before-flash"
        ]
      },
      "illustrates": "A one-frame flash at 4000, then a touchdown lock at 8000 with raw clock 0:08, flushed at the same timestamp as a menu frame. Only the flash is strictly before the lock, so it is the closed episode. The menu frame at 8000 opens the new episode. cutoff_ms is 8000. Evidence at 8250, while that menu is up, does not count."
    },
    {
      "id": "later-readable-frame-blocks-confirm",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9000,
          "away": 13,
          "home": 6,
          "conf": 0.85,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": null,
        "home": null,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "fail",
        "note": null,
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000
      },
      "illustrates": "A stable 6–3 read, no later lock, then one end-menu frame at 13–6 with confidence 0.85. 0.85 is at or above the 0.70 readability floor and is not a second stable run. It blocks the confirm. Neither score is posted. Review is fail."
    },
    {
      "id": "later-frame-below-readability-floor",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 9000,
          "away": 13,
          "home": 6,
          "conf": 0.69,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 6,
        "home": 3,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false,
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000
      },
      "illustrates": "The same 6–3 run, then one frame at 13–6 with confidence 0.69. That is below the 0.70 readability floor, so it does not block the confirm. The frame is still an end screen for the cutoff."
    },
    {
      "id": "early-pause-then-final-menu-clock-zero",
      "synthetic": true,
      "screen": "end_menu",
      "illustrates": "Same flash and the same final menu as early-pause-then-final-menu, but the lock at 15000 is 0:00. A 0:00 lock does not close the episode, so the flash and the final menu stay one episode. episode_e0_ms is 4000. Evidence at 16000 is inside that episode's dead zone and does not count. The menu still confirms 13–6.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 2,
          "clock_s": 120
        },
        {
          "t_ms": 15000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        },
        {
          "t_ms": 18000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 4000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 22000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 22500,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 23000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-pause",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "at-pause",
          "t_ms": 4000,
          "kind": "input_activity"
        },
        {
          "id": "between-pause-and-resume",
          "t_ms": 8000,
          "kind": "haptic"
        },
        {
          "id": "at-resume",
          "t_ms": 15000,
          "kind": "input_activity"
        },
        {
          "id": "after-resume-before-zero",
          "t_ms": 16000,
          "kind": "input_activity"
        },
        {
          "id": "after-zero-before-menu",
          "t_ms": 19000,
          "kind": "haptic"
        },
        {
          "id": "at-menu",
          "t_ms": 22000,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 13,
        "home": 6,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false,
        "cutoff_ms": 22000,
        "episode_e0_ms": 4000,
        "evidence_counts": [
          "before-pause",
          "after-zero-before-menu"
        ],
        "cutoff_reason": "open_episode"
      }
    },
    {
      "id": "options-press-at-t-minus-16000-is-outside",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "Press at exactly t-16000 for the first frame (4000). That boundary is outside the window, so picture_since_ms is not required and the frames confirm. Treating the boundary as inside fails the first frame closed and leaves only two frames.",
      "options_presses_ms": [
        4000
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 13,
        "home": 6,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false,
        "cutoff_ms": 20000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 20000
      }
    },
    {
      "id": "options-press-at-t-minus-15999-is-pause",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "picture_since_ms": 8801
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "picture_since_ms": 8801
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "picture_since_ms": 8801
        }
      ],
      "illustrates": "Press at 4801, which is t-15999 for the last frame at 20800. picture_since_ms is 4000 ms later (8801). The hold on that frame is 11999 ms, under 12000, and the press is inside (t-16000, t+500]. All three frames are pauses. The menu is not adopted. The posted score is the lock.",
      "options_presses_ms": [
        4801
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 6,
        "home": 3,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null
      }
    },
    {
      "id": "options-press-at-t-plus-500-is-pause",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "picture_since_ms": 20000
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "picture_since_ms": 20000
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true,
          "picture_since_ms": 20000
        }
      ],
      "illustrates": "Press at exactly t+500 for the first frame (20500). picture_since_ms is 500 ms earlier (20000), the edge of the opened-by-OPTIONS test. The hold is under 12 s on every frame. The menu is not adopted.",
      "options_presses_ms": [
        20500
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 6,
        "home": 3,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null
      }
    },
    {
      "id": "inputs-no-wall-anchor",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "No header, so there is no integer wall_anchor_ms. The loader returns null. bundle.press_events would use anchor 0 and return []. decide fails closed. The 13–6 menu is not adopted.",
      "inputs": [
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null
      }
    },
    {
      "id": "inputs-wall-anchor-not-integer",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "The header is present and wall_anchor_ms is a string. That is not an integer. The loader returns null and the menu is not adopted.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": "0"
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null
      }
    },
    {
      "id": "inputs-pad-not-connected",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "The log parses. Chunks cover the frame windows, and the only status is unplugged, so every frame's entry is null. An empty press list is not inferred. The menu is not adopted.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "unplugged",
          "detail": "USB link dropped"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null
      }
    },
    {
      "id": "inputs-malformed-row",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "One input row has no b and another has no t_us. The loader returns null for any malformed input row. It does not skip the row and return [].",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "in",
          "t_us": 2000
        },
        {
          "type": "in",
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null
      }
    },
    {
      "id": "inputs-well-formed-no-options-press",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "Exactly one header, integer anchor 0, chunks every 3000 ms through the window end, and the pad stays connected until a stall 1 ms after the last frame's deadline + 2500 (23801). Each frame's entry is []. The menu confirms.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "status",
          "t_us": 23801000,
          "state": "stalled",
          "detail": "no HID reports"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 13,
        "home": 6,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false,
        "cutoff_ms": 20000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 20000
      }
    },
    {
      "id": "stale-decrease-keeps-later-lock",
      "synthetic": true,
      "screen": "end_menu",
      "illustrates": "early-pause-then-final-menu's locks plus a stale 3–3 at 8000, and no later stable menu. The 6–3 chain and the 3–3 chain are the same length and both end 13–6, so the posted score is 13–6. The 3–3 lock is not on every chain, so it does not close. The 13–6 lock at 15000 still closes the flash. Evidence at 16000 counts. A greedy stop at 3–3 would leave E0 at 4000, drop that evidence, and post 6–3.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 2,
          "clock_s": 120
        },
        {
          "t_ms": 8000,
          "away": 3,
          "home": 3,
          "quarter": 3,
          "clock_s": 40
        },
        {
          "t_ms": 15000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 35
        },
        {
          "t_ms": 18000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 4000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-pause",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "at-pause",
          "t_ms": 4000,
          "kind": "input_activity"
        },
        {
          "id": "between-pause-and-resume",
          "t_ms": 8000,
          "kind": "haptic"
        },
        {
          "id": "at-resume",
          "t_ms": 15000,
          "kind": "input_activity"
        },
        {
          "id": "after-resume-before-zero",
          "t_ms": 16000,
          "kind": "input_activity"
        },
        {
          "id": "after-zero-before-menu",
          "t_ms": 19000,
          "kind": "haptic"
        },
        {
          "id": "at-menu",
          "t_ms": 22000,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "closed_episodes_only",
        "episode_e0_ms": null,
        "evidence_counts": [
          "before-pause",
          "after-resume-before-zero",
          "after-zero-before-menu",
          "at-menu"
        ],
        "backwards_lock": null
      }
    },
    {
      "id": "unreadable-menu-is-still-an-end-screen",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 4,
          "clock_s": 0
        },
        {
          "t_ms": 9000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 45
        }
      ],
      "frames": [
        {
          "t_ms": 4000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 12000,
          "away": 4,
          "home": 29,
          "conf": 0.4,
          "ok": false
        },
        {
          "t_ms": 12500,
          "away": 4,
          "home": 29,
          "conf": 0.4,
          "ok": false
        },
        {
          "t_ms": 13000,
          "away": 4,
          "home": 29,
          "conf": 0.4,
          "ok": false
        }
      ],
      "evidence": [
        {
          "id": "before-screen",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "inside-closed",
          "t_ms": 5500,
          "kind": "input_activity"
        },
        {
          "id": "while-unreadable-menu",
          "t_ms": 12250,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": 12000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 12000,
        "evidence_counts": [
          "before-screen"
        ],
        "backwards_lock": null
      },
      "illustrates": "The frames at 12000 are ok false and read 4–29. m27_mnp_end_menu_online.jpg is this shape: madden_final_screen passes and the away digits are covered by name tags, so the read is ok false. An end screen is the picture and the hold. Those frames open the next episode, and evidence at 12250 does not count. Readability is not required for that label, and the 4–29 is not posted."
    },
    {
      "id": "inputs-wall-anchor-bool",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "wall_anchor_ms is true. A bool is not an integer. The loader returns null for the log.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": true
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-non-object-line",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "A non-empty line that is not a JSON object (42) makes the loader return null. qorcap #97's Final rule calls that line malformed.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        42,
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-second-header",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "A second header row makes the loader return null. Exactly one header is required.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-b-true",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "Field b is true. A bool is not an integer, so the loader returns null for the log. It does not skip the row and return [].",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": true
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-status-t-us-true",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "A status t_us of true is not an integer. The loader returns null for the log.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": true,
          "state": "connected"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-status-backwards",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "Status rows go backwards in wall time. They are not sorted into order. The loader returns null for the log.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 5000000,
          "state": "connected"
        },
        {
          "type": "status",
          "t_us": 1000000,
          "state": "connected"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-log-ends-before-window-end",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "Chunks stop at 18000. The first frame's window end is 20500, so the last chunk is 2500 ms early, inside the 3000 ms step, and still does not cover. An OPTIONS press at 19800 is not in the file. Every frame's entry is null. The menu is not adopted. A connected status does not run to infinity.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-chunk-step-3001",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "The step from 3000 to 6001 is 3001 ms. qorcap #97's Final rule withholds. The 3000 ms step in inputs-well-formed-no-options-press covers.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6001000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9001000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12001000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15001000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18001000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21001000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24001000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-stall-at-window-end-plus-2500",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "The stall is stamped at 23800, the last frame's deadline + 2500. That frame's entry is null, so two frames remain and the menu is not adopted. The same stall at 23801 is inputs-well-formed-no-options-press and confirms.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "status",
          "t_us": 23800000,
          "state": "stalled",
          "detail": "no HID reports"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": 20000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 20000,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-unplug-reconnects",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "illustrates": "The pad drops and reconnects inside the frame window. qorcap #97's Final rule withholds, including a drop that reconnects. Every frame's entry is null.",
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "status",
          "t_us": 10000000,
          "state": "unplugged",
          "detail": "USB link dropped"
        },
        {
          "type": "status",
          "t_us": 12000000,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "posted_score": {
          "away": 13,
          "home": 6
        }
      }
    },
    {
      "id": "inputs-unplug-minutes-earlier-later-frame-confirms",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 180000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 180400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 180800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "status",
          "t_us": 8000000,
          "state": "unplugged",
          "detail": "USB link dropped"
        },
        {
          "type": "status",
          "t_us": 9000000,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 27000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 30000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 33000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 36000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 39000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 42000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 45000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 48000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 51000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 54000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 57000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 60000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 63000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 66000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 69000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 72000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 75000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 78000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 81000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 84000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 87000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 90000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 93000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 96000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 99000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 102000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 105000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 108000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 111000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 114000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 117000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 120000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 123000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 126000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 129000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 132000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 135000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 138000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 141000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 144000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 147000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 150000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 153000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 156000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 159000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 162000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 165000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 168000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 171000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 174000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 177000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 180000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 183000000,
          "n": 0
        }
      ],
      "illustrates": "An unplug at 8 s is inside the 20 s frame's OPTIONS window and outside the 180 s frame's window. Chunk rows cover both windows. The early frames are not end screens because the pad was not connected through that window. The later menu confirms. A gate over the whole tail would see the unplug and refuse the later menu too.",
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 13,
        "home": 6,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false,
        "cutoff_ms": 180000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 180000
      }
    },
    {
      "id": "new-picture-cancels-earlier-agreement",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 8000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true,
          "pixel": "replay"
        },
        {
          "t_ms": 12000,
          "away": 4,
          "home": 29,
          "conf": 0.4,
          "ok": false
        },
        {
          "t_ms": 12500,
          "away": 4,
          "home": 29,
          "conf": 0.4,
          "ok": false
        },
        {
          "t_ms": 13000,
          "away": 4,
          "home": 29,
          "conf": 0.4,
          "ok": false
        }
      ],
      "illustrates": "A stable 6–3 read, then a replay frame, then the online menu unreadable (ok false, 4–29) with no scorebug lock. The replay frame is not an end screen, so the unreadable frames are a new picture. That picture has no stable run. The earlier 6–3 is not posted.",
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": null,
        "home": null,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "fail",
        "note": null,
        "posted_score": null,
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000
      }
    },
    {
      "id": "same-picture-unreadable-flicker-keeps-agreement",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 7000,
          "away": 4,
          "home": 29,
          "conf": 0.4,
          "ok": false
        }
      ],
      "illustrates": "The same 6–3 run, then one ok-false 4–29 frame with no non-end-screen frame between them. The flicker stays in the same picture. It does not block and it does not cancel the agreement.",
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 6,
        "home": 3,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "downgrades_verdict": false,
        "posted_score": {
          "away": 6,
          "home": 3
        },
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000
      }
    },
    {
      "id": "side-swap-keeps-componentwise-chain",
      "synthetic": true,
      "locks": [
        {
          "t_ms": 1000,
          "away": 14,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        },
        {
          "t_ms": 8000,
          "away": 7,
          "home": 14,
          "quarter": 4,
          "clock_s": 40
        },
        {
          "t_ms": 15000,
          "away": 21,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [],
      "illustrates": "14–7, then 7–14, then 21–7. A lock follows only when away and home are both at least the earlier lock. 7–14 does not follow 14–7. The longest chain is 14–7 then 21–7. The posted score is 21–7. 7–14 is backwards_lock.",
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 21,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "posted_score": {
          "away": 21,
          "home": 7
        },
        "backwards_lock": {
          "away": 7,
          "home": 14,
          "t_ms": 8000
        },
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null
      }
    },
    {
      "id": "spurious-high-lock-does-not-poison",
      "synthetic": true,
      "screen": "end_menu",
      "illustrates": "The stale 3–3 is replaced by a misread 77–6. A greedy running maximum would keep 77–6 and drop every later lock. The longest chain skips 77–6, so the posted score is 13–6 and the lock at 15000 still closes. Evidence at 16000 counts.",
      "locks": [
        {
          "t_ms": 1000,
          "away": 6,
          "home": 3,
          "quarter": 2,
          "clock_s": 120
        },
        {
          "t_ms": 8000,
          "away": 77,
          "home": 6,
          "quarter": 3,
          "clock_s": 40
        },
        {
          "t_ms": 15000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 35
        },
        {
          "t_ms": 18000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 4000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-pause",
          "t_ms": 2000,
          "kind": "input_activity"
        },
        {
          "id": "at-pause",
          "t_ms": 4000,
          "kind": "input_activity"
        },
        {
          "id": "between-pause-and-resume",
          "t_ms": 8000,
          "kind": "haptic"
        },
        {
          "id": "at-resume",
          "t_ms": 15000,
          "kind": "input_activity"
        },
        {
          "id": "after-resume-before-zero",
          "t_ms": 16000,
          "kind": "input_activity"
        },
        {
          "id": "after-zero-before-menu",
          "t_ms": 19000,
          "kind": "haptic"
        },
        {
          "id": "at-menu",
          "t_ms": 22000,
          "kind": "input_activity"
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "closed_episodes_only",
        "episode_e0_ms": null,
        "evidence_counts": [
          "before-pause",
          "after-resume-before-zero",
          "after-zero-before-menu",
          "at-menu"
        ],
        "backwards_lock": {
          "away": 77,
          "home": 6,
          "t_ms": 8000
        }
      }
    },
    {
      "id": "longest-chains-tie-score-unconfirmed",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 5000,
          "away": 6,
          "home": 3,
          "quarter": 4,
          "clock_s": 20
        },
        {
          "t_ms": 10000,
          "away": 77,
          "home": 6,
          "quarter": 4,
          "clock_s": 15
        },
        {
          "t_ms": 11000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 15
        }
      ],
      "frames": [
        {
          "t_ms": 2000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 7000,
          "away": 6,
          "home": 3,
          "conf": 0.95,
          "ok": true
        }
      ],
      "evidence": [
        {
          "id": "before-shared-close",
          "t_ms": 1000,
          "kind": "input_activity"
        },
        {
          "id": "inside-shared-close",
          "t_ms": 3000,
          "kind": "input_activity"
        },
        {
          "id": "between-close-and-next-picture",
          "t_ms": 6000,
          "kind": "input_activity"
        },
        {
          "id": "while-later-picture-open",
          "t_ms": 8500,
          "kind": "input_activity"
        },
        {
          "id": "after-both-chain-ends",
          "t_ms": 12000,
          "kind": "input_activity"
        }
      ],
      "illustrates": "6–3, then 77–6, then 13–6. The chains 6–3→77–6 and 6–3→13–6 both have length 2 and end on different scores. The receipt score is null. Only the shared 6–3 lock may close, so the flash at 2000 closes and the screen at 7000 stays open. Evidence at 12000 does not count. Letting either later lock close would count it.",
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": null,
        "home": null,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "fail",
        "note": null,
        "posted_score": null,
        "backwards_lock": null,
        "cutoff_ms": 7000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 7000,
        "evidence_counts": [
          "before-shared-close",
          "between-close-and-next-picture"
        ]
      }
    },
    {
      "id": "dispute-later-menu-after-gameplay",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 8000,
          "away": 0,
          "home": 0,
          "conf": 0.95,
          "ok": true,
          "pixel": "replay"
        },
        {
          "t_ms": 12000,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 12400,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 12800,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "partial",
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000,
        "end_menu_disputed": true,
        "downgrades_verdict": false,
        "note": "later end-of-game menu read AWAY 10 - HOME 0 disagreed with the posted final AWAY 7 - HOME 0; score not replaced; final_confirmed=false",
        "away": 7,
        "home": 0
      },
      "illustrates": "A stable 7–0, then gameplay, then a stable 10–0. The later picture is a dispute, not a new candidate. The receipt keeps 7–0, unconfirmed, end_menu_disputed true."
    },
    {
      "id": "dispute-later-menu-reverse-order",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 8000,
          "away": 0,
          "home": 0,
          "conf": 0.95,
          "ok": true,
          "pixel": "replay"
        },
        {
          "t_ms": 12000,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 12400,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 12800,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "partial",
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000,
        "end_menu_disputed": true,
        "downgrades_verdict": false,
        "note": "later end-of-game menu read AWAY 7 - HOME 0 disagreed with the posted final AWAY 10 - HOME 0; score not replaced; final_confirmed=false",
        "away": 10,
        "home": 0
      },
      "illustrates": "The same pair in reverse order. The receipt keeps 10–0, the first stable score, and the note names the later 7–0."
    },
    {
      "id": "dispute-one-stray-frame",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 6000,
          "away": 0,
          "home": 0,
          "conf": 0.95,
          "ok": true,
          "pixel": "replay"
        },
        {
          "t_ms": 7000,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 7400,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 7800,
          "away": 10,
          "home": 0,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "partial",
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000,
        "end_menu_disputed": true,
        "downgrades_verdict": false,
        "note": "later end-of-game menu read AWAY 10 - HOME 0 disagreed with the posted final AWAY 7 - HOME 0; score not replaced; final_confirmed=false",
        "away": 7,
        "home": 0
      },
      "illustrates": "A stable 7–0, one stray non-end frame, then a stable 10–0. One frame between the pictures does not make 10–0 the candidate. The receipt stays 7–0, disputed."
    },
    {
      "id": "same-menu-after-gameplay-confirms",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [],
      "frames": [
        {
          "t_ms": 5000,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5400,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 5800,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 8000,
          "away": 0,
          "home": 0,
          "conf": 0.95,
          "ok": true,
          "pixel": "replay"
        },
        {
          "t_ms": 12000,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 12400,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 12800,
          "away": 7,
          "home": 0,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 7,
        "home": 0,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "cutoff_ms": 5000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 5000,
        "end_menu_disputed": false,
        "downgrades_verdict": false
      },
      "illustrates": "A stable 7–0, then gameplay, then the same 7–0. The later picture agrees, so the score confirms and end_menu_disputed is false."
    },
    {
      "id": "scorebug-return-same-score-confirms",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 10000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 2000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 2500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 3000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 3500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20500,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 21000,
          "away": 10,
          "home": 7,
          "conf": 0.95,
          "ok": true
        }
      ],
      "expect": {
        "adopt_menu": true,
        "final_confirmed": true,
        "away": 10,
        "home": 7,
        "method": "end_menu_read",
        "final_basis": "end_menu",
        "review": "pass",
        "note": null,
        "cutoff_ms": 20000,
        "cutoff_reason": "open_episode",
        "episode_e0_ms": 2000,
        "end_menu_disputed": false,
        "downgrades_verdict": false
      },
      "illustrates": "Both stable menus are 10–7, and a 0:00 lock sits between them. The scorebug does not clear the first run. The scores agree, so the menu confirms. The cutoff is the first frame of the later run."
    },
    {
      "id": "inputs-same-ms-unplugged-and-connected",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "inputs": [
        {
          "type": "header",
          "schema": "qorcap-inputs/v0",
          "t_us": 0,
          "wall_anchor_ms": 0
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "unplugged",
          "detail": "no pad"
        },
        {
          "type": "status",
          "t_us": 0,
          "state": "connected",
          "detail": "DualSense (USB)"
        },
        {
          "type": "status",
          "t_us": 23801000,
          "state": "stalled",
          "detail": "no HID reports"
        },
        {
          "type": "in",
          "t_us": 1000,
          "b": 0
        },
        {
          "type": "chunk",
          "t_us": 0,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 3000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 6000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 9000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 12000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 15000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 18000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 21000000,
          "n": 0
        },
        {
          "type": "chunk",
          "t_us": 24000000,
          "n": 0
        }
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "end_menu_disputed": false
      },
      "illustrates": "unplugged and connected share one millisecond, and connected is written last. Sorting by (wall, state) puts unplugged after connected, so the pad is not connected through the window. Last-write-wins would confirm 13–6. The loader returns a null entry for every frame, and the menu is not adopted."
    },
    {
      "id": "options-presses-wrong-shape",
      "synthetic": true,
      "screen": "end_menu",
      "locks": [
        {
          "t_ms": 1000,
          "away": 13,
          "home": 6,
          "quarter": 4,
          "clock_s": 0
        }
      ],
      "frames": [
        {
          "t_ms": 20000,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20400,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        },
        {
          "t_ms": 20800,
          "away": 13,
          "home": 6,
          "conf": 0.95,
          "ok": true
        }
      ],
      "options_presses_ms": [
        null
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "cutoff_ms": null,
        "cutoff_reason": "no_end_screen",
        "episode_e0_ms": null,
        "end_menu_disputed": false
      },
      "illustrates": "options_presses_ms is [null] beside three frames. That is neither null, nor a shared list of integers, nor a per-frame list of length 3. decide fails every frame closed. A reader that treated the value as [] would confirm."
    }
  ]
}
```

The stall case's three frames are the gate, at confidence 0.94 on invented timestamps. `reported_frames: 485` records the issue's count. The case is `"synthetic": true`. Issue 94 did not publish per-frame confidence or these timestamps. An implementation that checks only the first frame's confidence fails `conf-below-0-90-on-one-frame`.

`duplicate-timestamps` lists `5000`, `5000`, `5400`. That is two instants, and it does not confirm. `backwards-timestamp` lists `5000`, `5800`, `5400`. Sorting those into `5000`, `5400`, `5800` would be a stable run. The decision fails closed and does not confirm. `unreadable-frame-resets-run` has one good frame, one `ok: false` frame, and two good frames. The unreadable frame resets the streak. Bridging across it would be three frames and must not confirm. `unreadable-then-new-run-confirms` has three fresh frames after `ok: false` and does confirm, so the unreadable frame does not abort the rest of the decision. `conf-0-75-resets-run` is the 0.70–0.89 band, and it resets the same way. `conf-0-75-then-new-run-confirms` shows the new run.

`decide` derives every end screen from a frame it still labels an end menu. There is no `end_screens` input. `menu-frame-equal-to-scorebug-not-fresh` has frames at 5000, 5400, and 5800. The lock is at 5000, so the cutoff is 5400. The frame at 5000 is not fresh, and two fresh frames do not confirm. Evidence at 5000 is in the dead zone. Evidence at 5200 counts. Evidence at 5400 does not (`t_ms >= cutoff_ms`).

`qorcap-98-touchdown-lock-same-ts` is the qorcap#98 shape with synthetic scores away 13, home 6. The touchdown lock and the only menu frame share `t_ms` 8000. Nothing is strictly before that lock, so the episode stays open. The cutoff falls back to 8000. Evidence at 8000 and 8250 does not count. The frame is not fresh. The case does not confirm. The posted score is the lock, 13–6, and `final_reread` is partial. `equal-ts-opens-new-episode` adds a flash at 4000 in front of that same shared timestamp. The flash closes. The menu frame at 8000 opens the new episode. `cutoff_ms` is 8000, `cutoff_reason` is `open_episode`, and evidence at 8250 does not count.

`later-live-play` has a lock at `t_ms` 9000 after the menu frames, with a running clock, and the same score as the earlier lock. `decide` reads that from `locks`. There is no `later_live_play` field. The 13–6 run already met the stable gate, so the later scorebug does not clear it. The menu confirms 13–6. The stall note names Q4 0:45. That lock is kept, so it closes the episode. No end screen follows. `cutoff_ms` is null and `cutoff_reason` is `closed_episodes_only`. Evidence before the episode and evidence after the closing lock count. Evidence inside the closed band does not. `later-contradicting-menu` puts the second score in `frames` (17–7 after 10–7). The two stable reads disagree, so the receipt keeps the first menu, 10–7, with `end_menu_disputed` true, `method` `end_menu_read`, `final_basis` `end_menu`, and `final_reread` partial. The note names AWAY 17 - HOME 7 and does not add the scorebug phrase: the lock is before both runs. `no-lock-contradicting-menus` keeps 9–6 the same way, partial, with the note naming AWAY 16 - HOME 6. `later-readable-frame-blocks-confirm` is a stable 6–3 read and then one 13–6 frame at 0.85, with no lock. 0.85 is readable and is not a second stable run. Neither score is posted, and review is fail. `later-frame-below-readability-floor` is that shape at 0.69 and still confirms 6–3. `dispute-later-menu-after-gameplay`, `dispute-later-menu-reverse-order`, and `dispute-one-stray-frame` are the 7–0 / 10–0 pair with gameplay, the reverse order, and one stray non-end frame. Those scores are the qorcap #101 probe. They are synthetic, and they are not match 170123. Each keeps the first score and sets `end_menu_disputed` true. `same-menu-after-gameplay-confirms` is 7–0, gameplay, then 7–0, and it confirms.

`replay-lock-lower-after-menu` is 14–7 and then 3–0. Each lock is a longest chain of length 1, the terminal scores differ, and the receipt score is null. Neither lock is on both chains, so the running clock does not close the episode. `cutoff_ms` falls back to 5000, the first menu frame, and evidence after the stale lock does not count. `menu-below-an-earlier-lock` does not adopt the 14–0 menu, and the 21–0 and 14–0 locks are the same kind of tie, so the receipt score is null rather than 21–0. `tie-menu-stays-unconfirmed` and `final-of-1-stays-unconfirmed` stay unconfirmed (§4.7). `null-clock-long-gap` confirms 6–3 from a null raw `clock_s` and a 19000 ms wall gap. The stall note is on the sidecar and `downgrades_verdict` is false. `referee-cannot-measure-hold` has `options_presses_ms` null and does not pass. `cutoff_reason` is `no_end_screen`.

`scorebug-return-resets-agreed-score` keeps the early 3–0 run. The later 10–7 run is a dispute, `end_menu_disputed` true, and the note says a scorebug read came back between the two menus. The cutoff is 20000, the first frame of the later run. The early screen is a frame at 2000. `scorebug-return-same-score-confirms` is that shape with both runs at 10–7, and it confirms. `cutoff-dead-zone-and-inclusive-equal` keeps evidence before the first end screen and evidence strictly between the scorebug and the fresh menu, and drops every timestamp from the first end screen through the scorebug, plus every timestamp `>= cutoff_ms`. That first screen is one frame at 2000, so it is not a stable run for the scorebug to keep. The lock is 0:00, so the early screen and the final menu are one episode. `cutoff-falls-back-to-earliest-end-screen` is also a 0:00 lock. Its first screen is a stable 10–7 run at 2000, the lock matches that score, and the menu confirms. No end screen is after the lock, so `cutoff_ms` stays 2000. `early-pause-then-final-menu` is a one-frame flash at 4000, then a running-clock lock at 15000, then a real menu at 22000. The flash's episode closes at 15000. `episode_e0_ms` is 22000. Evidence at 16000 and 19000 counts. Evidence from 4000 through 15000 does not. A cutoff stuck at 4000 would drop the later rows. `early-pause-then-final-menu-clock-zero` is the same capture with that 15000 lock at 0:00. The flash and the final menu stay one episode. `episode_e0_ms` is 4000. Evidence at 16000 does not count. The menu still confirms 13–6, and the cutoff is still 22000. `pause-options-short-hold` carries `pixel` `end_menu_6`, `picture_since_ms` 1000, and `options_presses_ms` `[800]`. The press at 800 is inside `(t - 16000, t + 500]` for the frame at 5000, the lock at 500 is still up, and the hold is 4000 ms. `decide` labels those frames pause.

`options-press-at-t-minus-16000-is-outside` confirms. The press is exactly 16000 ms before the first frame, so it is outside the window. `options-press-at-t-minus-15999-is-pause` puts the picture 4000 ms after a press at `t - 15999` on the last frame. The hold is 11999 ms. The menu is not adopted. `options-press-at-t-plus-500-is-pause` is the other edge, and it is a pause.

`inputs-no-wall-anchor`, `inputs-wall-anchor-not-integer`, `inputs-wall-anchor-bool`, `inputs-non-object-line`, `inputs-second-header`, `inputs-malformed-row`, `inputs-b-true`, `inputs-status-t-us-true`, and `inputs-status-backwards` each make `load_options_presses_ms` return null for the log. `inputs-pad-not-connected` returns a null entry for every frame. The same 13–6 frames would confirm on `[]`. They do not. `inputs-log-ends-before-window-end`, `inputs-chunk-step-3001`, `inputs-stall-at-window-end-plus-2500`, and `inputs-unplug-reconnects` parse, and every frame's window fails qorcap #97's Final rule, so the menu is not adopted. `inputs-well-formed-no-options-press` is the per-frame `[]`, and it confirms. `inputs-unplug-minutes-earlier-later-frame-confirms` fails the early frame and confirms the later one. `inputs-same-ms-unplugged-and-connected` writes `unplugged` and then `connected` at the same millisecond. After the `(wall, state)` sort the unplugged row wins, every frame's entry is null, and the menu is not adopted. `options-presses-wrong-shape` passes `[null]` beside three frames. That length is not per-frame and the element is not an integer, so every frame fails closed.

`new-picture-cancels-earlier-agreement` does not post the earlier 6–3. `same-picture-unreadable-flicker-keeps-agreement` does. `side-swap-keeps-componentwise-chain` posts 21–7 and drops 7–14. `spurious-high-lock-does-not-poison` posts 13–6, not 77–6. `longest-chains-tie-score-unconfirmed` posts null, and evidence after the shared 6–3 close still sees the later picture open.

`stale-decrease-keeps-later-lock` adds a 3–3 lock at 8000 to the early-pause locks and leaves the final menu off, so the posted score is the lock. The 13–6 lock at 15000 still closes the flash. Evidence at 16000 counts. The posted score is 13–6. `unreadable-menu-is-still-an-end-screen` closes an episode on a readable flash, then shows `ok: false` frames at 4–29. Those frames are end screens. Evidence at 12250 does not count. The posted score stays 13–6.

## 8. Schema

`receipt.v0` stays frozen. `schema/**` is not part of this change. Pages publishes `schema/**` (`.github/workflows/pages.yml`). No new `result` field, no new `final_basis`, no new method string. `end_menu_read` and `last_scorebug_read_unconfirmed` are already in the contract.

`final.rule.json` is a sidecar, same class as `final.tail.json` and `device_attest.v0.json`. Its schema file is [`docs/design/final.rule.v1.schema.json`](final.rule.v1.schema.json), outside `schema/**`. Pages publishes `schema/**` and does not publish `docs/design`. The schema `$id` is the URN `urn:qortime:final.rule.v1` until that document is published. This change does not add a Pages route for it. Every final the new code produces writes that sidecar with `"rule": "final-rule/v1"`, including an unconfirmed final. `writer` is `client` or `referee`. `end_menu_disputed` is a boolean on that sidecar. It is true when two stable end-menu scores disagree. The file then keeps the first score, `final_confirmed` false. It is not a `receipt.v0` field. `cutoff_reason` `open_episode` requires an integer `cutoff_ms` and an integer `episode_e0_ms`. `no_end_screen` and `closed_episodes_only` require both `cutoff_ms` and `episode_e0_ms` null. The public note is copied out of `note` when the match page is built. `evidence_counted` stays on the file and is not rendered. The sidecar is not hashed into the receipt. An old bundle with no sidecar still verifies. The page shows no v1 note for it. When a review wrote a sidecar, the page shows that referee file. Otherwise it shows the client's file. When both exist and the scores differ, the page shows the referee score and a visible note of the client's score.

## 9. Rollout and compatibility

QorTime merges `qor_receipt.final_rule` first. QorCap then re-vendors that package and pins the merge. The client does not ship a private copy of the function ahead of the referee.

A receipt from a client at v0.3.32 or earlier keeps the status it was signed with. This rule change does not upgrade it to VERIFIED. VERIFIED requires a new official review signature.

When that re-review is actually run, the referee re-read is authoritative:

* The claim's numbers are compared to `decide`'s score. A difference is a `final_reread` fail, shown as a mismatch. The stored claim is not overwritten.
* The claim's `method` can be `last_scorebug_read_unconfirmed` while the re-read confirms the same numbers. The check may pass on the numbers. The receipt is not rewritten to VERIFIED by the rule change itself. The new review signature is what would change the status, and this design does not issue one.
* A new submission from an old client, whose claim is unconfirmed, cannot reach VERIFIED unless the referee's own `decide()` confirms (`final_confirmed` true). Passing on the numbers while `decide()` leaves the menu unconfirmed is not VERIFIED. A disputed final (`end_menu_disputed` true) is partial and cannot become VERIFIED, including when the claim equals the first stable score.
* `check_final` no longer errors when an adopted end menu is more than one legal play past the last scorebug. That changes a future verification of a bundle signed under the old rule: an old integrity error of that one shape would not fire again. Other errors still fire. Published pages are not rebuilt by the merge.

An old bundle is fail-closed on a re-review because `load_options_presses_ms` returns null, or a frame's entry is null, not because a caller special-cases the filename. The path is qorcap #97's Final rule on that frame's window: no header or a second header, a non-integer or bool `wall_anchor_ms`, a non-object line, a bool `b` or `t_us`, a status row that goes backwards, no `connected` status through `deadline_ms + 2500`, or chunk coverage that does not reach `deadline_ms`. `decide` then refuses that 6-item frame. Official review is `partial` when a chain posts a score, and `fail` when it does not. `inputs-no-wall-anchor` and `inputs-log-ends-before-window-end` are that path. Frozen receipts are not re-reviewed, so a published page does not flip to partial because the log is old.

The implementation PR does not merge until the §4.4 measurement is written down: blocking-flicker and streak-reset rates on 135645, 143212, 091651, 110028, and 162055. Those recordings are not vector templates, and the held-out set is not the measurement set.

Two sidecars can exist. The page shows the referee's `final.rule.json` when the review wrote one. Otherwise it shows the client's file. When both exist and the scores differ, the page shows the referee score and a visible note of the client's score. It does not average them and it does not hide the difference.

`rule` on the sidecar is how a reader tells a v1 final from an older one. An old bundle has no file and no stamp.

## 10. Cross-reference: qorcap #101 and #102

qorcap #101 is the confirmed-wrong-final bug. `tail_final_check` stopped at the first end-menu agreement. A recorded tail that showed 7–0 and then 10–0 could sign 7–0 with `final_confirmed` true. Live capture would have disputed and stayed unconfirmed.

qorcap #102 reads every frame of the recorded tail. A later menu that disagrees goes through `_note_end_menu_disagreement`. The first score stays, `final_confirmed` is false, and `end_menu_disputed` is true. The note names the later score. When a real scorebug row lies strictly between the two menus, the note adds the phrase from qorcap #100, ` after a scorebug read came back between the two menus`. Observations, rows with no home, and flushed touchdown holds do not count as that row. Two showings of the same menu still confirm, including a menu, then gameplay, then the same menu.

`final-rule/v1` folds that result into `decide`. `end_menu_disputed` is a field on `final.rule.json`. It is not a `receipt.v0` field. `receipt.v0` stays frozen. The verdict impact is partial, never VERIFIED. Held-out matches stay out of the vectors and out of this section.
