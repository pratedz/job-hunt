// job hunt — board UI & Career Wiki setup. Plain ES module, no build, no external requests.
// Untrusted text (titles, companies, JD, reasons) is only ever set through textContent.
import { api, IS_MOCK } from "./api.js";

// ---------------------------------------------------------------------------
// All user-facing strings (English for now; translate here).
// ---------------------------------------------------------------------------
const S = {
  appName: "job hunt",
  mockFlag: "Mock data · logos are synthetic",
  mockFlagTitle: "Mock mode: in-memory fixture data; the logos are generated shapes, not company logos.",
  searchLabel: "Search company or title",
  searchPlaceholder: "Search roles or companies",
  tabs: { daily: "Daily update", favorites: "Favorites", all: "Your job feed", centre: "Best matches", hold: "Needs a closer look", low: "Lower matches", filtered: "Filtered out", to_apply: "To apply", applied: "Applied", skipped: "Skipped", lessons: "Lessons", wiki: "Career Wiki" },
  tabIntro: {
    daily: "Run today’s job search when you open your Mac.",
    favorites: "Jobs you starred, saved in this browser.",
    all: "Scored jobs that passed the gates (centre, unknown and low), best first. Filtered, skipped and tracked jobs are in their own tabs; unscored jobs wait for the next run.",
    centre: "All four circles are 3 or more. Start here.",
    hold: "At least one circle is unknown (?). Check the JD before deciding.",
    low: "At least one circle is below 3.",
    filtered: "Failed an eligibility gate or the pre-filter. Kept so you can see why.",
    skipped: "Jobs you skipped. Unskip puts a job back on the board.",
    lessons: "What rejections and silences have in common. These feed the scoring prompt as “Recent outcomes”.",
    to_apply: "Tracker records in “Apply now” that are not submitted yet. Mark submitted once you have sent it.",
    applied: "Every tracker record past \"To apply\", newest first.",
    wiki: "Choose 1 AI subscription (Codex, Claude, or Gemini OAuth) and build or edit your Career Wiki.",
  },
  circles: { love: "Love", good_at: "Good at", needs: "Needs", paid: "Paid" },
  circleShort: { love: "Love", good_at: "Good", needs: "Needs", paid: "Paid" },
  circleHelp: {
    love: "A role you want",
    good_at: "Your real evidence matches the JD",
    needs: "A growing place with a growth path",
    paid: "Salary against your anchors",
  },
  verdict: { CENTRE: "Strong match", HOLD: "More to explore", LOW: "Lower match", FILTERED: "Filtered out", null: "Not scored" },
  est: "est.",
  unknown: "?",
  weakest: "weakest",
  allUnknown: "all circles unknown",
  colJob: "Job", colCircles: ["Love", "Good", "Needs", "Paid"], colCirclesFull: ["Love", "Good at", "Needs", "Paid"], colVerdict: "Verdict", colRank: "#",
  apply: "Apply", applying: "Applying…", applyAnyway: "Apply anyway", applyConfirm: "Click again to apply",
  regions: { all: "All", thailand: "Thailand", abroad: "Abroad" },
  regionLabel: "Where the job is",
  regionWhere: { thailand: "in Thailand", abroad: "abroad" },
  regionSuffix: { thailand: " · in Thailand", abroad: " · abroad" },
  keysTitle: "Keyboard shortcuts",
  shortcutKeys: [["/", "search"], ["j k", "move"], ["a", "apply"], ["s", "skip"], ["Enter", "details"]], skip: "Skip", unskip: "Unskip", details: "Details",
  askBadge: (n) => (n === 1 ? "1 question" : `${n} questions`),
  askLabel: (n) => `${n === 1 ? "1 question" : `${n} questions`} to ask the employer`,
  askMore: (n) => ` (+${n} more)`,
  askTipTitle: "Ask the employer:",
  trackedBadge: (st) => `In tracker · ${st || "record"}`,
  weakestAria: "weakest", weakestLegend: "= weakest circle",
  // M8: specific headlines, computed from the data
  head: {
    favorites: (n) => `${n} favorite ${n === 1 ? "job" : "jobs"}`,
    all: (n, r) => `${n} ${n === 1 ? "job" : "jobs"}${r && r !== "all" ? " " + S.regionWhere[r] : " to explore"}`,
    centre: (n) => `${n} strong ${n === 1 ? "match" : "matches"}`,
    hold: (n) => `${n} ${n === 1 ? "job" : "jobs"} to review`,
    low: (n) => `${n} lower ${n === 1 ? "match" : "matches"}`,
    filtered: (n) => `${n} ${n === 1 ? "job" : "jobs"} filtered out`,
    skipped: (n) => (n ? `${n} skipped` : "Nothing skipped"),
    to_apply: (n) => `${n} ${n === 1 ? "application" : "applications"} to finish`,
    applied: (n) => `${n} ${n === 1 ? "application" : "applications"} sent`,
    lessons: (n) => `${n} application ${n === 1 ? "outcome" : "outcomes"}`,
    search: (n, q) => `${n} ${n === 1 ? "match" : "matches"} for “${q}”`,
  },
  stageInline: (k, label) => (k === "unknown" || !k ? "an unknown stage" : label),
  shortHead: (c, n) => `${c} is the most common gap (${n} ${n === 1 ? "job" : "jobs"})`,
  shortHeadNone: "No circle falls short",
  sitesHead: (names) => `${names.length} careers ${names.length === 1 ? "page" : "pages"} changed: ${names.join(", ")}`,
  jobPosting: "Job posting",
  jdMissing: "No JD, scored from email",
  prefilter: "Auto-filtered",
  failed: "Ruled out",
  waitingScore: "Waiting to be scored on the next run.",
  scoreError: "Scoring failed:",
  posted: "posted", seen: "seen",
  sources: { linkedin: "LinkedIn", jobsdb: "JobsDB", greenhouse: "Greenhouse", lever: "Lever", workday: "Workday", smartrecruiters: "SmartRecruiters", gmail: "Gmail alert", manual: "Pasted" },
  lastRun: "Last run",
  noRun: "No run yet. Run `python3 -m jobhunt run` to fill the board.",
  newOf: (n, f) => `${n} new / ${f}`,
  errors: (n) => (n === 1 ? "1 error" : `${n} errors`),
  totalNew: (n) => `${n} new`,
  sourceErrors: (names) => `errors: ${names.join(", ")}`,
  bySource: "By source",
  sourceErrorsHelp: "These sources failed on the last run; their jobs may be missing. The next run retries them. Hover the new count for details.",
  srcLine: (c) => `${c.new} new / ${c.found} found · ${c.prefiltered} pre-filtered · ${c.scored} scored`,
  unscored: (n) => `${n} unscored (next run)`,
  loading: "Loading jobs…",
  showing: (n, t) => (n === t ? `${t} ${t === 1 ? "job" : "jobs"}` : `Showing ${n} of ${t}`),
  showingApps: (n) => `${n} ${n === 1 ? "record" : "records"}`,
  more: "Show more",
  retry: "Try again",
  loadError: "Could not load this list",
  emptyTitle: {
    favorites: "No favorites yet",
    all: "No jobs on the board", centre: "Nothing in the centre yet", hold: "No unknowns", low: "No low-scoring jobs",
    filtered: "Nothing filtered", skipped: "Nothing skipped", to_apply: "Nothing waiting to apply", applied: "No applications yet",
    lessons: "No lessons yet",
  },
  emptyBody: {
    favorites: "Use the star at the top right of a job card to save it here.",
    all: "Your feed fills up as jobs are reviewed. Use Add job to bring in a role you have found.",
    centre: "No job has all four circles at 3 or more yet. Try Needs a closer look to explore other opportunities.",
    hold: "Every scored job has all four circles known.",
    low: "No job has a circle below 3.",
    filtered: "No job failed a gate or the pre-filter.",
    skipped: "Skip a job from any list and it lands here.",
    to_apply: "No “Apply now” record is waiting. Press Apply on a job to create one.",
    applied: "Records past \"To apply\" show here with days waiting.",
    lessons: "Lessons appear once an application ends as Rejected or Ghosted, either confirmed from an update or set with Change status on the Applied tab.",
  },
  emptySearch: (q) => `No match for “${q}” in this tab.`,
  clearSearch: "Clear search",
  // drawer
  close: "Close", closeLabel: "Close details",
  openPosting: "Open posting", openObsidian: "Open record in Obsidian",
  secSummary: "At a glance", secCircles: "Your fit", secGate: "Before you apply", secFacts: "Role details", secJd: "Job description", secSnippet: "Email snippet",
  gatePass: "Pass: no eligibility problem found.",
  gateAsk: "Check with the employer:",
  gateFail: "Fail: filtered for these reasons.",
  gateNone: "No gate result (not scored).",
  prefilterBox: "Filtered by the pre-filter before scoring (no AI call):",
  showJd: "Show the full JD", noJd: "No JD text was fetched; the job was scored from the email snippet.",
  noReason: "No reason recorded.",
  salary: "Salary", salaryStated: "stated in the posting", salaryEst: "estimate",
  viewSalary: "View salary details", viewSalaryEst: "View salary estimate",
  facts: {
    min_years_experience: "Min. experience", start: "Start", graduation_years: "Graduation years", location: "Location",
    required_languages: "Languages", required_test_scores: "Test scores", gpa_floor: "GPA floor", degree_completed_required: "Degree done when applying",
    job_type: "Job type", other_hard_requirements: "Other requirements",
  },
  years: (n) => (n === 0 ? "none" : n === 1 ? "1 year" : `${n} years`),
  startKind: { fixed_date: "fixed date", immediate: "immediate", flexible: "flexible", unknown: "not stated" },
  jobType: { graduate_programme: "Graduate programme", full_time: "Full-time", internship: "Internship", contract: "Contract", part_time: "Part-time", unknown: "Not stated" },
  yes: "yes", no: "no", notStated: "not stated", remote: "remote",
  // apply result
  createdTitle: "Added to the tracker",
  createdBody: "A record was created in 61 Application Records with status “To apply”, section “Apply now”.",
  alreadyTitle: "Already in the tracker",
  alreadyBody: "A record for this job already exists, so nothing was created.",
  coverTitle: "Cover email draft",
  coverDialogTitle: "Cover email draft",
  coverPending: "No cover email draft yet for this job.",
  coverLoading: "Loading the draft…",
  coverFailed: (e) => `The record was created, but the draft failed: ${e}`,
  coverLoadFailed: (e) => `Could not load the draft: ${e}`,
  coverViolations: "Check before sending:",
  coverClean: "The checker found no problems (numbers, banned phrases, student-run wording, GPA, length, contact block).",
  coverSubject: "Subject", coverBody: "Body", coverPath: "Draft file",
  copySubject: "Copy subject", copyBody: "Copy body", copyPath: "Copy path", copied: "Copied",
  regenerate: "Regenerate", writeDraft: "Write draft", regenerating: "Writing…",
  regenBusy: (s) => `Writing a new draft (${s} s). This can take up to a minute.`,
  regenDone: "New draft written.",
  coverFooter: "Draft only. Nothing is sent: copy it into your email and send it yourself.",
  coverBtn: "Draft cover email",
  // §M10: Apply = open the real application page + add to To apply
  applyOn: { linkedin: "Apply on LinkedIn", jobsdb: "Apply on JobsDB", company_site: "Apply on company site", other: "Apply on job page" },
  destName: { linkedin: "LinkedIn", jobsdb: "JobsDB", company_site: "the company site", other: "the job page" },
  adding: "Adding…",
  appliedToast: (d) => `Opened ${d}. Added to To apply. Press Mark submitted once you have applied.`,
  alreadyToast: (d) => `Opened ${d}. Already in your tracker.`,
  noLinkToast: "Added to To apply. This job has no application link; open the posting from the details.",
  applyPage: "Apply page",
  // §M12: pre-filtered jobs
  notScored: "Not scored", scoreAnyway: "Score anyway", scoring: "Scoring…",
  scoredToast: (c) => `Scored: ${c}`, scorePrefilteredToast: (c) => `${c} is still filtered out.`,
  scoreErrorToast: (c, e) => `Could not score ${c}: ${e}. It will retry on the next run.`,
  openDraft: "Open draft file", revealed: "Shown in Finder",
  // M6: updates (suggestions)
  updatesTitle: (n) => (n === 1 ? "1 update from employers" : `${n} updates from employers`),
  updatesSub: "Suggested from your email and from silence. Nothing changes in the tracker until you confirm.",
  sugType: { rejection: "Rejection", test_invite: "Test invite", interview_invite: "Interview invite", offer: "Offer", ghosted: "No reply" },
  ghostedLine: (d, n) => `No reply since applying on ${d}${n != null ? ` (${n} days)` : ""}.`,
  emailFrom: "From", proposedNext: "Next step", showAll: "Click to show all",
  review: "Review", hideUpdates: "Hide",
  updatesBar: (types) => types.map(([t, n]) => `${n} ${t}`).join(", "),
  pressAgain: "Press a again to apply", pressAgainAnyway: "Press a again: apply anyway",
  lessonsFew: (n) => `Patterns (where it ended, why, weakest circle) show once there are 3 outcomes; ${n === 1 ? "there is 1" : `there are ${n}`} so far.`,
  confirmBtn: "Confirm", dismissBtn: "Dismiss", saveBtn: "Save", cancelBtn: "Cancel",
  confirmedToast: (c, st) => `${c}: now ${st}`, dismissedToast: (c) => `Dismissed the update for ${c}`,
  reasonCategory: "Reason", note: "Note (optional)", notePlaceholder: "What you learned, or the employer's words",
  // M6: manual status
  changeStatus: "Change status",
  statusForm: { my_status: "Status", outcome_stage: "Stage reached", outcome_reason_category: "Reason", outcome_reason: "Reason / note", next_step: "Next step" },
  statusSaved: (c, st) => `${c}: status set to ${st}`,
  keep: "(keep)",
  outcome: "Outcome",
  // vocabulary labels (values are the exact strings from M6.md)
  statuses: ["Applied", "Test", "Interview", "Offer", "Rejected", "Ghosted", "Withdrawn"],
  stages: { cv_screen: "CV screen", online_test: "Online test", assessment: "Assessment", interview: "Interview", final: "Final round", offer: "Offer", unknown: "Stage unknown" },
  categories: {
    gpa_academic: "GPA / academic", start_date: "Start date", experience: "Experience", language_test: "Language test",
    test_score: "Test score", interview_performance: "Interview performance", visa_location: "Visa / location",
    role_filled: "Role filled", no_reason_given: "No reason given", ghosted: "Ghosted (no reply)", other: "Other",
  },
  // M6: lessons
  byStage: "Where it ended", byCategory: "Why", byWeakest: "Weakest circle when applying",
  lessonsAggSub: "Rejected and Ghosted applications.",
  noIkigai: "not scored by the board",
  lessonsCount: (n) => `${n} ${n === 1 ? "lesson" : "lessons"}`,
  wordsCount: (n) => `${n} words`, charsCount: (n) => `${n} characters`,
  factsUsed: (f) => `Facts used: ${f.join(", ")}`,
  ok: "Done",
  // applications
  markSubmitted: "Mark submitted", confirmSubmitted: "Sent it today?", confirm: "Yes, mark submitted", cancel: "Cancel",
  submittedToast: (c) => `Marked submitted: ${c}`,
  appliedOn: "applied", daysWaiting: (n) => (n === 1 ? "day" : "days"), notApplied: "not applied",
  posting: "Posting", record: "Record",
  // panels
  shortTitle: "Where jobs fall short",  // aria label; the visible heading is shortHead
  shortSub: "Scored, not-filtered jobs with this circle below 3.",
  shortNone: "No scored jobs yet.",
  filteredLine: (n) => `Filtered: ${n}`,
  topReasons: "Top reasons:",
  sitesTitle: "Careers pages changed",
  sitesSub: "Company pages that changed since the last check. Check these pages by hand.",
  siteChanged: (c) => `${c} careers page changed`,
  newLinks: (n) => `${n} new ${n === 1 ? "link" : "links"}`,
  markSeen: "Mark seen", openPage: "Open page", detected: "detected",
  pasteTitle: "Add a job",
  pasteSub: "For jobs the scan cannot see (Facebook groups, referrals). Give a URL or paste the JD.",
  pasteUrl: "Job URL", pasteText: "JD text", pasteCompany: "Company (optional)", pasteRole: "Title (optional)", or: "or",
  pasteGo: "Add and score", pasting: "Scoring…",
  pasteBusy: (s) => `Fetching and scoring (${s} s). This can take up to a minute.`,
  pasteNeed: "Give a job URL or paste the JD text.",
  pasteBadUrl: "That URL does not look right (it must start with http:// or https://).",
  pasteScored: "Added and scored.",
  pastePrefiltered: "Added, but the pre-filter filtered it out. See the Filtered tab.",
  pasteError: (e) => `Added, but scoring failed: ${e}. It will retry on the next run.`,
  pasteOpen: "Open it",
  // toasts
  skipped: (c) => `Skipped ${c}`, unskipped: (c) => `Back on the board: ${c}`, undo: "Undo",
  seenToast: (c) => `Marked seen: ${c}`,
  actionFailed: (e) => `That did not work: ${e}`,
};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(n.dataset, v);
      else n.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return n;
}
const $ = (id) => document.getElementById(id);
// Keep these nodes (and their event listeners) when rebuilding the list header.
const addJobButton = $("add-job");
const updatesPanel = $("updates");
const siteChangesPanel = $("site-changes");
const SVG_NS = "http://www.w3.org/2000/svg";
/** One inline icon from the sprite in index.html (no external files). */
function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "i"); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");
const EASE = "cubic-bezier(.2, .8, .2, 1)";
/** Collapse a row or card out of the list before the list re-renders (150-250 ms, ease-out). */
async function collapseOut(node) {
  if (!node || !node.isConnected || REDUCED.matches) return;
  node.classList.add("leaving-row");
  const frames = node.classList.contains("feed-card") || node.classList.contains("card")
    ? [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "scale(.97)" }]
    : [{ height: node.offsetHeight + "px", opacity: 1 }, { height: "0px", opacity: 0, paddingTop: "0px", paddingBottom: "0px" }];
  try { await node.animate(frames, { duration: 200, easing: EASE, fill: "forwards" }).finished; } catch { /* interrupted */ }
}
/** "Singapore, Singapore" -> "Singapore"; "Bangkok, Bangkok City, Thailand" -> "Bangkok, Thailand". */
function place(loc) {
  if (!loc) return "";
  const out = [];
  for (const raw of loc.split(",").map((x) => x.trim()).filter(Boolean)) {
    const short = raw.replace(/\s+(City|Province|Metropolitan( Area)?|District)$/i, "");
    if (!out.some((o) => o.toLowerCase() === short.toLowerCase())) out.push(short);
  }
  return out.join(", ");
}
/** Scraped link text can carry HTML entities ("&amp;"): decode the common ones as plain text (no HTML parsing). */
const ENTITIES = { amp: "&", quot: "\"", apos: "'", "#39": "'", lt: "<", gt: ">", nbsp: " " };
const unescapeEntities = (x) => x.replace(/&(amp|quot|apos|#39|lt|gt|nbsp);/g, (_, k) => ENTITIES[k]);
/** Careers-page links: the link text when it reads as words, else a humanised path segment, never a bare number. */
function linkLabel(l) {
  const t = unescapeEntities((l.text || "").trim());
  const wordy = (x) => /[A-Za-z\u0E00-\u0E7F]{3,}/.test(x);
  if (wordy(t)) return t;
  try {
    const segs = new URL(l.href, "https://example.invalid/").pathname.split("/").filter(Boolean).map((x) => decodeURIComponent(x));
    const seg = segs.reverse().find((x) => wordy(x) && !/^(jobs?|careers?|position|vacanc(y|ies)|search|en|th)$/i.test(x));
    if (seg) return unescapeEntities(seg).replace(/[-_+]+/g, " ").replace(/\s*\(\s*/g, " (").replace(/\s+\)/g, ")").replace(/(\s+\d+|\s+[A-Z]{2})+$/, "").replace(/\s+,/g, ",").trim();
  } catch { /* fall through */ }
  return S.jobPosting;
}
const BKK = "Asia/Bangkok";
function fmtDate(iso, withTime = false) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00+07:00" : iso);
  if (isNaN(d)) return iso;
  const o = { timeZone: BKK, day: "numeric", month: "short" };
  if (withTime) Object.assign(o, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  return new Intl.DateTimeFormat("en-GB", o).format(d);
}
function safeHref(url) {
  if (!url) return null;
  try {
    const u = new URL(url, location.href);
    return ["http:", "https:", "obsidian:"].includes(u.protocol) ? u.href : null;
  } catch { return null; }
}
const thai = (t) => (/[\u0E00-\u0E7F]/.test(t || "") ? "th" : null);
const fmtNum = (n) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(n);
const scoreClass = (v) => (v == null ? "s-na" : v >= 5 ? "s-hi s-5" : v === 4 ? "s-hi" : v === 3 ? "s-mid" : "s-lo");
const verdictKey = (v) => (v ? v : "NONE");

/** "title_exclude: \\bsenior\\b" -> "title contains “senior”" (raw rule kept in title attr). */
function humanRule(rule) {
  if (!rule) return "";
  const m = rule.match(/^([a-z_]+):\s*(.*)$/);
  if (!m) return rule;
  // regex -> words: drop \b and grouping, unescape "\.", drop quantifiers ("sr\.?" -> "sr.")
  const clean = (s) => s.replace(/\\b/g, "").replace(/\\(.)/g, "$1").replace(/[()?*+^$]/g, "").replace(/\|/g, " / ").trim();
  if (m[1] === "title_exclude") return `title contains “${clean(m[2])}”`;
  if (m[1] === "title_include") return `title lacks “${clean(m[2])}”`;
  return `${m[1].replace(/_/g, " ")}: ${m[2]}`;
}

// ---------------------------------------------------------------------------
// state + routing
// ---------------------------------------------------------------------------
const JOB_TABS = ["all", "centre", "hold", "low", "filtered"];
const APP_TABS = ["to_apply", "applied"];
const TAB_ORDER = [...JOB_TABS, "favorites", "daily", "to_apply", "applied", "skipped", "lessons", "wiki"];
const PAGE = 100;
const DEFAULT_REGION = "thailand";

const state = {
  tab: "all", q: "", region: DEFAULT_REGION, stats: null, statsError: null,
  items: [], total: 0, loading: false, error: null, seq: 0,
  openId: null, lastFocus: null, wiki: null,
};

// Region filter: URL choice wins, then the saved choice, then Thailand.
const REGIONS = ["thailand", "all", "abroad"];
function readRegion() {
  const fromUrl = new URLSearchParams(location.search).get("region");
  let saved = null;
  try { saved = localStorage.getItem("jobhunt.region.v2"); } catch { /* storage blocked */ }
  state.region = REGIONS.includes(fromUrl) ? fromUrl : REGIONS.includes(saved) ? saved : DEFAULT_REGION;
}
function writeRegion() {
  try { localStorage.setItem("jobhunt.region.v2", state.region); } catch { /* storage blocked */ }
  const sp = new URLSearchParams(location.search);
  if (state.region === DEFAULT_REGION) sp.delete("region"); else sp.set("region", state.region);
  const qs = sp.toString();
  history.replaceState(null, "", location.pathname + (qs ? "?" + qs : "") + location.hash);
}
const isJobTab = () => [...JOB_TABS, "skipped"].includes(state.tab);
function setRegion(r) {
  if (r === state.region || !REGIONS.includes(r)) return;
  state.region = r;
  writeRegion();
  refreshStats();
  loadList(); // skeleton, then the filtered list
}

/** All · Thailand · Abroad, with counts for the current tab (stats.regions). Job tabs only. */
function regionControl() {
  if (!isJobTab()) return null;
  const counts = state.stats?.regions || {};
  return el("div", { class: "seg", role: "group", "aria-label": S.regionLabel },
    REGIONS.map((r) => el("button", { type: "button", class: "seg-btn", "aria-pressed": String(state.region === r), dataset: { region: r }, onclick: () => setRegion(r) },
      S.regions[r], counts[r] != null ? el("span", { class: "count", text: counts[r] }) : null)));
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const t = p.get("tab");
  state.tab = TAB_ORDER.includes(t) ? t : "all";
  state.q = p.get("q") || "";
}
function writeHash() {
  const p = new URLSearchParams();
  if (state.tab !== "all") p.set("tab", state.tab);
  if (state.q) p.set("q", state.q);
  const h = p.toString();
  history.replaceState(null, "", location.pathname + location.search + (h ? "#" + h : ""));
}

// ---------------------------------------------------------------------------
// header
// ---------------------------------------------------------------------------
function renderRun() {
  const box = $("run");
  box.replaceChildren();
  if (state.statsError) { box.append(el("span", { class: "err", text: state.statsError })); return; }
  const st = state.stats;
  if (!st) return;
  if (!st.last_run) { box.append(el("span", { text: S.noRun })); }
  else {
    const srcs = Object.entries(st.last_run.sources || {});
    const totalNew = srcs.reduce((n, [, c]) => n + (c.new || 0), 0);
    const bad = srcs.filter(([, c]) => c.errors).map(([k]) => S.sources[k] || k);
    box.append(el("span", null, S.lastRun + " ", el("b", { text: fmtDate(st.last_run.at, true) })));
    // One line in the header; the per-source breakdown sits in a hover/focus popover.
    if (srcs.length) box.append(el("span", { class: "srcs", tabindex: "0", "aria-label": srcs.map(([k, c]) => `${S.sources[k] || k}: ${S.srcLine(c)}${c.errors ? ", " + S.errors(c.errors) : ""}`).join(". ") },
      el("b", { class: "num", text: S.totalNew(totalNew) }), icon("chevron"),
      el("span", { class: "tip", role: "tooltip", "aria-hidden": "true" }, el("b", { text: S.bySource }),
        el("ul", null, srcs.map(([k, c]) => el("li", null, el("b", { text: (S.sources[k] || k) + ": " }), S.srcLine(c),
          c.errors ? el("span", { class: "tip-err", text: " · " + S.errors(c.errors) }) : null))))));
    if (bad.length) box.append(el("span", { class: "err", title: S.sourceErrorsHelp, text: S.sourceErrors(bad) }));
  }
  if (st.unscored) box.append(el("span", { text: S.unscored(st.unscored) }));
}

let prevCounts = null;
function renderTabs() {
  const nav = $("tabs");
  const counts = { ...((state.stats && state.stats.tabs) || {}), favorites: favorites.size };
  const icons = { daily: "clock", favorites: "star", all: "home", centre: "star", hold: "clock", low: "chart", filtered: "filter", to_apply: "bookmark", applied: "send", skipped: "eye-off", lessons: "chart", wiki: "book" };
  nav.replaceChildren(...TAB_ORDER.map((t) => {
    // a count that changed since the last render ticks once (e.g. after Skip or Apply)
    const changed = prevCounts && counts[t] != null && prevCounts[t] != null && prevCounts[t] !== counts[t];
    return el("button", {
      type: "button", class: "tab" + (t === "to_apply" || t === "wiki" ? " sep" : ""), "aria-current": t === state.tab ? "page" : null,
      title: S.tabIntro[t], dataset: { tab: t }, onclick: () => switchTab(t),
    }, icon(icons[t]), el("span", { class: "tab-label", text: S.tabs[t] }), (t === "daily" || t === "wiki" || t === "lessons" && counts[t] == null) ? null : el("span", { class: "count" + (changed ? " tick" : ""), "aria-label": `${counts[t] ?? 0} items`, text: counts[t] ?? "–" }));
  }));
  const primary = APP_TABS.includes(state.tab) ? "to_apply" : state.tab === "lessons" ? "lessons" : state.tab === "wiki" ? "wiki" : "all";
  document.querySelectorAll("[data-route]").forEach((button) => {
    if (button.dataset.route === primary) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const pBtn = $("profile-btn");
  if (pBtn) {
    if (state.tab === "wiki") pBtn.setAttribute("aria-current", "page");
    else pBtn.removeAttribute("aria-current");
  }
  const active = nav.querySelector('[aria-current="page"]');
  if (active && window.matchMedia("(max-width: 800px)").matches) {
    const left = active.offsetLeft - nav.offsetLeft;
    if (left < nav.scrollLeft || left + active.offsetWidth > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = left;
  }
  if (state.stats) prevCounts = { ...counts };
}

function setKind() {
  document.body.dataset.kind = [...JOB_TABS, "skipped"].includes(state.tab) ? "jobs" : "records";
  const upd = updatesPanel;
  if (upd.childElementCount) upd.hidden = !["all", "applied"].includes(state.tab);
  const sc = siteChangesPanel;
  if (sc.childElementCount) sc.hidden = !isJobTab();
  renderSetupBanner();
}

function switchTab(t) {
  if (t === state.tab) return;
  state.tab = t;
  setKind();
  writeHash();
  window.scrollTo(0, 0);
  renderTabs();
  loadList();
  refreshStats(); // stats.regions counts the current tab
}

// ---------------------------------------------------------------------------
// side panels
// ---------------------------------------------------------------------------
/** M11: "where jobs fall short" now lives on the Lessons tab, below the outcome charts. */
function shortfallPanel() {
  const st = state.stats;
  if (!st) return null;
  const box = el("section", { class: "panel shortfall-panel", "aria-label": S.shortTitle });
  box.append(el("h3", { class: "group-head first", text: S.shortTitle }));
  const vals = ["love", "good_at", "needs", "paid"].map((c) => [c, st.shortfall?.[c] ?? 0]);
  const max = Math.max(...vals.map((v) => v[1]));
  const worst = vals.find((v) => v[1] === max);
  box.append(el("h2", { title: S.shortSub, text: max ? S.shortHead(S.circles[worst[0]], max) : S.shortHeadNone }));
  if (!max && !(st.tabs?.all)) box.append(el("p", { class: "sub", text: S.shortNone }));
  box.append(el("ul", { class: "bars", "aria-label": S.shortTitle }, vals.map(([c, n]) => el("li", { class: "bar", title: S.circleHelp[c] },
    el("span", { class: "name", text: S.circles[c] }),
    el("span", { class: "track", "aria-hidden": "true" }, el("span", { class: "fill" + (n === max && n > 0 ? " top" : ""), style: null, dataset: { w: max ? n / max : 0 } })),
    el("span", { class: "n", text: n })))));
  box.querySelectorAll(".fill").forEach((f) => { f.style.width = `${Math.round(f.dataset.w * 100)}%`; });
  const reasons = st.filtered_top_reasons || [];
  box.append(el("div", { class: "filtered-line" },
    el("b", { text: S.filteredLine(st.tabs?.filtered ?? 0) }),
    reasons.length ? [" · " + S.topReasons, el("ol", null, reasons.map((r) => el("li", { title: r.reason }, reasonLabel(r.reason), ` (${r.count})`)))] : null));
  return box;
}

const absUrl = (h, base) => { try { return new URL(h || "", base || location.href).href; } catch { return null; } };
const reasonLabel = (r) => (r.includes("_") && /^[a-z_]+:/.test(r) ? S.prefilter + ": " + humanRule(r) : r);

/** M11: careers-page changes are a one-line notice above the job list (only when unseen); Review opens a dialog. */
let siteItems = [];
async function renderSiteChanges() {
  const box = siteChangesPanel;
  if (!state.stats || !state.stats.site_changes_unseen) { siteItems = []; box.hidden = true; box.replaceChildren(); return; }
  try { siteItems = (await api.siteChanges()).items || []; } catch (e) { siteItems = []; }
  if (!siteItems.length) { box.hidden = true; box.replaceChildren(); if ($("sites-dialog").open) $("sites-dialog").close(); return; }
  const names = [...new Set(siteItems.map((it) => it.company))];
  const review = el("button", { type: "button", class: "btn small", "aria-haspopup": "dialog", text: S.review, onclick: openSitesDialog });
  box.replaceChildren(el("p", { title: S.sitesSub }, sitesHeadline(names)), review);
  box.hidden = !isJobTab();
  if ($("sites-dialog").open) fillSitesDialog();
}
function sitesHeadline(names) {
  // company names never break inside: "2 careers pages changed: True, DKSH"
  const headText = S.sitesHead(names);
  const lead = headText.slice(0, headText.length - names.join(", ").length);
  return [lead, ...names.flatMap((n, i) => [i ? ", " : "", el("b", { class: "nowrap", text: n })])];
}
function openSitesDialog() {
  const dlg = $("sites-dialog");
  fillSitesDialog();
  if (!dlg.open) dlg.showModal();
  dlg.querySelector(".change .btn")?.focus();
}
function fillSitesDialog() {
  const dlg = $("sites-dialog");
  const names = [...new Set(siteItems.map((it) => it.company))];
  dlg.replaceChildren(el("div", { class: "m-body" },
    el("div", { class: "m-head" }, el("h2", { id: "sites-title" }, sitesHeadline(names)), modalClose(dlg)),
    el("p", { class: "sub", text: S.sitesSub }),
    ...siteItems.map((it) => {
      const btn = el("button", { type: "button", class: "btn small", text: S.markSeen });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try { await api.siteSeen(it.id); toast(S.seenToast(it.company)); await refreshStats(); }
        catch (e) { btn.disabled = false; toast(S.actionFailed(e.message), { error: true }); }
      });
      const href = safeHref(it.url);
      return el("div", { class: "change" },
        el("p", null, el("b", { text: it.company }), el("span", { class: "sub", text: ` · ${S.detected} ${fmtDate(it.detected_at)}` })),
        it.new_links?.length ? el("ul", null, it.new_links.slice(0, 8).map((l) => {
          const h = safeHref(absUrl(l.href, it.url));
          return el("li", null, h ? el("a", { href: h, target: "_blank", rel: "noopener noreferrer", title: h, text: linkLabel(l) }) : linkLabel(l));
        })) : null,
        el("div", { class: "row-actions" }, btn, href ? el("a", { class: "btn small link", href, target: "_blank", rel: "noopener noreferrer" }, S.openPage, icon("external")) : null));
    })));
}
function modalClose(dlg) {
  return el("button", { type: "button", class: "btn quiet icon close", "aria-label": S.close, title: S.close, onclick: () => dlg.close() }, icon("close"));
}

/** M11: "+ Add job" in the header opens this dialog (same paste behaviour and progress as before). */
function renderPaste() {
  const box = $("add-dialog");
  const url = el("input", { id: "p-url", type: "url", inputmode: "url", placeholder: "https://th.jobsdb.com/job/…", autocomplete: "off" });
  const text = el("textarea", { id: "p-text", rows: "4", placeholder: "Paste the job description here" });
  const company = el("input", { id: "p-company", type: "text", autocomplete: "off" });
  const title = el("input", { id: "p-title", type: "text", autocomplete: "off" });
  const go = el("button", { type: "submit", class: "btn primary", text: S.pasteGo });
  const msg = el("p", { class: "form-msg", role: "status", "aria-live": "polite" });
  const form = el("form", { class: "form", novalidate: true },
    el("div", { class: "field" }, el("label", { for: "p-url", text: S.pasteUrl }), url),
    el("div", { class: "or", "aria-hidden": "true", text: S.or }),
    el("div", { class: "field" }, el("label", { for: "p-text", text: S.pasteText }), text),
    el("div", { class: "two" },
      el("div", { class: "field" }, el("label", { for: "p-company", text: S.pasteCompany }), company),
      el("div", { class: "field" }, el("label", { for: "p-title", text: S.pasteRole }), title)),
    el("div", null, go), msg);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {};
    const u = url.value.trim(), t = text.value.trim();
    if (!u && !t) { setMsg("error", S.pasteNeed); (u ? url : text).focus(); return; }
    if (u && !/^https?:\/\/\S+$/i.test(u)) { setMsg("error", S.pasteBadUrl); url.focus(); return; }
    if (u) body.url = u;
    if (t) body.jd_text = t;
    if (company.value.trim()) body.company = company.value.trim();
    if (title.value.trim()) body.title = title.value.trim();
    const started = Date.now();
    go.disabled = true;
    go.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), S.pasting);
    form.setAttribute("aria-busy", "true");
    setMsg("busy", S.pasteBusy(0));
    const timer = setInterval(() => setMsg("busy", S.pasteBusy(Math.round((Date.now() - started) / 1000))), 1000);
    try {
      const res = await api.paste(body);
      clearInterval(timer);
      form.reset();
      const open = el("button", { type: "button", class: "btn small link", text: S.pasteOpen, onclick: () => { box.close(); openDrawer(res.job_id); } });
      if (res.state === "prefiltered") setMsg("ok", S.pastePrefiltered, open);
      else if (res.state === "error") setMsg("error", S.pasteError(res.error || "unknown error"), open);
      else setMsg("ok", S.pasteScored, open);
      await refreshAll();
      if (res.state === "scored") { box.close(); openDrawer(res.job_id); }
    } catch (e) {
      clearInterval(timer);
      setMsg("error", e.message);
    } finally {
      go.disabled = false;
      go.replaceChildren(S.pasteGo);
      form.removeAttribute("aria-busy");
    }
  });
  function setMsg(kind, t, extra) { msg.className = "form-msg " + kind; msg.replaceChildren(t, extra ? " " : "", extra || ""); }
  box.replaceChildren(el("div", { class: "m-body" },
    el("div", { class: "m-head" }, el("h2", { id: "add-title", text: S.pasteTitle }), modalClose(box)),
    el("p", { class: "sub", text: S.pasteSub }), form));
}
function openAddJob() {
  const dlg = $("add-dialog");
  if (!dlg.open) dlg.showModal();
  $("p-url").focus();
}

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------
function chip(circle, v, est, big = false, weakest = false, labelled = false) {
  const label = `${S.circles[circle]} ${v == null ? "unknown" : v + " out of 5"}${est ? " (estimated)" : ""}${weakest ? ", " + S.weakestAria : ""}`;
  return el("span", { class: "chip " + scoreClass(v) + (weakest ? " weakest" : ""), role: "img", "aria-label": label, title: `${S.circles[circle]}: ${S.circleHelp[circle]}${weakest ? " (" + S.weakestAria + ")" : ""}` },
    el("span", { class: "v", text: v == null ? S.unknown : labelled ? `${v}/5` : v }),
    labelled ? el("span", { class: "l", text: S.circleShort[circle] + (est ? " " + S.est : "") })
      : !big && est ? el("span", { class: "l est", text: S.est }) : null);
}

