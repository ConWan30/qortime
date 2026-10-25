# QorTime — Discovery / Findability Plan

**Date:** 2026-10-15 (CT)  
**Product:** Signed Madden end-of-game receipts + public verify at https://qortime.com/  
**Upsell:** Verified Match — $9.99 hosted-referee countersign (**early access / test until Stripe KYC**; do not claim live paid product for real money)

**Constraint:** Research + drafts only. No third-party sends from bots.

---

## 1. Site pages to build (priority order)

Current site: marketing homepage + `/verify/` only. No pricing, no Verified Match CTA, no dedicated how-it-works URL.

| Page / asset | Path (proposed) | Why | Copy honesty |
|---|---|---|---|
| **Pricing / early access** | `/pricing/` or `#pricing` on home | Makes Verified Match findable; Google/“pricing” queries; CTA funnel | Label **Coming soon / Early access / Test checkout** until KYC. Link test Payment Link only behind “test mode” language. Never imply live real-money charges. |
| **How it works** | `/how-it-works/` (or deep-link anchors already on home: `#capture` `#challenge` `#chain` `#receipt`) | Shareable URL for outreach; SEO long-tail | Keep honest limits (account sharing, collusion, capture-card FOV). |
| **Verify CTA polish** | `/verify/` + home hero | Primary conversion for platforms evaluating the product | Already live. Add “Paste a demo receipt” + link to sample JSON in repo. |
| **Verified Match explainer** | section on pricing or `/verified-match/` | Separates free self-verify from $9.99 hosted referee | Early access; countersign after completed match; test Stripe until KYC. |
| **For platforms** | `/for-platforms/` (short) | B2B landing for Locked / TrustBridge / MatchPoint outreach | Integration sketch: accept receipt JSON → call verify → pay out. No fake partners. |

### Immediate homepage CTAs (before new pages)

1. Nav: add **Pricing (early access)** and keep **Verifier**.
2. Hero secondary button: “Verified Match — early access” → pricing section (not a naked live buy claim).
3. Footer: verify URL, GitHub, `@QorTimeHQ`, email for Con when approved.

---

## 2. SEO / Open Graph

**Already present (keep):**

- `<title>`, meta description, canonical `https://qortime.com/`
- `og:title`, `og:description`, `og:url`, `og:image` → `assets/og.png`
- `twitter:card` = `summary_large_image`

**Gaps to close:**

| Item | Action |
|---|---|
| `twitter:title` / `twitter:description` / `twitter:site` | Add; set `twitter:site` to `@QorTimeHQ` |
| Per-page titles | Pricing / How-it-works / Verify need unique titles + descriptions |
| `robots.txt` + simple `sitemap.xml` | List `/`, `/verify/`, new pages; submit via Google Search Console when Con owns the property |
| Structured data | Optional `SoftwareApplication` / `WebApplication` JSON-LD (name QorTime, url, description; **offers** only as early-access / TBD price until live) |
| OG image refresh | Optional second OG for “Verified Match early access” when pricing page ships |
| Keywords in body | Natural phrases: “Madden match receipt”, “verify console score”, “money match dispute”, “hosted referee” — not stuffing |

**Do not:** Claim EA / NFL partnership, MCS affiliation, or live paid Verified Match.

---

## 3. GitHub README findability

README already positions QorTime for escrow platforms (Locked, TrustBridge, ArcadeBet, Wagr) and links site + verifier.

**Add near the top (badge / callout block):**

```markdown
## Try it
- Site: https://qortime.com/
- Verify in browser: https://qortime.com/verify/
- **Verified Match** ($9.99 hosted-referee countersign): early access / Stripe test mode until business KYC — not a live paid product yet.
```

Also:

- Topics: `madden`, `esports`, `ed25519`, `receipt`, `ps5`, `verify` (GitHub topics UI)
- Pin a short “Demo receipt” link in About
- Keep MIT + honest Status / Known gaps section (trust signal for platforms)

---

## 4. X (@QorTimeHQ) — pin + thread outline

