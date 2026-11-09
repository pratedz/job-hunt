# Job Hunt Interface Design

## Direction

A calm, familiar social-feed workspace for a private local job search. Navigation, readable cards, restrained blue actions (`#0a66c2`), and neutral surfaces keep dense career decisions scannable without clutter.

## Layout and hierarchy

- White top navigation contains the `jh.` wordmark, search, **Discover**, **Applications**, **Insights**, and **Wiki**.
- A compact left rail shows the user's Career Wiki profile button (`#profile-btn`), groups job discovery separately from application tracking, and links directly to **Career Wiki & AI Setup**.
- When a fresh clone has not yet been configured, a concise **First-time setup** banner invites the user to choose 1 AI subscription and scan their resume.
- Jobs form one vertical feed in ranked order: company/source/date, role/location, summary explanation, four labelled Ikigai scores (`x/5`), eligibility questions, and actions (`Apply on …` primary, `Skip` quiet).
- The **Career Wiki** view explains the 3 pillars (`facts.md`, `rubric.md`, gates & built-in tracker), lets the user pick 1 OAuth CLI (`Codex`, `Claude`, or `Gemini`), scans an uploaded/pasted resume + guided Q&A (with optional follow-up refinement), and lets the user edit and save all Markdown/TOML rules in place.

## Visual system

- CSS tokens in `board/styles.css`: system sans-serif with Thai fallbacks, `12/14/16/20/28px` type scale (`22px` role titles), `4px` spacing rhythm, white cards on cool light gray (`#f3f4f6`), and `#0a66c2` for primary actions and active navigation.
- Semantic green, amber, and red are reserved strictly for fit scores, gates, and outcome states.
- Single inline SVG sprite at `1.75` stroke width; locally cached company logos with neutral single-initial fallbacks; zero remote assets; zero italics outside quotations (`<blockquote>`).
- Monospace is reserved for file paths, CLI commands, Career Wiki Markdown editing, and keyboard shortcuts.

## Responsive behavior & accessibility

- At `800px` and below, the left rail collapses into horizontally scrollable filter pills.
- At `520px` and below, search and the 4-item primary navigation stack cleanly into two header rows with zero horizontal overflow down to `390px` mobile viewports.
- Native `<button>`, `<a>`, `<dialog>`, visible `:focus-visible`, `Escape` dismissal, `j`/`k`/`a`/`s`/`/` keyboard shortcuts, `prefers-reduced-motion`, and strict `textContent`-only DOM construction.
