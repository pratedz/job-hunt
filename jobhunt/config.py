"""Load config/profile.toml plus the Career Wiki prompt files.

On a fresh clone where ``config/profile.toml``, ``config/facts.md``, or
``config/cover_email.md`` have not been generated yet, :func:`load` falls back
cleanly to ``config/*.example.*`` so the web board and CLI start immediately and
guide the user through setup.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PLACEHOLDER_NAMES = ("", "your name")
PLACEHOLDER_VAULT_PATHS = ("", "data/wiki", "/home/your-user/career-wiki")


@dataclass(frozen=True)
class Config:
    profile: dict
    rubric: str
    facts: str
    root: Path
    profile_path: Path
    schema_path: Path
    cover: str = ""
    #: Where runtime data (db, drafts, logs, built-in wiki) lives. Separate from `root`
    #: so tests can point it at a temp directory without losing config and schema files.
    data_root: Path | None = None

    def get(self, dotted: str, default=None):
        """Read ``a.b.c`` from the profile."""
        node = self.profile
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def is_configured(self) -> bool:
        """True once the user has saved their own profile.toml (via onboarding or manually)."""
        if not self.profile_path.is_file() or self.profile_path.name.endswith(".example.toml"):
            return False
        if bool(self.get("onboarding.completed", False)):
            return True
        name = str(self.get("candidate.name") or "").strip().casefold()
        return name not in PLACEHOLDER_NAMES

    @property
    def data_dir(self) -> Path:
        return (self.data_root or self.root) / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobhunt.sqlite"

    @property
    def vault_path(self) -> Path:
        """Resolved path to the application-records wiki root (defaults to built-in data/wiki)."""
        raw = str(self.get("vault.path") or "").strip()
        if raw.casefold() in PLACEHOLDER_VAULT_PATHS:
            return self.data_dir / "wiki"
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            return (self.data_root or self.root) / candidate
        return candidate

    @property
    def uses_builtin_wiki(self) -> bool:
        raw = str(self.get("vault.path") or "").strip().casefold()
        return raw in PLACEHOLDER_VAULT_PATHS

    @property
    def input_schema_path(self) -> Path:
        return self.root / "schemas" / "job_input.schema.json"

    def input_schema(self) -> dict:
        return json.loads(self.input_schema_path.read_text(encoding="utf-8"))

    def score_schema(self) -> dict:
        return json.loads(self.schema_path.read_text(encoding="utf-8"))

    @property
    def cover_schema_path(self) -> Path:
        return self.root / "schemas" / "cover_email.schema.json"

    def cover_schema(self) -> dict:
        return json.loads(self.cover_schema_path.read_text(encoding="utf-8"))

    @property
    def onboarding_schema_path(self) -> Path:
        return self.root / "schemas" / "onboarding_wiki.schema.json"

    def onboarding_schema(self) -> dict:
        return json.loads(self.onboarding_schema_path.read_text(encoding="utf-8"))

    @property
    def facts_path(self) -> Path:
        return self.root / "config" / "facts.md"

    @property
    def rubric_path(self) -> Path:
        return self.root / "config" / "rubric.md"

    @property
    def cover_path(self) -> Path:
        return self.root / "config" / "cover_email.md"


def _resolve_readable(primary: Path, fallback: Path) -> Path:
    return primary if primary.is_file() else fallback


def load(
    root: str | Path | None = None,
    profile_path: str | Path | None = None,
    rubric_path: str | Path | None = None,
    facts_path: str | Path | None = None,
    schema_path: str | Path | None = None,
    cover_path: str | Path | None = None,
) -> Config:
    root = Path(root) if root else ROOT
    cfg_dir = root / "config"

    explicit_profile = Path(profile_path) if profile_path else None
    target_profile = explicit_profile or (cfg_dir / "profile.toml")
    read_profile = (
        target_profile
        if explicit_profile is not None
        else _resolve_readable(target_profile, cfg_dir / "profile.example.toml")
    )

    read_rubric = (
        Path(rubric_path)
        if rubric_path
        else _resolve_readable(cfg_dir / "rubric.md", cfg_dir / "rubric.example.md")
    )
    read_facts = (
        Path(facts_path)
        if facts_path
        else _resolve_readable(cfg_dir / "facts.md", cfg_dir / "facts.example.md")
    )
    schema_path = Path(schema_path) if schema_path else root / "schemas" / "score_batch.schema.json"
    read_cover = (
        Path(cover_path)
        if cover_path
        else _resolve_readable(cfg_dir / "cover_email.md", cfg_dir / "cover_email.example.md")
    )

    with open(read_profile, "rb") as fh:
        profile = tomllib.load(fh)

    return Config(
        profile=profile,
        rubric=Path(read_rubric).read_text(encoding="utf-8") if Path(read_rubric).is_file() else "",
        facts=Path(read_facts).read_text(encoding="utf-8") if Path(read_facts).is_file() else "",
        root=root,
        profile_path=target_profile,
        schema_path=schema_path,
        cover=Path(read_cover).read_text(encoding="utf-8") if Path(read_cover).is_file() else "",
        data_root=root,
    )


def candidate_block(cfg: Config) -> str:
    """The candidate facts given to the model, straight from profile.toml."""
    c = cfg.profile.get("candidate", {})
    degree = c.get("degree") or c.get("headline") or "Not specified"
    grad_year = c.get("graduation_year")
    deg_done = c.get("degree_completed")
    edu_parts = [str(degree)]
    if grad_year:
        edu_parts.append(f"graduation year {grad_year}")
    if deg_done is not None:
        edu_parts.append(f"degree completed: {deg_done}")

    avail_local = cfg.get("gates.start.available_th") or "flexible"
    avail_abroad = cfg.get("gates.start.available_abroad") or avail_local
    lines = [
        "## Candidate",
        f"Name: {c.get('name') or 'Candidate'}",
        f"Education / headline: {'; '.join(edu_parts)}",
        f"Full-time work experience: {c.get('fulltime_experience_years', 0)} years (student-run clubs/projects do not count as corporate years)",
        f"Nationality: {c.get('nationality') or 'unknown'}; based in {c.get('based_in') or 'unknown'}",
        f"Languages: {', '.join(c.get('languages', [])) or 'not specified'}",
        f"Standardised language test scores: {c.get('test_scores') or 'none'}",
        f"Available: {avail_local} locally, {avail_abroad} where visa/relocation is needed",
    ]
    return "\n".join(lines)
