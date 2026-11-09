// Read-only screenshot tour for design reviews (M8). Never clicks Apply, Skip, Mark submitted,
// Change status or Paste. Opening the drawer and the saved cover draft are GET requests only.
// node dev/shots.mjs --base <url> --prefix m8-after- [--cdp 61921] [--cover]   (--cover: mock only)
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const BASE = opt("--base", "http://127.0.0.1:8799/index.html?mock=1");
const PREFIX = opt("--prefix", "m8-");
const CDP = opt("--cdp", "61921");
const COVER = args.includes("--cover");
const VIEWS = (opt("--views", "all,drawer,cover,applied,lessons,filtered")).split(",");
const SIZES = opt("--sizes", "1280x860,1440x900").split(",").map((size) => size.split("x").map(Number));
const SCREENS = join(dirname(fileURLToPath(import.meta.url)), "..", "reviews", "screens");
mkdirSync(SCREENS, { recursive: true });

const target = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let id = 0;
const pending = new Map(), errors = [], external = [], posts = [], details = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.j(new Error(m.error.message)) : p.r(m.result); return; }
  if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errors.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
  if (m.method === "Network.requestWillBeSent") {
    const u = m.params.request.url;
    if (!/^(https?:\/\/127\.0\.0\.1[:/]|data:|blob:|about:)/.test(u)) external.push(u);
    if (m.params.request.method === "POST") posts.push(u);
    if (m.params.request.method === "GET" && /\/api\/jobs\/\d+$/.test(new URL(u).pathname)) details.push(u);
  }
};
const send = (method, params = {}) => new Promise((r, j) => { const n = ++id; pending.set(n, { r, j }); ws.send(JSON.stringify({ id: n, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ev = async (x) => (await send("Runtime.evaluate", { expression: `(async () => { ${x} })()`, awaitPromise: true, returnByValue: true })).result.value;
async function until(x, ms = 10000) { const t = Date.now(); while (Date.now() - t < ms) { if (await ev(`return !!(${x});`)) return true; await sleep(80); } return false; }
const idle = () => until(`document.querySelector('#list').getAttribute('aria-busy') === 'false' && !document.querySelector('#list .skel')`);
const shots = [];
const leaks = [];
async function shot(name) {
  await sleep(350); // let enter animations finish
  const leak = await ev(`return (document.body.innerText.match(/\\b(null|undefined|NaN)\\b|\\[object /g) || []).join(",");`);
  if (leak) leaks.push(`${name}: ${leak}`);
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  const f = PREFIX + name + ".png";
  writeFileSync(join(SCREENS, f), Buffer.from(data, "base64"));
  shots.push(f);
}
const tab = async (t) => { await ev(`document.querySelector('.tab[data-tab="${t}"]').click();`); await idle(); };

await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
// read-only guard (any base): fail every non-GET/HEAD request before it reaches the server
const blocked = [];
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.method !== "Fetch.requestPaused") return;
  const { requestId, request } = m.params;
  if (["GET", "HEAD"].includes(request.method)) send("Fetch.continueRequest", { requestId }).catch(() => {});
  else { blocked.push(request.method + " " + request.url); send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }).catch(() => {}); }
});
await send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
await send("Network.setCacheDisabled", { cacheDisabled: true });
// keep this tab foreground-like: background tabs throttle timers (the mock's delays), which made runs flaky
await send("Page.bringToFront"); await send("Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => {});
for (const [w, h] of SIZES) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE }); await sleep(400); await idle();
  if (VIEWS.includes("all")) {
    await until(`[...document.querySelectorAll('#list .logo img')].every(i => i.complete)`, 8000); await sleep(300);
    await shot(`${w}-all`);
    // logo + count summary of what is on the page (read-only)
    console.log(`${w} all: ` + JSON.stringify(await ev(`const rows = [...document.querySelectorAll('#list .rows > li.row')];
      const imgs = [...document.querySelectorAll('#list .logo img')];
      return { headline: document.querySelector('#list .list-head h2')?.textContent, rows: rows.length,
        tabAll: document.querySelector('.tab[data-tab="all"] .count')?.textContent,
        segment: [...document.querySelectorAll('.seg-btn')].map(b => b.textContent.replace(/\\s+/g, ' ').trim() + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')).join(' | '),
        logosShown: imgs.filter(i => i.naturalWidth > 0).length, logoTypes: [...new Set(imgs.filter(i => i.naturalWidth > 0).map(i => i.src.split('.').pop()))],
        initials: document.querySelectorAll('#list .logo.mono').length,
        countries: [...new Set(rows.map(r => r.dataset.country || '(none)'))].slice(0, 8),
        whyLines: document.querySelectorAll('#list .why:not(.pending):not(:empty)').length, pendingWhy: document.querySelectorAll('#list .why.pending').length };`)));
  }
  if (VIEWS.includes("drawer")) {
    const ok = await ev(`const rs = [...document.querySelectorAll('#list [data-id]')].filter(r => r.dataset.verdict === 'CENTRE' || r.querySelector('.v-CENTRE'));
      const r = rs.find(r => r.querySelector('.logo img')) || rs[0];
      if (!r) return false; r.querySelector('.open').click(); return true;`);
    if (ok && await until(`document.querySelector('#drawer[open] .d-body:not(.d-loading)')`)) {
      await shot(`${w}-drawer-centre`);
      await ev(`document.getElementById('drawer').close();`); await sleep(300);
    }
  }
  if (COVER && VIEWS.includes("cover")) {
    await tab("to_apply");
    await ev(`const b = [...document.querySelectorAll('#list .row button')].find(b => b.textContent.trim() === 'Cover email'); b && b.click();`);
    if (await until(`document.querySelector('#apply-result[open] .mail-body')`)) await shot(`${w}-to-apply-cover`);
    await ev(`document.getElementById('apply-result').close();`); await sleep(200);
  }
  for (const t of ["applied", "lessons", "filtered"]) if (VIEWS.includes(t)) { await tab(t); await shot(`${w}-${t}`); }
  if (VIEWS.includes("addjob")) { await tab("all"); await ev(`document.getElementById('add-job').click();`); await sleep(250); await shot(`${w}-add-job`); await ev(`document.getElementById('add-dialog').close();`); }
  if (VIEWS.includes("todo")) { await tab("to_apply"); await shot(`${w}-to-apply`); }
}
console.log("screens: " + shots.join(", "));
console.log("null/undefined text: " + (leaks.length ? leaks.join(" | ") : "none"));
console.log("console errors: " + (errors.length ? errors.join(" | ") : "none"));
console.log("requests off 127.0.0.1: " + (external.length ? external.join(" ") : "none"));
console.log("job detail GETs: " + details.length + " (drawer opens count 1 each)");
console.log("blocked write requests: " + (blocked.length ? blocked.join(" ") : "none"));
console.log("POST requests: " + (posts.length ? posts.join(" ") : "none"));
await fetch(`http://127.0.0.1:${CDP}/json/close/${target.id}`);
ws.close();
