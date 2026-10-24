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
2. The score is stably read: 3 fresh menu frames, spanning at least 400 ms, confidence at least 0.90 on every frame, every frame the same score, and every frame strictly after the latest scorebug read (§4.3).
3. That menu is the last such menu in the recording. A later contradicting menu leaves the final unconfirmed. A scorebug return resets the streak and the agreed score (§4.4). The evidence cutoff is re-derived from the end screens that remain (§4.6).

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

Each frame carries `pixel` (`end_menu_6`, `pause_8`, `faded_field`, `replay`, or `other`) and `picture_since_ms` (the first frame of the current dark picture). `options_presses_ms` is the list of OPTIONS rising edges, or null when that list was not measured. OPTIONS is DualSense button bit 9 (`OPTIONS_BIT = 1 << 9`, `qorcap_reader.py:55-63`; the same bit is `BUTTON_BITS` name `OPTIONS` bit 9 in `qor_receipt/review/bundle.py:306-307`). A press time is wall milliseconds: `inputs.jsonl` header `wall_anchor_ms` plus `t_us // 1000` on a rising edge of field `b` (`bundle.py:316-329`). The picture was opened by OPTIONS when `picture_since_ms` is within `[-500, OPTIONS_MENU_LEAD_MS]` of a press (`OPTIONS_MENU_LEAD_MS = 4000`) and the last scorebug was still up at that press (`qorcap_reader.py:977-997`). The hold is `t_ms - picture_since_ms`. A hold under `OPTIONS_COINCIDENT_HOLD_MS` (12000) labels the frame `pause`.

The referee re-read today does not pass those presses. `reread_final` (`media.py:179-191`) feeds frames to a fresh `ReaderCore` and does not call `note_options_from_inputs`. The bundle does contain `inputs.jsonl` (`bundle.py` `META_FILES`). v1 requires the re-read to load the same rising edges the client tail loads (`qorcap_match.py` `note_options_from_inputs` at the tail check). The tail window is 240 s at 2 Hz (`policy.py` `TAIL_MS`, `engine.py` `TAIL_HZ`). A tail can start after the dark picture is already up, so `picture_since_ms` can fall outside the decoded frames.

When `options_presses_ms` is null, or a 6-item frame has no `picture_since_ms`, the referee cannot measure the hold. That frame is not an end menu. The final stays unconfirmed. Official review is `partial` when a monotone lock exists, and `fail` when it does not. It is not `pass`.

Accepted risks, all of which need the recording to end on the picture. A later agreed scorebug read resets the streak and the agreed score, so none of these survive a scorebug that returns:

* A halftime screen that is pixel-identical to the six-item end menu, and was not opened by OPTIONS, passes the geometry. v1 does not put a clock gate behind it.
* Three forged end-menu frames over at least 400 ms at the very end of the recording, with confidence at least 0.90 and the same score, confirm. That is the case the clock-gate comment at `qorcap_reader.py:1518-1519` refused.
* A 6-item Practice / Play Now pause held at least 12 s, with the recording stopped on that picture (`match-20261020-171032`), confirms. The short hold still refuses it. The long hold at the end of the file does not.

### 4.3 Stably read

A menu is stably read when `decide` has 3 fresh menu frames. Fresh means `t_ms` strictly greater than the latest agreed scorebug read. Latest means the maximum `t_ms` among locks, not the last row in list order. A frame at that same timestamp is not fresh. With no scorebug read, every menu frame is fresh.

`decide` requires each of `frames` and `locks` to be listed in non-decreasing `t_ms`. A row with `t_ms` less than the previous row fails the decision closed: the menu is not adopted. A duplicate `t_ms` stays in the list and does not add a distinct instant or any span. The run needs 3 distinct, strictly increasing timestamps. Rows `5000, 5000, 5400` are two instants. Latest lock is the maximum `t_ms`. On a non-decreasing list that is the last lock. An `ok: false` frame resets the streak: the frames before it are dropped, and the frames after it must form a new run. A readable frame with confidence from 0.70 through 0.89 resets the streak the same way. The bad frame is not bridged. Two frames after the reset do not confirm. Three fresh frames after the reset can. A backwards timestamp is the case that fails the whole decision closed.

On the fresh, strictly increasing frames, all of these hold:

* at least 3 distinct timestamps
* the last timestamp minus the first is at least 400 ms
* every frame has `conf` ≥ 0.90
* every frame agrees on `(away, home)`
* every frame's `pixel` is `end_menu_6` and the OPTIONS hold does not label it `pause`
* the object carries `min_conf`, the minimum across the run

`EndMenuAgreement` (`sidecar/qorcap_endmenu.py:231-261`) already requires 3 consecutive readable frames and a span of at least 400 ms, and it already clears the run when the score changes or a frame is unreadable. It does not know about the scorebug. `offer` stores `conf` from the frame that first completes the run (`:251-256`) and later frames update only `until_ms` and `frames` (`:259-260`). A readable frame under 0.90 clears the run, the same way an unreadable frame does.

A scorebug return resets both the menu streak and the agreed score. Frames already offered, and a stable score already stored, are dropped. The next stable read is built only from frames strictly after that read. `_post_agreed` clears `_end_screen_earliest` (`qorcap_reader.py:1343-1344`) and does not clear `EndMenuAgreement`. The reset is in `decide`, so the tail and the referee re-read do the same thing.

The glyph floor stays what makes a frame readable: `MIN_SCORE = 0.70` and `MIN_MARGIN = 0.20` (`qorcap_endmenu.py:37-38`). 0.90 is the confirm gate on top of a readable frame. A covered-panel corroboration (`CORROB_MIN = 0.55`, `qorcap_endmenu.py:272`) fails 0.90 on every frame, so a covered panel does not confirm.

The referee fallback's `frames >= 2` (`media.py:207`, `:258-259`) is replaced by this 3-frame rule.

Issue 94 reported one confidence, 0.912, over 485 frames. That number is the completing frame in today's reader, not a per-frame minimum. The stall fixture is synthetic: three frames at confidence 0.94, with invented timestamps. It does not reuse 0.912. A 2 Hz tail spaces frames by 500 ms, so 3 frames span about 1000 ms and clear 400 ms.

### 4.4 Last menu, and what a failed gate produces

`decide` derives the last menu and later live play from `frames` and `locks`. There is no caller-supplied `later` flag.

The candidate is the stable read in the fresh suffix after the latest lock (`max t_ms`). A second stable read in that suffix with a different score is a contradicting later menu. `decide` sees it in `frames`. The final is unconfirmed. Neither menu score is posted. An agreed scorebug read with `t_ms` after menu frames is later live play. It is a lock. It resets the streak and the agreed score, including when `clock_s` is 0. If no stable read follows that lock, the final is unconfirmed.

Failed gates produce these results, on both sides:

| Case | Posted score | `method` | `final_basis` | `final_confirmed` | `final_reread` |
|---|---|---|---|---|---|
| Gate failed, locks are monotone | last lock (`max t_ms`) | `last_scorebug_read_unconfirmed` | absent | false | `partial` |
| A later lock is below an earlier lock | the last row of the monotone prefix, not the lower lock | `last_scorebug_read_unconfirmed` | absent | false | `partial` |
| No lock, and no adopted menu | null | `last_scorebug_read_unconfirmed` | absent | false | `fail` |
| No lock, and two menus that disagree | null. Neither menu is posted | `last_scorebug_read_unconfirmed` | absent | false | `fail` |

The no-score fail detail stays "referee could not read any score from the recorded tail" (`engine.py:804`).

`partial` on an official review check stays PARTIAL (`qor_receipt/receipt.py:242-243`). It does not become VERIFIED.

Today's `_note_end_menu_disagreement` (`qorcap_reader.py:1576-1595`) keeps the posted menu score and only clears `final_confirmed`. v1 does not keep that score when a later menu contradicts it. The receipt score is the monotone prefix. `select_offline_final` (`qorcap_match.py:2431-2445`) and `_disputed_final_result` (`:2457-2464`) follow the same result: that prefix, `final_confirmed` false. A later lock below an earlier lock is not the prefix.

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

A stall gap is recorded from the raw `clock_s` on the latest monotone lock. `decide` does not copy a clock from an earlier row. `_scorebug_for_final` (`qorcap_reader.py:1426-1449`) does that copy today. Callers pass the lock as it was read. A null `clock_s` stays null.

The note fires when either of these is true:

* raw `clock_s` is an integer greater than 0. Unread seconds are that `clock_s`. Issue 94's lock is 11.
* raw `clock_s` is null and `wall_gap_ms` is at least `STALL_WALL_GAP_MS` (10000). `wall_gap_ms` is the first frame of the adopted run minus the monotone lock's `t_ms`. Unread seconds are null. The note names the wall gap. Example: "End menu confirmed 6–3. Scorebug last locked at Q4 with no clock read (19000 ms unread on the wall clock)."