const askQs = (gate) => (gate?.result === "ask" ? gate.questions || [] : []);
/** Gate questions: a small icon + count next to the company; the full text is in the drawer (and the hover title). */
function askMark(gate) {
  const qs = askQs(gate);
  if (!qs.length) return null;
  return el("span", { class: "ask", tabindex: "0", role: "note", "aria-label": `${S.askLabel(qs.length)}: ${qs.join(" ")}` }, icon("question"),
    el("span", { class: "tip", role: "tooltip", "aria-hidden": "true" }, el("b", { text: S.askTipTitle }), el("ul", null, qs.map((q) => el("li", { text: q })))));
}

function trackedBadge(tr) {
  if (!tr) return null;
  const h = safeHref(tr.obsidian_url);
  return el(h ? "a" : "span", { class: "tracked", href: h, title: tr.file }, icon("check"), S.trackedBadge(tr.my_status));
}
const CIRCLES = ["love", "good_at", "needs", "paid"];

// §M9 logos. Only same-origin images are ever loaded (the server's /logos/, or mock fixtures), so the browser
// makes no request off 127.0.0.1; anything else, a null logo_url or an image that fails to decode shows a
// neutral one-initial square. The image is decorative (alt="") because the company name is always beside it.
function sameOriginImg(url) {
  if (!url) return null;
  try { const u = new URL(url, location.href); return u.origin === location.origin ? u.href : null; } catch { return null; }
}
const COMPANY_NOISE = /\b(co\.?|company|ltd\.?|limited|inc\.?|plc|public|corp(oration)?\.?|the)\b|\(.*?\)|[.,&'’"]/gi;
/** Fallback mark: ONE initial. Thai names skip "บริษัท" and leading vowels (first consonant). */
function initial(company) {
  const name = (company || "").trim().replace(/^บริษัท\s*/, "");
  if (/^[\u0E00-\u0E7F]/.test(name)) return (name.match(/[\u0E01-\u0E2E]/) || ["?"])[0];
  const w = name.replace(COMPANY_NOISE, " ").trim().match(/[A-Za-z0-9\u00C0-\u024F]/);
  return (w ? w[0] : name[0] || "?").toUpperCase();
}
/** Crop a same-origin logo to its content box (background = corner pixel colour or transparency).
 *  Returns true when it replaced img.src with the cropped copy. Never throws. */
function trimLogo(img) {
  img.dataset.trimmed = "1";
  try {
    const W = img.naturalWidth, H = img.naturalHeight;
    if (!W || !H) return false;
    const k = Math.min(1, 200 / Math.max(W, H));
    const w = Math.max(1, Math.round(W * k)), h = Math.max(1, Math.round(H * k));
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const g = c.getContext("2d", { willReadFrequently: true });
    g.drawImage(img, 0, 0, w, h);
    const d = g.getImageData(0, 0, w, h).data;
    const bg = [d[0], d[1], d[2], d[3]];
    const isBg = (i) => d[i + 3] < 24 || (bg[3] > 200 && Math.abs(d[i] - bg[0]) + Math.abs(d[i + 1] - bg[1]) + Math.abs(d[i + 2] - bg[2]) < 36);
    let x0 = w, y0 = h, x1 = -1, y1 = -1;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      if (!isBg((y * w + x) * 4)) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    }
    if (x1 < 0) return false;
    let cw = x1 - x0 + 1, ch = y1 - y0 + 1;
    // a very wide mark that starts with its own emblem (emblem, gap, wordmark) shows just the emblem,
    // which stays readable at 32 px; guarded so plain wordmarks are left whole
    if (cw / ch >= 2.2) {
      const colHas = (x) => { for (let y = y0; y <= y1; y++) if (!isBg((y * w + x) * 4)) return true; return false; };
      let x = x0; while (x <= x1 && colHas(x)) x++;
      const runEnd = x - 1;
      let gap = 0; while (x <= x1 && !colHas(x)) { gap++; x++; }
      let ry0 = h, ry1 = -1;
      for (let y = y0; y <= y1; y++) for (let xx = x0; xx <= runEnd; xx++) if (!isBg((y * w + xx) * 4)) { if (y < ry0) ry0 = y; if (y > ry1) ry1 = y; break; }
      const rw = runEnd - x0 + 1, rh = ry1 - ry0 + 1;
      if (gap >= Math.max(2, 0.03 * cw) && rh >= 0.75 * ch && rw / rh > 0.6 && rw / rh < 1.6) { x1 = runEnd; y0 = ry0; y1 = ry1; cw = rw; ch = rh; }
    }
    if (cw * ch > 0.8 * w * h) return false; // little margin: keep the original
    const m = Math.round(Math.max(cw, ch) * 0.06); // keep a hair of breathing room
    const sx = Math.max(0, (x0 - m) / k), sy = Math.max(0, (y0 - m) / k);
    const sw = Math.min(W - sx, (cw + 2 * m) / k), sh = Math.min(H - sy, (ch + 2 * m) / k);
    const out = document.createElement("canvas");
    const s2 = Math.min(1, 160 / Math.max(sw, sh));
    out.width = Math.max(1, Math.round(sw * s2)); out.height = Math.max(1, Math.round(sh * s2));
    out.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, out.width, out.height);
    img.src = out.toDataURL("image/png");
    return true;
  } catch { return false; }
}

