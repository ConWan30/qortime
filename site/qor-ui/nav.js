const DEFAULTS = {
  home: "/",
  receiptsHref: "https://qortime.com",
  captureHref: "https://qortime.com/cap",
  watchHref: "#watch-coming-soon",
  logoSrc: "brand/logo-icon.svg",
};

const reducedMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function resolveConfig(el) {
  const ds = el.dataset || {};
  return {
    active: el.getAttribute("active") || ds.active || "",
    home: el.getAttribute("home") || ds.home || DEFAULTS.home,
    receiptsHref:
      el.getAttribute("receipts-href") || ds.receiptsHref || DEFAULTS.receiptsHref,
    captureHref:
      el.getAttribute("capture-href") || ds.captureHref || DEFAULTS.captureHref,
    watchHref:
      el.getAttribute("watch-href") || ds.watchHref || DEFAULTS.watchHref,
    logoSrc: el.getAttribute("logo-src") || ds.logoSrc || DEFAULTS.logoSrc,
    wordmark: el.getAttribute("wordmark") || ds.wordmark || "QorTime",
    ctaLabel: el.getAttribute("cta-label") || ds.ctaLabel || "",
    ctaHref: el.getAttribute("cta-href") || ds.ctaHref || "",
  };
}

function patchHrefs(rootEl, cfg) {
  const map = {
    receipts: cfg.receiptsHref,
    capture: cfg.captureHref,
    watch: cfg.watchHref,
  };
  rootEl.querySelectorAll("[data-qor-link]").forEach((a) => {
    const key = a.getAttribute("data-qor-link");
    if (map[key]) a.setAttribute("href", map[key]);
    if (cfg.active) {
      if (key === cfg.active) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
  });
  const brand = rootEl.querySelector(".qor-nav__brand");
  if (brand && (rootEl.hasAttribute("home") || rootEl.dataset.home)) {
    brand.setAttribute("href", cfg.home);
  }
  const logoImg = rootEl.querySelector(".qor-nav__mark img");
  if (logoImg && (rootEl.hasAttribute("logo-src") || rootEl.dataset.logoSrc)) {
    logoImg.setAttribute("src", cfg.logoSrc);
    const shimmer = rootEl.querySelector(".qor-shimmer");
    if (shimmer) shimmer.style.setProperty("--qor-shimmer-mask", `url("${cfg.logoSrc}")`);
  }
}

export function enhanceNav(rootEl) {
  if (!rootEl || rootEl.dataset.qorEnhanced === "true") return;
  rootEl.dataset.qorEnhanced = "true";

  const cfg = resolveConfig(rootEl);
  patchHrefs(rootEl, cfg);

  const navBar = rootEl.classList.contains("qor-nav")
    ? rootEl
    : rootEl.querySelector(".qor-nav") || rootEl;

  // Sticky glass state on scroll
  const onScroll = () => {
    navBar.classList.toggle("is-scrolled", window.scrollY > 10);
  };
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });

  // Three-strand weave active/hover positioning
  const track = rootEl.querySelector(".qor-nav__track");
  const links = track ? Array.from(track.querySelectorAll(".qor-nav__link")) : [];
  const activeLink = () =>
    track?.querySelector('.qor-nav__link[aria-current="page"]') || links[0];

  const moveWeaveTo = (target, preview = false) => {
    if (!track || !target) return;
    const tRect = track.getBoundingClientRect();
    const lRect = target.getBoundingClientRect();
    if (!lRect.width) return;
    track.style.setProperty("--qor-weave-x", `${lRect.left - tRect.left}px`);
    track.style.setProperty("--qor-weave-w", `${lRect.width}px`);
    track.classList.add("has-weave-ready");
    track.classList.toggle("is-previewing", preview && !reducedMotion());
  };

  if (track && links.length) {
    moveWeaveTo(activeLink(), false);
    if (!reducedMotion()) {
      links.forEach((link) => {
        const isCurrent = () => link.getAttribute("aria-current") === "page";
        link.addEventListener("mouseenter", () => moveWeaveTo(link, !isCurrent()));
        link.addEventListener("focus", () => moveWeaveTo(link, !isCurrent()));
      });
      track.addEventListener("mouseleave", () => moveWeaveTo(activeLink(), false));
      track.addEventListener("focusout", (e) => {
        if (!track.contains(e.relatedTarget)) moveWeaveTo(activeLink(), false);
      });
    }
    window.addEventListener("resize", () => moveWeaveTo(activeLink(), false), {
      passive: true,
    });
  }

  // Mobile sheet + accessible focus trap
  const toggle = rootEl.querySelector(".qor-nav__toggle");
  const sheet = rootEl.querySelector(".qor-nav__sheet");
  if (!toggle || !sheet) return;

  let lastFocused = null;
  const focusables = () =>
    Array.from(
      sheet.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')
    );

  const closeSheet = (restoreFocus = true) => {
    if (sheet.hidden) return;
    sheet.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    document.body.style.overflow = "";
    if (restoreFocus && lastFocused) lastFocused.focus();
  };

  const openSheet = () => {
    lastFocused = document.activeElement;
    sheet.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
    const items = focusables();
    if (items[0]) items[0].focus();
  };

  toggle.addEventListener("click", () => {
    if (toggle.getAttribute("aria-expanded") === "true") closeSheet(true);
    else openSheet();
  });

  sheet.addEventListener("click", (e) => {
    if (e.target === sheet || e.target.closest("a")) closeSheet(false);
  });

  document.addEventListener("keydown", (e) => {
    if (sheet.hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeSheet(true);
      return;
    }
    if (e.key === "Tab") {
      const items = [toggle, ...focusables()];
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  window.matchMedia("(min-width: 761px)").addEventListener("change", (e) => {
    if (e.matches) closeSheet(false);
  });
}

let uid = 0;
export class QorNavElement extends HTMLElement {
  connectedCallback() {
    if (this.querySelector(".qor-nav")) {
      enhanceNav(this);
      return;
    }
    const cfg = resolveConfig(this);
    const sheetId = `qor-nav-sheet-${++uid}`;
    const items = [
      { key: "receipts", label: "Receipts", href: cfg.receiptsHref },
      { key: "capture", label: "Capture", href: cfg.captureHref },
      { key: "watch", label: "Watch", href: cfg.watchHref },
    ];
    const ctaMarkup =
      cfg.ctaLabel && cfg.ctaHref
        ? `<a class="qor-nav__cta" href="${cfg.ctaHref}">${cfg.ctaLabel}</a>`
        : "";
    const sheetCtaMarkup =
      cfg.ctaLabel && cfg.ctaHref
        ? `<a class="qor-nav__cta qor-nav__sheet-cta" href="${cfg.ctaHref}">${cfg.ctaLabel}</a>`
        : "";

    this.innerHTML = `
      <header class="qor-nav">
        <nav class="qor-nav__inner" aria-label="Qor product family">
          <a class="qor-nav__brand" href="${cfg.home}">
            <span class="qor-nav__mark">
              <img src="${cfg.logoSrc}" alt="" width="30" height="30" />
              <span class="qor-shimmer" style="--qor-shimmer-mask: url('${cfg.logoSrc}')" aria-hidden="true"></span>
            </span>
            <span class="qor-nav__wordmark">${cfg.wordmark}</span>
          </a>
          <div class="qor-nav__track">
            ${items
              .map(
                (it) =>
                  `<a class="qor-nav__link" data-qor-link="${it.key}" href="${it.href}"${
                    cfg.active === it.key ? ' aria-current="page"' : ""
                  }>${it.label}</a>`
              )
              .join("")}
            <span class="qor-nav__weave" aria-hidden="true">
              <span class="qor-nav__strand qor-nav__strand--blue"></span>
              <span class="qor-nav__strand qor-nav__strand--cyan"></span>
              <span class="qor-nav__strand qor-nav__strand--white"></span>
              <span class="qor-nav__knot"></span>
            </span>
          </div>
          <div class="qor-nav__actions">
            ${ctaMarkup}
            <button class="qor-nav__toggle" type="button" aria-expanded="false" aria-controls="${sheetId}" aria-label="Toggle family menu">
              <span class="qor-nav__toggle-lanes" aria-hidden="true"><span></span><span></span><span></span></span>
            </button>
          </div>
        </nav>
        <div class="qor-nav__sheet" id="${sheetId}" hidden>
          <div class="qor-nav__sheet-panel" role="dialog" aria-label="Qor family navigation">
            ${items
              .map(
                (it) =>
                  `<a class="qor-nav__sheet-link" data-qor-link="${it.key}" href="${it.href}"${
                    cfg.active === it.key ? ' aria-current="page"' : ""
                  }><span>${it.label}</span></a>`
              )
              .join("")}
            ${sheetCtaMarkup}
          </div>
        </div>
      </header>
    `;
    enhanceNav(this);
  }
}

if (!customElements.get("qor-nav")) {
  customElements.define("qor-nav", QorNavElement);
}

// Auto-enhance copy-paste HTML navs on the page
document.querySelectorAll("[data-qor-nav]").forEach((el) => enhanceNav(el));