`wall_gap_ms` is stored in both cases. Unread seconds are not subtracted from the clock, and they are not treated as 0:00. qorcap#85 logs a stall and does not mint a score from it. The menu mints the score. The gap is recorded. The note does not downgrade the verdict.

The menu must be greater than or equal to every lock on both sides, not only the latest. A menu below any lock is unconfirmed. A later lock that is below an earlier lock is a backwards lock. It resets freshness, because latest is `max t_ms`, and it is not the posted score. The posted score is the last row of the monotone prefix. The sidecar records that lock as `backwards_lock`. `check_final` (`integrity.py:195-202`) compares the receipt score to `last_state`. The caller passes that monotone row as `last_state`. The lower lock stays on the sidecar and is not the posted score.

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
  "downgrades_verdict": false
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

The public note is that `note` string, shown on the match page and on the matches index. It is rendered from the sidecar at publish time:

* `qor_receipt/match_index/build.py` copies `final.rule.json` onto the page index as `result.final_rule` (the result object is built near `:421`).
* `qor_receipt/match_index/viewer_template.html` `paintFinal` (`:984-995`) appends `result.final_rule.note` to `#finalWhy` when the final is confirmed. The gold class stays on `#finalSum` when the receipt is VERIFIED.
* `qor_receipt/match_index/publish.py` `_card` (`:452-474`) adds the same sentence under the score. The gold class stays when `final_confirmed` is true and `receipt_status` is `VERIFIED` (`:459`).

The note does not downgrade the verdict. `final_reread` stays `pass`. A VERIFIED receipt stays VERIFIED. The score on the page stays the menu score.

Issue 94's shape, under this rule, with a synthetic per-frame read: end menu away 10, home 7, every frame at 0.94, three timestamps strictly after the lock, span 1000 ms, pixels `end_menu_6`, no later menu in `frames`, no lock after those frames. Last raw lock Q4 0:11, same score. Confirmed. `final_reread` pass when a review is run. The sidecar note is the 11 unread seconds and that last lock. The client's current rejection does not stand. The referee's current pass stays a pass, with the note added. The 0.912 figure in sections 1–3 is the issue's single reported confidence. The vector does not copy it.

### 4.6 One function, and the clock leaves both callers

Both sides call `qor_receipt.final_rule.decide`. QorCap already imports `qor_receipt`. This is not `qor_receipt.referee_ai.referee.decide`, which turns integrity errors into a verdict (`referee.py:194`).

Removing the clock gate changes which frames count as end screens. A pixel match that `_accept_final_screen` used to reject for the clock (`qorcap_reader.py:999-1010`) can now be an end screen, and a `final_screen` timeline row from that match now feeds the cutoff. The cutoff is re-derived here. It is not `_end_screen_earliest` left over after `_post_agreed` clears it.

```text
decide(frames, locks, evidence, options_presses_ms) -> Decision
```

* `frames`: reads `{t_ms, away, home, conf, ok, pixel, picture_since_ms}`, listed in non-decreasing `t_ms`. A backwards step fails the decision closed. A duplicate timestamp does not add a distinct instant or any span. A frame counts toward the stable run only when `t_ms` is strictly greater than the latest lock, the timestamp is strictly greater than the previous counted frame, `ok` is true, `conf` ≥ 0.90, and §4.2 labels `pixel` as the end menu. The confirming run is the fresh suffix after the latest lock. A second stable score in that suffix is the contradicting later menu.
* `locks`: agreed scorebug rows, possibly empty, each with raw `clock_s` (integer or null). Latest is `max t_ms`. Used for the fresh suffix, the monotone posted score, the stall gap, and the disagreement note. `clock_s` is not a confirm precondition. `decide` does not require a Q4 row and does not copy a clock onto a null.
* `evidence`: clip, haptic, and input rows `{t_ms, kind}`. A scorebug row is a lock, not evidence.
* `options_presses_ms`: measured OPTIONS rising edges, or null when `inputs.jsonl` was not read. Null fails closed on a 6-item frame (§4.2).