function logoEl(company, url, size = "md") {
  const box = el("span", { class: `logo logo-${size}`, "aria-hidden": "true" });
  // real logo whenever logo_url is set; the fallback is a quiet neutral square with one initial (no generated colours)
  const mono = () => { box.className = `logo logo-${size} mono`; box.replaceChildren(initial(company)); };
  const src = sameOriginImg(url);
  if (!src) { mono(); return box; }
  const img = el("img", { src, alt: "", loading: "lazy", decoding: "async", width: "40", height: "40" });
  img.addEventListener("error", mono, { once: true });
  img.addEventListener("load", function onLoad() {
    // 1) trim the uniform margin many logo files carry (e.g. a 360×160 JobsDB banner with the mark in the middle);
    //    the cropped copy is a data: image, so the browser still makes no new request
    if (!img.dataset.trimmed && trimLogo(img)) return; // src changed: the next load event finishes the job
    // 2) banner-style marks get almost no padding so they use the full width of the square
    if (img.naturalHeight && img.naturalWidth / img.naturalHeight >= 1.6) box.classList.add("wide");
    img.removeEventListener("load", onLoad);
  });
  box.append(img);
  return box;
}
/** The weakest circle is marked only when it is a unique minimum (no marker on a tie, e.g. 4/4/4/4). */
function weakestOf(j) {
  if (!j.weakest || j.verdict === "FILTERED") return null;
  const known = CIRCLES.map((c) => j.scores?.[c]).filter((v) => v != null);
  const min = Math.min(...known);
  return known.filter((v) => v === min).length === 1 ? j.weakest.circle : null;
}
const chipsFor = (j, labelled = false) => {
  const w = weakestOf(j);
  return CIRCLES.map(c => chip(c, j.scores?.[c], c === "paid" && j.paid_estimated && j.scores?.paid != null, false, w === c, labelled));
};
const salaryVisibilityKey = IS_MOCK ? "jobhunt.salaryVisible.mock" : "jobhunt.salaryVisible";
let salaryVisible = true;
try { salaryVisible = localStorage.getItem(salaryVisibilityKey) !== "false"; } catch { /* use default */ }
const salaryCache = new Map();
async function loadFeedSalary(box) {
  const id = Number(box.dataset.salaryId);
  box.setAttribute("aria-busy", "true");
  box.textContent = "Loading salary…";
  try {
    if (!salaryCache.has(id)) salaryCache.set(id, api.job(id));
    const job = await salaryCache.get(id);
    if (!box.isConnected) return;
    const sal = job.salary;
    let amount = "Salary not available";
    if (sal?.currency && (sal.min != null || sal.max != null)) {
      const range = sal.min != null && sal.max != null ? `${fmtNum(sal.min)}–${fmtNum(sal.max)}` : fmtNum(sal.min ?? sal.max);
      amount = `${sal.currency} ${range} / ${sal.period || "month"}`;
    } else if (sal?.thb_month_mid != null) amount = `About THB ${fmtNum(sal.thb_month_mid)} / month`;
    box.replaceChildren(el("div", null,
      el("span", { class: "salary-label", text: sal?.stated ? "Salary in posting" : "Estimated salary" }),
      el("strong", { class: "salary-amount", text: amount })),
      el("button", { type: "button", class: "btn quiet small", text: "Details", onclick: e => { e.stopPropagation(); openDrawer(id, "paid"); } }));
  } catch {
    salaryCache.delete(id);
    if (box.isConnected) box.replaceChildren("Could not load salary", el("button", { type: "button", class: "btn quiet small", text: S.retry, onclick: e => { e.stopPropagation(); loadFeedSalary(box); } }));
  } finally { box.removeAttribute("aria-busy"); }
}
const salaryObserver = new IntersectionObserver(entries => {
  for (const entry of entries) if (entry.isIntersecting) {
    salaryObserver.unobserve(entry.target); loadFeedSalary(entry.target);
  }
}, { rootMargin: "400px" });
function feedSalary(j) {
  if (!salaryVisible) return null;
  const box = el("div", { class: "feed-salary", dataset: { salaryId: j.id }, "aria-busy": "true", text: "Loading salary…" });
  salaryObserver.observe(box);
  return box;
}
function salaryToggle() {
  return el("button", { type: "button", class: "btn salary-toggle", "aria-pressed": String(salaryVisible),
    text: salaryVisible ? "Hide salary estimates" : "Show salary estimates", onclick: () => {
      salaryVisible = !salaryVisible;
      try { localStorage.setItem(salaryVisibilityKey, String(salaryVisible)); } catch { /* session choice still works */ }
      renderList();
      document.querySelector(".salary-toggle")?.focus();
    } });
}
const metaFor = (j, short = false) => [
  j.location ? el("span", { text: place(j.location) }) : null,
  short ? null : el("span", { text: S.sources[j.source] || j.source }),
  el("span", { class: "num", text: j.posted_at ? `${S.posted} ${fmtDate(j.posted_at)}` : `${S.seen} ${fmtDate(j.first_seen_at)}` }),
];
const summaryCache = new Map();
/** §M8: JobRow.summary (str | null) is used as is; null means "no summary", so nothing is fetched.
 *  Only a server without the field (key absent) falls back to the detail fetch. */
const rowSummary = (j) => ("summary" in j ? j.summary || "" : summaryCache.get(j.id));
// The one-line "why" (summary) per row. JobRow has no summary yet, so rows fetch it from the job detail
// (GET) when they scroll near the viewport, 4 at a time, cached for the session.
const whyWaiting = [];
let whyBusy = 0;
function pumpWhy() {
  while (whyBusy < 4 && whyWaiting.length) {
    const p = whyWaiting.shift();
    if (!p.isConnected) continue;
    const id = p.dataset.why;
    whyBusy++;
    api.job(id).then((d) => { summaryCache.set(+id, d.summary || ""); setWhy(p, d.summary); })
      .catch(() => setWhy(p, ""))
      .finally(() => { whyBusy--; pumpWhy(); });
  }
}
function setWhy(p, t) { p.classList.remove("pending"); p.removeAttribute("aria-busy"); p.textContent = t || ""; p.title = t || ""; if (thai(t)) p.lang = "th"; }
const whyObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { whyObserver.unobserve(e.target); whyWaiting.push(e.target); }
  pumpWhy();
}, { rootMargin: "400px 0px" }) : null;
function whyLine(j) {
  const known = rowSummary(j);
  if (known !== undefined) return known ? el("p", { class: "why", lang: thai(known), title: known, text: known }) : null;
  if (j.verdict == null || j.prefilter_reason || !whyObserver) return null; // not scored: no summary to show
  const p = el("p", { class: "why pending", "aria-busy": "true", dataset: { why: j.id } });
  whyObserver.observe(p);
  return p;
}
/** §M10: where Apply goes. Only http(s); a job without apply_url (older server) falls back to its posting URL. */
function applyTarget(j) {
  const ok = (u) => { const h = safeHref(u); return h && /^https?:/.test(h) ? h : null; };
  const url = ok(j.apply_url) || ok(j.url);
  const via = !url ? null : ok(j.apply_url) ? (S.applyOn[j.apply_via] ? j.apply_via : "other") : "other";
  return { url, via };
}
const applyText = (j) => (j.verdict === "FILTERED" ? S.applyAnyway : (S.applyOn[applyTarget(j).via] || S.apply));
/** The Apply label names the destination and carries the external-link icon. */
function setApplyLabel(btn, text) { btn.replaceChildren(el("span", { class: "lbl", text }), icon("external")); }

const preFiltered = (j) => !!j.prefilter_reason && CIRCLES.every((c) => j.scores?.[c] == null);

/** §M12: "Score anyway" scores one pre-filtered job now (POST /api/jobs/<id>/score); like paste, up to a minute. */
async function doScore(j, btn) {
  const started = Date.now();
  const secs = el("span", { class: "secs" });
  btn.disabled = true;
  btn.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), S.scoring, secs);
  const timer = setInterval(() => { secs.textContent = ` ${Math.round((Date.now() - started) / 1000)} s`; }, 1000);
  try {
    const res = await api.score(j.id);
    clearInterval(timer);
    if (res.state === "scored") toast(S.scoredToast(j.company));
    else if (res.state === "prefiltered") toast(S.scorePrefilteredToast(j.company));
    else toast(S.scoreErrorToast(j.company, res.error || "unknown error"), { error: true });
    await refreshAll();
    if (res.state === "scored") openDrawer(j.id);
  } catch (e) {
    toast(S.actionFailed(e.message), { error: true });
  } finally {
    clearInterval(timer);
    if (btn.isConnected) { btn.disabled = false; btn.replaceChildren(S.scoreAnyway); }
  }
}

function rowButtons(j) {
  const skipped = j.status === "skipped";
  if (preFiltered(j)) {
    const scoreBtn = el("button", { type: "button", class: "btn primary small score", text: S.scoreAnyway });
    scoreBtn.addEventListener("click", (e) => { e.stopPropagation(); doScore(j, scoreBtn); });
    const skipBtn0 = el("button", { type: "button", class: "btn small quiet", text: skipped ? S.unskip : S.skip });
    skipBtn0.addEventListener("click", (e) => { e.stopPropagation(); doSkip(j, !skipped, skipBtn0); });
    return el("div", { class: "actions" }, scoreBtn, skipBtn0);
  }
  const applyBtn = el("button", { type: "button", class: "btn primary small apply" });
  setApplyLabel(applyBtn, applyText(j));
  applyBtn.addEventListener("click", (e) => { e.stopPropagation(); doApply(j, applyBtn); });
  const skipBtn = el("button", { type: "button", class: "btn small quiet", text: skipped ? S.unskip : S.skip });
  skipBtn.addEventListener("click", (e) => { e.stopPropagation(); doSkip(j, !skipped, skipBtn); });
  return el("div", { class: "actions" }, applyBtn, skipBtn);
}
function rowClick(li, id) {
  li.addEventListener("click", (e) => {
    if (e.target.closest("button, a, .ask")) return;
    if (window.getSelection()?.toString()) return;
    openDrawer(id);
  });
  return li;
}

const favoriteKey = IS_MOCK ? "jobhunt.favorites.mock" : "jobhunt.favorites";
const favorites = new Set();
try {
  const saved = JSON.parse(localStorage.getItem(favoriteKey) || "[]");
  if (Array.isArray(saved)) saved.filter(id => Number.isInteger(id) && id > 0).forEach(id => favorites.add(id));
} catch { /* unavailable or invalid storage: start empty */ }
function favoriteButton(j) {
  const btn = el("button", { type: "button", class: "favorite-btn", "aria-pressed": String(favorites.has(j.id)) }, icon("star"));
  const update = () => {
    const label = `${favorites.has(j.id) ? "Remove from" : "Add to"} favorites: ${j.title}`;
    btn.setAttribute("aria-label", label); btn.title = label;
    btn.setAttribute("aria-pressed", String(favorites.has(j.id)));
  };
  update();
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const next = new Set(favorites);
    if (next.has(j.id)) next.delete(j.id); else next.add(j.id);
    try { localStorage.setItem(favoriteKey, JSON.stringify([...next])); }
    catch { toast("Could not save favorites. Browser storage is unavailable.", { error: true }); return; }
    favorites.clear(); next.forEach(id => favorites.add(id));
    update(); renderTabs();
    toast(favorites.has(j.id) ? "Added to favorites" : "Removed from favorites");
    if (state.tab === "favorites") loadList();
  });
  return btn;
}

