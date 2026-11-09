"""The local application-records wiki/vault, read and written with care.

By default, records live inside ``data/wiki/61 Application Records/`` in the
project itself so new users need no external Obsidian setup. Users who do keep an
Obsidian vault can point ``[vault].path`` at it instead.

Only notes inside the configured records folder are ever written, and only in two
ways: creating a new note for an Apply, and editing the handful of keys that
status updates own. Nothing is ever deleted, no other part of the vault is
touched, and every write is atomic (temp file in the same folder, then rename).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

RECORDS_DIR = "61 Application Records"
TAGS = ["type/record", "db/applications"]
BODY = "Record in [[Applications]]."

#: Keys of a newly created record, in the order the contract lists them.
RECORD_KEYS = (
    "title", "type", "folder", "status", "updated", "summary", "database", "link",
    "apply_link", "company", "program", "section", "my_status", "next_step", "do_by",
    "deadline", "note", "job_platform", "location", "job_type", "ikigai",
    "ikigai_verdict", "jobhunt_id", "tags",
)

PLATFORMS = {
    "linkedin": "LinkedIn",
    "jobsdb": "JobsDB",
    "greenhouse": "Company career site",
    "lever": "Company career site",
    "workday": "Company career site",
    "smartrecruiters": "Company career site",
    "shopee": "Company career site",
    "tiktok": "Company career site",
    "company_site": "Company career site",
    "gmail": "Other",
    "manual": "Other",
}

JOB_TYPES = {
    "graduate_programme": "Graduate program",
    "full_time": "Full-time",
    "internship": "Internship",
    "contract": "Contract",
    "part_time": "Part-time",
}


class VaultError(RuntimeError):
    pass


def _infer_vault_name(path: Path, records_dir: str = RECORDS_DIR) -> str:
    parts = path.parts
    if records_dir in parts:
        idx = parts.index(records_dir)
        if idx >= 2 and parts[idx - 1] == "wiki":
            return parts[idx - 2]
        if idx >= 1:
            return parts[idx - 1]
    return path.parent.name or "job-hunt-wiki"


@dataclass
class Record:
    path: Path
    fields: dict
    lines: list[str] = field(default_factory=list)
    body: str = ""
    trailing_newline: bool = True
    records_dir: str = RECORDS_DIR
    vault_name: str = ""

    @property
    def rel(self) -> str:
        """Path relative to the wiki folder, as the API reports it."""
        parts = self.path.parts
        if self.records_dir in parts:
            index = parts.index(self.records_dir)
            return "/".join(parts[index:])
        return self.path.name

    @property
    def file_name(self) -> str:
        return self.path.name

    def get(self, key: str, default=None):
        return self.fields.get(key, default)

    def obsidian_url(self) -> str:
        name = self.vault_name or _infer_vault_name(self.path, self.records_dir)
        if name == "wiki" and "data" in self.path.parts:
            return ""
        return "obsidian://open?vault=" + _quote(name) + "&file=" + _quote(self.rel)

    def render(self) -> str:
        text = "---\n" + "\n".join(self.lines) + "\n---\n" + self.body
        if self.trailing_newline and not text.endswith("\n"):
            text += "\n"
        return text

    def with_updates(self, updates: dict) -> list[str]:
        """New frontmatter lines: the listed keys change, every other line is untouched."""
        lines = list(self.lines)
        index = {line.split(":", 1)[0].strip(): i for i, line in enumerate(lines) if ":" in line}
        for key, value in updates.items():
            new_line = f"{key}: {_fmt(value)}"
            if key in index:
                lines[index[key]] = new_line
            else:
                lines.append(new_line)
        return lines


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _fmt(value) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in value) + "]"
    if isinstance(value, date):
        return json.dumps(value.isoformat())
    return json.dumps(str(value), ensure_ascii=False)


def _parse_value(raw: str):
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(part) for part in inner.split(",")]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def parse_record(path: Path, *, records_dir: str = RECORDS_DIR, vault_name: str = "") -> Record | None:
    """Frontmatter plus body of one note. Returns None when there is no frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    front = lines[1:end]
    # The body is kept exactly as it was, so render() reproduces the file byte for byte.
    body = "\n".join(lines[end + 1:])
    fields = {}
    for line in front:
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = _parse_value(value)
    return Record(
        path=path,
        fields=fields,
        lines=front,
        body=body,
        trailing_newline=text.endswith("\n"),
        records_dir=records_dir,
        vault_name=vault_name,
    )