End-screen timestamps are derived inside `decide`. They are every frame `decide` still labels an end menu: `ok` true, `pixel` `end_menu_6`, and the OPTIONS hold does not label the frame pause. A 6-item frame the hold labels pause, and a 6-item frame whose hold was not measured, are not end screens. `picture_since_ms` on a frame that remains an end menu is included. A caller-supplied list that omits a later readable frame is not the cutoff. The frame is.

`Decision` carries `rule` = `final-rule/v1`, `cutoff_ms`, `evidence_counted`, and `posted_score`. `cutoff_ms` is not a receipt field.

The evidence cutoff does not grant or withhold `final_confirmed`. Its only consumer is the sidecar writer: `tail_final_check`, offline `finalize`, and `reread_final` copy `evidence_counted` onto `final.rule.json`. The publisher does not render that list. No confirm path reads it. A boundary bug in the list changes no verdict.

Cutoff, computed by `decide`:

* Let `E0` be the earliest derived end-screen timestamp. Let `S` be the latest lock's `t_ms`. When `locks` is empty there is no `S`, and `cutoff_ms` is `E0`.
* When `S` is present, the cutoff is the earliest end screen strictly after `S`. When no end screen is strictly after `S`, the cutoff falls back to `E0`.
* When any end screen exists, `cutoff_ms` is that integer. It does not come back as null. With no end screen, `cutoff_ms` is null. Today's `end_screen_evidence_cutoff_ms` (`qorcap_reader.py:1710-1733`) returns null whenever its inputs are missing, and the filter then skips (`:1703`), so late evidence counts. v1 uses the fallback instead of that skip.
* A row is counted only when `t_ms < cutoff_ms`. `t_ms >= cutoff_ms` is not counted. The comparison is inclusive of the equal timestamp. The code already uses `>=` at `:1704`. The contract keeps that operator.
* A row with `E0 <= t_ms <= S` is never counted, including when that timestamp is below `cutoff_ms`. That interval is the first end screen through the latest scorebug read, both ends included.

`ReaderCore._end_menu`, `tail_final_check`, offline `finalize`, and `reread_final` all call it. `end_menu_fallback` stops being a second rule. The rejection string is an output of a failed gate, not a switch that selects a looser rule.

The qorcap client tail and the qortime referee re-read both stop seeding or requiring the live-timeline clock.

The client tail seeds it today. `tail_final_check` copies `timeline.jsonl` onto `core.timeline` (`qorcap_match.py:2495`) before `on_frame`. That copy is why `_clock_ready_for_end_menu` sees Q4 0:11 on 135645. The assignment goes away. The lock rows are passed to `decide` as `locks`. They are not written onto the reader timeline first.

The referee re-read already refuses the seed (`reread_final`, `media.py:181`, "No laptop timeline seed") and then requires the clock by another door. An unseeded `ReaderCore` emits `no Q4 scorebug read before the end-of-game menu` (`qorcap_reader.py:1515-1517`), and `end_menu_fallback` accepts only that exact string (`media.py:205`, `:250-251`). A clock rejection is refused. Locks are applied after the fact and `clock_s` is ignored (`media.py:275-284`). v1 does not keep that door. `reread_final` still does not seed `core.timeline`. It also stops treating "no Q4 row in this tail" as the condition that confirms. Locks from `timeline.jsonl` go to `decide` as `locks` only.

Removing the seed and leaving the quarter block in `_end_menu` would still reject an unseeded 135645. Both the clock block and the prior-Q4 block leave the confirm path.

### 4.7 Open decision: ties and a final of 1

The owner has not signed off on removing these guards. They stay fail-closed.

* A tied menu score (`away == home`) is not adopted (`qorcap_reader.py:1536-1538`).
* A menu with either side equal to 1 is not adopted (`media.py:264-265`).

The posted result is the unconfirmed monotone lock, `final_reread` `partial`, or `fail` when no lock exists. Deleting either guard is a separate decision. This design does not delete them.

## 5. Code changes

Plan only. This change does not edit these files.