function jobRow(j) {
  const s = j.scores || {};
  const off = j.verdict === "FILTERED" || j.verdict == null;
  const openBtn = el("button", { type: "button", class: "open", lang: thai(j.title), "aria-haspopup": "dialog", text: j.title, title: j.title, onclick: (e) => { e.stopPropagation(); openDrawer(j.id); } });
  let note = null;
  if (j.error) note = el("div", { class: "note err" }, S.scoreError + " " + j.error);
  else if (j.verdict == null) note = el("div", { class: "note wait", text: S.waitingScore });
  else if (j.prefilter_reason) note = el("div", { class: "note pre", title: j.prefilter_reason }, S.prefilter + ": " + humanRule(j.prefilter_reason));
  else if (j.gate?.result === "fail" && j.gate.fails?.length) note = el("div", { class: "note fail" }, S.failed + ": " + j.gate.fails[0] + (j.gate.fails.length > 1 ? ` (+${j.gate.fails.length - 1})` : ""));

  else if (j.verdict && CIRCLES.every((c) => s[c] == null)) note = el("div", { class: "note", text: S.allUnknown });
  const summary = whyLine(j);
  const questions = askQs(j.gate);
  const li = el("li", { class: "row feed-card" + (off ? " dim" : "") + (state.openId === j.id ? " is-open" : ""), dataset: { id: j.id, verdict: verdictKey(j.verdict), country: j.country || "" } },
    el("div", { class: "feed-card-head" },
      logoEl(j.company, j.logo_url, "lg"),
      el("div", { class: "company-info" },
        el("span", { class: "company", lang: thai(j.company), title: j.company, text: j.company }),
        el("div", { class: "meta" }, el("span", { text: S.sources[j.source] || j.source }),
          el("span", { text: j.posted_at ? `${S.posted} ${fmtDate(j.posted_at)}` : `${S.seen} ${fmtDate(j.first_seen_at)}` }))), favoriteButton(j)),
    el("div", { class: "job" }, el("h3", { class: "job-title" }, openBtn),
      j.location ? el("div", { class: "job-location" }, icon("pin"), place(j.location)) : null,
      trackedBadge(j.tracked)),
    summary, note,
    preFiltered(j) ? el("div", { class: "not-scored", text: S.notScored }) : el("div", { class: "fit-line" },
      el("div", { class: "circles" }, chipsFor(j, true))),
    feedSalary(j),
    questions.length ? el("div", { class: "question-line" }, askMark(j.gate),
      el("button", { type: "button", class: "question-link", "aria-haspopup": "dialog", onclick: () => openDrawer(j.id), text: questions.length === 1 ? "One thing to check with the employer" : `${questions.length} things to check with the employer` })) : null,
    el("div", { class: "feed-card-footer" }, rowButtons(j)));
  return rowClick(li, j.id);
}

/** Draft/record paths inside free text render as code (mono, wraps anywhere); the rest stays text. */
function withPaths(t) {
  return String(t).split(/((?:job-hunt|my-job-hunt|Career-Wiki)\/[^\s]+?\.md)/).map((part, i) => (i % 2 ? el("code", { text: part }) : part)).filter((x) => x !== "");
}

function appRow(a) {
  const todo = state.tab === "to_apply";
  const links = [];
  const rec = safeHref(a.obsidian_url), post = safeHref(a.link);
  if (rec) links.push(el("a", { class: "btn small link", href: rec, title: a.record_file, text: S.record }));
  const applyHref = todo ? (() => { const h = safeHref(a.apply_link); return h && /^https?:/.test(h) ? h : null; })() : null;
  if (post && post !== applyHref) links.push(el("a", { class: "btn small link", href: post, target: "_blank", rel: "noopener noreferrer" }, S.posting, icon("external")));
  const titleNode = a.jobhunt_id != null
    ? el("button", { type: "button", class: "open", "aria-haspopup": "dialog", text: a.program, onclick: () => openDrawer(a.jobhunt_id) })
    : el("div", { class: "open", style: null, text: a.program });
  const main = el("div", { class: "job" },
    el("div", { class: "job-line1" }, el("span", { class: "company", text: a.company }),
      a.section && a.section !== "Apply now" && a.section !== "Archive" ? el("span", { class: "badge", text: a.section }) : null),
    titleNode,
    a.next_step ? el("div", { class: "next" }, withPaths(a.next_step)) : null);
  const ident = el("div", { class: "job-id" }, logoEl(a.company, a.logo_url, "md"), main);
  if (todo) {
    const actions = el("div", { class: "actions" });
    const mark = el("button", { type: "button", class: "btn primary small", text: S.markSubmitted });
    // §M10: "Apply page" takes him back to an unfinished application; the cover email is secondary
    const applyPage = applyHref ? el("a", { class: "btn small apply-page", href: applyHref, target: "_blank", rel: "noopener noreferrer" }, S.applyPage, icon("external")) : null;
    const coverBtn = a.jobhunt_id != null
      ? el("button", { type: "button", class: "btn small quiet", text: S.coverBtn, onclick: () => openCoverDialog(a.jobhunt_id, a.company, a.program, rec) }) : null;
    const reset = () => actions.replaceChildren(mark, ...[applyPage, coverBtn].filter(Boolean), ...links);
    mark.addEventListener("click", () => {
      const yes = el("button", { type: "button", class: "btn primary small", text: S.confirm });
      const no = el("button", { type: "button", class: "btn small", text: S.cancel, onclick: () => { reset(); mark.focus(); } });
      yes.addEventListener("click", async () => {
        yes.disabled = no.disabled = true;
        yes.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), S.confirm);
        try { await api.submitted(a.record_file); toast(S.submittedToast(a.company)); await refreshAll(); }
        catch (e) { toast(S.actionFailed(e.message), { error: true }); reset(); }
      });
      actions.replaceChildren(el("span", { class: "confirm-q", text: S.confirmSubmitted }), yes, no);
      yes.focus();
    });
    reset();
    return el("li", { class: "row app-row app-grid todo" }, ident, actions);
  }
  const outcome = [a.outcome_stage ? S.stages[a.outcome_stage] || a.outcome_stage : null,
    a.outcome_reason_category ? S.categories[a.outcome_reason_category] || a.outcome_reason_category : null,
    a.outcome_date ? fmtDate(a.outcome_date) : null].filter(Boolean);
  if (outcome.length || a.outcome_reason) {
    main.append(el("div", { class: "outcome" }, el("b", { text: S.outcome + ": " }), outcome.join(" · "),
      a.outcome_reason ? el("span", { class: "outcome-reason", text: (outcome.length ? " · " : "") + "“" + a.outcome_reason + "”" }) : null));
  }
  const closed = ["Rejected", "Ghosted", "Withdrawn"].includes(a.my_status);
  const change = el("button", { type: "button", class: "btn small", text: S.changeStatus, "aria-expanded": "false" });
  const li = el("li", { class: "row app-row app-grid" + (closed ? " quiet" : "") }, ident,
    el("div", null, el("span", { class: `pill status-pill st-${(a.my_status || "").replace(/\W/g, "")}`, text: a.my_status || "–" })),
    el("div", { class: "days" }, a.days_waiting != null
      ? [el("b", { class: "num", text: a.days_waiting }), " " + S.daysWaiting(a.days_waiting), el("div", { class: "sub num", text: `${S.appliedOn} ${fmtDate(a.date_applied)}` })]
      : el("span", { class: "sub", text: S.notApplied })),
    el("div", { class: "links" }, change, links));
  change.addEventListener("click", () => {
    const open = li.querySelector(".status-form");
    if (open) { open.remove(); change.setAttribute("aria-expanded", "false"); return; }
    change.setAttribute("aria-expanded", "true");
    li.append(statusForm(a, () => { li.querySelector(".status-form")?.remove(); change.setAttribute("aria-expanded", "false"); change.focus(); }));
    li.querySelector(".status-form select")?.focus();
  });
  return li;
}

function selectEl(id, options, value, { blank = null } = {}) {
  const sel = el("select", { id });
  if (blank !== null) sel.append(el("option", { value: "", text: blank }));
  for (const [v, label] of options) sel.append(el("option", { value: v, text: label, selected: v === value ? true : null }));
  return sel;
}

let formSeq = 0;
/** Manual status change (POST /api/applications/status), shown inline under an Applied row. */
function statusForm(a, close) {
  const n = ++formSeq;
  const st = selectEl(`sf-st-${n}`, S.statuses.map((x) => [x, x]), a.my_status);
  const stage = selectEl(`sf-stage-${n}`, Object.entries(S.stages), a.outcome_stage || "", { blank: S.keep });
  const cat = selectEl(`sf-cat-${n}`, Object.entries(S.categories), a.outcome_reason_category || "", { blank: S.keep });
  const reason = el("input", { id: `sf-reason-${n}`, type: "text", maxlength: "300", value: a.outcome_reason || "", placeholder: S.notePlaceholder });
  const next = el("input", { id: `sf-next-${n}`, type: "text", value: a.next_step || "" });
  const save = el("button", { type: "submit", class: "btn primary small", text: S.saveBtn });
  const cancel = el("button", { type: "button", class: "btn small", text: S.cancelBtn, onclick: close });
  const msg = el("p", { class: "form-msg", role: "status", "aria-live": "polite" });
  const f = (id, label, input) => el("div", { class: "field" }, el("label", { for: id, text: label }), input);
  const form = el("form", { class: "status-form", novalidate: true, "aria-label": `${S.changeStatus}: ${a.company}` },
    el("div", { class: "sf-grid" },
      f(st.id, S.statusForm.my_status, st), f(stage.id, S.statusForm.outcome_stage, stage), f(cat.id, S.statusForm.outcome_reason_category, cat),
      f(reason.id, S.statusForm.outcome_reason, reason), f(next.id, S.statusForm.next_step, next)),
    el("div", { class: "sf-actions" }, save, cancel, msg));
  form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = { record_file: a.record_file, my_status: st.value };
    if (stage.value) body.outcome_stage = stage.value;
    if (cat.value) body.outcome_reason_category = cat.value;
    if (reason.value.trim() && reason.value.trim() !== (a.outcome_reason || "")) body.outcome_reason = reason.value.trim();
    if (next.value.trim() && next.value.trim() !== (a.next_step || "")) body.next_step = next.value.trim();
    save.disabled = cancel.disabled = true;
    save.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), S.saveBtn);
    try {
      await api.appStatus(body);
      toast(S.statusSaved(a.company, st.value));
      await refreshAll();
    } catch (e) {
      save.disabled = cancel.disabled = false;
      save.replaceChildren(S.saveBtn);
      msg.className = "form-msg error"; msg.textContent = e.message;
    }
  });
  return form;
}

// ---------------------------------------------------------------------------
// M6: updates panel (suggestions)
// ---------------------------------------------------------------------------
async function renderUpdates() {
  const box = updatesPanel;
  if (!state.stats || !state.stats.suggestions_pending) { box.hidden = true; box.replaceChildren(); return; }
  let items = [], apps = [];
  try {
    const [sres, ares] = await Promise.all([api.suggestions(), api.applications("applied").catch(() => ({ applications: [] }))]);
    items = sres.items || [];
    apps = ares.applications || [];
  } catch (e) {
    box.hidden = false;
    box.replaceChildren(el("h2", { id: "updates-title", text: S.updatesTitle(state.stats.suggestions_pending) }), el("p", { class: "form-msg error", text: S.loadError + ": " + e.message }));
    return;
  }
  if (!items.length) { box.hidden = true; box.replaceChildren(); return; }
  const byFile = new Map(apps.map((a) => [a.record_file, a]));
  // one line by default ("4 updates from employers: 1 rejection, 1 test invite…"); Review opens the cards
  const types = Object.entries(items.reduce((m, sg) => { const t = (S.sugType[sg.type] || sg.type).toLowerCase(); m[t] = (m[t] || 0) + 1; return m; }, {}));
  const cards = el("ul", { class: "cards", id: "update-cards", hidden: !state.updatesOpen }, items.map((sg) => suggestionCard(sg, byFile.get(sg.record_file))));
  const toggle = el("button", { type: "button", class: "btn small upd-toggle", "aria-expanded": String(!!state.updatesOpen), "aria-controls": "update-cards" },
    el("span", { class: "lbl", text: state.updatesOpen ? S.hideUpdates : S.review }), icon("chevron"));
  toggle.addEventListener("click", () => {
    state.updatesOpen = !state.updatesOpen;
    cards.hidden = !state.updatesOpen;
    toggle.setAttribute("aria-expanded", String(state.updatesOpen));
    toggle.querySelector(".lbl").textContent = state.updatesOpen ? S.hideUpdates : S.review;
    if (state.updatesOpen) cards.querySelector("button")?.focus();
  });
  box.hidden = !["all", "applied"].includes(state.tab);
  box.replaceChildren(
    el("div", { class: "updates-head" },
      el("h2", { id: "updates-title", text: S.updatesTitle(items.length) }),
      el("p", { class: "sub", text: S.updatesBar(types) }), toggle),
    el("p", { class: "sub updates-note", hidden: !state.updatesOpen, text: S.updatesSub }),
    cards);
  toggle.addEventListener("click", () => { box.querySelector(".updates-note").hidden = !state.updatesOpen; });
}

/** Employer quote, clamped to 3 lines; click or Enter shows all of it. */
function excerptEl(text) {
  const q = el("blockquote", { class: "excerpt", lang: thai(text), tabindex: "0", "aria-expanded": "false", title: S.showAll }, text);
  const toggle = () => { const open = q.classList.toggle("expanded"); q.setAttribute("aria-expanded", String(open)); };
  q.addEventListener("click", toggle);
  q.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  return q;
}

function suggestionCard(sg, app) {
  const p = sg.proposed || {};
  const from = sg.current_status ?? app?.my_status; // current_status pinned in §M6 contract additions
  const card = el("li", { class: `card t-${sg.type}` });
  const confirm = el("button", { type: "button", class: "btn primary small", text: S.confirmBtn });
  const dismiss = el("button", { type: "button", class: "btn small", text: S.dismissBtn });
  const actions = el("div", { class: "card-actions" }, confirm, dismiss);
  const msg = el("p", { class: "form-msg", role: "status", "aria-live": "polite" });
  const busy = (b, on) => { confirm.disabled = dismiss.disabled = on; if (b) b.replaceChildren(...(on ? [el("span", { class: "spin", "aria-hidden": "true" })] : []), b.dataset.label); };
  confirm.dataset.label = S.confirmBtn; dismiss.dataset.label = S.dismissBtn;

  async function doConfirm(body) {
    busy(confirm, true);
    try {
      await api.suggestionConfirm(sg.id, body);
      toast(S.confirmedToast(sg.company, p.my_status));
        await collapseOut(card);
      await refreshAll();
    } catch (e) { busy(confirm, false); msg.className = "form-msg error"; msg.textContent = e.message; }
  }
  confirm.addEventListener("click", () => {
    if (sg.type !== "rejection" && sg.type !== "ghosted") { doConfirm({}); return; }
    // Rejection / ghosted: ask for the reason category (prefilled) and an optional note first.
    const id = "cf-" + sg.id;
    const cat = selectEl(id + "-cat", Object.entries(S.categories), p.outcome_reason_category || (sg.type === "ghosted" ? "ghosted" : "no_reason_given"));
    const note = el("textarea", { id: id + "-note", rows: "2", maxlength: "300", placeholder: S.notePlaceholder });
    const save = el("button", { type: "submit", class: "btn primary small", text: `${S.confirmBtn}: ${p.my_status}` });
    const cancel = el("button", { type: "button", class: "btn small", text: S.cancelBtn });
    const form = el("form", { class: "confirm-form", novalidate: true },
      el("div", { class: "field" }, el("label", { for: cat.id, text: S.reasonCategory }), cat),
      el("div", { class: "field" }, el("label", { for: note.id, text: S.note }), note),
      el("div", { class: "card-actions" }, save, cancel));
    cancel.addEventListener("click", () => { form.remove(); actions.hidden = false; confirm.focus(); });
    form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); cancel.click(); } });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      save.disabled = cancel.disabled = true;
      save.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), `${S.confirmBtn}: ${p.my_status}`);
      const body = { outcome_reason_category: cat.value };
      if (note.value.trim()) body.note = note.value.trim();
      try {
        await api.suggestionConfirm(sg.id, body);
        toast(S.confirmedToast(sg.company, p.my_status));
        await collapseOut(card);
        await refreshAll();
      } catch (e) {
        save.disabled = cancel.disabled = false; save.replaceChildren(`${S.confirmBtn}: ${p.my_status}`);
        msg.className = "form-msg error"; msg.textContent = e.message;
      }
    });
    actions.hidden = true;
    actions.after(form);
    cat.focus();
  });
  dismiss.addEventListener("click", async () => {
    busy(dismiss, true);
    try { await api.suggestionDismiss(sg.id); toast(S.dismissedToast(sg.company)); await collapseOut(card); await refreshAll(); }
    catch (e) { busy(dismiss, false); msg.className = "form-msg error"; msg.textContent = e.message; }
  });

  const rec = app && safeHref(app.obsidian_url);
  if (rec) actions.append(el("a", { class: "btn small link push", href: rec, title: sg.record_file, text: S.record }));
  card.append(...[
    el("div", { class: "card-top" },
      el("span", { class: `pill sug-pill s-${sg.type}`, text: S.sugType[sg.type] || sg.type }),
      el("span", { class: "company", lang: thai(sg.company), text: sg.company }),
      el("span", { class: "card-prog", lang: thai(sg.program), text: sg.program })),
    el("p", { class: "change-line" }, el("span", { class: "from", text: from || "?" }), " → ", el("b", { text: p.my_status || "?" }),
      p.outcome_stage ? el("span", { class: "sub", text: ` · ${S.stages[p.outcome_stage] || p.outcome_stage}` }) : null),
    sg.type === "ghosted"
      ? el("p", { class: "excerpt-meta", text: S.ghostedLine(app?.date_applied ? fmtDate(app.date_applied) : "?", app?.days_waiting) })
      : null,
    sg.excerpt ? excerptEl(sg.excerpt) : null,
    sg.email ? el("p", { class: "excerpt-meta" }, `${S.emailFrom} ${sg.email.from || "?"}`, sg.email.subject ? ` · “${sg.email.subject}”` : "", sg.email.date ? ` · ${fmtDate(sg.email.date, true)}` : "") : null,
    p.next_step ? el("p", { class: "next" }, el("b", { text: S.proposedNext + ": " }), p.next_step) : null,
    actions, msg].filter(Boolean));
  return card;
}

