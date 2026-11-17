# Qor shared design system (`qor-*`)

One look across **Receipts** (qortime.com), **Capture** (qortime.com/cap) and **Watch** (QorStream).
Plain CSS + plain ES modules. No npm, no TypeScript, no bundler, no build step. All asset paths are relative.

## Files

| File | What it is |
|---|---|
| `tokens.css` | CSS custom properties: color, type, spacing, radius, shadow, motion. Base reset. Per-site lane lead via `data-qor-site`. |
| `nav.css` / `nav.html` / `nav.js` | Shared top nav: Q weave mark, Receipts / Capture / Watch, three-strand "knot" active indicator, mobile sheet (Esc closes, focus trap, focus restore), `<qor-nav>` custom element optional. |
| `motion.css` / `motion.js` | Motion primitives: `.qor-reveal` (scroll reveal), `.qor-shimmer` (weave shimmer masked to the logo), `.qor-section-weave` (section transition divider), `.qor-lanes`. Everything is static under `prefers-reduced-motion`. |
| `footer.css` / `footer.html` / `footer.js` | Family footer: brand, tagline, Receipts / Capture / Watch links, record line, legal slot. `<qor-footer>` optional. |
| `brand/` | Logo assets (copied unchanged from qorstream `web/public/`): `logo.svg`, `logo-icon.svg`, `favicon.svg`, `favicon-32.png`, `apple-touch-icon.png`, `icon-512.png`. |
| `demo.html` | The nav in all three active states, motion primitives, semantic gold, footer. |
| `shots/` | Demo screenshots (desktop + mobile). |
| `carbon/` | Carbon prompt, raw output and notes. |

## Static HTML drop-in (GitHub Pages, no build)

1. Copy this folder into the site, e.g. as `qor-ui/` (keep `brand/` inside it, or point the paths at your own copy).
2. In `<head>`, set the site on `<html>` and load the CSS:
   ```html
   <html lang="en" data-qor-site="receipts">   <!-- receipts | capture | watch -->
   <link rel="stylesheet" href="qor-ui/tokens.css">
   <link rel="stylesheet" href="qor-ui/motion.css">
   <link rel="stylesheet" href="qor-ui/nav.css">
   <link rel="stylesheet" href="qor-ui/footer.css">
   ```
3. Paste `nav.html` right after `<body>` and `footer.html` before `</body>`. Then:
   - set `data-active` on the `<header data-qor-nav>` to `receipts`, `capture` or `watch` (nav.js sets `aria-current` from it);
   - change the image paths `brand/logo-icon.svg` to `qor-ui/brand/logo-icon.svg` (both the `<img src>` and the `--qor-shimmer-mask` url);
   - change the wordmark text (`QorTime` + sub-label) and the brand `href` for that site;
   - the `Self-Host` CTA is optional; delete the two `qor-nav__cta` anchors if the site does not need one.
   - the mobile sheet `id` / `aria-controls` must be unique if a page ever has more than one nav.
4. Before `</body>`:
   ```html
   <script type="module">
     import "./qor-ui/nav.js";
     import "./qor-ui/footer.js";
     import { initReveal, initSectionTransitions } from "./qor-ui/motion.js";
     initReveal();
     initSectionTransitions();
   </script>
   ```
   Add `class="qor-reveal"` to anything that should rise in on scroll, and `<div class="qor-section-weave" aria-hidden="true"></div>` between sections.

ES modules do not run from `file://`. Preview with any static server (e.g. `python3 -m http.server`) or on Pages itself.

## The Watch link: one config spot

Links are set from attributes on the nav root (and the footer root). The anchors are patched by nav.js / footer.js:

```html
<header class="qor-nav" data-qor-nav data-active="receipts"
  data-receipts-href="https://qortime.com"
  data-capture-href="https://qortime.com/cap"
  data-watch-href="#watch-coming-soon">
```

On qortime.com, Watch is **`#watch-coming-soon`** until the public Watch URL exists. When it does, change `data-watch-href` on the nav (and the footer) and nothing else. The same default lives in `nav.js` / `footer.js` (`DEFAULTS.watchHref`) for the `<qor-nav>` element form. The QorStream landing itself sets `data-watch-href="/"`.

## Gold is semantic only

`--qor-verified` (`#e4c56d`) and `--qor-verified-dim` exist **only** to mark a referee-VERIFIED match record (utility class `.qor-verified-record`). Never use them for decoration, gradients, nav, footer, buttons, practice records or marketing. If a record is not referee-VERIFIED, it is not gold.

## Brand

Navy base, blue `--qor-blue` `#4f7cff`, cyan `--qor-cyan` `#2ef2ff`, strand white. The three strands of the Q weave are the motion idea: the nav knot sits under the active product, `data-qor-site` decides which strand leads, section dividers carry the strands between sections.

## Wording

Fair-proof / skill-proof language only. Avoid absolute claims about players or skill; when a number isn't measured, say UNMEASURED.
