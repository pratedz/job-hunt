# Job Hunt

<!-- impeccable:product-schema 1 -->

## Platform

web (local loopback `127.0.0.1`)

## Users and purpose

Job Hunt is a free, open-source, local-first job-search workspace. It collects roles from public job boards and company career pages, checks hard eligibility gates, scores every job across the four Ikigai circles using **1 AI subscription the user already owns** (Codex, Claude, or Gemini OAuth CLI), drafts fact-checked cover emails, and tracks applications and outcomes.

Most users do not start with a structured Career Wiki. Built-in **Career Wiki & AI Setup** scans an uploaded or pasted Resume plus a short guided Q&A to generate `config/facts.md`, `config/rubric.md`, and `config/profile.toml`, backed by a built-in local tracker in `data/wiki/61 Application Records/`.

## Stack

Python 3.11+ standard-library server; plain HTML, CSS, and JavaScript ES modules. No build step, third-party frontend dependencies, paid API keys, local proxy servers, remote fonts, or external browser requests.

## Workflows and constraints

- First-time users choose **1 OAuth AI CLI** (`codex`, `claude`, or `gemini`), upload or paste their resume, answer guided Q&A prompts, and review/save their generated Career Wiki directly in the Web UI.
- Browse ranked jobs, search by company/title, and filter by region and fit verdict.
- Star jobs from the top right of each card; Favorites persist locally in the browser.
- Read job descriptions, score reasons, and eligibility questions in the detail drawer.
- Add a posting by URL or JD text, skip/unskip a job, and open its real application destination.
- Apply opens the external application page and creates a local Markdown tracking record (`data/wiki/61 Application Records/` or an optional Obsidian vault). The user submits the application themselves and confirms submission separately.
- Draft fact-checked cover emails on demand (every number and claim verified against `facts.md`), track employer updates, and review outcome lessons.
- Daily update runs the pipeline on demand or via an optional macOS LaunchAgent.
- Preserve the API contract, local data privacy, same-origin image policy, strict CSP, and `textContent`-only rendering of untrusted content.