// ---------------------------------------------------------------------------
// M6: lessons
// ---------------------------------------------------------------------------
function barList(title, counts, labels) {
  const entries = Object.entries(counts || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map((e) => e[1]));
  const ul = el("ul", { class: "bars", "aria-label": title }, entries.map(([k, n]) => {
    const fill = el("span", { class: "fill" });
    fill.style.width = `${Math.round((n / max) * 100)}%`;
    return el("li", { class: "bar wide" }, el("span", { class: "name", text: labels[k] || k }), el("span", { class: "track", "aria-hidden": "true" }, fill), el("span", { class: "n", text: n }));
  }));
  return el("section", { class: "panel agg" }, el("h3", { text: title }), entries.length ? ul : el("p", { class: "sub", text: "–" }));
}

function lessonAggregates(res) {
  if (!res) return null;
  const n = (res.lessons || []).length;
  if (n < 3) return el("p", { class: "sub lessons-few", text: S.lessonsFew(n) });
  return el("div", { class: "aggs" },
    barList(S.byStage, res.by_stage, S.stages),
    barList(S.byCategory, res.by_category, S.categories),
    barList(S.byWeakest, res.by_weakest_circle, { ...S.circles, null: S.noIkigai, unknown: S.noIkigai }));
}

/** "L5 G4 N4 P3e" -> chips */
function ikigaiChips(code, weakest) {
  const m = /^L(\d|\?) G(\d|\?) N(\d|\?) P(\d|\?)(e?)$/.exec((code || "").trim());
  if (!m) return null;
  const v = (x) => (x === "?" ? null : +x);
  return el("div", { class: "circles mini" }, CIRCLES.map((c, i) => chip(c, v(m[i + 1]), c === "paid" && m[5] === "e", false, c === weakest)));
}

function lessonRow(l) {
  return el("li", { class: "row lesson-row" },
    el("div", { class: "job" },
      el("div", { class: "job-line1" }, el("span", { class: "company", lang: thai(l.company), text: l.company }),
        el("span", { class: `pill status-pill st-${(l.my_status || "").replace(/\W/g, "")}`, text: l.my_status || "–" }),
        l.outcome_date ? el("span", { class: "sub", text: fmtDate(l.outcome_date) }) : null),
      el("div", { class: "open plain", lang: thai(l.program), text: l.program }),
      el("div", { class: "meta" },
        el("span", { text: S.stages[l.outcome_stage] || l.outcome_stage || S.stages.unknown }),
        el("span", { text: S.categories[l.outcome_reason_category] || l.outcome_reason_category || S.categories.no_reason_given })),
      l.outcome_reason ? el("div", { class: "next", text: "“" + l.outcome_reason + "”" }) : null),
    el("div", { class: "lesson-ik" }, ikigaiChips(l.ikigai, l.weakest_circle) || el("span", { class: "sub", text: S.noIkigai })));
}

/** Headlines describe the current results, not the navigation label. */
function headline(total) {
  if (state.error || (state.loading && !state.items.length)) return S.tabs[state.tab];
  if (state.q) return S.head.search(total, state.q);
  if (state.tab === "all") return S.head.all(total, state.region);
  return S.head[state.tab](total);
}

function listDescription() {
  if (state.error) return "Try again to reload this list.";
  if (state.loading && !state.items.length) return S.loading;
  if (state.q) return `In ${S.tabs[state.tab].toLowerCase()}`;
  if (state.tab === "all") return "Best matches first";
  if (state.tab === "applied") {
    const open = state.items.filter(a => !["Rejected", "Ghosted", "Withdrawn"].includes(a.my_status)).length;
    return `${open} still open`;
  }
  if (state.tab === "filtered") {
    const top = state.stats?.filtered_top_reasons?.[0];
    return top ? `Most excluded for ${reasonLabel(top.reason).replace(/^(Pre-filter|Auto-filtered): /, "")} (${top.count})` : S.tabIntro.filtered;
  }
  return {
    favorites: "Your starred jobs · saved in this browser",
    centre: "All four fit scores are at least 3 out of 5.",
    hold: "The posting leaves at least one fit score unanswered.",
    low: "At least one fit score is below 3 out of 5.",
    skipped: "Roles you set aside. Unskip to return one to your feed.",
    to_apply: "Finish on the employer’s website, then mark submitted.",
    lessons: "What you learned from applications that ended.",
  }[state.tab];
}

function listHeader(shown, total) {
  const isApps = APP_TABS.includes(state.tab) || state.tab === "lessons";
  const partial = !isApps && !state.loading && !state.error && shown < total;
  return el("div", { class: "feed-start" },
    el("div", { class: "list-head" },
      el("div", { class: "list-heading-row" },
        el("div", { class: "list-heading" }, el("h2", { text: headline(total) }),
          el("p", { class: "feed-description", text: listDescription() })), el("div", { class: "feed-controls" }, !isApps ? salaryToggle() : null, addJobButton)),
      el("div", { class: "list-tools" }, regionControl(), partial ? el("p", { class: "num", text: S.showing(shown, total) }) : null)),
    el("div", { class: "feed-notices" }, updatesPanel, siteChangesPanel));
}


