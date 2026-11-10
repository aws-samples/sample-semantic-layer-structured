/**
 * Repeatable page-load measurement harness (zero npm dependencies).
 *
 * Drives system Google Chrome in headless mode over the Chrome DevTools
 * Protocol (CDP) using Node's built-in global WebSocket (Node >= 21). For each
 * route it performs N cold navigations (cache disabled, fresh document each
 * time) and records, from the page's own Performance timeline:
 *   - ttfb   : responseStart - navigationStart
 *   - fcp    : first-contentful-paint
 *   - domcl  : domContentLoadedEventEnd
 *   - load   : load event end  (the headline "page load" number)
 *
 * Repeatable test conditions (held constant across every run):
 *   - same Chrome binary, headless=new, fixed 1440x900 viewport
 *   - cache disabled via Network.setCacheDisabled (true cold load)
 *   - CPU throttling rate fixed (PERF_CPU_THROTTLE, default 1 = none)
 *   - same local static server, Cache-Control: no-store
 *   - N iterations per route, report the MEDIAN (robust to GC/jitter)
 *
 * Usage:
 *   node measure.js <baseUrl> [iterations]
 * Prints a table and a JSON blob (parseable) to stdout.
 */
const { spawn } = require("child_process");
const http = require("http");
const os = require("os");
const path = require("path");
const fs = require("fs");

const CHROME =
  process.env.CHROME_BIN ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const BASE_URL = process.argv[2] || "http://127.0.0.1:8099";
const ITERATIONS = Number(process.argv[3] || process.env.PERF_ITERATIONS || 7);
const CPU_THROTTLE = Number(process.env.PERF_CPU_THROTTLE || 1);
const DEBUG_PORT = Number(process.env.PERF_CDP_PORT || 9222);

// Routes measured. Each is a distinct "page" in the SPA. Paths that render
// behind auth still load the same JS bundle(s), so the shell-load timing is
// representative and, crucially, repeatable.
const ROUTES = [
  { name: "query/ask", path: "/query/ask" },
  { name: "admin", path: "/admin" },
  { name: "admin/describe-intent", path: "/admin/describe-intent" },
  { name: "admin/select-datasources", path: "/admin/select-datasources" },
  { name: "admin/review-metadata", path: "/admin/review-metadata" },
  { name: "admin/build-graph", path: "/admin/build-graph" },
  { name: "admin/view-graph", path: "/admin/view-graph" },
  { name: "admin/ground-truth", path: "/admin/ground-truth" },
  { name: "admin/evaluations", path: "/admin/evaluations" },
  { name: "settings", path: "/settings" },
];

/** Sleep helper. */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** GET a CDP JSON endpoint (http://host:port/json/...). */
function cdpHttp(pathname) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port: DEBUG_PORT, path: pathname }, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch (e) {
            resolve(data);
          }
        });
      })
      .on("error", reject);
  });
}

/** Thin CDP client over one target's WebSocket. */
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

/** Open a CDP session against the first available page target. */
async function connect() {
  // Retry until Chrome's debugging endpoint is up.
  let target = null;
  for (let i = 0; i < 50; i++) {
    try {
      const list = await cdpHttp("/json/list");
      target = Array.isArray(list)
        ? list.find((t) => t.type === "page" && t.webSocketDebuggerUrl)
        : null;
      if (target) break;
    } catch (e) {
      /* not up yet */
    }
    await sleep(100);
  }
  if (!target) throw new Error("no CDP page target found");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  return new CDP(ws);
}

/**
 * Navigate to `url` and read the page's own performance timeline once the load
 * event has fired. Returns milliseconds relative to navigation start.
 */
async function measureOnce(cdp, url) {
  // Fresh, cold conditions for every navigation.
  await cdp.send("Network.clearBrowserCache");
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });

  // Navigate to about:blank first so timings never bleed across iterations.
  await cdp.send("Page.navigate", { url: "about:blank" });
  await sleep(50);
  await cdp.send("Page.navigate", { url });

  // Poll the load event + paint timing from inside the page.
  const script = `
    (function () {
      const nav = performance.getEntriesByType('navigation')[0];
      if (!nav || nav.loadEventEnd === 0) return null;
      const paints = performance.getEntriesByType('paint');
      const fcp = paints.find(p => p.name === 'first-contentful-paint');
      return JSON.stringify({
        ttfb: nav.responseStart,
        domcl: nav.domContentLoadedEventEnd,
        load: nav.loadEventEnd,
        fcp: fcp ? fcp.startTime : null,
      });
    })()
  `;
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const { result } = await cdp.send("Runtime.evaluate", {
      expression: script,
      returnByValue: true,
    });
    if (result && result.value) return JSON.parse(result.value);
    await sleep(30);
  }
  throw new Error(`timeout waiting for load: ${url}`);
}

/** Median of a numeric array. */
function median(xs) {
  const s = xs.filter((x) => x != null).sort((a, b) => a - b);
  if (!s.length) return null;
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

async function main() {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "perf-chrome-"));
  const chrome = spawn(
    CHROME,
    [
      "--headless=new",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${userDataDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-background-networking",
      "--window-size=1440,900",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  let cdp;
  try {
    cdp = await connect();
    await cdp.send("Page.enable");
    await cdp.send("Network.enable");
    await cdp.send("Runtime.enable");
    if (CPU_THROTTLE > 1) {
      await cdp.send("Emulation.setCPUThrottlingRate", { rate: CPU_THROTTLE });
    }

    // Warm-up navigation (JIT, DNS, socket) that is not recorded.
    await measureOnce(cdp, BASE_URL + ROUTES[0].path);

    const results = [];
    for (const route of ROUTES) {
      const url = BASE_URL + route.path;
      const samples = { ttfb: [], fcp: [], domcl: [], load: [] };
      for (let i = 0; i < ITERATIONS; i++) {
        const m = await measureOnce(cdp, url);
        samples.ttfb.push(m.ttfb);
        samples.fcp.push(m.fcp);
        samples.domcl.push(m.domcl);
        samples.load.push(m.load);
      }
      results.push({
        route: route.name,
        ttfb: median(samples.ttfb),
        fcp: median(samples.fcp),
        domcl: median(samples.domcl),
        load: median(samples.load),
      });
    }

    // Human-readable table.
    console.log(
      "\nroute".padEnd(34) +
        "ttfb".padStart(9) +
        "fcp".padStart(9) +
        "domcl".padStart(9) +
        "load".padStart(9),
    );
    console.log("-".repeat(70));
    for (const r of results) {
      console.log(
        r.route.padEnd(34) +
          `${r.ttfb.toFixed(1)}`.padStart(9) +
          `${r.fcp == null ? "-" : r.fcp.toFixed(1)}`.padStart(9) +
          `${r.domcl.toFixed(1)}`.padStart(9) +
          `${r.load.toFixed(1)}`.padStart(9),
      );
    }
    const worst = Math.max(...results.map((r) => r.load));
    console.log("-".repeat(70));
    console.log(`worst load: ${worst.toFixed(1)} ms   (target < 50 ms)`);
    console.log(
      "\nPERF_JSON=" +
        JSON.stringify({
          iterations: ITERATIONS,
          cpuThrottle: CPU_THROTTLE,
          worstLoad: worst,
          results,
        }),
    );
  } finally {
    if (cdp && cdp.ws) cdp.ws.close();
    chrome.kill("SIGKILL");
    try {
      fs.rmSync(userDataDir, { recursive: true, force: true });
    } catch (e) {
      /* best effort */
    }
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
