# QorTime

**Repo:** https://github.com/ConWan30/qortime · **Site:** https://qortime.com/ · **Verifier:** https://qortime.com/verify/

## What it is
QorTime produces a **signed Madden end-of-game receipt**: final score, teams, how the final was decided, and the evidence chain, signed with Ed25519. Anyone can drop the receipt into the public verify page and get **VERIFIED / PARTIAL / FAIL**. Change one number and it fails.

History: it started as **qor-receipt** (the Python package is still `qor_receipt`, and `qortime` is a CLI alias). It is a pivot away from the shelved **Qoresence** project.

## Architecture
- **Signatures:** Ed25519 over **JCS** (RFC 8785 canonical JSON). Code: `qor_receipt/jcs.py`, `keys.py`, `receipt.py`. Spec: `spec/match-receipt-v0.md`. Schema: `schema/receipt.v0.json`.
- **Checkpoints:** a hash chain (frames_hash + input_hash, each one linked to the previous) that the referee co-signs. Code: `qor_receipt/checkpoints.py` (`make_checkpoint`, `countersign`, `Chain`, `verify_chain`).
- **Nonce challenges** (proof a human is playing): the referee sends a nonce. The player must pause and press a 4-button pattern derived from it, and both the controller log and the video must show it within the window (about 600 ms). Code: `qor_receipt/challenge.py`. Motion correlation is supporting evidence only, not proof.
- **Referee:** a FastAPI app with in-memory state (`qor_receipt/referee/app.py`): matches, challenges, checkpoints, timeline, finalize, sign, receipt. **It is not hosted anywhere yet.**
- **Trust tiers:** `compute_tier` in `receipt.py` (DUAL / SINGLE). The verifier returns VERIFIED or PARTIAL. Without an independent hosted referee, receipts are **PARTIAL / self-attested**.
- **`result.final_basis`:** `clock_zero` | `scorebug_gone` | `final_screen`. **`final_screen` is only a hook.** No final-screen detector exists.
- **Score reader** (vendored glyph reader, `qor_receipt/_vendor/qoresence/vision/local_scorebug`, pack `madden27_standard_v1.npz`):
  - Madden 27 scorebug: **left score = AWAY, right score = HOME** (fixed in #8). Display as `away @ home`.
  - Fail-closed. It needs several agreeing reads (`final_detect.py`: 3-read agreement). The **red clock** (final minute) needs **5 agreeing reads** plus a structural minute=0 check.
- **Agent** (`qor_receipt/agent/`): Windows DirectShow capture, DualSense USB HID reader (`hid.py`), pause detection, live and replay runners. Docs: `docs/AGENT.md`, `docs/PHASE2.md`, `docs/RISKS.md`.
- **Browser verifier:** `verify/` (noble-ed25519, `verify.js`). Test: `node verify/test.mjs`.
- **Site:** `site/` (WebGL seal-clock hero, GSAP/ScrollTrigger, Lenis, reduced-motion fallback), deployed by `.github/workflows/pages.yml`.

## Merged PRs
| PR | What it did |
|---|---|
| #1 | Windows capture agent (DShow, DualSense HID, pause detect, runner) plus replay mode |
| #2 | Scorebug clock made best-effort so live reads agree; read-health logging; live fixtures |
| #3 | QorTime rebrand, `qortime` CLI alias, marketing site at the Pages root, verifier at /verify/ |
| #4 | Site v2: WebGL seal-clock, GSAP scroll story, Lenis |
| **#5** | README: accurate status (agent built, live reader results, site live, honest gaps) |
| **#6** | eval: hand labels for **236 Madden 27 frames** (`eval/labels/frames-2026-10-13.jsonl`) plus `eval/score_reader.py` scoring |
| **#7** | End-of-game hardening: read the red clock (fail-closed), `scorebug_gone` / `final_screen` fallbacks, `result.final_basis` |
| **#8** | Home/away fix: away is on the left. Replay vector regenerated, `away@home` display |
| **#9** | Red-clock gate: structural minute=0 check plus a stricter seconds margin. **Red 14/16, 0 wrong. All clocks: 40 correct, 122 refused, 0 wrong** |

## Run it
```bash
pip install -e ".[dev,vision]"
ruff check . && pytest -q
node verify/test.mjs
python eval/score_reader.py      # reader accuracy against the hand labels (see eval/README.md)
```
CI (`.github/workflows/ci.yml`) runs ruff, pytest and the node verifier test. Keep it green. Con allows autonomous merging of fix PRs only when CI is green.

## Honest gaps
1. **No hosted referee server**, so receipts top out at **PARTIAL / self-attested**.
2. **No final-screen detector**: `final_screen` is a hook only.
3. **No controller/HID data in real receipts yet.** `hid.py` exists, but the first real QorCap test captured video only.
4. The reader refuses many clock frames (122 of 162). That is safe, but it means the end of a game needs enough agreeing frames.