let dailyTimer = null;
let dailyStarting = false;
function scheduleDailyPoll() {
  clearTimeout(dailyTimer);
  dailyTimer = setTimeout(async () => {
    if (state.tab !== "daily") return;
    try {
      const result = await api.daily();
      if (state.tab !== "daily") return;
      const wasRunning = state.daily?.phase === "running";
      state.daily = result; state.error = null;
      renderDaily($("list"));
      if (wasRunning && result.phase !== "running") refreshStats();
    } catch (e) {
      if (state.tab !== "daily") return;
      state.error = e.message; renderDaily($("list"));
    }
    if (state.tab === "daily") scheduleDailyPoll();
  }, 3000);
}
async function startDaily() {
  if (dailyStarting) return;
  dailyStarting = true; state.error = null; renderDaily($("list"));
  try {
    const result = await api.runDaily();
    if (state.tab === "daily") { state.daily = result; scheduleDailyPoll(); }
  } catch (e) { if (state.tab === "daily") state.error = e.message; }
  finally { dailyStarting = false; if (state.tab === "daily") renderDaily($("list")); }
}
function renderDaily(main) {
  const d = state.daily;
  main.setAttribute("aria-busy", String(state.loading));
  const labels = { running: "กำลังดึงข้อมูลวันนี้", completed: "อัปเดตวันนี้แล้ว", partial: "รันแล้ว แต่มีบางขั้นตอนที่ต้องตรวจสอบ", failed: "รันไม่สำเร็จ", not_run: "วันนี้ยังไม่ได้อัปเดตครบ" };
  const summary = d?.summary;
  const when = value => value ? new Intl.DateTimeFormat("th-TH", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Bangkok" }).format(new Date(value)) : "ยังไม่มีประวัติ";
  const issues = [...(summary?.problems || summary?.errors || [])];
  if (d?.error) issues.unshift(d.error);
  const start = el("button", { type: "button", class: "btn primary daily-start", disabled: dailyStarting || state.loading || !d?.can_start || !!state.error,
    onclick: startDaily, text: dailyStarting || d?.phase === "running" ? "กำลังดึงข้อมูล…" : d?.phase === "completed" ? "วันนี้อัปเดตแล้ว" : d?.phase === "partial" || d?.phase === "failed" ? "ลองดึงข้อมูลอีกครั้ง" : "ดึงข้อมูลวันนี้" });
  main.replaceChildren(el("div", { class: "daily-page", lang: "th" },
    el("div", { class: "daily-heading" }, el("div", null, el("h2", { text: "อัปเดตงานประจำวัน" }),
      el("p", { class: "sub", text: "เปิด Mac หลัง 7 โมงก็กดรันได้ ระบบจะค้นหางาน ดึงรายละเอียด ให้คะแนน และตรวจสถานะ" })), start),
    el("section", { class: "daily-status" }, el("h3", { role: "status", text: state.loading ? "กำลังตรวจสถานะ…" : labels[d?.phase] || "ยังโหลดสถานะไม่ได้" }),
      el("p", { text: "รันล่าสุด: " + when(summary?.at) }),
      el("p", { text: "เสร็จล่าสุด: " + when(summary?.finished_at) }),
      el("p", { class: "sub", text: "นับวันตามเวลาไทย · ระหว่างรันให้เปิด Mac และตัว server ไว้ คุณเปลี่ยนแท็บหรือปิดหน้าเว็บได้" }),
      summary ? el("p", { class: "num", text: `ผลรอบล่าสุด: ให้คะแนน ${summary.scored || 0} งาน · ดึงรายละเอียด ${summary.jd_fetched || 0} งาน` }) : null),
    state.error ? el("div", { class: "state error", role: "alert" }, el("p", { text: state.error }), el("button", { type: "button", class: "btn", text: "โหลดสถานะใหม่", onclick: () => loadList() })) : null,
    issues.length ? el("section", null, el("h3", { text: "สิ่งที่ต้องตรวจสอบจากรอบล่าสุด" }), el("ul", null, issues.map(text => el("li", { text })))) : null,
    d?.lines?.length ? el("section", null, el("h3", { text: "ความคืบหน้า" }), el("pre", { class: "daily-log", text: d.lines.join("\n") })) : null));
}

// ---------------------------------------------------------------------------
// Career Wiki & OAuth AI Setup
// ---------------------------------------------------------------------------
const wikiDraft = {
  loaded: false,
  provider: "codex",
  model: "",
  resumeFilename: "",
  resumeText: "",
  qa: {
    target_roles: "",
    exclusions: "",
    work_style: "",
    locations_timing: "",
    compensation: "",
    extra_context: "",
  },
  followUpAnswers: "",
  lastSummary: "",
  followUpQuestions: [],
  docTab: "facts_md",
  candidate: {
    name: "", email: "", degree: "", gpa: 0, graduation_year: 2027,
    degree_completed: false, fulltime_experience_years: 0, languages: "English",
    nationality: "", based_in: "",
  },
  gates: {
    pass_max_years: 0, ask_max_years: 2, available_th: "2027-06", available_abroad: "2027-07",
    pass_cities: "Bangkok", pass_countries: "Thailand, Singapore", remote: "pass",
  },
  anchorsText: "30000, 40000, 50000, 65000",
  keywordsText: "",
  vaultPath: "data/wiki",
  docs: { facts_md: "", rubric_md: "", cover_md: "" },
};

function initialsForName(name) {
  const clean = String(name || "").trim();
  if (!clean) return "JH";
  const parts = clean.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  return clean.slice(0, 2).toUpperCase();
}

function providerShortLabel(pid) {
  return ({ codex: "Codex OAuth", claude: "Claude OAuth", gemini: "Gemini OAuth" })[pid] || "AI Setup";
}

function syncWikiDraftFromServer(w, { overwrite = false } = {}) {
  if (!w) return;
  if (wikiDraft.loaded && !overwrite) {
    wikiDraft.provider = w.llm?.provider || wikiDraft.provider;
    return;
  }
  wikiDraft.loaded = true;
  wikiDraft.provider = w.llm?.provider || "codex";
  wikiDraft.model = w.llm?.model || "";
  const c = w.candidate || {};
  wikiDraft.candidate = {
    name: c.name || "",
    email: c.email || "",
    degree: c.degree || "",
    gpa: c.gpa ?? 0,
    graduation_year: c.graduation_year ?? 2027,
    degree_completed: !!c.degree_completed,
    fulltime_experience_years: c.fulltime_experience_years ?? 0,
    languages: (c.languages || ["English"]).join(", "),
    nationality: c.nationality || "",
    based_in: c.based_in || "",
  };
  const g = w.gates || {};
  wikiDraft.gates = {
    pass_max_years: g.pass_max_years ?? 0,
    ask_max_years: g.ask_max_years ?? 2,
    available_th: g.available_th || "2027-06",
    available_abroad: g.available_abroad || "2027-07",
    pass_cities: (g.pass_cities || []).join(", "),
    pass_countries: (g.pass_countries || []).join(", "),
    remote: g.remote || "pass",
  };
  wikiDraft.anchorsText = (w.paid?.anchors_thb_month || [30000, 40000, 50000, 65000]).join(", ");
  wikiDraft.keywordsText = (w.search?.keywords || []).join(", ");
  wikiDraft.vaultPath = w.vault?.path || "data/wiki";
  wikiDraft.docs = {
    facts_md: w.facts_md || "",
    rubric_md: w.rubric_md || "",
    cover_md: w.cover_md || "",
  };
}

function applyGeneratedWiki(gen) {
  if (!gen) return;
  wikiDraft.lastSummary = gen.wiki_summary || "";
  wikiDraft.followUpQuestions = Array.isArray(gen.follow_up_questions) ? gen.follow_up_questions : [];
  if (gen.candidate) {
    const c = gen.candidate;
    wikiDraft.candidate = {
      name: c.name || wikiDraft.candidate.name,
      email: c.email || wikiDraft.candidate.email,
      degree: c.degree || wikiDraft.candidate.degree,
      gpa: c.gpa ?? 0,
      graduation_year: c.graduation_year ?? 2027,
      degree_completed: !!c.degree_completed,
      fulltime_experience_years: c.fulltime_experience_years ?? 0,
      languages: Array.isArray(c.languages) && c.languages.length ? c.languages.join(", ") : wikiDraft.candidate.languages,
      nationality: c.nationality || "",
      based_in: c.based_in || "",
    };
  }
  if (gen.gates) {
    const g = gen.gates;
    wikiDraft.gates = {
      pass_max_years: g.pass_max_years ?? 0,
      ask_max_years: g.ask_max_years ?? 2,
      available_th: g.available_th || wikiDraft.gates.available_th,
      available_abroad: g.available_abroad || wikiDraft.gates.available_abroad,
      pass_cities: Array.isArray(g.pass_cities) ? g.pass_cities.join(", ") : wikiDraft.gates.pass_cities,
      pass_countries: Array.isArray(g.pass_countries) ? g.pass_countries.join(", ") : wikiDraft.gates.pass_countries,
      remote: g.remote || "pass",
    };
  }
  if (Array.isArray(gen.paid?.anchors_thb_month) && gen.paid.anchors_thb_month.length) {
    wikiDraft.anchorsText = gen.paid.anchors_thb_month.join(", ");
  }
  if (Array.isArray(gen.search?.keywords) && gen.search.keywords.length) {
    wikiDraft.keywordsText = gen.search.keywords.join(", ");
  }
  if (gen.facts_md) wikiDraft.docs.facts_md = gen.facts_md;
  if (gen.rubric_md) wikiDraft.docs.rubric_md = gen.rubric_md;
}

function renderProfileMeta() {
  const w = state.wiki;
  const av = $("profile-avatar");
  const nm = $("profile-name");
  const sb = $("profile-sub");
  if (!av || !nm || !sb) return;
  const name = w?.candidate?.name?.trim() || "";
  av.textContent = initialsForName(name);
  nm.textContent = name || "Set up Career Wiki";
  sb.textContent = w?.configured ? `${providerShortLabel(w.llm?.provider)} · Career Wiki` : "Choose 1 AI · Setup";
}

function renderSetupBanner() {
  const box = $("setup-banner");
  if (!box) return;
  if (!state.wiki || state.wiki.configured || state.tab === "wiki") {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  box.hidden = false;
  box.replaceChildren(
    el("div", { class: "setup-banner-copy" },
      el("h2", { text: "Welcome! Choose 1 AI subscription & build your Career Wiki" }),
      el("p", { text: "Sign in with Codex, Claude, or Gemini OAuth (no API keys, no proxy), then upload your resume and answer a short Q&A so AI builds your personal Career Wiki." })),
    el("button", { type: "button", class: "btn primary", onclick: () => switchTab("wiki") }, icon("spark"), "Set up Career Wiki"));
}

async function refreshWikiMeta() {
  try {
    const w = await api.wiki();
    state.wiki = w;
    syncWikiDraftFromServer(w);
    renderProfileMeta();
    renderSetupBanner();
  } catch { /* ignore when server is offline or in error scenario */ }
}

function splitCsv(val) {
  return String(val || "").split(",").map((s) => s.trim()).filter(Boolean);
}

function parseAnchors(val) {
  const nums = String(val || "").split(/[,\s]+/).map((s) => Number(s.replace(/[^0-9.]/g, ""))).filter((n) => Number.isFinite(n) && n > 0);
  return nums.length ? nums : [30000, 40000, 50000, 65000];
}

function renderWiki(main) {
  main.setAttribute("aria-busy", state.loading ? "true" : "false");
  if (state.loading && !state.wiki) {
    main.replaceChildren(el("div", { class: "rows" }, Array.from({ length: 2 }, loadingCard)));
    return;
  }
  if (state.error && !state.wiki) {
    main.replaceChildren(el("div", { class: "state error", role: "alert" },
      el("h3", { text: S.loadError }), el("p", { text: state.error }),
      el("button", { type: "button", class: "btn primary", text: S.retry, onclick: () => loadList() })));
    return;
  }
  const w = state.wiki || {};
  const providers = w.llm?.providers || [];
  const configuredPill = el("span", { class: "pill " + (w.configured ? "v-CENTRE" : "v-HOLD"), text: w.configured ? "Career Wiki ready" : "First-time setup" });

  // --- Explainer card: What is a Career Wiki? ---
  const explainer = el("section", { class: "wiki-card", "aria-label": "What is a Career Wiki?" },
    el("div", { class: "wiki-card-head" },
      el("div", null,
        el("h3", { text: "What is a Career Wiki?" }),
        el("p", { class: "wiki-card-sub", text: "Most job seekers only have a static PDF resume. A Career Wiki turns your background and preferences into 3 plain-text files on your Mac so AI scores jobs honestly and never hallucinates." }))),
    el("div", { class: "wiki-pillars" },
      el("div", { class: "wiki-pillar" },
        el("h4", { text: "1. Verified Facts (facts.md)" }),
        el("p", { text: "Numbered facts (F01, F02…) extracted from your real resume. Every cover email and 'Good at' score is audited against this ledger so AI never invents metrics or employers." })),
      el("div", { class: "wiki-pillar" },
        el("h4", { text: "2. Ikigai Rubric (rubric.md)" }),
        el("p", { text: "Your personal 1–5 anchors across 4 circles: What you love, What you are good at, What the world needs, and What you can be paid." })),
      el("div", { class: "wiki-pillar" },
        el("h4", { text: "3. Gates & Built-in Tracker" }),
        el("p", { text: "Automatic eligibility gates (experience cap, start date, cities, salary) plus built-in application tracking in data/wiki — no external Obsidian setup required." }))));

  // --- Step 1: Choose 1 AI Subscription (OAuth CLI) ---
  const provMsg = el("p", { class: "form-msg", role: "status", "aria-live": "polite" });
  const modelInput = el("input", {
    id: "wiki-model", type: "text", value: wikiDraft.model,
    placeholder: "Default CLI model (leave blank recommended)", autocomplete: "off",
    oninput: (e) => { wikiDraft.model = e.target.value; },
  });
  const saveProvBtn = el("button", { type: "button", class: "btn primary", text: "Use selected AI" });
  const recheckBtn = el("button", { type: "button", class: "btn", text: "Re-check login status" });

  const selectProvider = (pid) => {
    wikiDraft.provider = pid;
    renderWiki(main);
  };

  saveProvBtn.addEventListener("click", async () => {
    saveProvBtn.disabled = recheckBtn.disabled = true;
    saveProvBtn.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), "Saving…");
    try {
      const res = await api.wikiProvider(wikiDraft.provider, wikiDraft.model);
      if (state.wiki) state.wiki.llm = res.llm;
      renderProfileMeta();
      toast(`Active AI set to ${providerShortLabel(wikiDraft.provider)}`);
      renderWiki(main);
    } catch (e) {
      saveProvBtn.disabled = recheckBtn.disabled = false;
      saveProvBtn.replaceChildren("Use selected AI");
      provMsg.className = "form-msg error"; provMsg.textContent = e.message;
    }
  });

  recheckBtn.addEventListener("click", async () => {
    recheckBtn.disabled = true;
    try {
      const fresh = await api.wiki();
      state.wiki = fresh;
      wikiDraft.provider = fresh.llm?.provider || wikiDraft.provider;
      renderProfileMeta();
      renderWiki(main);
      toast("Refreshed CLI login status");
    } catch (e) {
      recheckBtn.disabled = false;
      provMsg.className = "form-msg error"; provMsg.textContent = e.message;
    }
  });

  const providerCards = providers.map((pr) => {
    const isSel = pr.id === wikiDraft.provider;
    const statusText = pr.ready ? "Ready" : pr.installed ? "Sign in needed" : "Not installed";
    const statusCls = pr.ready ? "v-CENTRE" : pr.installed ? "v-HOLD" : "v-FILTERED";
    const cmdText = pr.installed ? pr.login_cmd : pr.install_cmd;
    const copyBtn = el("button", { type: "button", class: "btn small quiet" }, icon("copy"), el("span", { class: "lbl", text: "Copy" }));
    copyBtn.addEventListener("click", () => copyText(cmdText, copyBtn));
    return el("div", { class: "provider-card" + (isSel ? " selected" : ""), dataset: { provider: pr.id } },
      el("div", { class: "provider-top" },
        el("button", { type: "button", class: "provider-select", "aria-pressed": String(isSel), onclick: () => selectProvider(pr.id) },
          el("span", { class: "provider-dot", "aria-hidden": "true" }),
          el("span", { text: pr.label })),
        el("span", { class: "pill " + statusCls, text: statusText })),
      el("p", { class: "provider-tier", text: pr.subscription }),
      el("p", { class: "provider-detail", text: pr.detail }),
      el("div", { class: "provider-cmd" },
        el("code", { text: cmdText }),
        copyBtn));
  });

  const step1 = el("section", { class: "wiki-card", "aria-label": "Step 1: Choose 1 AI subscription" },
    el("div", { class: "wiki-card-head" },
      el("div", null,
        el("h3", { text: "Step 1 · Choose 1 AI Subscription (OAuth CLI)" }),
        el("p", { class: "wiki-card-sub", text: "Pick the 1 subscription you already have — Codex, Claude, or Gemini. Sign in once in your terminal via browser OAuth; no API keys or local proxy needed." }))),
    el("div", { class: "provider-grid" }, providerCards),
    el("div", { class: "wiki-inline-bar" },
      el("div", { class: "field" }, el("label", { for: "wiki-model", text: "Optional model override" }), modelInput),
      saveProvBtn, recheckBtn),
    provMsg);

  // --- Step 2: Scan Resume + Guided Q&A ---
  const genMsg = el("p", { class: "form-msg", role: "status", "aria-live": "polite" });
  const fileStatus = el("p", { class: "sub", text: wikiDraft.resumeFilename ? `Loaded ${wikiDraft.resumeFilename} (${fmtNum(wikiDraft.resumeText.length)} characters)` : "Supports .pdf, .docx, .odt, .rtf, .txt, or .md (extracted locally on your Mac)" });
  const resumeArea = el("textarea", {
    id: "wiki-resume-text", rows: "6", value: wikiDraft.resumeText,
    placeholder: "Paste your resume / CV text here, or upload a file above…",
    oninput: (e) => { wikiDraft.resumeText = e.target.value; },
  });
  resumeArea.value = wikiDraft.resumeText;

  const fileInput = el("input", {
    id: "wiki-resume-file", type: "file", class: "visually-hidden",
    accept: ".pdf,.docx,.odt,.rtf,.txt,.md",
  });
  const uploadBtn = el("button", { type: "button", class: "btn", onclick: () => fileInput.click() }, icon("upload"), "Upload resume file");

  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    uploadBtn.disabled = true;
    fileStatus.textContent = `Extracting text from ${file.name}…`;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const res = await api.wikiExtractResume(file.name, String(reader.result || ""));
        wikiDraft.resumeFilename = res.filename;
        wikiDraft.resumeText = res.text || "";
        resumeArea.value = wikiDraft.resumeText;
        fileStatus.textContent = `Extracted ${fmtNum(res.char_count)} characters from ${res.filename}`;
        toast(`Resume loaded: ${res.filename}`);
      } catch (e) {
        fileStatus.textContent = e.message;
        toast(S.actionFailed(e.message), { error: true });
      } finally {
        uploadBtn.disabled = false;
        fileInput.value = "";
      }
    };
    reader.onerror = () => {
      uploadBtn.disabled = false;
      fileStatus.textContent = "Could not read that file.";
    };
    reader.readAsDataURL(file);
  });

  const qaSpecs = [
    ["target_roles", "1. What roles & industries excite you? (What you love)", "e.g. Product Manager, Commercial Management Trainee, Business Analyst in tech or FMCG"],
    ["exclusions", "2. What roles or tasks do you want filtered out?", "e.g. Cold-call telesales, pure accounting audit, 5+ years senior roles"],
    ["work_style", "3. What team & mentorship environment do you want?", "e.g. Structured coaching, strong peers, clear ownership and progression"],
    ["locations_timing", "4. Preferred cities/countries, remote & start month?", "e.g. Bangkok or Singapore, hybrid/remote OK, available from June 2027"],
    ["compensation", "5. Monthly salary expectations (acceptable vs great)?", "e.g. Minimum THB 35,000/mo, good at THB 50,000/mo, strong at THB 70,000+/mo"],
    ["extra_context", "6. Extra highlights, test scores, or student clubs?", "e.g. IELTS 7.5, university club roles were student-run, no bond contracts"],
  ];
  const qaFields = qaSpecs.map(([key, label, ph]) => {
    const ta = el("textarea", {
      id: `wiki-qa-${key}`, rows: "2", placeholder: ph,
      oninput: (e) => { wikiDraft.qa[key] = e.target.value; },
    });
    ta.value = wikiDraft.qa[key] || "";
    return el("div", { class: "field" }, el("label", { for: `wiki-qa-${key}`, text: label }), ta);
  });

  const genBtn = el("button", { type: "button", class: "btn primary" }, icon("spark"), "Scan Resume & Build Career Wiki");

  async function runGenerate(btn, busyLabel) {
    const started = Date.now();
    btn.disabled = true;
    btn.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), busyLabel);
    genMsg.className = "form-msg busy";
    genMsg.textContent = `Building your Career Wiki with ${providerShortLabel(wikiDraft.provider)} (0 s)…`;
    const timer = setInterval(() => {
      genMsg.textContent = `Building your Career Wiki with ${providerShortLabel(wikiDraft.provider)} (${Math.round((Date.now() - started) / 1000)} s)…`;
    }, 1000);
    try {
      await api.wikiProvider(wikiDraft.provider, wikiDraft.model);
      const res = await api.wikiGenerate({
        resume_text: wikiDraft.resumeText,
        qa: wikiDraft.qa,
        follow_up_answers: wikiDraft.followUpAnswers,
      });
      clearInterval(timer);
      applyGeneratedWiki(res.generated);
      toast("Career Wiki generated! Review Step 3 and click Save.");
      renderWiki(main);
      document.getElementById("wiki-step-3")?.scrollIntoView({ behavior: REDUCED.matches ? "auto" : "smooth", block: "start" });
    } catch (e) {
      clearInterval(timer);
      btn.disabled = false;
      btn.replaceChildren(icon("spark"), busyLabel);
      genMsg.className = "form-msg error";
      genMsg.textContent = e.message;
    }
  }
  genBtn.addEventListener("click", () => runGenerate(genBtn, "Generating…"));

  let followUpBox = null;
  if (wikiDraft.lastSummary || wikiDraft.followUpQuestions.length) {
    const followTa = el("textarea", {
      id: "wiki-followup-ans", rows: "3",
      placeholder: "Optional: answer any of the follow-up questions above and click Refine Wiki…",
      oninput: (e) => { wikiDraft.followUpAnswers = e.target.value; },
    });
    followTa.value = wikiDraft.followUpAnswers;
    const refineBtn = el("button", { type: "button", class: "btn small primary" }, icon("spark"), "Refine Wiki with answers");
    refineBtn.addEventListener("click", () => runGenerate(refineBtn, "Refining…"));
    followUpBox = el("div", { class: "wiki-followup" },
      el("h4", { text: "AI Career Wiki Summary & Optional Refinement" }),
      wikiDraft.lastSummary ? el("p", { text: wikiDraft.lastSummary }) : null,
      wikiDraft.followUpQuestions.length ? el("ol", null, wikiDraft.followUpQuestions.map((q) => el("li", { text: q }))) : null,
      el("div", { class: "field" }, el("label", { for: "wiki-followup-ans", text: "Your clarifications (optional)" }), followTa),
      el("div", { class: "wiki-actions" }, refineBtn));
  }

  const step2 = el("section", { class: "wiki-card", "aria-label": "Step 2: Scan resume and Q&A" },
    el("div", { class: "wiki-card-head" },
      el("div", null,
        el("h3", { text: "Step 2 · Scan Resume & Guided Q&A" }),
        el("p", { class: "wiki-card-sub", text: "Upload or paste your resume and answer any questions you care about. Your chosen AI extracts verified F01..Fn facts, 1–5 Ikigai scoring rules, and eligibility gates." }))),
    el("div", { class: "upload-bar" }, fileInput, uploadBtn, fileStatus),
    el("div", { class: "field" }, el("label", { for: "wiki-resume-text", text: "Resume / CV text" }), resumeArea),
    el("div", { class: "wiki-qa-grid" }, qaFields),
    el("div", { class: "wiki-actions" }, genBtn, genMsg),
    followUpBox);

  // --- Step 3: Review, Edit & Save Career Wiki ---
  const mkInput = (id, label, val, onInput, attrs = {}) => {
    const inp = el("input", { id, value: val, autocomplete: "off", oninput: (e) => onInput(e.target.value), ...attrs });
    return el("div", { class: "field" }, el("label", { for: id, text: label }), inp);
  };
  const degDoneSel = selectEl("wf-deg-done", [["false", "No (graduating soon)"], ["true", "Yes (completed)"]], String(!!wikiDraft.candidate.degree_completed));
  degDoneSel.addEventListener("change", () => { wikiDraft.candidate.degree_completed = degDoneSel.value === "true"; });
  const remoteSel = selectEl("wf-remote", [["pass", "Pass (allow remote)"], ["ask", "Ask employer"], ["fail", "Filter out remote"]], wikiDraft.gates.remote || "pass");
  remoteSel.addEventListener("change", () => { wikiDraft.gates.remote = remoteSel.value; });

  const fieldsGrid = el("div", { class: "wiki-fields-grid" },
    mkInput("wf-name", "Full name *", wikiDraft.candidate.name, (v) => { wikiDraft.candidate.name = v; }, { type: "text", placeholder: "Your full name" }),
    mkInput("wf-email", "Contact email", wikiDraft.candidate.email, (v) => { wikiDraft.candidate.email = v; }, { type: "email", placeholder: "you@example.com" }),
    mkInput("wf-degree", "Degree / background", wikiDraft.candidate.degree, (v) => { wikiDraft.candidate.degree = v; }, { type: "text", placeholder: "e.g. B.Sc. Computer Science" }),
    mkInput("wf-gpa", "GPA (0 = skip GPA gate)", String(wikiDraft.candidate.gpa ?? 0), (v) => { wikiDraft.candidate.gpa = Number(v) || 0; }, { type: "number", step: "0.01", min: "0", max: "4" }),
    mkInput("wf-grad", "Graduation year", String(wikiDraft.candidate.graduation_year ?? 2027), (v) => { wikiDraft.candidate.graduation_year = Number(v) || 2027; }, { type: "number", min: "1990", max: "2035" }),
    el("div", { class: "field" }, el("label", { for: "wf-deg-done", text: "Degree completed?" }), degDoneSel),
    mkInput("wf-exp", "Full-time experience (years)", String(wikiDraft.candidate.fulltime_experience_years ?? 0), (v) => { wikiDraft.candidate.fulltime_experience_years = Number(v) || 0; }, { type: "number", step: "0.5", min: "0" }),
    mkInput("wf-max-exp", "Max years auto-pass gate", String(wikiDraft.gates.pass_max_years ?? 0), (v) => { wikiDraft.gates.pass_max_years = Number(v) || 0; }, { type: "number", step: "0.5", min: "0" }),
    mkInput("wf-langs", "Languages (comma-separated)", wikiDraft.candidate.languages, (v) => { wikiDraft.candidate.languages = v; }, { type: "text", placeholder: "English, Thai" }),
    mkInput("wf-avail-th", "Earliest local start (YYYY-MM)", wikiDraft.gates.available_th, (v) => { wikiDraft.gates.available_th = v; }, { type: "text", placeholder: "2027-06" }),
    mkInput("wf-avail-ab", "Earliest abroad start (YYYY-MM)", wikiDraft.gates.available_abroad, (v) => { wikiDraft.gates.available_abroad = v; }, { type: "text", placeholder: "2027-07" }),
    el("div", { class: "field" }, el("label", { for: "wf-remote", text: "Remote roles" }), remoteSel),
    mkInput("wf-cities", "Preferred cities (comma-separated)", wikiDraft.gates.pass_cities, (v) => { wikiDraft.gates.pass_cities = v; }, { type: "text", placeholder: "Bangkok" }),
    mkInput("wf-countries", "Preferred countries (comma-separated)", wikiDraft.gates.pass_countries, (v) => { wikiDraft.gates.pass_countries = v; }, { type: "text", placeholder: "Thailand, Singapore" }),
    mkInput("wf-anchors", "Salary anchors THB/mo (4 steps)", wikiDraft.anchorsText, (v) => { wikiDraft.anchorsText = v; }, { type: "text", placeholder: "30000, 40000, 50000, 65000" }),
    mkInput("wf-keywords", "Search keywords (comma-separated)", wikiDraft.keywordsText, (v) => { wikiDraft.keywordsText = v; }, { type: "text", placeholder: "product manager, business analyst" }),
    mkInput("wf-vault", "Local tracker path (built-in: data/wiki)", wikiDraft.vaultPath, (v) => { wikiDraft.vaultPath = v; }, { type: "text", placeholder: "data/wiki" }));

  const docTabs = [
    ["facts_md", "Verified Facts (facts.md)"],
    ["rubric_md", "Ikigai Rubric (rubric.md)"],
    ["cover_md", "Cover Email Rules (cover_email.md)"],
  ];
  const docEditor = el("textarea", {
    id: "wiki-doc-editor", class: "wiki-editor", rows: "14",
    oninput: (e) => { wikiDraft.docs[wikiDraft.docTab] = e.target.value; },
  });
  docEditor.value = wikiDraft.docs[wikiDraft.docTab] || "";

  const docTabBar = el("div", { class: "seg", role: "group", "aria-label": "Career Wiki Markdown files" },
    docTabs.map(([key, label]) => el("button", {
      type: "button", class: "seg-btn", "aria-pressed": String(wikiDraft.docTab === key),
      onclick: () => { wikiDraft.docTab = key; renderWiki(main); document.getElementById("wiki-doc-editor")?.focus(); },
      text: label,
    })));

  const saveMsg = el("p", { class: "form-msg", role: "status", "aria-live": "polite" });
  const saveWikiBtn = el("button", { type: "button", class: "btn primary" }, icon("check"), "Save Career Wiki");
  saveWikiBtn.addEventListener("click", async () => {
    if (!wikiDraft.candidate.name.trim()) {
      saveMsg.className = "form-msg error";
      saveMsg.textContent = "Enter your full name before saving your Career Wiki.";
      document.getElementById("wf-name")?.focus();
      return;
    }
    saveWikiBtn.disabled = true;
    saveWikiBtn.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), "Saving…");
    saveMsg.className = "form-msg busy";
    saveMsg.textContent = "Saving config/profile.toml, facts.md, rubric.md & recomputing gates…";
    try {
      const saved = await api.wikiSave({
        llm: { provider: wikiDraft.provider, model: wikiDraft.model },
        candidate: {
          name: wikiDraft.candidate.name.trim(),
          email: wikiDraft.candidate.email.trim(),
          degree: wikiDraft.candidate.degree.trim(),
          gpa: Number(wikiDraft.candidate.gpa) || 0,
          graduation_year: Number(wikiDraft.candidate.graduation_year) || 2027,
          degree_completed: !!wikiDraft.candidate.degree_completed,
          fulltime_experience_years: Number(wikiDraft.candidate.fulltime_experience_years) || 0,
          languages: splitCsv(wikiDraft.candidate.languages),
          nationality: wikiDraft.candidate.nationality.trim(),
          based_in: wikiDraft.candidate.based_in.trim(),
        },
        gates: {
          pass_max_years: Number(wikiDraft.gates.pass_max_years) || 0,
          ask_max_years: Math.max(Number(wikiDraft.gates.ask_max_years) || 2, Number(wikiDraft.gates.pass_max_years) || 0),
          available_th: wikiDraft.gates.available_th.trim(),
          available_abroad: wikiDraft.gates.available_abroad.trim(),
          pass_cities: splitCsv(wikiDraft.gates.pass_cities),
          pass_countries: splitCsv(wikiDraft.gates.pass_countries),
          remote: wikiDraft.gates.remote || "pass",
        },
        paid: { anchors_thb_month: parseAnchors(wikiDraft.anchorsText) },
        search: { keywords: splitCsv(wikiDraft.keywordsText) },
        vault: { path: wikiDraft.vaultPath.trim() || "data/wiki" },
        facts_md: wikiDraft.docs.facts_md,
        rubric_md: wikiDraft.docs.rubric_md,
        cover_md: wikiDraft.docs.cover_md,
      });
      state.wiki = saved;
      syncWikiDraftFromServer(saved, { overwrite: true });
      renderProfileMeta();
      renderSetupBanner();
      renderWiki(main);
      toast("Career Wiki saved!");
      await refreshStats();
    } catch (e) {
      saveWikiBtn.disabled = false;
      saveWikiBtn.replaceChildren(icon("check"), "Save Career Wiki");
      saveMsg.className = "form-msg error";
      saveMsg.textContent = e.message;
    }
  });

  const step3 = el("section", { class: "wiki-card", id: "wiki-step-3", "aria-label": "Step 3: Review and save Career Wiki" },
    el("div", { class: "wiki-card-head" },
      el("div", null,
        el("h3", { text: "Step 3 · Review, Edit & Save Your Career Wiki" }),
        el("p", { class: "wiki-card-sub", text: "Everything stays in local Markdown and TOML files under config/ and data/wiki/. Edit anything freely and click Save." }))),
    fieldsGrid,
    docTabBar,
    el("div", { class: "field" }, el("label", { for: "wiki-doc-editor", text: docTabs.find((d) => d[0] === wikiDraft.docTab)?.[1] || "Markdown" }), docEditor),
    el("div", { class: "wiki-actions" }, saveWikiBtn, saveMsg));

  main.replaceChildren(el("div", { class: "wiki-page" },
    el("div", { class: "wiki-head" },
      el("div", null,
        el("h2", { text: "Career Wiki & AI Setup" }),
        el("p", { text: "Power your local job board with 1 AI subscription you already own, and turn your resume + Q&A into a grounded Career Wiki." })),
      configuredPill),
    explainer, step1, step2, step3));
}

