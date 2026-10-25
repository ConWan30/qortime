# Roadmap (in priority order)

## 1. Real full-game test of v0.2.1 (next)
- Get **v0.2.1** onto MyTurn (from the release zip) next to `C:\QorCap\qorcap-v0.2.0-windows-x64`.
- Close OBS, run **Start Match.cmd**, and play a **full game to Q4 0:00**. Confirm the signed self-attested receipt passes on the verify page.
- Confirm separate audio capture works, or that the video-only fallback kicks in cleanly.
- If the game ends without a clock_zero read, try **Finalize Last Match.cmd**.

## 2. Hosted referee server (unlocks the VERIFIED tier)
- Deploy `qor_receipt/referee/app.py` with persistent storage and its own key (Railway, Supabase and Convex connectors are available). QorCap should then countersign against it instead of local keys.

## 3. Controller HID evidence
- The **DualSense Edge is plugged into the laptop by USB while it plays over Bluetooth to the PS5. That setup is intended.** Log HID (`qor_receipt/agent/hid.py`) into the bundle so input_hash and challenges use real data.

## 4. Final-screen detector
- Implement `final_basis = final_screen` (it is only a hook today). Keep it fail-closed.
- Done: Jev 1.14 shadow detector (`qor_receipt/agent/final_screen_jev.py`, hook `AgentCore.final_screen_shadow`).
  It logs only and never sets the basis. What the final frame looks like (from Con, Madden 27): when the clock
  hits 0:00 Madden goes straight to an end-of-game **options menu showing the final score** (team names and
  scores); the bottom scorebug bar is not shown. The Jev criteria describe exactly that and reject the mid-game
  pause menu (closest look-alike), halftime screens, touchdown graphics and broadcast overlays. Shadow records
  also carry `last_scorebug` (last agreed quarter/clock) so the deterministic rule can be
  "last clock seen was Q4 near 0:00" + "menu with final score". Next: capture real end-of-game frames and run
  `scripts/jev_final_screen_smoke.py` on them for recall. Then use `export_labels` shadow labels to build a
  deterministic local detector for `final_screen_detector`. Jev must not become the basis itself.

## 5. Meta Global AI Developer Hackathon: AI Referee
- **Dates:** Oct 26 – Nov 4 2026, virtual, **Indie track**. Application **Pending**. Approvals arrive by **Oct 26, 9 AM ET**.
- Applied through Cerebral Valley with **qoresence@mail.grokbot.com**, handle **conwanzo**.
- **Concept:** a separately signed ruling from a Meta model (its own `ai-referee` key, stored in a `model_attestations[]` block). It **never overrides** the deterministic verdict, and `compute_tier` ignores it. Models: **Muse Spark + SAM 3.1**. $150 API credits.
- **Judging:** Impact 30 · Meta tooling 25 · Demo 25 · Tech depth 20.
- **Cut lines:** Oct 31 and Nov 2. **Submit by Nov 4.** Aim for **5 PM CT**. The official deadline in the plan was 9 PM ET / 8 PM CT, so 5 PM CT leaves a buffer.
- Full plan: `ai-referee-plan.md`, `application.md` and `answers.txt` on the original bot's box (`/workspace/meta-hackathon/`). Ask Con for copies if needed. The plan's key point: the model can only cite hash-anchored evidence.

## 6. Monetization
- **Stripe** account in **test mode**, created with qoresence@mail.grokbot.com, `acct_1UQVjXCatAPuUvUA`. **Business details and KYC are not done.** The Stripe connector is **not connected** because Con got locked out after login attempts.
- Plan: paid **"Verified Match" receipts**, **tournament licenses**, sponsors. Model: a bot-run business in the style of Alex Finn.
- **Caution:** this is gambling-adjacent (money matches). Stay a neutral evidence and verification service, don't handle wagers, and check the law before going live.

## 7. X marketing
- **@QorTimeHQ** (renamed from @Qoresence). The launch post is pinned.
- Posts **2–5** are queued, one per day at **6:50 PM CT** (Oct 15–18), from `qortime-x/posts/queue.md` on the original box. Con approved them on 2026-10-14: post exactly as written and mark each one posted. Copy of the queue below.
- The account is **"Under review"**, and the X subscription shows a **payment-failed** warning. Tell Con. Don't try to fix billing yourself.
- **@Qortroller** is reserved for the future QorTroller product.

<details><summary>Queued posts (approved 2026-10-14)</summary>

2 (Oct 15): Tested the score reader on 236 hand-labeled Madden 27 frames. Wrong reads: 0. When it's unsure, it refuses instead of guessing. A missed read is fine. A wrong one isn't.

3 (Oct 16): Drop a QorTime receipt into the verify page and get VERIFIED, PARTIAL or FAIL. Edit one number in the score and it fails. No account, no trusting us. https://qortime.com/verify/

4 (Oct 17): How do you prove a person is playing, not a bot? At random moments the referee asks for a pause plus a button pattern. The controller log and the video both have to show it within 600 ms.

5 (Oct 18): QorTime is early: no partners, no customers yet. The code is open → https://github.com/ConWan30/qortime. If you run money matches or tournaments, I'd love to hear what proof you'd need.
</details>
