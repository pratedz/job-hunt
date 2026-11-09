// API client for the board.
// Live mode talks to /api on the same origin. Mock mode (?mock=1) loads dev/mock.js, which
// answers from dev/fixtures/ in memory; the mock module is never loaded in live mode.

const params = new URLSearchParams(location.search);
export const IS_MOCK = params.get("mock") === "1";

let mockHandler = null;
async function mock() {
  if (!mockHandler) {
    const mod = await import("./dev/mock.js");
    mockHandler = await mod.createMock({ scenario: params.get("scenario") || "", cover: params.get("cover") || "" });
  }
  return mockHandler;
}

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function request(method, path, body) {
  if (IS_MOCK) {
    const handle = await mock();
    const res = await handle(method, path, body);
    if (res.status >= 400 || (res.json && res.json.ok === false)) {
      throw new ApiError((res.json && res.json.error) || `HTTP ${res.status}`, res.status);
    }
    return res.json;
  }
  const init = { method, headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" };
  if (method === "POST") {
    init.headers["X-Requested-With"] = "jobhunt";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError("Cannot reach the board server. Is `python3 -m jobhunt board` running?", 0);
  }
  let json = null;
  try { json = await res.json(); } catch (e) { /* non-JSON body */ }
  if (!res.ok || (json && json.ok === false)) {
    throw new ApiError((json && json.error) || `HTTP ${res.status} ${res.statusText}`, res.status);
  }
  return json;
}

const q = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();

export const api = {
  daily: () => request("GET", "/api/daily"),
  runDaily: () => request("POST", "/api/daily"),
  // §M9: region=all|thailand|abroad ("all" is the default, so it is left out). stats also gets the tab,
  // because stats.regions counts the current tab.
  stats: (region, tab) => request("GET", `/api/stats${(region && region !== "all") || tab ? "?" + q({ region: region === "all" ? undefined : region, tab }) : ""}`),
  jobs: (tab, query, offset = 0, limit = 100, region) => request("GET", `/api/jobs?${q({ tab, q: query, limit, offset, region: region === "all" ? undefined : region })}`),
  job: (id) => request("GET", `/api/jobs/${encodeURIComponent(id)}`),
  skip: (id) => request("POST", `/api/jobs/${encodeURIComponent(id)}/skip`),
  unskip: (id) => request("POST", `/api/jobs/${encodeURIComponent(id)}/unskip`),
  apply: (id) => request("POST", `/api/jobs/${encodeURIComponent(id)}/apply`),
  score: (id) => request("POST", `/api/jobs/${encodeURIComponent(id)}/score`), // §M12: "Score anyway"
  coverGet: (id) => request("GET", `/api/jobs/${encodeURIComponent(id)}/cover-email`),
  coverRegen: (id) => request("POST", `/api/jobs/${encodeURIComponent(id)}/cover-email`),
  coverReveal: (id) => request("POST", `/api/jobs/${encodeURIComponent(id)}/cover-email/reveal`),
  suggestions: () => request("GET", "/api/suggestions"),
  suggestionConfirm: (id, body) => request("POST", `/api/suggestions/${encodeURIComponent(id)}/confirm`, body),
  suggestionDismiss: (id) => request("POST", `/api/suggestions/${encodeURIComponent(id)}/dismiss`),
  appStatus: (body) => request("POST", "/api/applications/status", body),
  lessons: () => request("GET", "/api/lessons"),
  applications: (status) => request("GET", `/api/applications?${q({ status })}`),
  submitted: (recordFile) => request("POST", "/api/applications/submitted", { record_file: recordFile }),
  paste: (body) => request("POST", "/api/paste", body),
  siteChanges: () => request("GET", "/api/site-changes"),
  siteSeen: (id) => request("POST", `/api/site-changes/${encodeURIComponent(id)}/seen`),
  wiki: () => request("GET", "/api/wiki"),
  wikiProvider: (provider, model = "") => request("POST", "/api/wiki/provider", { provider, model }),
  wikiExtractResume: (filename, data_base64) => request("POST", "/api/wiki/extract-resume", { filename, data_base64 }),
  wikiGenerate: (body) => request("POST", "/api/wiki/generate", body),
  wikiSave: (body) => request("POST", "/api/wiki/save", body),
};
