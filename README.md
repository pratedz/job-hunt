# Job Hunt

**A local-first, open-source AI job search board, Ikigai scorer, fact-checked cover-email writer, and application tracker.**

- **Zero API keys, zero proxy, zero monthly SaaS fees** — pick **1 AI subscription** you already use (**Codex**, **Claude Code**, or **Gemini CLI**) and sign in via browser OAuth.
- **Built-in Career Wiki + AI Resume & Q&A Onboarding** — upload your resume (`.pdf`, `.docx`, `.odt`, `.rtf`, `.txt`, `.md`) and answer 6 guided questions; AI builds your grounded Career Wiki (`facts.md`, `rubric.md`, `profile.toml`) automatically.
- **100% private on `127.0.0.1`** — Python 3.11+ standard library only (`0` required `pip` packages, `0` npm runtime dependencies). Nothing auto-submits applications or sends emails behind your back.

---

## Quick Start (3 Minutes)

### 1. Clone the repository

```bash
git clone https://github.com/pratedz/job-hunt.git
cd job-hunt
```

*(Requires **Python 3.11+** and `curl`, which ship with macOS and most Linux distributions.)*

### 2. Sign in to **1** AI Subscription (Browser OAuth)

Choose **one** CLI tool below — whichever subscription you already have (Gemini CLI works with a free Google account):

| Option | Subscription | Install CLI | Browser OAuth Sign-In |
| :--- | :--- | :--- | :--- |
| **Codex** | ChatGPT Plus / Pro / Team | `npm install -g @openai/codex` | `codex login` |
| **Claude** | Claude Pro / Max / Team | `npm install -g @anthropic-ai/claude-code` | `claude auth login` |
| **Gemini** | Free Google Account / Gemini Code Assist | `npm install -g @google/gemini-cli` | `gemini` *(choose Login with Google)* |

### 3. Open the Web Board & Build Your Career Wiki

```bash
python3 -m jobhunt board
```

Your browser opens **`http://127.0.0.1:8765`**.
Click **Set up Career Wiki** (or the **Wiki** tab in the top navigation):

1. **Step 1 · Choose 1 AI Subscription**: Click **Codex**, **Claude**, or **Gemini** (the board auto-detects whether the CLI is installed and signed in).
2. **Step 2 · Scan Resume & Guided Q&A**: Upload your Resume (`.pdf`, `.docx`, `.odt`, `.rtf`, `.txt`, or `.md`) or paste your CV text, answer any of the 6 guided preference questions, and click **Scan Resume & Build Career Wiki**.
   - AI extracts your verified facts ledger, personalized 1–5 Ikigai rubric, eligibility gates, salary anchors, and job search keywords — plus asks 2–3 optional follow-up questions if you want to refine anything.
3. **Step 3 · Review & Save**: Inspect or edit any field or Markdown tab right in the browser and click **Save Career Wiki**.

---

## What Is a Career Wiki? (Why Not Just a PDF Resume?)

Most AI job tools hallucinate skills on cover letters or give vague "85% match" scores. **Job Hunt** solves this by turning your resume and preferences into a small, human-readable **Career Wiki** on your computer:

1. **`config/facts.md` — Verified Candidate Facts (`F01`, `F02`, …)**
   Every real metric, project, degree, language, and contact detail from your resume is catalogued as a numbered fact.
   - When scoring **"What you are good at"**, AI may only credit skills backed by `facts.md`.
   - When drafting a **Cover Email**, a deterministic checker verifies that **every number** in the email exists in `facts.md`, blocks generic AI buzzwords/em-dashes, and flags any unverified claim before you copy it.
2. **`config/rubric.md` — Your 4-Circle Ikigai Scoring Rubric (`1` to `5`)**
   Every job is scored from `1` to `5` (or `?` when unknown) on four circles:
   - **Love** — Roles, problems, and industries you genuinely want.
   - **Good at** — Direct evidence from your `facts.md` matching the JD.
   - **Needs** — Mentorship, team calibre, training, and growth trajectory.
   - **Paid** — Posted or estimated compensation measured against your 4 salary anchors.
   Jobs with all four circles `>= 3` land in **Best matches (`CENTRE`)**, sorted by weakest circle first so weak spots never hide behind averages.
