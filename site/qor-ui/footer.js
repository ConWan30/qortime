const FOOTER_DEFAULTS = {
  home: "/",
  receiptsHref: "https://qortime.com",
  captureHref: "https://qortime.com/cap",
  watchHref: "#watch-coming-soon",
  logoSrc: "brand/logo-icon.svg",
  tagline: "Signed match receipts, local capture, and a browser verifier.",
  legal: "QorTime product family.",
};

function readFooterConfig(el) {
  const ds = el.dataset || {};
  return {
    active: el.getAttribute("active") || ds.active || "",
    home: el.getAttribute("home") || ds.home || FOOTER_DEFAULTS.home,
    receiptsHref:
      el.getAttribute("receipts-href") || ds.receiptsHref || FOOTER_DEFAULTS.receiptsHref,
    captureHref:
      el.getAttribute("capture-href") || ds.captureHref || FOOTER_DEFAULTS.captureHref,
    watchHref:
      el.getAttribute("watch-href") || ds.watchHref || FOOTER_DEFAULTS.watchHref,
    logoSrc: el.getAttribute("logo-src") || ds.logoSrc || FOOTER_DEFAULTS.logoSrc,
    wordmark: el.getAttribute("wordmark") || ds.wordmark || "QorTime",
    tagline: el.getAttribute("tagline") || ds.tagline || FOOTER_DEFAULTS.tagline,
    legal: el.getAttribute("legal") || ds.legal || FOOTER_DEFAULTS.legal,
  };
}

export function enhanceFooter(rootEl) {
  if (!rootEl || rootEl.dataset.qorFooterEnhanced === "true") return;
  rootEl.dataset.qorFooterEnhanced = "true";

  const cfg = readFooterConfig(rootEl);
  const hrefMap = {
    receipts: cfg.receiptsHref,
    capture: cfg.captureHref,
    watch: cfg.watchHref,
  };

  rootEl.querySelectorAll("[data-qor-link]").forEach((a) => {
    const key = a.getAttribute("data-qor-link");
    if (hrefMap[key]) a.setAttribute("href", hrefMap[key]);
    if (cfg.active) {
      if (key === cfg.active) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
  });

  const brand = rootEl.querySelector(".qor-footer__brand");
  if (brand && (rootEl.hasAttribute("home") || rootEl.dataset.home)) {
    brand.setAttribute("href", cfg.home);
  }
  const logoImg = rootEl.querySelector(".qor-footer__mark img");
  if (logoImg && (rootEl.hasAttribute("logo-src") || rootEl.dataset.logoSrc)) {
    logoImg.setAttribute("src", cfg.logoSrc);
  }
}

export class QorFooterElement extends HTMLElement {
  connectedCallback() {
    if (this.querySelector(".qor-footer")) {
      enhanceFooter(this);
      return;
    }
    const cfg = readFooterConfig(this);
    const slotTagline = this.querySelector('[slot="tagline"]')?.innerHTML || cfg.tagline;
    const slotLegal = this.querySelector('[slot="legal"]')?.innerHTML || cfg.legal;

    const links = [
      { key: "receipts", label: "Receipts", href: cfg.receiptsHref },
      { key: "capture", label: "Capture", href: cfg.captureHref },
      { key: "watch", label: "Watch", href: cfg.watchHref },
    ];

    this.innerHTML = `
      <footer class="qor-footer">
        <div class="qor-footer__inner">
          <div class="qor-footer__top">
            <div class="qor-footer__brand-col">
              <a class="qor-footer__brand" href="${cfg.home}">
                <span class="qor-footer__mark">
                  <img src="${cfg.logoSrc}" alt="" width="26" height="26" />
                </span>
                <span>${cfg.wordmark}</span>
              </a>
              <p class="qor-footer__tagline">${slotTagline}</p>
            </div>
            <nav class="qor-footer__nav" aria-label="Qor family footer">
              ${links
                .map(
                  (it) =>
                    `<a class="qor-footer__link" data-qor-link="${it.key}" href="${it.href}"${
                      cfg.active === it.key ? ' aria-current="page"' : ""
                    }>${it.label}</a>`
                )
                .join("")}
            </nav>
          </div>
          <div class="qor-footer__bottom">
            <p class="qor-footer__record-line">
              <span class="qor-footer__record-dot" aria-hidden="true"></span>
              <span>One signed record format across Receipts and Capture</span>
            </p>
            <div class="qor-footer__legal">${slotLegal}</div>
          </div>
        </div>
      </footer>
    `;
    enhanceFooter(this);
  }
}

if (!customElements.get("qor-footer")) {
  customElements.define("qor-footer", QorFooterElement);
}

document.querySelectorAll("[data-qor-footer]").forEach((el) => enhanceFooter(el));