**Account:** [@QorTimeHQ](https://x.com/QorTimeHQ) (brand). Personal [@ConWanZo](https://x.com/ConWanZo) for replies when Con wants human tone.  
**Do not post from bots** unless Con already has an approved draft→send workflow. Prefer drafts in this folder.

### Pin (draft concept — Con posts when ready)

> The final whistle, sealed.  
> QorTime reads the Madden scorebug, logs controller reports from the same session, and seals an Ed25519 receipt a referee co-signs. It does not prove who held the controller, or that the console obeyed that pad. Anyone verifies in-browser — nothing uploaded.  
> Verify: https://qortime.com/verify/  
> Verified Match (hosted referee countersign, $9.99): **early access / coming soon** until we finish KYC.  
> Spec + code: github.com/ConWan30/qortime

### Thread outline (6–8 posts)

1. **Hook** — Money-match disputes are still screenshots + vibes. We built a signed receipt.
2. **Capture** — Scorebug off the capture card; lock only on frame agreement.
3. **Challenge** — Referee nonce → DualSense pattern in HID + video ≤600 ms.
4. **Chain** — Rolling hashes ~4s; referee countersigns so you can’t forge the past later.
5. **Seal** — Agents + referee sign; dual-attested if both timelines agree.
6. **Verify** — Paste JSON in browser → VERIFIED / PARTIAL / FAIL. Link `/verify/`.
7. **Honest limits** — Doesn’t prove who physically holds the pad; doesn’t stop collusion.
8. **Verified Match CTA** — Hosted referee countersign as **early access**; $9.99 list price; Stripe still test until KYC. Soft ask: platforms DMs welcome via Con.

Media: OG image, scorebug mock (no EA marks), receipt JSON snippet screenshot.

---

## 5. Communities — list only (no spam)

Post only if Con manually joins and follows each community’s rules. Prefer “show the verifier” over “buy now.”

### Reddit (list)

| Subreddit | Fit |
|---|---|
| r/Madden | Broad; money-match / dispute threads |
| r/MaddenFranchise | Money franchise / league recruiting |
| r/Madden25 / r/Madden26 / seasonal subs | Title-version communities (check which are active for 26/27) |
| r/PS5 | Occasional H2H / peripheral posts — low priority |
| r/esports | Platform / tooling angle — careful, not hype |
| r/SideProject / r/IndieDev | Builder audience for open receipt format |

### Discord (list — public discovery pages / known invites)

| Community | Notes |
|---|---|
| Bless League | Money Madden league ($25 buy-in); discord.me/bless-league |
| GFL Money League | Recruiting posts point to discord.gg/zuaEJJPQ3 (verify invite still live before joining) |
| EA-forum money franchises | Often Discord-ran (e.g. discord.gg/Kjj5Qk4c from EA forum post — verify) |
| MCS official | Challengermode MCS Discords (e.g. discord.gg/mcs27 / MCS26 variants) — **rules-heavy; no unsolicited promo** |
| NeonSportz-connected league Discords | Commissioners already run score/import workflows |
| Compete4ever / ISG Madden | Player Discords are often **current-players only** — do not cold-spam; contact organizers via their signup sites instead |

### Other

- EA Answers / Madden franchise forums (recruiting threads → commissioner outreach, not product spam)
- Challengermode MCS tournament pages (spectator/competitor context)

---

## 6. Money-match / Madden / esports platforms that fit

Platforms where escrow + dispute volume makes a **machine-checkable receipt** valuable. Prefer those already using screenshots / human mods / AI on screenshots.

| Platform | URL | Fit |
|---|---|---|
| **Locked** | https://getlckd.com/play/madden-26 | Madden 1v1 money matches; escrow; **both submit screenshot**; human disputes ≤24h. Strongest product fit. |
| **TrustBridge** | https://www.trustbridge.gg/ | PS-focused escrow; AI on screenshots + PSN cross-check. Named in README. (Site fetch flaky — confirm live before outreach.) |
| **MatchPoint** | https://matchpointgaming.org/ | Lists **Madden NFL 27**; escrow; evidence disputes. Contact: /contact |
| **ArcadeBet** | https://arcadebet.gg/ | Screen-share + AI winner detection; Madden listed. Complementary / competitive — angle = open verify + offline receipt. X `@ArcadeBet` was suspended (uncertain brand health). |
| **DuelX** | https://playduelx.com/ | Escrow + dual report + human evidence queue. Games skew CoD/2K/RL today — **ask Madden roadmap** or general receipt API. |
| **TrenPlay** | https://trenplay.gg/ | On-chain escrow; screenshot disputes 24–48h. Good “cryptographic receipt” angle. |
| **NeonSportz** | https://www.neonsportz.com/ | Not escrow; **league commissioners** care about disputed scores / imports. Soft tool pitch. |
| **Compete4ever / ISG** | https://compete4ever.com/maddenleague/ · https://theisgshow.com/madden/ | Weekly Madden money matchups (TV). Organizer outreach, not player spam. |
| **Challengermode (MCS)** | https://www.challengermode.com/s/MCS27 | Official MCS ops. Long-shot B2B; contact via challengermode.com/contact |
| **Wagr** | https://wagr.co/ | Skill-gaming cash app — **uncertain Madden H2H fit**; verify before prioritizing. |
| **PvP Protocol** | https://pvpprotocol.com/DisputeCenter | Explicit dispute center; admin evidence review. |
| **Match Arena** | https://matcharena.app/ | Appears in money-match SERP — **verify still active** before outreach. |

---

## 7. Measurement (lightweight)

- GitHub traffic / clones after README callout
- Site: GitHub Pages analytics or Plausible later (privacy-friendly)
- X: pin impressions, profile visits, link clicks (manual)
- Outreach: reply rate in a simple spreadsheet Con owns

---

## 8. What “done” looks like for findability (30 days)

1. Pricing + Verified Match early-access section live on site  
2. README callout + topics updated  
3. Pin + one educational thread drafted and posted by Con  
4. 5+ platforms / organizers contacted **by Con** from approved drafts  
5. At least one demo receipt linked from verify page  