3. **`config/profile.toml` + `data/wiki/61 Application Records/` — Gates & Built-in Tracker**
   - **Deterministic Eligibility Gates**: Graduation year, max years of experience, earliest start month, allowed cities/countries, remote rules, and GPA floor are checked in pure Python. Changing a gate or salary anchor re-ranks stored jobs instantly (`python3 -m jobhunt recompute`) with **zero AI calls**.
   - **Built-in Local Tracker (Obsidian-optional)**: Pressing **Apply** opens the real posting URL and writes a clean Markdown record inside `data/wiki/61 Application Records/`. If you use Obsidian, you can optionally point `[vault].path` at your Obsidian vault.

---

## Running Job Discovery & Scoring

Once your Career Wiki is saved, pull fresh jobs either from the **Daily update** tab in the Web UI or from your terminal:

```bash
# Run the full daily pipeline (health check -> discover -> fetch JDs -> score -> track -> logos)
python3 -m jobhunt run

# Or run individual steps:
python3 -m jobhunt discover --source all          # LinkedIn guest search, JobsDB, JobThai, company career pages
python3 -m jobhunt fetch-jd --limit 40            # Fetch full job descriptions for pre-filter survivors
python3 -m jobhunt score --limit 25               # Score jobs with your chosen OAuth AI CLI
python3 -m jobhunt recompute                      # Re-evaluate gates & Paid anchors instantly (0 AI calls)
python3 -m jobhunt cover <job_id>                 # Draft & fact-check a cover email for one job
python3 -m jobhunt list --verdict CENTRE          # Inspect top matches in the terminal
```

### Add Any Job Manually
Found a role in a chat group, referral, or niche site? Click **+ Add job** in the top bar of the Web UI (paste a URL or raw JD text) to fetch and score it immediately.

---

## Optional Extras

### macOS Morning Schedule (`launchd`)
Want fresh jobs waiting when you open your Mac?
```bash
./launchd/install.sh      # Installs ~/Library/LaunchAgents/com.jobhunt.daily.plist (runs at 07:00)
./launchd/uninstall.sh    # Removes the LaunchAgent cleanly
```
You can also double-click **`My Job Hunt.command`** in Finder on macOS to start and open the board without touching Terminal.

### Optional PDF Extraction Boost
Job Hunt includes a zero-dependency pure-Python PDF/DOCX/ODT/RTF text extractor out of the box. For complex multi-column PDFs, installing `pypdf` (`pip install pypdf`) or `poppler` (`brew install poppler`) automatically upgrades extraction fidelity.

### Optional Read-Only Gmail Tracking
If you want Job Hunt to scan job alerts or suggest application status updates (e.g. interview invites or rejections for you to confirm in 1 click), enable `[sources].gmail = true` in `config/profile.toml` and store a Gmail App Password in macOS Keychain (`security add-generic-password -s jobhunt-gmail -a you@gmail.com -w`). Mailbox access uses strict read-only `EXAMINE` + `BODY.PEEK` so unread state is never touched.

---

## Development & Tests

```bash
# Run the full offline Python unit test suite (760+ tests, stdlib unittest)
python3 -m unittest discover -s tests

# Preview the board UI with synthetic mock fixtures (zero database writes)
# Open http://127.0.0.1:8799/index.html?mock=1 (or ?mock=1&scenario=onboarding)
python3 -m http.server 8799 --bind 127.0.0.1 --directory board
```

---

## Privacy & Safety Architecture

- **Loopback-only server**: `board_server.py` forces binding to `127.0.0.1` and requires `X-Requested-With: jobhunt` on every `POST` route.
- **Strict Content-Security-Policy**: No external scripts, fonts, analytics, or third-party image requests from the browser; company logos are downloaded and validated locally (`magic bytes`, raster-only).
- **Prompt-injection & XSS defence**: Job postings are treated as untrusted input — isolated in XML tags in prompts and rendered exclusively via DOM `textContent` (enforced by automated source checks).
- **Personal data stays gitignored**: `config/profile.toml`, `config/facts.md`, `config/rubric.md`, `config/cover_email.md`, and `data/` (your SQLite DB, Career Wiki tracker, and drafts) are excluded via `.gitignore`.

## License

Released under the [MIT License](LICENSE).
