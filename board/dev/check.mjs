// Board smoke check over the Chrome DevTools Protocol (no npm deps; Node >= 22 for global WebSocket).
// 1. python3 -m http.server 8799 --bind 127.0.0.1 --directory board
// 2. Start a Chromium browser with a throwaway profile, e.g.
//    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" --remote-debugging-port=61920 --user-data-dir=/tmp/board-brave-profile about:blank
// 3. node board/dev/check.mjs [--base http://127.0.0.1:8799/index.html] [--cdp 61920] [--live]
// Runs the M4b acceptance flows in mock mode, saves screenshots to board/reviews/screens/, and fails on
// console errors or any request that leaves 127.0.0.1. --live is READ-ONLY: it runs no mutating flow, and every
// non-GET request is blocked at the network layer (Fetch domain) before it can reach the server, so a live run can
// never write to the vault. The old write modes (--live-writes, --live-m56, --live-final) were deleted on 8 Nov 2026.
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const LIVE = args.includes("--live");
const BASE = opt("--base", "http://127.0.0.1:8799/index.html");
const CDP = opt("--cdp", "61921");
const SCREENS = join(dirname(fileURLToPath(import.meta.url)), "..", "reviews", "screens");
mkdirSync(SCREENS, { recursive: true });

const target = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let msgId = 0;
const pending = new Map();
const consoleErrors = [], external = [], requests = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.j(new Error(m.error.message)) : p.r(m.result); return; }
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning", "assert"].includes(m.params.type)) consoleErrors.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
  if (m.method === "Runtime.exceptionThrown") consoleErrors.push("exception: " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error") consoleErrors.push("log: " + m.params.entry.text + " " + (m.params.entry.url || ""));
  if (m.method === "Network.requestWillBeSent") {
    const u = m.params.request.url;
    requests.push(m.params.request.method + " " + u);
    if (!/^(https?:\/\/127\.0\.0\.1[:/]|data:|blob:|about:)/.test(u)) external.push(u);
  }
};
const send = (method, params = {}) => new Promise((r, j) => { const id = ++msgId; pending.set(id, { r, j }); ws.send(JSON.stringify({ id, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function ev(expr) {
  const res = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (res.exceptionDetails) throw new Error("eval failed: " + (res.exceptionDetails.exception?.description || res.exceptionDetails.text) + "\n" + expr);
  return res.result.value;
}
async function until(expr, ms = 8000) {
  const t = Date.now();
  while (Date.now() - t < ms) { if (await ev(`return !!(${expr});`)) return; await sleep(80); }
  throw new Error("timeout waiting for: " + expr);
}
async function size(w, h) { await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false }); await sleep(150); }
// §M10: Apply calls window.open; the stub records [url, target, features] so the check asserts the URL and the
// browser never loads LinkedIn, JobsDB or a company site.
const STUB_OPEN = `window.__opened = []; window.open = (u, t, f) => { window.__opened.push([u, t, f]); return null; };`;
async function go(qs = "", region = "all") { await send("Page.navigate", { url: BASE + "?mock=1" + (region ? "&region=" + region : "") + qs }); await sleep(300); await ev(STUB_OPEN).catch(() => {}); }
async function shot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  // a --live run pointed at ?mock=1 is a dry run: never label its screenshots "live-"
  const file = (LIVE ? (BASE.includes("mock=1") ? "dry-" : "live-") : "") + name + ".png";
  writeFileSync(join(SCREENS, file), Buffer.from(data, "base64"));
  shots.push(file);
}
async function key(k, code = k, keyCode) {
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode, ...(k === "Enter" ? { text: "\r" } : {}) });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
}
const results = [], shots = [];
function check(name, cond, info = "") { results.push([cond ? "PASS" : "FAIL", name, info]); }
const count = (tab) => ev(`return +document.querySelector('.tab[data-tab="${tab}"] .count').textContent;`);
const rows = () => ev(`return document.querySelectorAll('#list .rows > li.row').length;`); // best-bet cards + compact rows
const clickTab = async (t) => { await ev(`document.querySelector('.tab[data-tab="${t}"]').click();`); await until(`document.querySelector('#list').getAttribute('aria-busy') === 'false'`); };
const ready = () => until(`document.querySelector('#list').getAttribute('aria-busy') === 'false' && document.querySelector('.tab .count') && document.querySelector('.tab .count').textContent !== '–'`);

