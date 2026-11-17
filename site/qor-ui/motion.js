const isReducedMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function initReveal(root = document) {
  const items = Array.from(root.querySelectorAll(".qor-reveal"));
  if (!items.length) return;

  if (isReducedMotion() || !("IntersectionObserver" in window)) {
    items.forEach((el) => el.classList.add("is-in"));
    return;
  }

  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          io.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }
  );

  items.forEach((el) => io.observe(el));
}

export function initSectionTransitions(selector = "[data-qor-section], section") {
  const sections = Array.from(document.querySelectorAll(selector));
  if (!sections.length) return;

  if (isReducedMotion()) {
    sections.forEach((sec) => {
      sec.classList.add("is-in");
      sec.style.setProperty("--qor-progress", "1");
    });
    return;
  }

  if ("IntersectionObserver" in window) {
    const secObserver = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) entry.target.classList.add("is-in");
        });
      },
      { threshold: 0.12 }
    );
    sections.forEach((sec) => secObserver.observe(sec));
  }

  let ticking = false;
  const updateProgress = () => {
    const vh = window.innerHeight || 1;
    for (let i = 0; i < sections.length; i++) {
      const sec = sections[i];
      const rect = sec.getBoundingClientRect();
      const total = vh + rect.height;
      const raw = (vh - rect.top) / (total || 1);
      const clamped = raw < 0 ? 0 : raw > 1 ? 1 : raw;
      sec.style.setProperty("--qor-progress", clamped.toFixed(3));
    }
    ticking = false;
  };

  const requestTick = () => {
    if (!ticking) {
      ticking = true;
      requestAnimationFrame(updateProgress);
    }
  };

  updateProgress();
  window.addEventListener("scroll", requestTick, { passive: true });
  window.addEventListener("resize", requestTick, { passive: true });
}