| Where | What changes |
|---|---|
| New `qor_receipt/final_rule.py` | `decide` as in §4.6. QorCap imports it. One copy. |
| `sidecar/qorcap_reader.py` `madden_final_screen` `:819-855` | Keep the geometry. Delete the `:833` sentence that the detector only runs behind the Q4-low-clock gate. |
| `sidecar/qorcap_reader.py` `on_frame` `:1025-1036` | Keep the OPTIONS short-hold return that happens before `_end_menu`. |
| `sidecar/qorcap_reader.py` `_end_context` `:970-975`, `_accept_final_screen` `:999-1010` | Stop using them as a precondition for `_end_menu`. They require quarter and clock today. |
| `sidecar/qorcap_reader.py` `_end_menu` `:1511-1538` | Remove the quarter rejection (`:1515-1517`), the clock rejection (`:1529-1530`), and the `end_menu_consistency` refusal (`:1532-1535`). Keep the tie refusal (`:1536-1538`) and the referee's final-of-1 refusal (`media.py:264-265`) until §4.7 is signed off. Call `decide`. |
| `sidecar/qorcap_endmenu.py` `EndMenuAgreement.offer` `:242-261` | Track `min_conf`. Every frame in the run has `conf` ≥ 0.90. A lower frame clears the run. `n` stays 3. `min_span_ms` stays 400. Frames at or before the latest scorebug read are not offered into the confirming run. |
| `sidecar/qorcap_reader.py` `end_menu_consistency` `:1646-1657` | No longer a refusal. The delta feeds the sidecar note. `end_menu_delta_corroborated` (`:1660-1707`) is not required to confirm. |
| `sidecar/qorcap_reader.py` `_note_end_menu_disagreement` `:1576-1595` | A later different menu fails the last-menu gate. Neither menu score is kept. The receipt score is the monotone lock, `final_confirmed` false. |
| `sidecar/qorcap_match.py` `tail_final_check` `:2495` | Stop `core.timeline = live`. Pass those rows to `decide` as locks. Do not break at the first `end_menu` (`:2520-2521`); scan the window. |
| `sidecar/qorcap_match.py` `apply_final` `:2094-2096` | A missing `final_confirmed` is false. |
| `sidecar/qorcap_match.py` `select_offline_final` `:2431-2445`, `_disputed_final_result` `:2457-2464`, `finalize` `:2887` | Same `decide`. A disputed menu signs the monotone prefix unconfirmed. A later lock that went down is not that score. Offline finalize does not reintroduce a timeline clock seed. |
| `qor_receipt/review/media.py` `reread_final` `:179-201` | Keep "no laptop timeline seed". Do not break at the first `end_menu` (`:190-191`). Delete the `end_menu_fallback` call. Load OPTIONS rising edges from `inputs.jsonl` through `bundle.press_events`. If that file or the picture-start time is missing, a 6-item frame fails closed. |
| `qor_receipt/review/media.py` `end_menu_fallback` `:236-298` | Remove. The reason switch at `:250` and the thresholds at `:205-207` are not the rule. `clock_s` is not read. A prior Q4 row is not required. |
| `qor_receipt/review/engine.py` `_final_check` `:799-828` | A confirmed menu is `pass` when the claim equals the menu score, including a stall and a multi-play delta. A failed gate with a monotone lock is `partial` at that prefix. A note does not flip `pass` to `partial`. Score mismatch stays `fail` (`:807-809`). |
| `qor_receipt/review/engine.py` `_result_from_reread` `:831-847` | Write `decide`'s boolean. The `:840` test `is not False` becomes that boolean. Missing is false. |
| `qor_receipt/referee_ai/integrity.py` `check_final` `:189-202` | An adopted end menu more than one legal step past the last scorebug is not an error. `:201` is that error today, and `referee.py:184` and `:189` both append it. Leaving it in place would FAIL a multi-play menu and downgrade the verdict. `decide` does not adopt a menu below any lock. An unconfirmed backwards lock posts the monotone prefix, and that prefix is the `last_state` `check_final` sees, so the lower row is not a mismatch. A backward menu posted anyway still errors. |
| `sidecar/qorcap_reader.py` `end_screen_evidence_cutoff_ms` `:1710-1733`, filter `:1703-1704` | Replaced by `decide`'s cutoff. Null no longer means "all evidence counts". Fallback is the earliest end screen. The test stays `t_ms >= cutoff_ms`. Evidence from the first end screen through the latest scorebug read is out. | 
| `sidecar/qorcap_reader.py` `_post_agreed` `:1343-1344` | Still clears the running `_end_screen_earliest` pointer. Also clears the menu streak and the agreed score. `decide` re-derives the cutoff from `end_screens`, so a null pointer is not a null cutoff. | 
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