function loadingCard() {
  return el("div", { class: "row feed-skeleton", "aria-hidden": "true" },
    el("div", { class: "skeleton-identity" }, el("div", { class: "skel skeleton-logo" }),
      el("div", null, el("div", { class: "skel line short" }), el("div", { class: "skel line" }))),
    el("div", { class: "skel skeleton-title" }),
    el("div", { class: "skeleton-copy" }, el("div", { class: "skel line" }), el("div", { class: "skel line short" })),
    el("div", { class: "skel skeleton-action" }));
}

function renderList() {
  salaryObserver.disconnect();
  const main = $("list");
  if (state.tab === "daily") { renderDaily(main); return; }
  if (state.tab === "wiki") { renderWiki(main); return; }
  main.setAttribute("aria-busy", state.loading ? "true" : "false");
  if (state.loading && !state.items.length) {
    main.replaceChildren(listHeader(0, 0), el("p", { class: "visually-hidden", role: "status", text: S.loading }),
      el("div", { class: "rows" }, Array.from({ length: 3 }, loadingCard)));
    return;
  }
  if (state.error) {
    main.replaceChildren(listHeader(0, 0), el("div", { class: "state error", role: "alert" },
      el("h3", { text: S.loadError }), el("p", { text: state.error }),
      el("button", { type: "button", class: "btn primary", text: S.retry, onclick: () => refreshAll() })));
    return;
  }
  if (!state.items.length) {
    const q = state.q;
    main.replaceChildren(listHeader(0, 0), el("div", { class: "state" },
      el("h3", { text: q ? S.emptySearch(q) : S.emptyTitle[state.tab] }),
      q ? el("button", { type: "button", class: "btn", text: S.clearSearch, onclick: () => { $("q").value = ""; setQuery(""); } })
        : el("p", { text: S.emptyBody[state.tab] })));
    if (state.tab === "lessons") { const sf = shortfallPanel(); if (sf) main.append(sf); }
    return;
  }
  if (state.tab === "lessons") {
    main.replaceChildren(...[listHeader(state.items.length, state.total), lessonAggregates(state.lessons),
      el("ol", { class: "rows", "aria-label": S.tabs.lessons }, state.items.map(lessonRow)),
      shortfallPanel()].filter(Boolean));
    return;
  }
  const isApps = APP_TABS.includes(state.tab);
  const rows = el("ol", { class: "rows", "aria-label": S.tabs[state.tab] },
    state.items.map((item) => isApps ? appRow(item) : jobRow(item)));
  const more = !isApps && state.items.length < state.total
    ? el("div", { class: "more" }, el("button", { type: "button", class: "btn", disabled: state.loading, onclick: () => loadList(true) },
      state.loading ? [el("span", { class: "spin", "aria-hidden": "true" }), S.loading] : S.more)) : null;
  main.replaceChildren(...[listHeader(state.items.length, state.total), rows, more].filter(Boolean));
}

async function loadList(append = false, keep = false) {
  clearTimeout(dailyTimer);
  if (append && state.loading) return;
  const seq = ++state.seq;
  state.loading = true;
  state.error = null;
  if (!append && !keep) state.items = [];
  if (!keep) renderList();
  try {
    if (state.tab === "daily") {
      const result = await api.daily();
      if (seq !== state.seq) return;
      state.daily = result;
      scheduleDailyPoll();
    } else if (state.tab === "wiki") {
      const res = await api.wiki();
      if (seq !== state.seq) return;
      state.wiki = res;
      syncWikiDraftFromServer(res);
      renderProfileMeta();
      renderSetupBanner();
    } else if (state.tab === "favorites") {
      const jobs = await Promise.all([...favorites].map(id => api.job(id)));
      if (seq !== state.seq) return;
      const q = state.q.toLowerCase();
      state.items = jobs.filter(j => !q || `${j.company} ${j.title}`.toLowerCase().includes(q));
      state.total = state.items.length;
    } else if (state.tab === "lessons") {
      const res = await api.lessons();
      if (seq !== state.seq) return;
      state.lessons = res;
      const q = state.q.toLowerCase();
      state.items = (res.lessons || []).filter((l) => !q || `${l.company} ${l.program}`.toLowerCase().includes(q));
      state.total = state.items.length;
    } else if (APP_TABS.includes(state.tab)) {
      const res = await api.applications(state.tab);
      if (seq !== state.seq) return;
      const q = state.q.toLowerCase();
      state.items = (res.applications || []).filter((a) => !q || `${a.company} ${a.program}`.toLowerCase().includes(q));
      state.total = state.items.length;
    } else {
      const res = await api.jobs(state.tab, state.q, append ? state.items.length : 0, PAGE, isJobTab() ? state.region : "all");
      if (seq !== state.seq) return;
      state.items = append ? state.items.concat(res.jobs || []) : (res.jobs || []);
      state.total = res.total ?? state.items.length;
    }
  } catch (e) {
    if (seq !== state.seq) return;
    state.error = e.message;
  }
  state.loading = false;
  renderList();
}

async function refreshStats() {
  try {
    state.stats = await api.stats(state.region, ["favorites", "daily", "wiki"].includes(state.tab) ? "all" : state.tab);
    state.statsError = null;
  } catch (e) {
    state.statsError = S.loadError + ": " + e.message;
  }
  renderRun();
  renderTabs();
  // the region counts live in the list header; refresh them in place
  const seg = document.querySelector("#list .list-head .seg");
  if (seg) seg.replaceWith(regionControl() || "");
  const sf = document.querySelector("#list .shortfall-panel");
  if (sf) sf.replaceWith(shortfallPanel() || "");
  renderSiteChanges();
  renderUpdates();
}

async function refreshAll() {
  await Promise.all([refreshStats(), loadList(false, state.items.length > 0 && !state.error)]);
}

let qTimer = null;
function setQuery(v) {
  state.q = v.trim();
  writeHash();
  loadList();
}

// ---------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------
async function doSkip(j, skip, btn) {
  if (btn) btn.disabled = true;
  const ids = state.items.map((x) => x.id);
  const nextId = ids[ids.indexOf(j.id) + 1] ?? ids[ids.indexOf(j.id) - 1];
  const fromRow = btn && btn.closest(".row");
  try {
    await (skip ? api.skip(j.id) : api.unskip(j.id));
    const undo = el("button", { type: "button", class: "btn small", text: S.undo });
    undo.addEventListener("click", async () => {
      undo.disabled = true;
      try { await (skip ? api.unskip(j.id) : api.skip(j.id)); await refreshAll(); dismissToast(undo.closest(".toast")); }
      catch (e) { toast(S.actionFailed(e.message), { error: true }); }
    });
    toast(skip ? S.skipped(j.company) : S.unskipped(j.company), { action: undo, key: "job-" + j.id });
    if (state.openId === j.id) closeDrawer();
    await collapseOut(document.querySelector(`#list .row[data-id="${CSS.escape(String(j.id))}"]`));
    await refreshAll();
    if (fromRow && nextId != null) document.querySelector(`.row[data-id="${CSS.escape(String(nextId))}"] .open`)?.focus();
  } catch (e) {
    if (btn) btn.disabled = false;
    toast(S.actionFailed(e.message), { error: true });
  }
}

async function doApply(j, btn) {
  const labelText = () => btn.dataset.label || applyText(j);
  // A FILTERED job failed a gate: ask once before opening the page or creating a tracker record.
  if (j.verdict === "FILTERED" && btn.dataset.armed !== "1") {
    btn.dataset.armed = "1";
    btn.dataset.label = applyText(j);
    btn.textContent = S.applyConfirm;
    setTimeout(() => { if (btn.dataset.armed === "1") { btn.dataset.armed = ""; setApplyLabel(btn, labelText()); } }, 5000);
    return;
  }
  btn.dataset.armed = ""; btn.dataset.karmed = "";
  // §M10: open the application page FIRST, synchronously inside the click/keydown, so pop-up blockers allow it.
  // The app only opens the page; the user fills in and submits it themselves.
  const target = applyTarget(j);
  if (target.url) window.open(target.url, "_blank", "noopener");
  const ids = state.items.map((x) => x.id);
  const nextId = ids[ids.indexOf(j.id) + 1] ?? ids[ids.indexOf(j.id) - 1] ?? null;
  btn.disabled = true;
  btn.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), S.adding);
  try {
    const res = await api.apply(j.id);
    const dest = S.destName[res.apply_via || target.via] || S.destName.other;
    toast(!target.url ? S.noLinkToast : res.already_tracked ? S.alreadyToast(dest) : S.appliedToast(dest));
    if (res.created && !res.already_tracked) await collapseOut(document.querySelector(`#list .row[data-id="${CSS.escape(String(j.id))}"]`));
    await refreshAll();
    if (state.openId === j.id) api.job(j.id).then((d) => { if (state.openId === j.id) renderDrawer(d); }).catch(() => {});
    else if (nextId != null && res.created) document.querySelector(`#list .row[data-id="${CSS.escape(String(nextId))}"] .open`)?.focus();
  } catch (e) {
    toast(S.actionFailed(e.message), { error: true });
  } finally {
    btn.disabled = false;
    if (btn.isConnected) setApplyLabel(btn, labelText());
  }
}

/** Opens the cover-email view for an already-tracked job (To apply tab, drawer). */
function openCoverDialog(jobId, company, title, recordHref) {
  const dlg = $("apply-result");
  const done = el("button", { type: "button", class: "btn", text: S.ok, onclick: () => dlg.close() });
  dlg.replaceChildren(
    el("div", { class: "m-body" },
      el("h2", { id: "apply-title", text: S.coverDialogTitle }),
      el("p", null, el("b", { lang: thai(company), text: company }), " · ", el("span", { lang: thai(title), text: title })),
      coverView(jobId, { load: true, noHead: true })),
    el("div", { class: "m-actions" }, el("p", { class: "m-foot", text: S.coverFooter }), recordHref ? el("a", { class: "btn", href: recordHref, text: S.openObsidian }) : null, done));
  if (!dlg.open) dlg.showModal();
  done.focus();
}

async function copyText(text, btn) {
  // Clipboard API first; fall back to execCommand when it is missing or refused (unfocused page, Safari quirks).
  const legacy = () => {
    const ta = el("textarea", { class: "visually-hidden", "aria-hidden": "true", readonly: true });
    ta.value = text; document.body.append(ta); ta.select();
    const ok = document.execCommand("copy"); ta.remove();
    if (!ok) throw new Error("the browser blocked copying; select the text and copy it by hand");
  };
  try {
    try {
      if (!navigator.clipboard) throw new Error("no clipboard API");
      await navigator.clipboard.writeText(text);
    } catch { legacy(); }
    const span = btn.querySelector(".lbl") || btn;
    const label = btn.dataset.label || span.textContent;
    btn.dataset.label = label;
    span.textContent = S.copied;
    setTimeout(() => { span.textContent = label; }, 1600);
  } catch (e) { toast(S.actionFailed(e.message), { error: true }); }
}

/** The response of GET/POST /api/jobs/<id>/cover-email: accept {cover_email, cover_email_error} or a bare draft object. */
function coverFrom(res) {
  if (!res) return { cover: null, error: null };
  if ("cover_email" in res || "cover_email_error" in res) return { cover: res.cover_email || null, error: res.cover_email_error || null };
  return res.subject !== undefined ? { cover: res, error: null } : { cover: null, error: null };
}

/**
 * Cover-email section. opts: {cover, error, load, afterApply}. Returns a <section> that re-renders itself
 * on Regenerate. All draft text goes through textContent.
 */
let coverChanged = false; // a draft was written: the record's next step changed, so refresh the list on close
function coverView(jobId, opts) {
  const sec = el("section", { class: "cover", "aria-label": S.coverTitle, "aria-busy": "false" });
  const msg = el("p", { class: "form-msg", role: "status", "aria-live": "polite" });

  function regenButton(label) {
    const b = el("button", { type: "button", class: "btn small quiet", text: label });
    b.addEventListener("click", async () => {
      const started = Date.now();
      sec.querySelectorAll("button").forEach((x) => { x.disabled = true; });
      b.replaceChildren(el("span", { class: "spin", "aria-hidden": "true" }), S.regenerating);
      sec.setAttribute("aria-busy", "true");
      msg.className = "form-msg busy"; msg.textContent = S.regenBusy(0);
      const timer = setInterval(() => { msg.textContent = S.regenBusy(Math.round((Date.now() - started) / 1000)); }, 1000);
      try {
        const res = coverFrom(await api.coverRegen(jobId));
        clearInterval(timer);
        render(res.cover, res.error);
        if (res.cover) { msg.className = "form-msg ok"; msg.textContent = S.regenDone; coverChanged = true; }
      } catch (e) {
        clearInterval(timer);
        sec.querySelectorAll("button").forEach((x) => { x.disabled = false; });
        b.replaceChildren(label);
        msg.className = "form-msg error"; msg.textContent = e.message;
      } finally {
        sec.setAttribute("aria-busy", "false");
      }
    });
    return b;
  }

  function render(cover, error) {
    const head = opts.noHead ? null : el("h3", { text: S.coverTitle });
    if (!cover) {
      sec.replaceChildren(...[head].filter(Boolean),
        error ? el("div", { class: "viol", role: "alert", text: opts.afterApply ? S.coverFailed(error) : S.coverLoadFailed(error) })
          : el("p", { class: "pending", text: S.coverPending }),
        el("div", { class: "cover-actions" }, regenButton(error ? S.regenerate : S.writeDraft)), msg);
      return;
    }
    const body = cover.body || "";
    const isThai = !!thai(body);
    const size = isThai ? S.charsCount(body.length) : S.wordsCount(body.trim().split(/\s+/).filter(Boolean).length);
    const viol = cover.violations || [];
    const copyS = el("button", { type: "button", class: "btn small" }, icon("copy"), el("span", { class: "lbl", text: S.copySubject }));
    copyS.addEventListener("click", () => copyText(cover.subject || "", copyS));
    const copyB = el("button", { type: "button", class: "btn small primary" }, icon("copy"), el("span", { class: "lbl", text: S.copyBody }));
    copyB.addEventListener("click", () => copyText(body, copyB));
    let pathRow = null;
    if (cover.path) {
      const copyP = el("button", { type: "button", class: "btn small quiet" }, icon("copy"), el("span", { class: "lbl", text: S.copyPath }));
      copyP.addEventListener("click", () => copyText(cover.path, copyP));
      const reveal = el("button", { type: "button", class: "btn small quiet" }, icon("folder"), S.openDraft);
      reveal.addEventListener("click", async () => {
        reveal.disabled = true;
        try { await api.coverReveal(jobId); toast(S.revealed); }
        catch (e) { toast(S.actionFailed(e.message), { error: true }); }
        finally { reveal.disabled = false; }
      });
      pathRow = el("div", { class: "cover-path" }, el("span", { class: "sub", text: S.coverPath + ": " }), el("code", { text: cover.path }), reveal, copyP);
    }
    sec.replaceChildren(...[head].filter(Boolean),
      viol.length
        ? el("div", { class: "viol", role: "alert" }, el("b", { text: S.coverViolations }), el("ul", null, viol.map((v) => el("li", { text: v }))))
        : el("p", { class: "clean" }, icon("check"), el("span", { text: S.coverClean })),
      el("div", { class: "mail" },
        el("div", { class: "mail-row" }, el("span", { class: "mail-k", text: S.coverSubject }), el("span", { class: "subj", lang: thai(cover.subject), text: cover.subject || "" })),
        el("div", { class: "mail-body", lang: isThai ? "th" : null, tabindex: "0", "aria-label": S.coverBody, text: body })),
      el("div", { class: "cover-actions" }, copyB, copyS, regenButton(S.regenerate),
        el("span", { class: "sub", title: cover.facts_used?.length ? S.factsUsed(cover.facts_used) : null, text: size })),
      pathRow, msg);
  }

  if (opts.load) {
    sec.replaceChildren(...[opts.noHead ? null : el("h3", { text: S.coverTitle }), el("p", { class: "pending", text: S.coverLoading })].filter(Boolean));
    sec.setAttribute("aria-busy", "true");
    api.coverGet(jobId)
      .then((res) => { const r = coverFrom(res); render(r.cover, r.error); })
      .catch((e) => { if (e.status === 404) render(null, null); else render(null, e.message); })
      .finally(() => sec.setAttribute("aria-busy", "false"));
  } else {
    render(opts.cover, opts.error);
  }
  return sec;
}