await send("Page.enable"); await send("Runtime.enable"); await send("Log.enable"); await send("Network.enable");
const blockedWrites = [];
if (LIVE) {
  // read-only guard: pause every request; fail anything that is not GET/HEAD, continue the rest
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method !== "Fetch.requestPaused") return;
    const { requestId, request } = m.params;
    if (["GET", "HEAD"].includes(request.method)) send("Fetch.continueRequest", { requestId }).catch(() => {});
    else { blockedWrites.push(request.method + " " + request.url); send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }).catch(() => {}); }
  });
  await send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
}
await send("Network.setCacheDisabled", { cacheDisabled: true });
// keep this tab foreground-like: background tabs throttle timers (the mock's delays), which made runs flaky
await send("Page.bringToFront"); await send("Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => {});
if (LIVE) { await send("Page.navigate", { url: BASE }); await sleep(300); } else await go();

for (const [w, h] of [[1280, 860], [1440, 900]]) {
  await size(w, h);
  if (LIVE) { await send("Page.navigate", { url: BASE }); await sleep(300); } else await go();
  await ready();
  check(`${w}: sync status fits its container`, await ev(`const r = document.getElementById('run'); return r.scrollWidth <= r.clientWidth + 1;`));
  check(`${w}: no horizontal scroll`, await ev(`return document.documentElement.scrollWidth <= innerWidth;`), await ev(`return document.documentElement.scrollWidth + ' <= ' + innerWidth;`));
  await shot(`${w}-all`);
  // drawer on a CENTRE job with an ask gate
  const opened = await ev(`
    const r = [...document.querySelectorAll('#list .row')].find(r => r.dataset.verdict === 'CENTRE' && r.querySelector('.ask'));
    if (!r) return null; r.querySelector('.open').click(); return r.querySelector('.company').textContent;`);
  check(`${w}: a CENTRE + ask row exists`, !!opened, opened || "");
  if (opened) {
    await until(`document.querySelector('#drawer[open] .d-body:not(.d-loading)')`);
    check(`${w}: drawer shows ask questions`, await ev(`return !!document.querySelector('#drawer .g-ask li');`));
    await shot(`${w}-drawer-centre-ask`);
    await key("Escape", "Escape", 27);
    await until(`!document.querySelector('#drawer').open`, 3000);
    check(`${w}: Esc closes the drawer`, true);
    await sleep(150);
    check(`${w}: focus returns to the row`, await ev(`return !!document.activeElement.closest('.row');`), await ev(`return document.activeElement.tagName + '.' + document.activeElement.className;`));
  }
  await clickTab("filtered");
  await shot(`${w}-filtered`);
  check(`${w}: filtered rows == min(tab count, 100)`, (await rows()) === Math.min(await count("filtered"), 100));
  if ((await count("filtered")) > 100) check(`${w}: "Show more" offered past 100`, await ev(`return !!document.querySelector('#list .more button');`));
  await clickTab("applied");
  await shot(`${w}-applied`);
  check(`${w}: applied rows == tab count`, (await rows()) === (await count("applied")));
  check(`${w}: no horizontal scroll (applied)`, await ev(`return document.documentElement.scrollWidth <= innerWidth;`));
}

if (!LIVE) {
  await size(1280, 860);
  await go(); await ready();
  // tab counts == rows for every job tab
  for (const t of ["all", "centre", "hold", "low", "filtered", "skipped", "to_apply", "applied"]) {
    await clickTab(t);
    const c = await count(t), n = await rows();
    check(`tab ${t}: count ${c} == rows ${n}`, c === n);
  }
  await clickTab("all");
  check("Feed shows one ranked list without duplicate sections or filler badges", await ev(`
    const api = (await import('./api.js')).api;
    const expected = (await api.jobs('all', '', 0, 100)).jobs.map(j => String(j.id));
    const actual = [...document.querySelectorAll('#list .rows > .row')].map(r => r.dataset.id);
    return document.querySelectorAll('#list > .rows').length === 1 && !document.querySelector('#list .match-label, #list .group-head') && JSON.stringify(actual) === JSON.stringify(expected);`));
  check("Every scored job exposes its title, four labelled scores, and Apply", await ev(`return [...document.querySelectorAll('#list .feed-card')].every(b => b.querySelector('.open')?.textContent && b.querySelectorAll('.chip[aria-label]').length === 4 && b.querySelector('.btn.primary')?.textContent.startsWith('Apply on'));`));
  check("Rows use JobRow.summary without per-row detail fetches", await ev(`return document.querySelectorAll('#list .feed-card .why:not(.pending)').length > 3 && !document.querySelector('#list .why.pending');`));
  check("Main headline gives the actual result count", /^\d+ jobs? to explore$/.test(await ev(`return document.querySelector('#list .list-head h2').textContent;`)));
  check("M8: no verdict pill or weakest text in list rows", await ev(`return !document.querySelector('#list .row .pill.v-CENTRE, #list .row .weak');`));
  check("M8: weakest chip highlighted on scored rows", await ev(`return [...document.querySelectorAll('#list .row')].filter(r => r.dataset.verdict !== 'FILTERED' && r.dataset.verdict !== 'NONE').every(r => r.querySelectorAll('.chip.weakest').length <= 1) && !!document.querySelector('#list .chip.weakest');`));
  check("M8: Skip is a quiet button, Apply the only primary per row", await ev(`return [...document.querySelectorAll('#list .row')].every(r => r.querySelectorAll('.btn.primary').length === 1 && r.querySelector('.btn.quiet'));`));
  check("M8: no duplicated location parts", await ev(`return [...document.querySelectorAll('#list .meta span:first-child')].every(s => { const p = s.textContent.split(', ').map(x => x.toLowerCase()); return new Set(p).size === p.length; });`));
  check("M8: no italics outside quotations", await ev(`return [...document.querySelectorAll('body *')].filter(n => getComputedStyle(n).fontStyle === 'italic' && !n.closest('blockquote')).length === 0;`));
  await ev(`document.querySelector('#site-changes .btn').click();`); await sleep(150);
  check("M8: careers links never show a bare number", await ev(`return document.querySelectorAll('#sites-dialog li').length > 0 && [...document.querySelectorAll('#sites-dialog li')].every(l => !/^\\s*\\d+\\s*$/.test(l.textContent));`));
  await ev(`document.getElementById('sites-dialog').close();`);
  // j/k keyboard navigation
  await ev(`document.activeElement.blur();`);
  await key("j", "KeyJ", 74); await key("j", "KeyJ", 74);
  check("j moves focus to row 2", await ev(`return document.activeElement.closest('.row') === document.querySelectorAll('#list .row')[1];`));
  await key("k", "KeyK", 75);
  check("k moves focus back to row 1", await ev(`return document.activeElement.closest('.row') === document.querySelectorAll('#list .row')[0];`));
  await key("a", "KeyA", 65); await sleep(150);
  check("M8: one 'a' only arms Apply (no record yet)", await ev(`const b = document.activeElement.closest('.row').querySelector('.btn.primary'); return b.textContent === 'Press a again to apply' && !document.getElementById('apply-result').open;`));
  await sleep(4200);
  check("M8: armed Apply resets after 4 s", await ev(`return document.activeElement.closest('.row').querySelector('.btn.primary').textContent.startsWith('Apply on');`));
  // search
  await ev(`const q = document.getElementById('q'); q.value = 'agoda'; q.dispatchEvent(new Event('input'));`);
  await sleep(400); await until(`document.querySelector('#list').getAttribute('aria-busy') === 'false'`);
  const agoda = await ev(`return [...document.querySelectorAll('#list .row .company')].map(c => c.textContent);`);
  check("search 'agoda' only shows Agoda", agoda.length > 0 && agoda.every((c) => c === "Agoda"), agoda.join(", "));
  check("search is kept in the URL hash", (await ev(`return location.hash;`)).includes("q=agoda"));
  await ev(`const q = document.getElementById('q'); q.value = 'zzzz-nothing'; q.dispatchEvent(new Event('input'));`);
  await sleep(400); await until(`document.querySelector('#list .state')`);
  await shot("1280-empty-search");
  await ev(`document.querySelector('#list .state button').click();`); await until(`document.querySelectorAll('#list .row .actions').length > 5 && document.querySelector('#list').getAttribute('aria-busy') === 'false'`); await sleep(300);
  // skip + unskip via keyboard (focus the Skip button, press Enter)
  const allBefore = await count("all"), skBefore = await count("skipped");
  const skippedCo = await ev(`const r = document.querySelector('#list .row'); r.querySelector('.actions .btn:not(.primary)').focus(); return r.querySelector('.company').textContent;`);
  await key("Enter", "Enter", 13);
  await until(`document.querySelector('.toast')`);
  await sleep(500);
  check("focus moves to the next row after Skip", await ev(`return !!document.activeElement.closest('.row');`));
  check(`Skip (Enter key) moves ${skippedCo} out of All`, (await count("all")) === allBefore - 1 && (await count("skipped")) === skBefore + 1);
  await clickTab("skipped");
  check("Skipped tab lists it", await ev(`return [...document.querySelectorAll('#list .row .company')].some(c => c.textContent === ${JSON.stringify(skippedCo)});`));
  check("Skipped tab button reads Unskip", await ev(`return document.querySelector('#list .row .actions .btn:not(.primary)').textContent === 'Unskip';`));
  await ev(`[...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === ${JSON.stringify(skippedCo)}).querySelector('.actions .btn:not(.primary)').click();`);
  await sleep(900);
  check("Unskip puts it back", (await count("all")) === allBefore && (await count("skipped")) === skBefore);
  await clickTab("all");
  // ---- §M10 Apply: open the application page (synchronously) + add to To apply; no dialog, no draft ----
  const toApply0 = await count("to_apply");
  await ev(STUB_OPEN);
  const rowSel = (co) => `[...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === ${JSON.stringify(co)})`;
  const tk = await ev(`const r = ${rowSel("TikTok Shop")}; const b = r.querySelector('.btn.primary'); const label = b.textContent; b.click();
    return { label, openedNow: window.__opened.slice(), id: +r.dataset.id };`);
  const tkJob = await ev(`return (await import('./api.js')).api.job(${tk.id});`);
  check("M10: the Apply label names the destination", tk.label === ({ linkedin: "Apply on LinkedIn", jobsdb: "Apply on JobsDB", company_site: "Apply on company site", other: "Apply on job page" })[tkJob.apply_via], `${tk.label} (${tkJob.apply_via})`);
  check("M10: Apply opens apply_url in a new tab synchronously (noopener)", tk.openedNow.length === 1 && tk.openedNow[0][0] === tkJob.apply_url && tk.openedNow[0][1] === "_blank" && tk.openedNow[0][2] === "noopener", JSON.stringify(tk.openedNow));
  await sleep(150);
  check("M10: Apply shows progress (Adding…) and no dialog", await ev(`const b = ${rowSel("TikTok Shop")}?.querySelector('.btn.primary'); return !!b && b.disabled && b.textContent.includes('Adding') && !document.querySelector('#apply-result').open;`));
  const t0 = Date.now();
  await until(`[...document.querySelectorAll('.toast')].some(t => t.textContent.startsWith('Opened ') && t.textContent.includes('Added to To apply'))`, 3000);
  check("M10: toast says where it opened and what to do next (no codex wait)", Date.now() - t0 < 2500, await ev(`return [...document.querySelectorAll('.toast')].map(t => t.textContent).join(' | ');`));
  await sleep(500);
  check("To apply count +1 after Apply", (await count("to_apply")) === toApply0 + 1);
  check("M8: focus moves to the next row after Apply", await ev(`return !!document.activeElement.closest('#list .row');`), await ev(`return document.activeElement.tagName + '.' + document.activeElement.className;`));
  await shot("1280-apply-created");
  // a LinkedIn job with an off-site link goes to the company site; a plain one to LinkedIn
  const vias = await ev(`const api = (await import('./api.js')).api; const res = await api.jobs('all', '', 0, 100); return res.jobs.map(j => j.apply_via);`);
  check("M10: mock rows carry apply_url/apply_via (company_site and linkedin present)", vias.includes("company_site") && vias.includes("linkedin") && vias.includes("jobsdb"), [...new Set(vias)].join(", "));
  // apply (already tracked): still opens the page, says so
  await ev(STUB_OPEN);
  await ev(`[...document.querySelectorAll('#list .row')].find(r => r.querySelector('.tracked')).querySelector('.btn.primary').click();`);
  check("M10: tracked Apply still opens the page", (await ev(`return window.__opened.length;`)) === 1);
  await until(`[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Already in your tracker'))`, 3000);
  check("Apply (tracked) says already in tracker", true);
  check("To apply count unchanged after tracked Apply", (await count("to_apply")) === toApply0 + 1);
  // apply on a FILTERED job needs a second click; the first click opens nothing
  await clickTab("filtered");
  check("FILTERED (gate) rows read 'Apply anyway'", await ev(`const b = [...document.querySelectorAll('#list .row .btn.apply')]; return b.length > 0 && b.every(b => b.textContent === 'Apply anyway');`));
  // §M12: pre-filtered rows: no "? ? ? ?" chips, a muted "Not scored", the reason line, and "Score anyway"
  check("M12: pre-filtered rows show 'Not scored' (no chips), the reason, and Score anyway", await ev(`const rs = [...document.querySelectorAll('#list .row')].filter(r => r.querySelector('.note.pre'));
    return rs.length > 0 && rs.every(r => !r.querySelector('.chip') && r.querySelector('.not-scored')?.textContent === 'Not scored' && /^Auto-filtered: /.test(r.querySelector('.note.pre').textContent) && r.querySelector('.btn.primary').textContent === 'Score anyway');`));
  await shot("1280-filtered-m12");
  await ev(STUB_OPEN);
  await ev(`document.querySelector('#list .row .btn.apply').click();`); await sleep(200);
  check("Apply on FILTERED asks 'Click again to apply' first and opens nothing", await ev(`return document.querySelector('#list .row .btn.apply').textContent === 'Click again to apply' && window.__opened.length === 0;`));
  await ev(`document.querySelector('#list .row .btn.apply').click();`);
  check("M10: the second click on FILTERED opens the page", (await ev(`return window.__opened.length;`)) === 1);
  await sleep(900);
  // §M12: Score anyway scores the job now and opens it
  const all1 = await count("all");
  const preCo = await ev(`const r = [...document.querySelectorAll('#list .row')].find(r => r.querySelector('.btn.score')); r.querySelector('.btn.score').click(); return r.querySelector('.company').textContent;`);
  await sleep(200);
  check("M12: Score anyway shows progress", await ev(`const b = document.querySelector('#list .row .btn.score[disabled]'); return !!b && b.textContent.includes('Scoring');`));
  await until(`document.querySelector('#drawer').open && document.querySelector('#drawer .d-body:not(.d-loading)')`, 6000);
  check("M12: the scored job opens with its four circles, and All gains it", await ev(`return document.querySelector('#drawer .company').textContent === ${JSON.stringify(preCo)} && document.querySelectorAll('#drawer .reason .chip').length === 4 && !document.querySelector('#drawer .reason .chip.s-na');`) && (await count("all")) === all1 + 1, preCo);
  await key("Escape", "Escape", 27); await sleep(400);
  await clickTab("all");
  // mark submitted
  await clickTab("to_apply");
  check("M10: To apply rows: Mark submitted, Apply page link (apply_link), Draft cover email", await ev(`const r = ${rowSel("TikTok Shop")}; const a = r?.querySelector('a.apply-page');
    return !!a && a.target === '_blank' && a.rel.includes('noopener') && a.textContent.trim() === 'Apply page' && r.querySelector('.btn.primary').textContent === 'Mark submitted' && [...r.querySelectorAll('button')].some(b => b.textContent === 'Draft cover email');`));
  check("M10: the new record's next step says where to apply (no cover email)", await ev(`const t = ${rowSel("TikTok Shop")}.querySelector('.next').textContent; return /^Apply via .+ Then press Mark submitted\.$/.test(t) && !/cover/i.test(t);`), await ev(`return ${rowSel("TikTok Shop")}.querySelector('.next').textContent;`));
  await shot("1280-to-apply");
  const applied0 = await count("applied"), toApplyNow = await count("to_apply");
  await ev(`document.querySelector('#list .row .btn.primary').click();`);
  await ev(`document.querySelector('#list .row .btn.primary').click();`); // confirm
  await sleep(900);
  check("Mark submitted moves a record to Applied", (await count("applied")) === applied0 + 1 && (await count("to_apply")) === toApplyNow - 1);
  await clickTab("all");
  // paste (M11: "+ Add job" header button opens a dialog)
  await ev(`document.getElementById('add-job').click();`); await sleep(150);
  check("M11: + Add job opens the dialog with the URL field focused", await ev(`return document.getElementById('add-dialog').open && document.activeElement.id === 'p-url';`));
  await shot("1280-add-job");
  await ev(`document.getElementById('p-url').value = 'https://th.jobsdb.com/job/fail-123'; document.querySelector('#add-dialog form').requestSubmit();`);
  await sleep(300);
  check("Paste disables the button with progress", await ev(`const b = document.querySelector('#add-dialog button[type=submit]'); return b.disabled && b.textContent.includes('Scoring');`));
  await until(`document.querySelector('#add-dialog .form-msg.error')`, 5000);
  check("Paste error is shown and the URL kept", await ev(`return document.getElementById('p-url').value.includes('fail');`));
  await shot("1280-paste-error");
  // paste: success
  const all0 = await count("all");
  await ev(`document.getElementById('p-url').value = ''; document.getElementById('p-text').value = 'Key Account Executive, FMCG, Bangkok. Fresh graduates welcome.'; document.getElementById('p-company').value = 'Paste Test Co.'; document.querySelector('#add-dialog form').requestSubmit();`);
  await until(`document.querySelector('#drawer').open && document.querySelector('#drawer .d-body:not(.d-loading)')`, 6000);
  check("Paste success opens the scored job", await ev(`return document.querySelector('#drawer .company').textContent === 'Paste Test Co.';`));
  await key("Escape", "Escape", 27); await sleep(300);
  check("Paste success adds a job to All", (await count("all")) === all0 + 1);
  // site changes (M11: one-line notice, Review opens a dialog)
  check("M11: careers notice is one line", await ev(`const n = document.getElementById('site-changes'); return !n.hidden && n.offsetHeight < 56 && /careers pages? changed/.test(n.textContent);`));
  await ev(`document.querySelector('#site-changes .btn').click();`); await sleep(200);
  const sc0 = await ev(`return document.querySelectorAll('#sites-dialog .change').length;`);
  await shot("1280-sites-dialog");
  await ev(`document.querySelector('#sites-dialog .change .btn').click();`); await sleep(900);
  check("Mark seen removes a site change", (await ev(`return document.querySelectorAll('#sites-dialog .change').length;`)) === sc0 - 1);
  await ev(`document.getElementById('sites-dialog').close();`);
  // ---- M5 cover email: on demand since §M10 (To apply / drawer → Draft cover email) ----
  await send("Browser.grantPermissions", { permissions: ["clipboardReadWrite", "clipboardSanitizedWrite"] }).catch(() => {});
  const coverFor = async (co) => {
    await ev(`[...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === ${JSON.stringify(co)}).querySelector('.actions').querySelectorAll('button').forEach(b => { if (b.textContent === 'Draft cover email') b.click(); });`);
    await until(`document.querySelector('#apply-result').open && document.querySelector('#apply-result .cover').getAttribute('aria-busy') === 'false'`, 6000);
  };
  const writeDraft = async () => {
    await ev(`[...document.querySelectorAll('#apply-result .cover button')].find(b => b.textContent === 'Write draft').click();`);
    await until(`document.querySelector('#apply-result .mail-body, #apply-result .cover .form-msg.error')`, 6000);
  };
  await go(); await ready();
  // apply on Agoda (no saved draft) so it is in To apply, then write its draft on demand
  await ev(`[...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === 'Agoda').querySelector('.btn.primary').click();`);
  await sleep(900); await clickTab("to_apply");
  await coverFor("Agoda");
  check("M5: no saved draft (404) offers Write draft", await ev(`return [...document.querySelectorAll('#apply-result .cover button')].some(b => b.textContent === 'Write draft');`));
  await writeDraft();
  check("M5: Write draft shows subject + body", await ev(`return document.querySelector('#apply-result .mail .subj').textContent.startsWith('Application: ') && document.querySelector('#apply-result .mail-body').textContent.includes('Best regards');`));
  check("M5: Copy subject / Copy body / Regenerate / path / footer present", await ev(`const t = [...document.querySelectorAll('#apply-result button')].map(b => b.textContent);
    return ['Copy subject', 'Copy body', 'Regenerate', 'Copy path'].every(x => t.includes(x)) && !!document.querySelector('#apply-result .cover-path code') && document.querySelector('#apply-result .m-foot').textContent.startsWith('Draft only. Nothing is sent');`));
  check("M5: clean draft says no problems", await ev(`return !!document.querySelector('#apply-result .clean') && !document.querySelector('#apply-result .viol');`));
  await send("Page.bringToFront");
  await ev(`[...document.querySelectorAll('#apply-result button')].find(b => b.textContent === 'Copy body').click();`);
  await sleep(300);
  check("M5: Copy body confirms", await ev(`return [...document.querySelectorAll('#apply-result button')].some(b => b.textContent === 'Copied');`));
  const clip = await ev(`try { return await navigator.clipboard.readText(); } catch (e) { return 'unreadable: ' + e.message; }`);
  check("M5: clipboard holds the body", clip.startsWith("Dear Hiring Team,"), clip.slice(0, 40));
  await ev(`[...document.querySelectorAll('#apply-result button')].find(b => b.textContent === 'Open draft file').click();`);
  await until(`[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Shown in Finder'))`, 4000);
  check("M5: Open draft file calls reveal", true);
  await shot("1280-cover-draft");
  const path1 = await ev(`return document.querySelector('#apply-result .cover-path code').textContent;`);
  await ev(`[...document.querySelectorAll('#apply-result button')].find(b => b.textContent === 'Regenerate').click();`);
  await sleep(250);
  check("M5: Regenerate disables buttons and shows progress", await ev(`const b = [...document.querySelectorAll('#apply-result .cover button')]; return b.every(x => x.disabled) && document.querySelector('#apply-result .cover .form-msg').textContent.includes('Writing a new draft');`));
  await until(`document.querySelector('#apply-result .cover .form-msg.ok')`, 6000);
  check("M5: Regenerate shows a new draft file", (await ev(`return document.querySelector('#apply-result .cover-path code').textContent;`)) !== path1);
  await key("Escape", "Escape", 27); await sleep(500);
  check("M10: a drafted cover email is added to the record's next step", await ev(`return [...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === 'Agoda').querySelector('.next').textContent.includes('Cover email draft: job-hunt/data/drafts/');`));
  // saved draft (P&G) opens directly
  await coverFor("P&G Thailand");
  check("M8: cover dialog has no null/undefined text", await ev(`return !/\\b(null|undefined)\\b/.test(document.getElementById('apply-result').innerText);`));
  check("M5: To apply opens the saved draft (GET cover-email)", await ev(`return document.querySelector('#apply-result .subj').textContent.includes('Account Manager') && document.getElementById('apply-title').textContent === 'Cover email draft';`));
  await shot("1280-to-apply-cover");
  await key("Escape", "Escape", 27); await sleep(400);
  // violations (first draft has planted problems)
  await go("&cover=viol"); await ready(); await clickTab("to_apply");
  await coverFor("Agoda"); await writeDraft();
  check("M5: violations shown above the text", await ev(`const v = document.querySelector('#apply-result .viol'); return !!v && v.textContent.startsWith('Check before sending:') && v.textContent.includes('900K') && !!(v.compareDocumentPosition(document.querySelector('#apply-result .mail')) & Node.DOCUMENT_POSITION_FOLLOWING);`));
  await shot("1280-cover-violations");
  await key("Escape", "Escape", 27); await sleep(400);
  // codex down: the write fails with an error, the next try succeeds
  await go("&cover=fail"); await ready(); await clickTab("to_apply");
  await coverFor("Agoda");
  await ev(`[...document.querySelectorAll('#apply-result .cover button')].find(b => b.textContent === 'Write draft').click();`);
  await until(`document.querySelector('#apply-result .cover .form-msg.error')`, 6000);
  check("M5: a failed draft shows the error and keeps Write draft", await ev(`return document.querySelector('#apply-result .cover .form-msg.error').textContent.includes('codex') && [...document.querySelectorAll('#apply-result .cover button')].some(b => b.textContent === 'Write draft' && !b.disabled);`));
  await shot("1280-cover-failed");
  await writeDraft();
  check("M5: trying again after a failure produces a draft", await ev(`return !!document.querySelector('#apply-result .mail-body');`));
  await key("Escape", "Escape", 27); await sleep(400);
  // Thai posting: apply, then draft from the drawer
  await go(); await ready();
  await ev(`[...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').lang === 'th').querySelector('.btn.primary').click();`);
  await sleep(900); await clickTab("to_apply");
  await coverFor("บริษัท ไทยเบฟเวอเรจ จำกัด (มหาชน)"); await writeDraft();
  check("M5: Thai draft is marked lang=th and counted in characters", await ev(`return document.querySelector('#apply-result .mail-body').lang === 'th' && document.querySelector('#apply-result .cover-actions .sub').textContent.includes('characters');`));
  await shot("1280-cover-thai");
  await key("Escape", "Escape", 27); await sleep(400);
  // ---- M6: updates, confirm form, change status, lessons ----
  await go(); await ready();
  await until(`!document.getElementById('updates').hidden && document.querySelectorAll('#updates .card').length`, 5000);
  check("M8: updates start as one line with Review", await ev(`return document.getElementById('update-cards').hidden && document.querySelector('#updates .upd-toggle').getAttribute('aria-expanded') === 'false' && /1 rejection/.test(document.querySelector('#updates .updates-head .sub').textContent);`));
  await ev(`document.querySelector('#updates .upd-toggle').click();`);
  const cards = () => ev(`return document.querySelectorAll('#updates .card').length;`);
  const card = (co) => `[...document.querySelectorAll('#updates .card')].find(c => c.querySelector('.company').textContent === ${JSON.stringify(co)})`;
  check("M6: Updates panel shows 4 cards", (await cards()) === 4);
  check("M6: card shows the change, excerpt and email", await ev(`const c = ${card("Example Retail Co.")}; return c.querySelector('.change-line').textContent === 'Applied → Rejected · CV screen' && !!c.querySelector('blockquote') && c.querySelector('.excerpt-meta').textContent.includes('careers@example-retail.invalid');`));
  check("M6: ghosted card says how long", await ev(`return ${card("Example Logistics Co.")}.textContent.includes('No reply since applying on');`));
  const leak = `return (document.body.innerText.match(/\\b(null|undefined)\\b|\\[object /g) || []).join(',');`;
  check("no null/undefined/[object text (All + updates)", (await ev(leak)) === "", await ev(leak));
  await shot("1280-updates");
  // interview invite: direct confirm
  await ev(`${card("Example FMCG Co.")}.querySelector('.btn.primary').click();`);
  await until(`!${card("Example FMCG Co.")}`, 5000);
  check("M6: Confirm (interview) removes the card", (await cards()) === 3);
  // rejection: form first
  await ev(`${card("Example Retail Co.")}.querySelector('.btn.primary').click();`);
  await sleep(200);
  check("M6: Confirm on a rejection opens the reason form, prefilled", await ev(`const c = ${card("Example Retail Co.")}; return !!c.querySelector('.confirm-form') && c.querySelector('select').value === 'start_date' && document.activeElement === c.querySelector('select');`));
  await ev(`const c = ${card("Example Retail Co.")}; c.querySelector('textarea').value = 'They want a January 2027 start.';`);
  await shot("1280-confirm-form");
  await ev(`${card("Example Retail Co.")}.querySelector('.confirm-form').requestSubmit();`);
  await until(`!${card("Example Retail Co.")}`, 5000);
  check("M6: rejection confirmed", (await cards()) === 2);
  // dismiss
  await ev(`[...${card("Example Bank (Singapore)")}.querySelectorAll('button')].find(b => b.textContent === 'Dismiss').click();`);
  await until(`!${card("Example Bank (Singapore)")}`, 5000);
  check("M6: Dismiss removes the card", (await cards()) === 1);
  // ghosted confirm
  await ev(`${card("Example Logistics Co.")}.querySelector('.btn.primary').click();`); await sleep(150);
  check("M6: ghosted form prefilled 'ghosted'", await ev(`return ${card("Example Logistics Co.")}.querySelector('select').value === 'ghosted';`));
  await ev(`${card("Example Logistics Co.")}.querySelector('.confirm-form').requestSubmit();`);
  await until(`document.getElementById('updates').hidden`, 5000);
  check("M6: panel hides when nothing is pending", true);
  // applied tab
  await clickTab("applied");
  const rowOf = (co) => `[...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === ${JSON.stringify(co)})`;
  check("M6: confirmed records show their outcome", await ev(`return ${rowOf("Example Retail Co.")}.querySelector('.outcome').textContent.includes('Start date') && ${rowOf("Example FMCG Co.")}.querySelector('.status-pill').textContent === 'Interview';`));
  check("M6: Rejected/Ghosted rows are quieter", await ev(`return ${rowOf("Example Retail Co.")}.classList.contains('quiet') && ${rowOf("Example Logistics Co.")}.classList.contains('quiet') && !${rowOf("Example FMCG Co.")}.classList.contains('quiet');`));
  await ev(`[...${rowOf("Example Bank (Singapore)")}.querySelectorAll('button')].find(b => b.textContent === 'Change status').click();`);
  await sleep(150);
  await ev(`const f = ${rowOf("Example Bank (Singapore)")}.querySelector('.status-form'); const [st, stage] = f.querySelectorAll('select'); st.value = 'Test'; stage.value = 'online_test';`);
  check("no null/undefined/[object text (Applied)", (await ev(leak)) === "", await ev(leak));
  await shot("1280-applied-status-form");
  await ev(`${rowOf("Example Bank (Singapore)")}.querySelector('.status-form').requestSubmit();`);
  await until(`${rowOf("Example Bank (Singapore)")}?.querySelector('.status-pill').textContent === 'Test'`, 5000);
  check("M6: Change status saves", await ev(`return ${rowOf("Example Bank (Singapore)")}.querySelector('.outcome').textContent.includes('Online test');`));
  check("M6: applied tab has no horizontal scroll", await ev(`return document.documentElement.scrollWidth <= innerWidth;`));
  // lessons
  await clickTab("lessons");
  check("M6: Lessons shows 3 aggregate panels", (await ev(`return document.querySelectorAll('#list .agg').length;`)) === 3);
  check("M6: Lessons tab count == rows", (await count("lessons")) === (await ev(`return document.querySelectorAll('#list .lesson-row').length;`)));
  check("M6: Lessons rows = 5 (AlphaSights + 2 examples + 2 confirmed)", (await ev(`return document.querySelectorAll('#list .lesson-row').length;`)) === 5);
  check("M6: AlphaSights lesson present", await ev(`return [...document.querySelectorAll('#list .lesson-row .company')].some(c => c.textContent.startsWith('AlphaSights'));`));
  check("no null/undefined/[object text (Lessons)", (await ev(leak)) === "", await ev(leak));
  await shot("1280-lessons");
  await size(1440, 900); await sleep(200); await shot("1440-lessons"); await size(1280, 860);
  await go("&scenario=empty"); await ready(); await clickTab("lessons");
  check("M6: Lessons empty state explains", await ev(`return document.querySelector('#list .state h3').textContent === 'No lessons yet';`));
  await shot("1280-lessons-empty");
  // states
  // ---- M9: logos + region filter ----
  await go(); await ready(); await sleep(600);
  await ev(`localStorage.removeItem('jobhunt.region.v2');`);
  // Feed cards are taller than the old table: visit each lazy logo before asserting decoding/fallback.
  await ev(`for (const img of [...document.querySelectorAll('#list .logo img')]) {
    img.scrollIntoView({ block: 'center' });
    await new Promise(resolve => {
      if (img.complete) { resolve(); return; }
      img.addEventListener('load', resolve, { once: true });
      img.addEventListener('error', resolve, { once: true });
      setTimeout(resolve, 2000);
    });
  } window.scrollTo(0, 0);`);
  await sleep(300);
  check("M9: logos load from local files (img decoded)", await ev(`const imgs = [...document.querySelectorAll('#list .logo img')]; return imgs.length >= 3 && imgs.every(i => i.complete) && imgs.filter(i => i.naturalWidth > 0).length >= 3;`));
  check("M9: every logo image is same-origin (or its trimmed data: copy)", await ev(`return [...document.querySelectorAll('img')].every(i => new URL(i.src).origin === location.origin || i.src.startsWith('data:image/png'));`));
  check("M9: a broken logo falls back to the monogram", await ev(`const r = [...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === 'Nestlé Thailand'); return !!r && r.querySelector('.logo.mono')?.textContent === 'N';`), await ev(`const r = [...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === 'Nestlé Thailand'); return r ? r.querySelector('.logo').outerHTML.slice(0, 80) : 'no row';`));
  check("M9: null logo shows ONE initial (Thai name too)", await ev(`const t = (co) => [...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === co)?.querySelector('.logo.mono')?.textContent; return t('Grab Thailand') === 'G' && t('Siam Piwat') === 'S' && t('Gulf Development') === 'G' && /^[\\u0E01-\\u0E2E]$/.test(t('บริษัท ไทยเบฟเวอเรจ จำกัด (มหาชน)') || '');`));
  check("M9: the fallback is neutral grey (no generated colours)", await ev(`const ms = [...document.querySelectorAll('.logo.mono')]; const bg = new Set(ms.map(m => getComputedStyle(m).backgroundColor)); return ms.length > 0 && bg.size === 1 && [...bg][0] === 'rgb(236, 238, 242)' && ms.every(m => m.textContent.length === 1);`));
  check("M9 polish: a wide logo gets the .wide class (less padding)", await ev(`const r = [...document.querySelectorAll('#list .row')].find(r => r.querySelector('.company').textContent === 'Accenture'); return !!r?.querySelector('.logo.wide img');`));
  check("Feed company names are complete and fit their container", await ev(`return [...document.querySelectorAll('#list .feed-card-head .company')].every(c => c.textContent.length > 0 && c.scrollWidth <= c.clientWidth + 1 && c.title === c.textContent);`));
  check("M9: logos are decorative (aria-hidden, alt empty)", await ev(`return [...document.querySelectorAll('.logo')].every(l => l.getAttribute('aria-hidden') === 'true') && [...document.querySelectorAll('.logo img')].every(i => i.getAttribute('alt') === '');`));
  const regionOn = async (r) => { await ev(`document.querySelector('.seg-btn[data-region="${r}"]').click();`); await sleep(300); await until(`document.querySelector('#list').getAttribute('aria-busy') === 'false' && document.querySelector('.seg-btn[data-region="${r}"]')?.getAttribute('aria-pressed') === 'true'`); await sleep(300); };
  const allCount = await count("all");
  check("M9: region control shows counts (All == tab count)", await ev(`return +document.querySelector('.seg-btn[data-region="all"] .count').textContent;`) === allCount);
  await regionOn("abroad");
  const ab = await ev(`return { n: document.querySelectorAll('#list .rows > li.row').length, c: [...document.querySelectorAll('#list .rows > li.row')].map(r => r.dataset.country), seg: +document.querySelector('.seg-btn[data-region="abroad"] .count').textContent, url: location.search, ls: localStorage.getItem('jobhunt.region.v2'), head: document.querySelector('#list .list-head h2').textContent };`);
  check("M9: Abroad lists only abroad jobs; tab count + segment count match", ab.n > 0 && ab.c.every((c) => c && c !== "Thailand") && ab.n === (await count("all")) && ab.seg === ab.n && ab.n < allCount, JSON.stringify(ab));
  check("M9: region is in the URL and localStorage; headline says abroad", ab.url.includes("region=abroad") && ab.url.includes("mock=1") && ab.ls === "abroad" && /jobs? abroad$/.test(ab.head), ab.head);
  await go("", null); await ready(); await sleep(500);
  check("M9: region survives a reload (localStorage)", await ev(`return document.querySelector('.seg-btn[data-region="abroad"]').getAttribute('aria-pressed') === 'true';`));
  await regionOn("thailand");
  const th = await ev(`return { n: document.querySelectorAll('#list .rows > li.row').length, c: [...document.querySelectorAll('#list .rows > li.row')].map(r => r.dataset.country) };`);
  check("M9: Thailand lists only Thai jobs", th.n > 0 && th.c.every((c) => c === "Thailand") && th.n === (await count("all")), `${th.n} rows`);
  check("M9: Thailand + Abroad < All (unknown country only under All)", th.n + ab.n < allCount, `${th.n} + ${ab.n} vs ${allCount}`);
  await clickTab("applied");
  check("M9: no region control on record tabs", await ev(`return !document.querySelector('.seg');`));
  await clickTab("all"); await regionOn("all");
  check("M9: back to All restores the count and sets the explicit URL param", (await count("all")) === allCount && (await ev(`return new URLSearchParams(location.search).get('region') === 'all';`)));
  await ev(`
    const api = (await import('./api.js')).api;
    const original = api.jobs;
    const template = (await original('all', '', 0, 100)).jobs[0];
    const jobs = Array.from({ length: 210 }, (_, i) => ({ ...template, id: 10000 + i, rank: i + 1 }));
    window.__pageOffsets = [];
    api.jobs = async (tab, query, offset = 0, limit = 100, region) => {
      if (tab !== 'all') return original(tab, query, offset, limit, region);
      window.__pageOffsets.push(offset);
      await new Promise(r => setTimeout(r, 500));
      return { total: jobs.length, jobs: jobs.slice(offset, offset + limit) };
    };`);
  await clickTab("centre"); await clickTab("all");
  const firstPage = await ev(`return [...document.querySelectorAll('#list .feed-card')].map(r => r.dataset.id);`);
  await ev(`document.querySelector('#list .more button').click(); document.querySelector('#list .more button').click();`);
  check("Show more displays progress and disables repeat clicks", await ev(`const b = document.querySelector('#list .more button'); return b.disabled && b.textContent.includes('Loading') && document.querySelector('#list').getAttribute('aria-busy') === 'true';`));
  await ready();
  check("Pagination appends once, preserving the existing order", await ev(`const ids = [...document.querySelectorAll('#list .feed-card')].map(r => r.dataset.id); return ids.length === 200 && new Set(ids).size === 200 && JSON.stringify(ids.slice(0, 100)) === ${JSON.stringify(JSON.stringify(firstPage))} && window.__pageOffsets.filter(n => n === 100).length === 1;`));
  await ev(`document.querySelector('#list .more button').click();`); await ready();
  check("Final page removes Show more after all results arrive", await ev(`return document.querySelectorAll('#list .feed-card').length === 210 && !document.querySelector('#list .more');`));
  await go("&scenario=oldrows"); await ready(); await sleep(800);
  await ev(`for (const row of [...document.querySelectorAll('#list .feed-card')].slice(0, 5)) { row.scrollIntoView({ block: 'center' }); await new Promise(r => setTimeout(r, 150)); }`);
  await until(`document.querySelectorAll('#list .feed-card .why:not(.pending):not(:empty)').length > 3`);
  check("§M8: without JobRow.summary, rows fall back to the detail fetch", await ev(`return document.querySelectorAll('#list .feed-card .why:not(.pending):not(:empty)').length > 3;`));
  await go("&scenario=slow"); await sleep(600);
  check("loading state (skeleton) shows", await ev(`return document.querySelectorAll('#list .skel').length > 0;`));
  await shot("1280-state-loading");
  await go("&scenario=empty"); await ready();
  check("empty state shows", await ev(`return !!document.querySelector('#list .state h3');`));
  await shot("1280-state-empty");
  await go("&scenario=error"); await until(`document.querySelector('#list .state.error')`);
  check("error state shows with Retry", await ev(`return !!document.querySelector('#list .state.error button');`));
  await shot("1280-state-error");
  // New shell: primary navigation and every task remain usable on small screens.
  for (const width of [390, 768]) {
    await size(width, 844); await go(); await ready();
    for (const t of ["all", "centre", "hold", "low", "filtered", "to_apply", "applied", "skipped", "lessons", "wiki"]) {
      await clickTab(t);
      check(`${width}: ${t} fits without horizontal page scrolling`, await ev(`return document.documentElement.scrollWidth <= innerWidth;`));
      check(`${width}: ${t} has one active list and main navigation item`, await ev(`return document.querySelectorAll('.tab[aria-current]').length === 1 && document.querySelectorAll('.primary-nav [aria-current]').length === 1;`));
    }
    await ev(`document.querySelector('[data-route="all"]').click();`); await ready();
    check(`${width}: Discover returns to feed`, await ev(`return document.querySelector('.tab[aria-current]').dataset.tab === 'all';`));
    await ev(`document.querySelector('#list .open').click();`); await until(`document.querySelector('#drawer[open] .d-body:not(.d-loading)')`);
    check(`${width}: job details and footer fit`, await ev(`const d = document.getElementById('drawer'); return d.scrollWidth <= d.clientWidth && d.getBoundingClientRect().right <= innerWidth && !!d.querySelector('.d-foot .apply');`));
    await key("Escape", "Escape", 27); await until(`!document.getElementById('drawer').open`);
    await ev(`document.getElementById('add-job').click();`);
    check(`${width}: Add job fits`, await ev(`const d = document.getElementById('add-dialog'); return d.open && d.scrollWidth <= d.clientWidth && d.getBoundingClientRect().right <= innerWidth;`));
    await key("Escape", "Escape", 27);
    await ev(`document.querySelector('#list .row .actions .quiet').click();`); await until(`document.querySelector('.toast')`);
    check(`${width}: action feedback stays inside the viewport`, await ev(`const r = document.querySelector('.toast').getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth;`));
  }
  // Career Wiki & OAuth Onboarding end-to-end check
  await size(1280, 860);
  await go("&scenario=onboarding"); await ready();
  await until(`!document.getElementById('setup-banner').hidden`, 4000);
  check("Wiki: first-run setup banner shows when unconfigured", await ev(`return !document.getElementById('setup-banner').hidden;`));
  await ev(`document.querySelector('#setup-banner .btn.primary').click();`);
  await until(`document.querySelector('#list .wiki-page') && document.querySelector('#list').getAttribute('aria-busy') === 'false'`, 4000);
  check("Wiki: setup banner CTA opens Career Wiki tab and hides banner", await ev(`return location.hash.includes('tab=wiki') && document.getElementById('setup-banner').hidden && document.querySelectorAll('.provider-card').length === 3;`));
  await shot("1280-wiki-onboarding");
  await ev(`document.querySelector('.provider-card[data-provider="claude"] .provider-select').click();`);
  check("Wiki: selecting Claude highlights Claude provider card", await ev(`return document.querySelector('.provider-card[data-provider="claude"]').classList.contains('selected');`));
  await ev(`
    const r = document.getElementById('wiki-resume-text');
    r.value = 'Jordan Lee\\nB.B.A. Finance & Analytics (Expected May 2027, GPA 3.55)\\nOperations Lead at Campus Venture Lab (student-run): closed THB 670K.';
    r.dispatchEvent(new Event('input'));
    const q = document.getElementById('wiki-qa-target_roles');
    q.value = 'Commercial management trainee and partner growth';
    q.dispatchEvent(new Event('input'));
    [...document.querySelectorAll('.wiki-card button.btn.primary')].find(b => b.textContent.includes('Scan Resume')).click();
  `);
  await until(`!!document.querySelector('.wiki-followup') && document.getElementById('wf-name').value === 'Jordan Lee'`, 6000);
  check("Wiki: AI scan populates candidate fields, facts.md, and follow-up Q&A", await ev(`return document.getElementById('wf-name').value === 'Jordan Lee' && document.getElementById('wiki-doc-editor').value.includes('Jordan Lee') && document.querySelectorAll('.wiki-followup li').length >= 2;`));
  check("Wiki: no italics outside quotations", await ev(`return [...document.querySelectorAll('body *')].filter(n => getComputedStyle(n).fontStyle === 'italic' && !n.closest('blockquote')).length === 0;`));
  await ev(`[...document.querySelectorAll('#wiki-step-3 button.btn.primary')].find(b => b.textContent.includes('Save Career Wiki')).click();`);
  await until(`document.getElementById('profile-name').textContent === 'Jordan Lee'`, 5000);
  check("Wiki: Save Career Wiki updates sidebar profile and marks ready", await ev(`return document.querySelector('.wiki-head .pill').textContent === 'Career Wiki ready' && document.getElementById('profile-avatar').textContent === 'JL';`));
  await shot("1280-wiki-saved");
  // the error scenario logs a failed request? (mock is in-memory: no network). Clear known-intentional nothing.
}

// innerHTML guard: no source file uses innerHTML / outerHTML / insertAdjacentHTML / document.write
const src = await Promise.all(["app.js", "api.js", "dev/mock.js"].map((f) => fetch(new URL(f, BASE)).then((r) => r.text())));
check("no innerHTML-family calls in board JS", !src.some((s) => /innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(s)));

if (LIVE) check("live: read-only (no write request was even attempted)", blockedWrites.length === 0, blockedWrites.join(" "));
check("no console errors", consoleErrors.length === 0, consoleErrors.join(" | "));
check("no requests outside 127.0.0.1", external.length === 0, external.join(" "));
for (const [s, n, i] of results) console.log(`${s}  ${n}${i ? "  — " + i : ""}`);
console.log(`\nrequests seen: ${requests.length} (all 127.0.0.1: ${external.length === 0})`);
console.log("screens: " + shots.join(", "));
await fetch(`http://127.0.0.1:${CDP}/json/close/${target.id}`);
ws.close();
process.exit(results.some((r) => r[0] === "FAIL") ? 1 : 0);