`match-20261024-135645` (qorcap#94, submission `f26ac3935d038288`) is a production submission. It is not on the site. The owner's held-out set is 170123, 021003, 125430, 084523, and 193534. The issue says the client tail rejected the menu because the clock was not 0:00, the last lock was Q4 0:11, and the referee fallback accepted 10–7 from one reported confidence of 0.912 over 485 frames. Per-frame confidences and the three frame timestamps are not in that report. The vector in §7 is labeled synthetic and uses 0.94. Under the rule, a stable end menu after that lock confirms away 10, home 7, with a stall note for 11 unread seconds, and the note does not downgrade a review that is actually run. This design does not re-review the submission.

## 7. Fixtures and tests (plan only)

This change adds no fixture file and no test. When `decide` is implemented, the same JSON bytes go to:

| Repo | File | Runner |
|---|---|---|
| QorTime | `tests/vectors/final_rule/v1.json` | `tests/test_final_rule.py`, calling `qor_receipt.final_rule.decide`, on the existing `pytest -q` job |
| QorCap | `sidecar/fixtures/final_rule/v1.json` | `sidecar/test_final_rule.py`, importing that same function, on `python -m pytest sidecar` |

A hash mismatch fails the test. The two pause cases also have pixel fixtures beside that JSON: `_menu(items=8)` and the OPTIONS short-hold on `_menu(items=6)` from `sidecar/test_final_screen_pause.py`. The 6-item picture alone must not be the pause case. `madden_final_screen` accepts it. In the JSON, `screen` is expanded onto each frame as `pixel` before `decide`. `pause-options-short-hold` keeps `pixel` `end_menu_6` and lists the OPTIONS press. `decide` labels the frame pause because the hold is under 12 s. `options_hold_ms: null` with the presses key absent means OPTIONS was measured and not pressed. `options_presses_ms: null` means `inputs.jsonl` was not read, and `decide` fails that frame closed.

`today` on the stall case is the drift at the commits in the header. `expect` is `final-rule/v1`. The stall case is synthetic (`"synthetic": true`): issue 94 did not publish per-frame confidence or the three frame timestamps, so the vector uses 0.94 and invented times. It does not copy 170123's 10–0 or its stored 0.912. v1 vectors do not carry `LOCKS_084523` or any row from the owner's held-out set (170123, 021003, 125430, 084523, 193534). The `LOCKS_084523` citation in §1.1 is the current referee test, not a v1 input. A JSON `screen` key is expanded onto each frame as `pixel` before `decide`. A frame that already has `pixel` keeps it. `decide` does not take `screen`, `later`, `later_menu`, or `later_live_play`. Review `pass` means the claim equals `decide`'s score and the check does not drop to `partial` because of the note.

```json
{
  "rule": "final-rule/v1",
  "agree_frames": 3,
  "agree_span_ms": 400,
  "min_conf_each_frame": 0.9,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": 4000,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null,
        "final_basis": "absent"
      },
      "illustrates": "The second score is in frames, after the first stable run. decide posts neither menu. The receipt score is the lock."
    },
    {
      "id": "later-live-play",
      "screen": "end_menu",
      "options_hold_ms": null,
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
      "end_screens": [
        5000,
        5400,
        5800
      ],
      "expect": {
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 13,
        "home": 6,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null,
        "cutoff_ms": 5000,
        "final_basis": "absent"
      },
      "illustrates": "A lock at 9000 is after the menu frames. decide reads later live play from that lock. Those frames are not fresh."
    },
    {
      "id": "score-went-down",
      "screen": "end_menu",
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
        "cutoff_ms": null
      }
    },
    {
      "id": "scorebug-return-resets-agreed-score",
      "screen": "end_menu",
      "options_hold_ms": null,
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
      "end_screens": [
        2000,
        20000
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
        "downgrades_verdict": false
      }
    },
    {
      "id": "menu-frame-equal-to-scorebug-not-fresh",
      "screen": "end_menu",
      "options_hold_ms": null,
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
      "end_screens": [
        5000,
        5400,
        5800
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
        "final_basis": "absent"
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
      "options_hold_ms": null,
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
      "end_screens": [
        2000,
        20000
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
        "downgrades_verdict": false
      }
    },
    {
      "id": "cutoff-falls-back-to-earliest-end-screen",
      "screen": "end_menu",
      "options_hold_ms": null,
      "locks": [
        {
          "t_ms": 10000,
          "away": 10,
          "home": 7,
          "quarter": 4,
          "clock_s": 11
        }
      ],
      "frames": [
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
      "end_screens": [
        2000
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
        "adopt_menu": false,
        "final_confirmed": false,
        "away": 10,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "review": "partial",
        "note": null,
        "cutoff_ms": 2000,
        "evidence_counts": [
          "before-first-screen"
        ]
      }
    },
    {
      "id": "qorcap-98-touchdown-lock-same-ts",
      "source": "qorcap#98",
      "synthetic": true,
      "illustrates": "A touchdown lock is flushed at the same timestamp as a menu frame. Evidence is at that timestamp and 250 ms later. No end screen is strictly after the lock, so the cutoff falls back to 8000. Neither evidence row counts. The frame is not fresh. The case does not confirm.",
      "screen": "end_menu",
      "options_hold_ms": null,
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
      "end_screens": [
        8000
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
        }
      }
    },
    {
      "id": "duplicate-timestamps",
      "screen": "end_menu",
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
      "illustrates": "A stale replay lock at 3-0 is after the menu and below the earlier 14-7 lock. It is not the posted score. The posted score is 14-7, the last row of the monotone prefix. The final stays unconfirmed because that later lock is the latest timestamp.",
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
        "away": 14,
        "home": 7,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "posted_score": {
          "away": 14,
          "home": 7
        },
        "backwards_lock": {
          "away": 3,
          "home": 0,
          "t_ms": 12000
        }
      }
    },
    {
      "id": "menu-below-an-earlier-lock",
      "screen": "end_menu",
      "options_hold_ms": null,
      "illustrates": "The menu equals the later lock and is below the earlier lock. Scores never go down checks every lock. The posted score is 21-0, not 14-0.",
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
        "away": 21,
        "home": 0,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "partial",
        "note": null,
        "posted_score": {
          "away": 21,
          "home": 0
        },
        "backwards_lock": {
          "away": 14,
          "home": 0,
          "t_ms": 2000
        }
      }
    },
    {
      "id": "tie-menu-stays-unconfirmed",
      "screen": "end_menu",
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
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
      "options_hold_ms": null,
      "illustrates": "No lock, and two menus that disagree. Posted score is null. Neither menu is adopted. method is last_scorebug_read_unconfirmed. final_basis is absent. final_confirmed is false. final_reread is fail.",
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
        "away": null,
        "home": null,
        "method": "last_scorebug_read_unconfirmed",
        "final_basis": "absent",
        "review": "fail",
        "note": null,
        "cutoff_ms": 5000
      }
    },
    {
      "id": "referee-cannot-measure-hold",
      "screen": "end_menu",
      "options_presses_ms": null,
      "illustrates": "options_presses_ms is null: inputs.jsonl was not read. The frames are six-item and have no picture_since_ms. decide does not pass. Official review is partial at the lock.",
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
        "cutoff_ms": null
      }
    }
  ]
}

```

The stall case's three frames are the gate, at confidence 0.94 on invented timestamps. `reported_frames: 485` records the issue's count. The case is `"synthetic": true`. Issue 94 did not publish per-frame confidence or these timestamps. An implementation that checks only the first frame's confidence fails `conf-below-0-90-on-one-frame`.

`duplicate-timestamps` lists `5000`, `5000`, `5400`. That is two instants, and it does not confirm. `backwards-timestamp` lists `5000`, `5800`, `5400`. Sorting those into `5000`, `5400`, `5800` would be a stable run. The decision fails closed and does not confirm. `unreadable-frame-resets-run` has one good frame, one `ok: false` frame, and two good frames. The unreadable frame resets the streak. Bridging across it would be three frames and must not confirm. `unreadable-then-new-run-confirms` has three fresh frames after `ok: false` and does confirm, so the unreadable frame does not abort the rest of the decision. `conf-0-75-resets-run` is the 0.70–0.89 band, and it resets the same way. `conf-0-75-then-new-run-confirms` shows the new run.

`decide` derives end screens from every frame it still labels an end menu. A listed `end_screens` that drops a later readable frame is not the cutoff. `menu-frame-equal-to-scorebug-not-fresh` therefore has end screens at 5000, 5400, and 5800. The lock is at 5000, so the cutoff is 5400. The frame at 5000 is not fresh, and two fresh frames do not confirm. Evidence at 5000 is in the dead zone. Evidence at 5200 counts. Evidence at 5400 does not (`t_ms >= cutoff_ms`).

`qorcap-98-touchdown-lock-same-ts` is the qorcap#98 shape with synthetic scores away 13, home 6. The touchdown lock and the menu frame share `t_ms` 8000. Evidence is at 8000 and 8250. No end screen is strictly after 8000, so the cutoff falls back to 8000. Neither evidence row counts. The frame is not fresh. The case does not confirm. The posted score is the lock, 13–6, and `final_reread` is partial.

`later-live-play` has a lock at `t_ms` 9000 after the menu frames. `decide` reads that from `locks`. There is no `later_live_play` field. The frames are not fresh. The posted score is 13–6. `later-contradicting-menu` puts the second score in `frames` (17–7 after 10–7). Neither menu is posted. The receipt score is the lock. `no-lock-contradicting-menus` has no lock and two menus that disagree: posted score null, neither menu adopted, `method` `last_scorebug_read_unconfirmed`, `final_basis` absent, `final_confirmed` false, `final_reread` fail.

`replay-lock-lower-after-menu` posts 14–7, the last row of the monotone prefix, and does not post the later 3–0 lock. `menu-below-an-earlier-lock` posts 21–0. `tie-menu-stays-unconfirmed` and `final-of-1-stays-unconfirmed` stay unconfirmed (§4.7). `null-clock-long-gap` confirms 6–3 from a null raw `clock_s` and a 19000 ms wall gap. The stall note is on the sidecar and `downgrades_verdict` is false. `referee-cannot-measure-hold` has `options_presses_ms` null and does not pass.

`scorebug-return-resets-agreed-score` confirms 10–7 from the frames after the lock and drops the 3–0 run. `cutoff-dead-zone-and-inclusive-equal` keeps evidence before the first end screen and evidence strictly between the scorebug and the fresh menu, and drops every timestamp from the first end screen through the scorebug, plus every timestamp `>= cutoff_ms`. `cutoff-falls-back-to-earliest-end-screen` has no end screen after the lock, so `cutoff_ms` is 2000 and the menu frames before the lock do not confirm. `pause-options-short-hold` carries `pixel` `end_menu_6`, `picture_since_ms` 1000, and `options_presses_ms` `[800]`. The hold at the first frame is 4000 ms. `decide` labels those frames pause.

## 8. Schema

`receipt.v0` stays frozen. `schema/**` is not part of this change. Pages publishes `schema/**` (`.github/workflows/pages.yml`). No new `result` field, no new `final_basis`, no new method string. `end_menu_read` and `last_scorebug_read_unconfirmed` are already in the contract.

`final.rule.json` is a sidecar, same class as `final.tail.json` and `device_attest.v0.json`. Its schema file is [`docs/design/final.rule.v1.schema.json`](final.rule.v1.schema.json), outside `schema/**`. Every final the new code produces writes that sidecar with `"rule": "final-rule/v1"`, including an unconfirmed final. The public note is copied out of `note` when the match page is built. `evidence_counted` stays on the file and is not rendered. The sidecar is not hashed into the receipt. An old bundle with no sidecar still verifies. The page shows no v1 note for it.

## 9. Rollout and compatibility

QorTime merges `qor_receipt.final_rule` first. QorCap then re-vendors that package and pins the merge. The client does not ship a private copy of the function ahead of the referee.

A receipt from a client at v0.3.32 or earlier keeps the status it was signed with. This rule change does not upgrade it to VERIFIED. VERIFIED requires a new official review signature.

When that re-review is actually run, the referee re-read is authoritative:

* The claim's numbers are compared to `decide`'s score. A difference is a `final_reread` fail, shown as a mismatch. The stored claim is not overwritten.
* The claim's `method` can be `last_scorebug_read_unconfirmed` while the re-read confirms the same numbers. The check may pass on the numbers. The receipt is not rewritten to VERIFIED by the rule change itself. The new review signature is what would change the status, and this design does not issue one.
* `check_final` no longer errors when an adopted end menu is more than one legal play past the last scorebug. That changes a future verification of a bundle signed under the old rule: an old integrity error of that one shape would not fire again. Other errors still fire. Published pages are not rebuilt by the merge.

Two sidecars can exist. The page shows the referee's `final.rule.json` when the review wrote one. Otherwise it shows the client's file. When both exist and the scores differ, the page shows the referee score and a visible note of the client's score. It does not average them and it does not hide the difference.

`rule` on the sidecar is how a reader tells a v1 final from an older one. An old bundle has no file and no stamp.
