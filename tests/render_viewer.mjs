// Render one match page and print the headline and the refusal banner.
// Usage: node tests/render_viewer.mjs PAGE.html /path/to/jsdom-prefix/package.json
import {createRequire} from "node:module";
import {readFileSync} from "node:fs";

const require = createRequire(process.argv[3]);
const {JSDOM, VirtualConsole} = require("jsdom");
const html = readFileSync(process.argv[2], "utf8");
const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (err) => errors.push(String(err && err.stack || err)));
virtualConsole.on("error", (err) => errors.push(String(err)));
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole,
  url: "https://example.test/matches/m/index.html",
  beforeParse(window) {
    window.TextEncoder = TextEncoder;
    window.TextDecoder = TextDecoder;
    window.crypto = globalThis.crypto;
    window.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
    for (const key of ["clientWidth", "clientHeight", "offsetWidth", "offsetHeight"]) {
      Object.defineProperty(window.HTMLElement.prototype, key, {get() { return 320; }});
    }
    window.HTMLElement.prototype.scrollTo = () => {};
    window.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, {
      get: () => () => {},
      set: () => true,
    });
  },
});
await new Promise((resolve) => setTimeout(resolve, 100));
const doc = dom.window.document;
const st = doc.querySelector("#status");
const banner = doc.querySelector("#rcCard .ind.bad");
const out = {
  statusText: st ? st.textContent : "",
  statusHtml: st ? st.innerHTML : "",
  title: st ? (st.getAttribute("title") || "") : "",
  role: st ? (st.getAttribute("role") || "") : "",
  className: st ? st.className : "",
  banner: banner ? banner.textContent : "",
  bannerHtml: banner ? banner.innerHTML : "",
  errors,
};
process.stdout.write(JSON.stringify(out));
dom.window.close();
process.exit(0);