class Vault:
    def __init__(self, root: str | Path, records_dir: str = RECORDS_DIR):
        self.root = Path(root)
        # profile.toml writes the folder as "wiki/61 Application Records", so accept
        # both that form and the bare folder name.
        relative = str(records_dir).strip("/")
        self.records_dir = relative.split("/")[-1]
        self.vault_name = self.root.name or "job-hunt-wiki"
        self.path = (self.root / relative) if relative.startswith("wiki/") else (
            self.root / "wiki" / relative)

    # -- reading ----------------------------------------------------------- #
    def exists(self) -> bool:
        return self.path.is_dir()

    def ensure_dir(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def records(self) -> list[Record]:
        if not self.exists():
            return []
        out = []
        for path in sorted(self.path.glob("*.md")):
            record = parse_record(path, records_dir=self.records_dir, vault_name=self.vault_name)
            if record is not None:
                out.append(record)
        return out

    def record(self, rel: str) -> Record | None:
        path = ((self.root / rel) if rel.startswith("wiki/") else (self.root / "wiki" / rel)).resolve()
        if self.path.resolve() not in path.parents:
            raise VaultError(f"refusing to touch {rel}: outside {self.records_dir}")
        return parse_record(path, records_dir=self.records_dir, vault_name=self.vault_name) if path.is_file() else None

    # -- writing ----------------------------------------------------------- #
    def write_record(self, record: Record) -> Path:
        if self.path.resolve() not in record.path.resolve().parents:
            raise VaultError("refusing to write outside the application records folder")
        _atomic_write(record.path, record.render())
        return record.path

    def new_record(self, fields: dict) -> Record:
        """A record with the contract's key order, plus a filename that cannot collide."""
        ordered = {key: fields[key] for key in RECORD_KEYS if key in fields}
        for key, value in fields.items():
            if key not in ordered:
                ordered[key] = value
        lines = [f"{key}: {_fmt(value)}" for key, value in ordered.items()]
        path = self.path / f"{safe_name(str(fields['title']))}.md"
        self.path.mkdir(parents=True, exist_ok=True)
        path = _free_name(path)
        return Record(
            path=path,
            fields=dict(ordered),
            lines=lines,
            body=BODY + "\n",
            records_dir=self.records_dir,
            vault_name=self.vault_name,
        )

    def update_keys(self, record: Record, updates: dict) -> Record:
        """Rewrite only the listed keys. Every other byte stays as it was."""
        record.lines = record.with_updates(updates)
        record.fields.update(updates)
        self.write_record(record)
        return record


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 1000):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise VaultError(f"too many name collisions for {path.name}")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                        prefix=".jobhunt-", suffix=".tmp", delete=False)
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def safe_name(text: str) -> str:
    """Filename-safe company/title text, as the mock UI does it."""
    out = []
    for char in text:
        out.append("-" if char in '\\/:*?"<>|#^[]' else char)
    return "".join(out).strip()


def record_title(company: str | None, title: str | None) -> str:
    company = (company or "Unknown company").strip()
    title = (title or "Untitled role").strip()
    return f"{company} — {title}"


def summary_line(record: Record) -> str:
    company = record.get("company") or ""
    program = record.get("program") or ""
    status = record.get("my_status") or ""
    section = record.get("section") or ""
    do_by = record.get("do_by") or ""
    head = f"{company} — {program}: " if company or program else ""
    tail = f"{status}"
    if section:
        tail += f", section {section}"
    if do_by:
        tail += f", do by {do_by}"
    return head + tail


def days_between(start: str | None, today: date) -> int | None:
    if not start:
        return None
    try:
        return (today - date.fromisoformat(str(start)[:10])).days
    except ValueError:
        return None


def do_by_date(today: date, days: int = 3) -> str:
    return (today + timedelta(days=days)).isoformat()
