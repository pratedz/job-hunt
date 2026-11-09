// In-memory fake of the board API (contract: pair/M4-api.md) for ?mock=1.
// Loads dev/fixtures/_db.json (built by make_fixtures.py) and applies POSTs to that copy only.
// Scenarios (?scenario=): "empty" = no data · "error" = every GET fails · "slow" = 2.5 s GETs · "oldrows" = JobRow without summary (pre-§M8).
// Cover email (on demand since §M10, POST …/cover-email) (?cover=): "" = clean draft · "viol" = the first draft
// has checker violations (planted THB 900K, an em dash) · "fail" = the first write fails with 502 (codex down),
// the next one succeeds. Apply itself never drafts. Mock drafts use only facts.md wording (F01, F02, F21, F23, F04); they are layout data, not output.
// Paste: a URL containing "fail" returns an HTTP error; a title or JD containing "senior" is
// pre-filtered; JD text containing "codex" ends in state "error"; anything else is scored.

const CIRCLES = ["love", "good_at", "needs", "paid"];
const ORDER = { CENTRE: 0, HOLD: 1, LOW: 2, FILTERED: 3 };
const TAB_VERDICT = { centre: "CENTRE", hold: "HOLD", low: "LOW", filtered: "FILTERED" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (o) => JSON.parse(JSON.stringify(o));
const today = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Bangkok" });
// §M9: synthetic fixture logos (dev/make_logos.py), keyed by company. "broken.png" fails to decode on purpose
// (monogram fallback); companies not listed have logo_url null (monogram). Live serves /logos/<file>.
const LOGOS = {
  "TikTok Shop": "ring.png", "Unilever Thailand": "dot.png", "Shopee": "bars.png", "Agoda": "split.png",
  "Lalamove": "tri.png", "Accenture": "wide.png", "Nestlé Thailand": "broken.png", "P&G Thailand": "dot.png",
  "Sea Limited": "bars.png", "Deutsche Bank (Singapore)": "split.png", "DKSH Thailand": "ring.png",
};
const logoFor = (company) => (LOGOS[company] ? `dev/fixtures/logos/${LOGOS[company]}` : null);
// §M9 region rule (mirrors the backend): facts.location_country first, else parse the location; unknown -> null.
const COUNTRY_OVERRIDE = { 111: ["Singapore", "Singapore"], 105: ["Malaysia", "Kuala Lumpur, Malaysia"], 176: [null, ""] }; // mock variety: two more abroad, one unknown
const THAI = /thailand|bangkok|samut|chonburi|phuket|khon kaen|chiang|nonthaburi|rayong|ไทย|กรุงเทพ/i;
const ABROAD = { singapore: "Singapore", "hong kong": "Hong Kong", malaysia: "Malaysia", vietnam: "Vietnam", indonesia: "Indonesia", philippines: "Philippines", japan: "Japan" };
function countryOf(j) {
  if (j.id in COUNTRY_OVERRIDE) { j.location = COUNTRY_OVERRIDE[j.id][1]; return COUNTRY_OVERRIDE[j.id][0]; }
  if (j.facts?.location_country) return j.facts.location_country;
  const loc = j.location || "";
  if (THAI.test(loc)) return "Thailand";
  const k = Object.keys(ABROAD).find((x) => loc.toLowerCase().includes(x));
  return k ? ABROAD[k] : null;
}
const regionOf = (j) => (j.country == null ? null : j.country === "Thailand" ? "thailand" : "abroad");
const inRegion = (j, region) => !region || region === "all" || regionOf(j) === region;
// §M10: where Apply sends him. LinkedIn jobs with an off-site apply link go to the company site (every third
// one here, to a .invalid host so nothing real is ever loaded); the rest stay on LinkedIn. JobsDB -> JobsDB.
// Greenhouse/Lever/Workday/SmartRecruiters -> their posting page (company_site). Pasted/manual -> other.
function applyFor(j) {
  const url = j.url || null;
  if (!url) return { apply_url: null, apply_via: null };
  if (j.source === "linkedin") {
    return j.id % 3 === 0
      ? { apply_url: `https://careers.${j.company.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}.invalid/apply/${j.id}`, apply_via: "company_site" }
      : { apply_url: url, apply_via: "linkedin" };
  }
  if (j.source === "jobsdb") return { apply_url: url, apply_via: "jobsdb" };
  if (["greenhouse", "workday", "smartrecruiters"].includes(j.source)) return { apply_url: url, apply_via: "company_site" };
  if (j.source === "lever") return { apply_url: url.replace(/\/$/, "") + "/apply", apply_via: "company_site" };
  return { apply_url: url, apply_via: "other" };
}
const VIA_NAME = { linkedin: "LinkedIn", jobsdb: "JobsDB", company_site: "the company site", other: "the job page" };
const PLATFORM = { linkedin: "LinkedIn", jobsdb: "JobsDB", company_site: "Company career site", other: "Other" };
const DETAIL_ONLY = ["reasons", "facts", "salary", "jd_text", "jd_missing", "email_snippet"];

function rankKey(j) {
  return [
    j.verdict == null ? 4 : ORDER[j.verdict],
    -(j.weakest ? j.weakest.score : -1),
    -(j.mean ?? -1),
    -Date.parse(j.first_seen_at),
  ];
}
function cmp(a, b) {
  const ka = rankKey(a), kb = rankKey(b);
  for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
  return 0;
}
function derive(j) {
  const known = CIRCLES.filter((c) => j.scores[c] != null).map((c) => [c, j.scores[c]]);
  if (known.length) {
    const lo = Math.min(...known.map((k) => k[1]));
    j.weakest = { circle: known.find((k) => k[1] === lo)[0], score: lo };
    j.mean = known.reduce((s, k) => s + k[1], 0) / known.length;
  } else { j.weakest = null; j.mean = null; }
  if (j.gate.result === "fail" || j.prefilter_reason) j.verdict = "FILTERED";
  else if (!known.length) j.verdict = "HOLD";
  else if (known.some((k) => k[1] < 3)) j.verdict = "LOW";
  else j.verdict = known.length < 4 ? "HOLD" : "CENTRE";
  return j;
}
// Contract change 7 Nov 2026: to_apply = my_status "To apply" AND section "Apply now".
const isToApply = (a) => a.my_status === "To apply" && a.section === "Apply now";
const obsidian = (file) => "obsidian://open?vault=Career-Wiki&file=" + encodeURIComponent(file);
const ok = (json) => ({ status: 200, json });
const fail = (status, error) => ({ status, json: { ok: false, error } });

const CONTACT = "Alex Rivera\n+66 81 555 0199\nalex.rivera@example.com\nlinkedin.com/in/alex-rivera";
function draftFor(j, mode, n) {
  const slug = (t) => t.toLowerCase().replace(/[^a-z0-9\u0E00-\u0E7F]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
  const path = `job-hunt/data/drafts/${today()}_${slug(j.company)}_${slug(j.title)}${n > 1 ? "_v" + n : ""}.md`;
  if (/[\u0E00-\u0E7F]/.test(j.title + j.company)) {
    return {
      subject: `Application: ${j.title} – Alex Rivera`,
      body: `เรียน ฝ่ายบุคคล ${j.company}\n\nผมเป็นนักศึกษาชั้นปีสุดท้าย สาขาการเงินและการวิเคราะห์ธุรกิจ คาดว่าจะสำเร็จการศึกษาในเดือนพฤษภาคม 2027 และพร้อมเริ่มงานเต็มเวลาตั้งแต่เดือนมิถุนายน 2027 ขอสมัครตำแหน่ง ${j.title} ครับ\n\nในองค์กรนักศึกษา Campus Venture Lab ผมเจรจาพันธมิตรค้าปลีกโมเดิร์นเทรดสำเร็จและช่วยขยายสินค้าผู้ประกอบการรายย่อยเข้าสู่ 100 สาขา\n\nแนบเรซูเม่มาพร้อมนี้ และยินดีอย่างยิ่งหากได้มีโอกาสพูดคุยครับ\n\nขอแสดงความนับถือ\n${CONTACT}`,
      path, violations: [], facts_used: ["F01", "F02", "F21", "F04"],
    };
  }
  const body = `Dear Hiring Team,\n\nI am a final-year Finance and Analytics student graduating in May 2027 and available full-time from June 2027, and I am applying for the ${j.title} role.\n\nAt Campus Venture Lab, a student-run enterprise, I negotiated a modern-trade retail partnership and helped launch an SME shelf across 100 retail branches. As Operations Lead of the same student-run enterprise, with one week left and THB 400K short of breakeven, I led a 4-day sales sprint that closed THB 670K in revenue, 170% of the gap.\n\n${mode === "viol" ? "The posting says the team grew sales to THB 900K last year — I would like to help it go further. " : ""}The ${j.title} role puts this kind of partner and sales work at the centre. My CV is attached, and I would welcome a conversation.\n\nBest regards,\n${CONTACT}`;
  return {
    subject: `Application: ${j.title} – Alex Rivera`, body, path,
    violations: mode === "viol" ? ["Number \"900K\" (900000) is not in facts.md", "Em dash \"—\" is not allowed"] : [],
    facts_used: ["F01", "F02", "F21", "F23", "F04"],
  };
}

function createInitialWikiState(onboarding) {
  return {
    ok: true,
    configured: !onboarding,
    candidate: {
      name: onboarding ? "" : "Alex Rivera",
      email: onboarding ? "" : "alex.rivera@example.com",
      degree: onboarding ? "" : "B.B.A. Finance & Analytics",
      gpa: onboarding ? 0 : 3.45,
      graduation_year: 2027,
      degree_completed: false,
      fulltime_experience_years: 0,
      languages: ["English", "Thai"],
      test_scores: onboarding ? {} : { IELTS: 7.5 },
      nationality: onboarding ? "" : "Thai",
      based_in: onboarding ? "" : "Bangkok, Thailand",
    },
    gates: {
      pass_max_years: 0,
      ask_max_years: 2,
      available_th: "2027-06",
      available_abroad: "2027-07",
      pass_cities: ["Bangkok"],
      pass_countries: ["Thailand", "Singapore"],
      remote: "pass",
      internship: "ask",
      contract: "ask",
    },
    paid: { anchors_thb_month: [30000, 40000, 55000, 75000] },
    search: { keywords: ["management trainee", "commercial graduate", "key account executive", "business development"] },
    llm: {
      provider: "codex",
      model: "",
      providers: [
        {
          id: "codex", label: "Codex (ChatGPT OAuth)", subscription: "ChatGPT Plus / Pro / Team",
          binary: "codex", binary_path: "/usr/local/bin/codex", install_cmd: "npm install -g @openai/codex",
          login_cmd: "codex login", installed: true, authenticated: true, ready: true,
          detail: "Logged in using ChatGPT OAuth", selected: true,
        },
        {
          id: "claude", label: "Claude Code (Claude OAuth)", subscription: "Claude Pro / Max / Team",
          binary: "claude", binary_path: "/usr/local/bin/claude", install_cmd: "npm install -g @anthropic-ai/claude-code",
          login_cmd: "claude auth login", installed: true, authenticated: false, ready: false,
          detail: "Installed, not signed in yet — run `claude auth login`", selected: false,
        },
        {
          id: "gemini", label: "Gemini CLI (Google OAuth)", subscription: "Free Google Account / Gemini Code Assist",
          binary: "gemini", binary_path: null, install_cmd: "npm install -g @google/gemini-cli",
          login_cmd: "gemini", installed: false, authenticated: false, ready: false,
          detail: "CLI not installed (`npm install -g @google/gemini-cli`)", selected: false,
        },
      ],
    },
    vault: { builtin: true, path: "data/wiki" },
    facts_md: onboarding
      ? "# Verified Candidate Facts\n\nUpload your resume or fill in the guided Q&A above to generate your grounded F01..Fn facts ledger."
      : "# Verified Candidate Facts\n\n## Identity & Contact\n- **F01**: Alex Rivera — B.B.A. in Finance & Analytics, graduating May 2027.\n- **F02**: Available full-time in Bangkok from June 2027 and abroad from July 2027.\n- **F04**: Contact: alex.rivera@example.com | +66 81 555 0199 | linkedin.com/in/alex-rivera\n\n## Experience & Projects\n- **F21**: At Campus Venture Lab (student-run), negotiated a modern-trade retail partnership across 100 branches.\n- **F23**: Led a 4-day sales sprint closing THB 670K (170% of THB 400K breakeven gap).\n- **F26**: Commercial analytics intern automating weekly KPI reporting in SQL and Python.\n\n## Student-company markers\n- `Campus Venture Lab`\n\n## Banned phrases\n- `passionate self-starter` / `synergy` / `rockstar` / `engineered`",
    rubric_md: "# Ikigai Scoring Rubric\n\n## What you love (1 to 5)\n- **5**: Commercial ownership, account growth, partner negotiation, or rotational graduate tracks.\n- **3**: Mixed analytical and operational roles with clear business exposure.\n- **1**: Pure back-office compliance, cold-call-only telesales, or unrelated support.\n\n## What you are good at (1 to 5)\n- **5**: Directly matches verified facts in `facts.md` (partner negotiation, commercial reporting, SQL/Python).\n- **3**: Transferable graduate requirements.\n- **1**: Requires senior domain credentials not in `facts.md`.\n\n## What the world needs (1 to 5)\n- **5**: Structured mentorship, strong team calibre, and clear progression.\n- **3**: Standard entry-level onboarding.\n- **1**: High-churn role with little mentorship.",
    cover_md: "# Cover Email Rules\n\n- Keep under 220 words in English (or concise formal Thai for Thai postings).\n- Cite only verified numbers and organizations from `facts.md`.\n- Never use em dashes or generic AI buzzwords.",
  };
}

export async function createMock({ scenario = "", cover = "" } = {}) {
  let dailyStarted = null;
  let wikiState = createInitialWikiState(scenario === "onboarding");
  let db;
  if (scenario === "empty") {
    db = { jobs: [], applications: [], site_changes: [], last_run: null };
  } else {
    const res = await fetch(new URL("./fixtures/_db.json", import.meta.url));
    db = await res.json();
  }
  let nextId = 900;
  const coverFailedOnce = new Set();
  for (const j of db.jobs) { Object.assign(j, applyFor(j)); j.country = countryOf(j); j.logo_url = logoFor(j.company); if (j.id in COUNTRY_OVERRIDE && j.facts) { j.facts.location_country = COUNTRY_OVERRIDE[j.id][0]; j.facts.location_city = j.location.split(",")[0] || null; } }
  for (const a of db.applications) {
    a.logo_url = logoFor(a.company);
    const j = db.jobs.find((x) => x.id === a.jobhunt_id);
    a.apply_link = a.apply_link || (j && j.apply_url) || a.link || null;
  }
  db.suggestions = db.suggestions || [];
  const CLOSED = ["Rejected", "Ghosted", "Withdrawn"];
  const daysSince = (d) => Math.round((Date.parse(today()) - Date.parse(d)) / 86400000);
  function setStatus(a, fields) {
    const was = a.my_status;
    Object.assign(a, fields);
    if (fields.my_status && fields.my_status !== was) {
      a.outcome_date = today();
      if (was === "To apply" && fields.my_status !== "To apply") { a.date_applied = a.date_applied || today(); }
      if (CLOSED.includes(fields.my_status)) a.section = "Archive";
    }
    a.days_waiting = a.date_applied ? daysSince(a.date_applied) : null;
  }
  function lessonsRes() {
    const ls = db.applications.filter((a) => a.my_status === "Rejected" || a.my_status === "Ghosted").map((a) => ({
      company: a.company, program: a.program, my_status: a.my_status, outcome_stage: a.outcome_stage || null,
      outcome_reason_category: a.outcome_reason_category || null, outcome_reason: a.outcome_reason || null,
      outcome_date: a.outcome_date || null, ikigai: a._ikigai || null, weakest_circle: a._weakest || null,
    }));
    const agg = (k, none) => ls.reduce((o, l) => { const v = l[k] || none; o[v] = (o[v] || 0) + 1; return o; }, {});
    return { lessons: ls, by_stage: agg("outcome_stage", "unknown"), by_category: agg("outcome_reason_category", "no_reason_given"), by_weakest_circle: agg("weakest_circle", "null") };
  }
  db.drafts = db.drafts || {};
  const drafts = new Map(Object.entries(db.drafts).map(([k, v]) => [+k, [v]]));
  const latest = (id) => { const d = drafts.get(id); return d ? d[d.length - 1] : null; };
  function writeDraft(j, mode) {
    const n = (drafts.get(j.id)?.length || 0) + 1;
    const d = draftFor(j, mode, n);
    drafts.set(j.id, [...(drafts.get(j.id) || []), d]);
    return d;
  }

  const inTab = (j, tab) => {
    if (tab === "skipped") return j.status === "skipped";
    if (j.status !== "new") return false;
    // Contract change 7 Nov 2026: all = CENTRE + HOLD + LOW only; unscored jobs show only as stats.unscored.
    return tab === "all" ? ["CENTRE", "HOLD", "LOW"].includes(j.verdict) : j.verdict === TAB_VERDICT[tab];
  };
  const matches = (j, q) => !q || (j.company + " " + j.title).toLowerCase().includes(q.toLowerCase());
  // §M8: JobRow carries summary (str | null). scenario=oldrows drops it, as a pre-M8 server would.
  const row = (j) => {
    const r = clone(j); DETAIL_ONLY.forEach((k) => delete r[k]);
    if (scenario === "oldrows") delete r.summary; else r.summary = r.summary ?? null;
    return r;
  };

  function stats(region = "all", tab = "all") {
    const tabs = {};
    for (const t of ["all", "centre", "hold", "low", "filtered", "skipped"]) tabs[t] = db.jobs.filter((j) => inTab(j, t) && inRegion(j, region)).length;
    tabs.to_apply = db.applications.filter(isToApply).length;
    tabs.applied = db.applications.filter((a) => a.my_status !== "To apply").length;
    tabs.lessons = db.applications.filter((a) => a.my_status === "Rejected" || a.my_status === "Ghosted").length;
    const live = db.jobs.filter((j) => j.status === "new" && ["CENTRE", "HOLD", "LOW"].includes(j.verdict) && inRegion(j, region));
    const jobTab = ["all", "centre", "hold", "low", "filtered", "skipped"].includes(tab) ? tab : "all";
    const inT = db.jobs.filter((j) => inTab(j, jobTab));
    const regions = { all: inT.length, thailand: inT.filter((j) => regionOf(j) === "thailand").length, abroad: inT.filter((j) => regionOf(j) === "abroad").length };
    const shortfall = {};
    for (const c of CIRCLES) shortfall[c] = live.filter((j) => j.scores[c] != null && j.scores[c] < 3).length;
    const reasons = new Map();
    for (const j of db.jobs.filter((x) => x.status === "new" && x.verdict === "FILTERED")) {
      for (const r of (j.gate.fails.length ? j.gate.fails : [j.prefilter_reason])) {
        const k = r.split(";")[0];
        reasons.set(k, (reasons.get(k) || 0) + 1);
      }
    }
    return {
      last_run: db.last_run, tabs, shortfall, regions,
      filtered_top_reasons: [...reasons].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([reason, count]) => ({ reason, count })),
      site_changes_unseen: db.site_changes.length,
      suggestions_pending: db.suggestions.length,
      unscored: db.jobs.filter((j) => j.verdict == null).length,
    };
  }

  // §M10: Apply creates the record and returns where to apply; it never drafts a cover email (no codex wait).
  function applyJob(j) {
    const out = { apply_url: j.apply_url, apply_via: j.apply_via, cover_email: null };
    if (j.tracked) {
      return { ok: true, created: false, already_tracked: true, record_file: j.tracked.file, obsidian_url: j.tracked.obsidian_url, ...out };
    }
    const safe = (s) => s.replace(/[\\/:*?"<>|#^[\]]/g, "-").trim();
    let file = `61 Application Records/${safe(j.company)} — ${safe(j.title)}.md`;
    j.tracked = { file, obsidian_url: obsidian(file), my_status: "To apply" };
    j.status = "to_apply";
    const s = j.scores, letter = (c, l) => l + (s[c] ?? "?") + (c === "paid" && j.paid_estimated ? "e" : "");
    db.applications.unshift({
      record_file: file, obsidian_url: obsidian(file), company: j.company, program: j.title, my_status: "To apply",
      section: "Apply now", date_applied: null, days_waiting: null, logo_url: j.logo_url,
      next_step: `Apply via ${VIA_NAME[j.apply_via] || "the job page"}: ${j.apply_url}. Then press Mark submitted.`,
      jobhunt_id: j.id, link: j.url, apply_link: j.apply_url, job_platform: PLATFORM[j.apply_via] || "Other",
      _ikigai: [letter("love", "L"), letter("good_at", "G"), letter("needs", "N"), letter("paid", "P")].join(" "),
      _weakest: j.weakest ? j.weakest.circle : null,
    });
    return { ok: true, created: true, already_tracked: false, record_file: file, obsidian_url: obsidian(file), ...out };
  }

  function paste(body) {
    const url = (body.url || "").trim(), text = (body.jd_text || "").trim();
    if (!url && !text) return fail(400, "Give a job URL or paste the JD text.");
    if (/fail/i.test(url)) return fail(502, "Could not fetch the job page (HTTP 404 from th.jobsdb.com). Paste the JD text instead.");
    const title = body.title || (url ? "Pasted job " + nextId : text.split("\n")[0].slice(0, 80));
    const j = derive({
      id: nextId++, rank: null, company: body.company || (url ? new URL(url).hostname.replace(/^www\./, "") : "Unknown company"),
      title, location: "Bangkok, Thailand", country: "Thailand", logo_url: null, apply_url: url || null, apply_via: url ? "other" : null, source: "manual", url: url || null, posted_at: null,
      first_seen_at: new Date().toISOString(), verdict: null, status: "new",
      scores: { love: 4, good_at: 3, needs: 4, paid: 3 }, paid_estimated: true, weakest: null, mean: null,
      gate: { result: "pass", fails: [], questions: [] }, prefilter_reason: null, tracked: null, error: null,
      reasons: { love: "Mock reason: commercial graduate role.", good_at: "Mock reason: F26 reporting.", needs: "Mock reason: formal training.", paid: "Mock estimate about THB 42,500/month." },
      summary: "Mock score for a pasted job.", facts: {}, salary: null,
      jd_text: text || (url ? `(mock) JD text fetched from ${url}` : null), jd_missing: false, email_snippet: null,
    });
    let state = "scored", error;
    if (/senior/i.test(title + " " + text)) {
      Object.assign(j, { prefilter_reason: "title_exclude: \\bsenior\\b", scores: { love: null, good_at: null, needs: null, paid: null }, reasons: {}, summary: null, gate: { result: null, fails: [], questions: [] } });
      derive(j); state = "prefiltered";
    } else if (/codex/i.test(text)) {
      Object.assign(j, { verdict: null, weakest: null, mean: null, scores: { love: null, good_at: null, needs: null, paid: null }, gate: { result: null, fails: [], questions: [] }, error: "codex timeout after 120 s; will retry next run", reasons: {}, summary: null });
      state = "error"; error = j.error;
    }
    db.jobs.push(j);
    return ok({ ok: true, job_id: j.id, state, ...(error ? { error } : {}) });
  }

  return async function handle(method, path, body) {
    if (path === "/api/daily") {
      if (method === "POST") dailyStarted = Date.now();
      const running = dailyStarted && Date.now() - dailyStarted < 2500;
      return { status: 200, json: { phase: running ? "running" : dailyStarted ? "completed" : "not_run", can_start: !dailyStarted,
        today: "2026-11-08", summary: dailyStarted && !running ? { at: new Date(dailyStarted).toISOString(), finished_at: new Date().toISOString(), scored: 8, jd_fetched: 8, problems: [] } : null,
        lines: dailyStarted ? ["discover: start", "fetch-jd: start", running ? "score: start" : "done: 8 scored"] : [] } };
    }
    const isPost = method === "POST";
    await sleep(isPost ? (/\/(paste|cover-email|score)$/.test(path) ? 1500 : 350) : scenario === "slow" ? 2500 : 120); // §M10: apply is quick
    if (!isPost && scenario === "error") return fail(500, "Mock server error: database is locked (scenario=error)");
    const u = new URL(path, location.origin);
    const p = u.pathname;
    let m;
    const region = u.searchParams.get("region") || "all";
    if (!["all", "thailand", "abroad"].includes(region)) return fail(400, `Unknown region: ${region}`);
    if (!isPost && p === "/api/stats") return ok(stats(region, u.searchParams.get("tab") || "all"));
    if (!isPost && p === "/api/jobs") {
      const tab = u.searchParams.get("tab") || "all";
      if (!["all", "centre", "hold", "low", "filtered", "skipped"].includes(tab)) return fail(400, `Unknown tab: ${tab}`);
      const query = u.searchParams.get("q") || "";
      const limit = +(u.searchParams.get("limit") || 100), offset = +(u.searchParams.get("offset") || 0);
      const sel = db.jobs.filter((j) => inTab(j, tab) && matches(j, query) && inRegion(j, region)).sort(cmp);
      let r = 0;
      const rows = sel.map((j) => { const x = row(j); x.rank = j.verdict == null ? null : ++r; return x; });
      return ok({ total: rows.length, jobs: rows.slice(offset, offset + limit) });
    }
    if (!isPost && (m = p.match(/^\/api\/jobs\/(\d+)$/))) {
      const j = db.jobs.find((x) => x.id === +m[1]);
      return j ? ok(clone(j)) : fail(404, `No job ${m[1]}`);
    }
    if (!isPost && p === "/api/applications") {
      const st = u.searchParams.get("status");
      let apps = db.applications.filter((a) => (st === "to_apply" ? isToApply(a) : a.my_status !== "To apply"));
      if (st !== "to_apply") apps = apps.sort((a, b) => (b.date_applied || "").localeCompare(a.date_applied || ""));
      return ok({ applications: clone(apps).map((a) => { delete a._ikigai; delete a._weakest; return a; }) });
    }
    if (!isPost && p === "/api/site-changes") return ok({ items: clone(db.site_changes) });
    if (isPost && (m = p.match(/^\/api\/jobs\/(\d+)\/cover-email\/reveal$/))) {
      const j = db.jobs.find((x) => x.id === +m[1]);
      return j && latest(j.id) ? ok({ ok: true }) : fail(404, `No cover email draft for job ${m[1]}`);
    }
    if (!isPost && p === "/api/suggestions") {
      return ok({ items: clone(db.suggestions).map((sg) => ({ ...sg, current_status: db.applications.find((x) => x.record_file === sg.record_file)?.my_status ?? null })) });
    }
    if (isPost && (m = p.match(/^\/api\/suggestions\/(\d+)\/(confirm|dismiss)$/))) {
      const sg = db.suggestions.find((x) => x.id === +m[1]);
      if (!sg) return fail(404, `No suggestion ${m[1]}`);
      db.suggestions = db.suggestions.filter((x) => x !== sg);
      if (m[2] === "dismiss") return ok({ ok: true });
      const a = db.applications.find((x) => x.record_file === sg.record_file);
      if (!a) return fail(404, "Record not found");
      const pr = sg.proposed;
      const reason = [sg.excerpt, body && body.note].filter(Boolean).join(" / ").slice(0, 300) || null;
      setStatus(a, { my_status: pr.my_status, next_step: pr.next_step || a.next_step, outcome_stage: pr.outcome_stage,
        outcome_reason_category: (body && body.outcome_reason_category) || pr.outcome_reason_category || null, outcome_reason: reason });
      return ok({ ok: true, record_file: a.record_file });
    }
    if (isPost && p === "/api/applications/status") {
      const a = db.applications.find((x) => x.record_file === (body && body.record_file));
      if (!a) return fail(404, "Record not found");
      const allowed = ["Applied", "Test", "Interview", "Offer", "Rejected", "Ghosted", "Withdrawn", "To apply"];
      if (!allowed.includes(body.my_status)) return fail(400, `Unknown status: ${body.my_status}`);
      const f = { my_status: body.my_status };
      for (const k of ["outcome_stage", "outcome_reason_category", "outcome_reason", "next_step"]) if (body[k]) f[k] = body[k];
      setStatus(a, f);
      return ok({ ok: true });
    }
    if (!isPost && p === "/api/lessons") return ok(lessonsRes());
    if ((m = p.match(/^\/api\/jobs\/(\d+)\/cover-email$/))) {
      const j = db.jobs.find((x) => x.id === +m[1]);
      if (!j) return fail(404, `No job ${m[1]}`);
      if (!isPost) { const d = latest(j.id); return d ? ok({ ok: true, cover_email: clone(d) }) : fail(404, "No cover email draft for this job yet."); }
      if (cover === "fail" && !coverFailedOnce.has(j.id)) { coverFailedOnce.add(j.id); return fail(502, "codex exited 1: run codex login to sign in"); }
      const d = writeDraft(j, cover === "viol" && !latest(j.id) ? "viol" : "");
      const app = db.applications.find((a) => a.jobhunt_id === j.id);
      if (app) app.next_step = app.next_step.replace(/ Cover email draft: .*$/, "") + " Cover email draft: " + d.path;
      return ok({ ok: true, cover_email: clone(d) });
    }
    // §M12: score one pre-filtered job now (like paste). A JD containing "codex" fails, as in paste.
    if (isPost && (m = p.match(/^\/api\/jobs\/(\d+)\/score$/))) {
      const j = db.jobs.find((x) => x.id === +m[1]);
      if (!j) return fail(404, `No job ${m[1]}`);
      if (/codex/i.test(j.jd_text || "")) return ok({ ok: true, job_id: j.id, state: "error", error: "codex timeout after 120 s; will retry next run" });
      Object.assign(j, { prefilter_reason: null, scores: { love: 3, good_at: 3, needs: 4, paid: 3 }, paid_estimated: true,
        gate: { result: "pass", fails: [], questions: [] }, summary: "Mock score for a job scored anyway: a plausible fit, but check the seniority the title suggests.",
        reasons: { love: "Mock reason.", good_at: "Mock reason.", needs: "Mock reason.", paid: "Mock estimate." }, error: null });
      derive(j);
      return ok({ ok: true, job_id: j.id, state: "scored" });
    }
    if (isPost && (m = p.match(/^\/api\/jobs\/(\d+)\/(skip|unskip|apply|submitted)$/))) {
      const j = db.jobs.find((x) => x.id === +m[1]);
      if (!j) return fail(404, `No job ${m[1]}`);
      if (m[2] === "skip") { j.status = "skipped"; return ok({ ok: true, status: "skipped" }); }
      if (m[2] === "unskip") { j.status = "new"; return ok({ ok: true, status: "new" }); }
      if (m[2] === "apply") return ok(applyJob(j));
      return fail(400, "Use POST /api/applications/submitted");
    }
    if (isPost && p === "/api/applications/submitted") {
      const a = db.applications.find((x) => x.record_file === (body && body.record_file));
      if (!a) return fail(404, "No such record");
      a.my_status = "Applied"; a.date_applied = today(); a.days_waiting = 0;
      a.next_step = "Applied " + today() + ". Watch email for the next step.";
      const j = db.jobs.find((x) => x.id === a.jobhunt_id);
      if (j) { j.status = "applied"; if (j.tracked) j.tracked.my_status = "Applied"; }
      return ok({ ok: true });
    }
    if (isPost && p === "/api/paste") return paste(body || {});
    if (isPost && (m = p.match(/^\/api\/site-changes\/(\d+)\/seen$/))) {
      db.site_changes = db.site_changes.filter((s) => s.id !== +m[1]);
      return ok({ ok: true });
    }
    if (!isPost && p === "/api/wiki") return ok(clone(wikiState));
    if (isPost && p === "/api/wiki/provider") {
      const pid = String((body && body.provider) || "").trim().toLowerCase();
      if (!["codex", "claude", "gemini"].includes(pid)) return fail(400, "Choose one of: codex, claude, gemini");
      wikiState.llm.provider = pid;
      wikiState.llm.model = String((body && body.model) || "").trim();
      for (const pr of wikiState.llm.providers) pr.selected = pr.id === pid;
      return ok({ ok: true, llm: clone(wikiState.llm) });
    }
    if (isPost && p === "/api/wiki/extract-resume") {
      const filename = String((body && body.filename) || "resume.txt").trim();
      const b64 = String((body && body.data_base64) || "");
      if (!b64) return fail(400, "No resume data uploaded.");
      let decoded = "";
      if (/\.(txt|md)$/i.test(filename)) {
        try {
          const raw = b64.includes(",") ? b64.split(",")[1] : b64;
          const bytes = Uint8Array.from(atob(raw), (c) => c.charCodeAt(0));
          decoded = new TextDecoder("utf-8").decode(bytes).trim();
        } catch { /* fallback below */ }
      }
      const text = decoded || `Alex Rivera — B.B.A. Finance & Analytics (Expected May 2027, GPA 3.55)\nalex.rivera@example.com | +66 81 555 0199\nExperience:\n- Commercial Analytics Intern, Regional Commerce Co. (Jun–Aug 2026): automated weekly SQL/Python KPI dashboards for 14 categories.\n- Operations Lead, Campus Venture Lab (student-run, 2025–2026): negotiated modern-trade partnership across 100 branches and closed THB 670K in a 4-day sprint.`;
      return ok({ ok: true, filename, text, char_count: text.length });
    }
    if (isPost && p === "/api/wiki/generate") {
      const rText = String((body && body.resume_text) || "").trim();
      const qa = (body && body.qa) || {};
      const hasQa = Object.values(qa).some((v) => String(v || "").trim().length > 0);
      if (rText.length < 20 && !hasQa) return fail(400, "Upload or paste your resume (or fill in the Q&A answers) before generating your Career Wiki.");
      const firstLine = rText.split("\n")[0] || "";
      const nameMatch = firstLine.match(/^([A-Za-z][A-Za-z .'-]{2,40})/);
      const candName = (nameMatch ? nameMatch[1].trim() : "") || wikiState.candidate.name || "Alex Rivera";
      const generated = {
        wiki_summary: `Built a grounded Career Wiki for ${candName} with 6 verified facts (F01–F26), custom 1–5 Ikigai scoring anchors, and automatic Bangkok + Singapore eligibility gates.`,
        follow_up_questions: [
          "Do you have an English proficiency test score (IELTS / TOEIC / TOEFL) to add to your eligibility gates?",
          "Are there specific industries or job types (such as 100% commission sales or night-shift ops) you want auto-filtered?",
          "What monthly base salary in THB feels like a 3/5 (acceptable) versus a 5/5 (strong offer)?",
        ],
        candidate: {
          name: candName,
          email: wikiState.candidate.email || "alex.rivera@example.com",
          degree: "B.B.A. Finance & Analytics",
          gpa: 3.55,
          graduation_year: 2027,
          degree_completed: false,
          fulltime_experience_years: 0,
          languages: ["English", "Thai"],
          test_scores: { IELTS: 7.5 },
          nationality: "Thai",
          based_in: "Bangkok, Thailand",
        },
        gates: {
          pass_max_years: 0,
          ask_max_years: 2,
          available_th: "2027-06",
          available_abroad: "2027-07",
          pass_cities: ["Bangkok"],
          pass_countries: ["Thailand", "Singapore"],
          remote: "pass",
          internship: "ask",
          contract: "ask",
        },
        paid: { anchors_thb_month: [32000, 42000, 55000, 75000] },
        search: {
          keywords: ["management trainee", "commercial analyst", "business development", "key account executive"],
          title_exclude_extra: [],
        },
        facts_md: `# Verified Candidate Facts\n\n## Identity & Contact\n- **F01**: ${candName} — B.B.A. in Finance & Analytics (GPA 3.55), graduating May 2027.\n- **F02**: Available full-time in Bangkok from June 2027 and abroad from July 2027.\n- **F04**: Contact: alex.rivera@example.com | +66 81 555 0199\n\n## Experience & Projects\n- **F21**: Operations Lead at Campus Venture Lab (student-run): negotiated modern-trade partnership across 100 branches.\n- **F23**: Led a 4-day sales sprint closing THB 670K in revenue.\n- **F26**: Commercial Analytics Intern: automated weekly SQL and Python KPI dashboards across 14 product categories.\n\n## Student-company markers\n- \`Campus Venture Lab\`\n\n## Banned phrases\n- \`passionate self-starter\` / \`synergy\` / \`rockstar\` / \`engineered\``,
        rubric_md: `# Ikigai Scoring Rubric\n\n## What you love (1 to 5)\n- **5**: Commercial strategy, partner growth, category ownership, or structured graduate programmes${qa.target_roles ? " (" + String(qa.target_roles).slice(0, 80) + ")" : ""}.\n- **3**: General business analyst or operational coordination roles.\n- **1**: Pure cold-calling or back-office administration.\n\n## What you are good at (1 to 5)\n- **5**: Directly matches verified facts in \`facts.md\` (SQL/Python KPI automation F26, retail partner negotiation F21).\n- **3**: Entry-level analytical or commercial requirements.\n- **1**: Requires multi-year domain credentials absent from \`facts.md\`.\n\n## What the world needs (1 to 5)\n- **5**: Strong mentorship, high-calibre peers, and clear progression.\n- **3**: Standard onboarding.\n- **1**: High-churn role with unclear growth.`,
      };
      return ok({ ok: true, generated });
    }
    if (isPost && p === "/api/wiki/save") {
      const candIn = (body && body.candidate) || {};
      const name = String(candIn.name ?? wikiState.candidate.name ?? "").trim();
      if (!name) return fail(400, "Candidate name is required before saving your Career Wiki.");
      if (body.llm) {
        const pid = String(body.llm.provider || wikiState.llm.provider).toLowerCase();
        if (["codex", "claude", "gemini"].includes(pid)) {
          wikiState.llm.provider = pid;
          for (const pr of wikiState.llm.providers) pr.selected = pr.id === pid;
        }
        if ("model" in body.llm) wikiState.llm.model = String(body.llm.model || "").trim();
      }
      if (body.candidate) Object.assign(wikiState.candidate, body.candidate, { name });
      if (body.gates) Object.assign(wikiState.gates, body.gates);
      if (body.paid && Array.isArray(body.paid.anchors_thb_month)) wikiState.paid.anchors_thb_month = body.paid.anchors_thb_month;
      if (body.search && Array.isArray(body.search.keywords)) wikiState.search.keywords = body.search.keywords;
      if (body.vault && body.vault.path) {
        wikiState.vault.path = String(body.vault.path).trim();
        wikiState.vault.builtin = wikiState.vault.path === "data/wiki";
      }
      if (typeof body.facts_md === "string") wikiState.facts_md = body.facts_md;
      if (typeof body.rubric_md === "string") wikiState.rubric_md = body.rubric_md;
      if (typeof body.cover_md === "string") wikiState.cover_md = body.cover_md;
      wikiState.configured = true;
      return ok({ ...clone(wikiState), recomputed: db.jobs.length });
    }
    return fail(404, `Mock: no route for ${method} ${p}`);
  };
}