/** Toasts leave with a short exit animation. */
function dismissToast(t) {
  if (!t || t.classList.contains("leaving")) return;
  if (REDUCED.matches) { t.remove(); return; }
  t.classList.add("leaving");
  setTimeout(() => t.remove(), 170);
}

function toast(text, { error = false, action = null, key = null } = {}) {
  if (key) document.querySelectorAll(`.toast[data-key="${CSS.escape(key)}"]`).forEach((n) => n.remove());
  const t = el("div", { class: "toast" + (error ? " error" : ""), dataset: key ? { key } : null }, el("span", { text }), action);
  $("toasts").append(t);
  // keep at most 3 on screen; the oldest without focus goes first
  const all = [...$("toasts").children];
  for (const old of all.slice(0, Math.max(0, all.length - 3))) if (!old.contains(document.activeElement)) dismissToast(old);
  // Toasts with an action stay while hovered or focused, so keyboard users can reach Undo.
  let left = action ? 10000 : 4000, timer = null, started = 0;
  const run = () => { started = Date.now(); timer = setTimeout(() => dismissToast(t), left); };
  const pause = () => { clearTimeout(timer); left -= Date.now() - started; };
  t.addEventListener("mouseenter", pause); t.addEventListener("focusin", pause);
  t.addEventListener("mouseleave", run); t.addEventListener("focusout", run);
  run();
}

// ---------------------------------------------------------------------------
// drawer
// ---------------------------------------------------------------------------
async function openDrawer(id, section = null) {
  const dlg = $("drawer");
  if (!dlg.open) state.lastFocus = document.activeElement;
  state.openId = id;
  document.querySelectorAll(".row.is-open").forEach((r) => r.classList.remove("is-open"));
  document.querySelector(`.row[data-id="${CSS.escape(String(id))}"]`)?.classList.add("is-open");
  dlg.classList.remove("closing");
  // skeleton with the same shape as the loaded drawer, so nothing jumps
  dlg.replaceChildren(el("div", { class: "d-head" }, el("div", { class: "top-line" },
    el("div", { class: "titles" }, el("div", { class: "skel line short", "aria-hidden": "true" }), el("h2", { id: "drawer-title", class: "visually-hidden", text: S.loading }),
      el("div", { class: "skel line", "aria-hidden": "true", style: null })), closeButton())),
    el("div", { class: "d-body d-loading", "aria-busy": "true" }, el("div", { class: "skel block", "aria-hidden": "true" }), el("div", { class: "skel block", "aria-hidden": "true" }), el("div", { class: "skel block", "aria-hidden": "true" })),
    el("div", { class: "d-foot" }, el("button", { type: "button", class: "btn primary", disabled: true, text: S.apply })));
  if (!dlg.open) dlg.showModal();
  dlg.querySelector(".close").focus();
  try {
    const j = await api.job(id);
    if (state.openId !== id) return;
    renderDrawer(j);
    if (section === "paid") {
      const paid = dlg.querySelector("#drawer-paid");
      paid.focus({ preventScroll: true });
      paid.scrollIntoView({ block: "start" });
    }
  } catch (e) {
    if (state.openId !== id) return;
    dlg.querySelector(".d-loading").removeAttribute("aria-busy");
    dlg.querySelector(".d-loading").replaceChildren(el("div", { class: "state error", role: "alert" },
      el("h3", { text: S.loadError }), el("p", { text: e.message }),
      el("button", { type: "button", class: "btn primary", text: S.retry, onclick: () => openDrawer(id, section) })));
  }
}

function closeButton() {
  return el("button", { type: "button", class: "btn quiet icon close", "aria-label": S.closeLabel, title: S.close, onclick: () => closeDrawer() }, icon("close"));
}

/** Slide the drawer out, then close it (instant with reduced motion). */
function closeDrawer() {
  const dlg = $("drawer");
  if (!dlg.open || dlg.classList.contains("closing")) return;
  if (REDUCED.matches) { dlg.close(); return; }
  dlg.classList.add("closing");
  const done = () => { clearTimeout(t); dlg.removeEventListener("animationend", done); dlg.classList.remove("closing"); if (dlg.open) dlg.close(); };
  const t = setTimeout(done, 260);
  dlg.addEventListener("animationend", done);
}

function factRows(f) {
  if (!f || !Object.keys(f).length) return null;
  const rows = [];
  const add = (k, v) => { if (v !== null && v !== undefined && v !== "") rows.push(el("dt", { text: S.facts[k] }), el("dd", { text: v })); };
  add("min_years_experience", f.min_years_experience == null ? S.notStated : S.years(f.min_years_experience));
  add("start", f.start_kind ? `${S.startKind[f.start_kind] || f.start_kind}${f.start_date ? " · " + f.start_date : ""}` : null);
  add("graduation_years", f.graduation_years?.length ? f.graduation_years.join(", ") : S.notStated);
  add("location", [f.location_city, f.location_country].filter(Boolean).join(", ") + (f.remote ? ` (${S.remote})` : "") || S.notStated);
  add("required_languages", f.required_languages?.length ? f.required_languages.join(", ") : S.notStated);
  if (f.required_test_scores?.length) add("required_test_scores", f.required_test_scores.join(", "));
  if (f.gpa_floor != null) add("gpa_floor", f.gpa_floor.toFixed(2));
  if (f.degree_completed_required != null) add("degree_completed_required", f.degree_completed_required ? S.yes : S.no);
  add("job_type", f.job_type ? S.jobType[f.job_type] || f.job_type : null);
  if (f.other_hard_requirements?.length) add("other_hard_requirements", f.other_hard_requirements.join("; "));
  return el("dl", { class: "facts" }, rows);
}

function salaryLine(sal) {
  if (!sal || sal.thb_month_mid == null) return null;
  const cur = sal.currency || "THB";
  const range = sal.min != null && sal.max != null ? `${cur} ${fmtNum(sal.min)}–${fmtNum(sal.max)} / ${sal.period || "month"}` : null;
  const mid = `about THB ${fmtNum(sal.thb_month_mid)} / month`;
  return `${range ? range + " · " : ""}${mid} (${sal.stated ? S.salaryStated : S.salaryEst})`;
}

function renderDrawer(j) {
  const dlg = $("drawer");
  const s = j.scores || {};
  const skipped = j.status === "skipped";
  const applyBtn = el("button", { type: "button", class: "btn primary apply" });
  if (preFiltered(j)) { applyBtn.textContent = S.scoreAnyway; applyBtn.addEventListener("click", () => doScore(j, applyBtn)); }
  else { setApplyLabel(applyBtn, applyText(j)); applyBtn.addEventListener("click", () => doApply(j, applyBtn)); }
  const skipBtn = el("button", { type: "button", class: "btn quiet", text: skipped ? S.unskip : S.skip, onclick: () => doSkip(j, !skipped, skipBtn) });
  const post = safeHref(j.url);
  const rec = j.tracked && safeHref(j.tracked.obsidian_url);

  const head = el("div", { class: "d-head" },
    el("div", { class: "top-line" },
      logoEl(j.company, j.logo_url, "lg"),
      el("div", { class: "titles" },
        el("div", { class: "job-line1" }, el("span", { class: "company", text: j.company }),
          el("span", { class: "verdict-note", text: S.verdict[j.verdict] ?? S.verdict.null }),
          trackedBadge(j.tracked)),
        el("h2", { id: "drawer-title", lang: thai(j.title), text: j.title }),
        el("div", { class: "meta" },
          j.location ? el("span", { text: place(j.location) }) : null,
          el("span", { text: S.sources[j.source] || j.source }),
          j.posted_at ? el("span", { text: `${S.posted} ${fmtDate(j.posted_at)}` }) : null,
          el("span", { text: `${S.seen} ${fmtDate(j.first_seen_at, true)}` }))),
      closeButton()));
  // Apply stays visible: the actions live in a footer under the scrolling body.
  // §M10 footer: Apply on <destination> (primary), Skip, Open posting; the cover email is a secondary action
  const foot = el("div", { class: "d-foot" }, applyBtn, skipBtn,
    post ? el("a", { class: "btn link", href: post, target: "_blank", rel: "noopener noreferrer" }, S.openPosting, icon("external")) : null,
    j.tracked ? el("button", { type: "button", class: "btn small quiet push-right", text: S.coverBtn, onclick: () => openCoverDialog(j.id, j.company, j.title, rec) }) : null,
    rec ? el("a", { class: "btn small link", href: rec, title: j.tracked.file, text: S.openObsidian }) : null);

  const secs = [];
  if (j.error) secs.push(el("div", { class: "d-sec" }, el("div", { class: "gate-box g-fail", role: "note" }, S.scoreError + " " + j.error)));
  if (j.summary) secs.push(el("section", { class: "d-sec" }, el("h3", { text: S.secSummary }), el("p", { class: "summary", text: j.summary })));

  // gate first when it blocks or asks: that is the decision-changing information.
  const g = j.gate || {};
  let gateBox;
  if (j.prefilter_reason) {
    gateBox = el("div", { class: "gate-box g-none" }, S.prefilterBox, el("ul", null, el("li", { title: j.prefilter_reason, text: humanRule(j.prefilter_reason) })));
  } else if (g.result === "fail") {
    gateBox = el("div", { class: "gate-box g-fail" }, el("b", { text: S.gateFail }), el("ul", null, (g.fails || []).map((f) => el("li", { text: f }))),
      g.questions?.length ? [el("div", { text: S.askTipTitle, style: null }), el("ul", null, g.questions.map((q) => el("li", { text: q })))] : null);
  } else if (g.result === "ask") {
    gateBox = el("div", { class: "gate-box g-ask" }, el("b", { text: S.gateAsk }), el("ul", null, (g.questions || []).map((q) => el("li", { text: q }))));
  } else if (g.result === "pass") {
    gateBox = el("div", { class: "gate-box g-pass", text: S.gatePass });
  } else {
    gateBox = el("div", { class: "gate-box g-none", text: S.gateNone });
  }
  const gateSec = el("section", { class: "d-sec" }, el("h3", { text: S.secGate }), gateBox);

  const reasons = j.reasons || {};
  const circlesSec = el("section", { class: "d-sec" }, el("h3", { text: S.secCircles }),
    el("div", { class: "reason-list" }, ["love", "good_at", "needs", "paid"].map((c) => {
      const est = c === "paid" && j.paid_estimated && s.paid != null;
      return el("div", { class: "reason", id: c === "paid" ? "drawer-paid" : null,
        tabindex: c === "paid" ? "-1" : null,
        role: c === "paid" ? "region" : null,
        "aria-label": c === "paid" ? (est ? S.viewSalaryEst : S.salary) : null,
      }, chip(c, s[c], est, true, weakestOf(j) === c),
        el("div", null,
          el("h4", null, S.circles[c], est ? ` (${S.est})` : "", el("span", { class: "sub", text: ` · ${S.circleHelp[c]}` })),
          el("p", { text: (reasons[c] || S.noReason).replace(/ ?-> ?/g, " → ").replace(/\s*\((?:F\d+(?:,\s*)?)+\)/g, "") }),
          c === "paid" && salaryLine(j.salary) ? el("p", { class: "sub", text: `${S.salary}: ${salaryLine(j.salary)}` }) : null));
    })));

  if (g.result === "fail" || g.result === "ask" || j.prefilter_reason) secs.push(gateSec, circlesSec);
  else secs.push(circlesSec, gateSec);

  const facts = factRows(j.facts);
  if (facts) secs.push(el("section", { class: "d-sec" }, el("h3", { text: S.secFacts }), facts));
  if (j.email_snippet) secs.push(el("section", { class: "d-sec" }, el("h3", { text: S.secSnippet }), el("p", { class: "jd-text", text: j.email_snippet })));
  secs.push(el("section", { class: "d-sec" }, el("h3", { text: S.secJd }),
    j.jd_text ? el("details", { class: "jd" }, el("summary", null, S.showJd, icon("chevron")), el("div", { class: "jd-text", lang: /[฀-๿]/.test(j.jd_text) ? "th" : null, text: j.jd_text }))
      : el("p", { class: "sub", text: S.noJd })));

  dlg.replaceChildren(head, el("div", { class: "d-body" }, secs), foot);
  dlg.querySelector(".close").focus();
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
function boot() {
  document.title = S.appName;
  if (IS_MOCK) $("mock-flag").hidden = false;
  $("mock-flag").textContent = S.mockFlag;
  $("mock-flag").title = S.mockFlagTitle;
  readRegion();
  readHash();
  const q = $("q");
  q.placeholder = S.searchPlaceholder;
  $("q-label").textContent = S.searchLabel;
  q.value = state.q;
  q.addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => setQuery(q.value), 220); });
  q.addEventListener("keydown", (e) => { if (e.key === "Escape" && q.value) { q.value = ""; setQuery(""); } });
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.querySelector("dialog[open]");
    if (!typing && !e.metaKey && !e.ctrlKey && !e.altKey && ["j", "k", "a", "s"].includes(e.key)) {
      const opens = [...document.querySelectorAll("#list .row button.open")];
      if (!opens.length) return;
      const row = document.activeElement?.closest?.(".row");
      const cur = row ? opens.indexOf(row.querySelector(".open")) : -1;
      if (e.key === "j" || e.key === "k") {
        e.preventDefault();
        const n = e.key === "j" ? Math.min(cur + 1, opens.length - 1) : Math.max(cur - 1, 0);
        opens[n].focus(); opens[n].scrollIntoView({ block: "nearest" });
      } else if (row && row.querySelector(".actions")) {
        e.preventDefault();
        const b = e.key === "a" ? row.querySelector(".actions .btn.primary") : row.querySelector(".actions .btn:not(.primary)");
        if (e.key === "a" && b && !b.disabled) {
          // a single key must not create a tracker record: the first "a" arms, the second applies
          if (b.dataset.karmed === "1") { b.dataset.karmed = ""; b.dataset.armed = "1"; b.click(); }
          else {
            b.dataset.karmed = "1"; b.dataset.label = b.dataset.label || b.textContent;
            b.textContent = row.dataset.verdict === "FILTERED" ? S.pressAgainAnyway : S.pressAgain;
            setTimeout(() => { if (b.dataset.karmed === "1") { b.dataset.karmed = ""; setApplyLabel(b, b.dataset.label); } }, 4000);
          }
          return;
        }
        b?.click();
      }
      return;
    }
    if (e.key === "/" && !e.metaKey && !e.ctrlKey && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) && !document.querySelector("dialog[open]")) {
      e.preventDefault(); q.focus();
    }
  });
  const dlg = $("drawer");
  dlg.addEventListener("close", () => {
    const id = state.openId;
    state.openId = null;
    document.querySelectorAll(".row.is-open").forEach((r) => r.classList.remove("is-open"));
    let back = state.lastFocus;
    state.lastFocus = null;
    // Safari does not focus buttons on mouse click, so fall back to the row's title button.
    if (!back || back === document.body || !document.contains(back)) {
      back = id != null ? document.querySelector(`.row[data-id="${CSS.escape(String(id))}"] .open`) : null;
    }
    if (back) back.focus();
  });
  // Esc and a click on the backdrop close the drawer with the slide-out
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); closeDrawer(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) closeDrawer(); });
  $("apply-result").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
  $("apply-result").addEventListener("close", () => { if (coverChanged) { coverChanged = false; loadList(false, true); } });
  window.addEventListener("hashchange", () => { const t = state.tab, qq = state.q; readHash(); if (t !== state.tab || qq !== state.q) { q.value = state.q; setKind(); renderTabs(); loadList(); } });
  const setTop = () => document.documentElement.style.setProperty("--top-h", document.querySelector("header.top").offsetHeight + "px");
  setTop(); window.addEventListener("resize", setTop);
  $("keys-pop").replaceChildren(el("h2", { text: S.keysTitle }), el("ul", null, S.shortcutKeys.map(([k, what]) => el("li", null, ...k.split(" ").flatMap((x, n) => [n ? " " : "", el("kbd", { text: x })]), " " + what))));
  setKind();
  renderTabs();
  renderPaste();
  $("add-job").addEventListener("click", openAddJob);
  $("profile-btn")?.addEventListener("click", () => switchTab("wiki"));
  document.querySelectorAll("[data-route]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.route)));
  document.querySelector(".brand").addEventListener("click", (event) => {
    event.preventDefault(); $("q").value = ""; state.q = "";
    if (state.tab !== "all") switchTab("all"); else { writeHash(); loadList(); window.scrollTo(0, 0); }
  });
  $("add-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
  $("sites-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
  refreshWikiMeta();
  refreshAll();
}

boot();
