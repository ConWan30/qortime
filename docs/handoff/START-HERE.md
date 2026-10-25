# START HERE: QorTime + QorCap handoff

This folder lets a new Grok Bot, with no memory of earlier work, pick up QorTime and QorCap development. The same docs live in both repos:
- `ConWan30/qortime` → `docs/handoff/`
- `ConWan30/qorcap` → `handoff/` (repo root, on purpose: `qorcap/docs/` is the GitHub Pages site and anything there gets published)

Written 2026-10-14 (America/Chicago).

## Who you work for
- **Contravious "Con" Battle**, GitHub **ConWan30**. Lives in Bastrop, LA. Time zone **America/Chicago** (report times in CT).
- Usually on his **phone**. Keep replies short and skimmable, and put links where he can tap them.
- **Very high design bar.** He wants cinematic, premium UI: glass panels, depth, rich motion (WebGL, GSAP/ScrollTrigger, Lenis), and a reduced-motion fallback. Nothing generic or "bootstrap".
- **Con commits and decides. Bots propose.** He gave standing permission to **merge fix PRs on your own when CI is green**. Larger product or scope changes need his OK.
- **Never send messages, emails, posts or DMs as him without his explicit OK** for that specific message.
- **Never print secrets** (tokens, passwords, keys) in chat, logs, commits or docs.

## Product vision (one paragraph)
Money matches and ladder games in Madden run on trust. **QorTime** turns a game into a **signed end-of-game receipt**: the final score and evidence, signed with Ed25519, hash-chained, and checkable by anyone on a public verify page with no account. **QorCap** is the Windows capture-card recorder that produces the tamper-evident video evidence (segment hash chain plus checkpoints) and runs QorTime's score reader. The goal is "VERIFIED" receipts co-signed by a hosted referee. Later: paid Verified Match receipts, tournament licenses, and an AI Referee that writes an explanatory ruling which never overrides the math.

## Current state at a glance (2026-10-14)
| Area | State |
|---|---|
| QorTime repo | PRs #1–#9 merged, CI green. Site live: https://qortime.com/ and verifier at /verify/ |
| Score reader | 236 hand-labeled frames: **0 wrong reads**. Clocks 40 correct / 122 refused / 0 wrong. Red clock 14/16 |
| QorCap | v0.1.0, v0.2.0, v0.2.1 released. Site: https://conwan30.github.io/qorcap/ |
| First real test | 2026-10-14 on laptop **MyTurn**: about 14.5 min recorded, 439 segments, verify-bundle OK. Game didn't reach Q4 0:00, so **no signed receipt** yet |
| v0.2.1 on MyTurn | **Not installed yet** (v0.2.0 is at `C:\QorCap\qorcap-v0.2.0-windows-x64`) |
| Trust tier | Tops out at **PARTIAL / self-attested**. There is no hosted referee server yet |
| Meta hackathon | Applied (Pending). Oct 26 – Nov 4 2026, Indie track |
| X | @QorTimeHQ, launch post pinned, posts 2–5 queued. Account "Under review" |
| Stripe | Test-mode account created. KYC not done. Connector not connected |

## Reading order
1. This page
2. [QORTIME.md](QORTIME.md): protocol, reader, PR history, gaps
3. [QORCAP.md](QORCAP.md): recorder, API, first real test log
4. [ROADMAP.md](ROADMAP.md): what to do next, in priority order
5. [ACCOUNTS-AND-ACCESS.md](ACCOUNTS-AND-ACCESS.md): what you can and can't reach
6. [NEW-BOT-PROMPT.md](NEW-BOT-PROMPT.md): the paste-in prompt that started you

## Shelved side projects (don't restart unless Con asks)
- **Qoresence**: the earlier, broader "presence/truth" project. QorTime grew out of it (some code is vendored at `qor_receipt/_vendor/qoresence`). Shelved.
- **SATURDAY SURGE**: a college-football game concept. **Con stopped it.** Files are only on the original bot's box at `/workspace/cfb-impact-spec`, which you probably can't reach.
- **QorTroller**: a future controller product. The X handle @Qortroller is reserved for it.
