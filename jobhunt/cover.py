"""Cover-letter email drafts (M5). Never sent.

The model writes one draft following ``config/cover_email.md``; a deterministic
checker then enforces the hard rules of ``config/facts.md``. A draft with
violations is regenerated once with those violations listed, and whatever remains
is shown next to the draft so the user can review every check.

Nothing here sends anything: the draft is a file plus a JSON object.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import store

JD_LIMIT = 6000

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

_DATE_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\d{4}-\d{2}\b"),
    re.compile(r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}",
               re.IGNORECASE),
    re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}",
               re.IGNORECASE),
)
_NUMBER = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|[kKmM]\b|million|thousand|percent|baht|THB)?")
_ALLOWED_YEARS = range(2015, 2032)

SALES_WORDS = ("sale", "sales", "revenue", "orders", "customers")


class CoverError(RuntimeError):
    """The draft could not be produced (LLM unavailable, bad reply)."""


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #

def _mask_dates(text: str) -> str:
    for pattern in _DATE_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def numbers_in(text: str) -> set[float]:
    """Every number token in ``text``, normalised (670K == 670,000 == 670000; 22% -> 22)."""
    text = (text or "").translate(THAI_DIGITS)
    cleaned = _mask_dates(text)
    found: set[float] = set()
    for match in _NUMBER.finditer(cleaned):
        raw, suffix = match.group(1), (match.group(2) or "").casefold()
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix in ("k",):
            value *= 1000
        elif suffix in ("m", "million"):
            value *= 1_000_000
        elif suffix == "thousand":
            value *= 1000
        if value in tuple(_ALLOWED_YEARS) and not suffix:
            continue                      # a plain year is always fine
        found.add(value)
    return found


def number_tokens(text: str) -> list[tuple[str, float]]:
    """(as written, value) for the messages, so a violation quotes what the model wrote."""
    text = (text or "").translate(THAI_DIGITS)
    cleaned = _mask_dates(text)
    out = []
    for match in _NUMBER.finditer(cleaned):
        raw, suffix = match.group(1), (match.group(2) or "").casefold()
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix == "k":
            value *= 1000
        elif suffix in ("m", "million"):
            value *= 1_000_000
        elif suffix == "thousand":
            value *= 1000
        if value in tuple(_ALLOWED_YEARS) and not suffix:
            continue
        out.append((match.group(0).strip(), value))
    return out


# --------------------------------------------------------------------------- #
# rules from facts.md
# --------------------------------------------------------------------------- #

def _section_backtick_items(facts: str, heading_prefix: str, bullets_only: bool = False) -> list[str]:
    items: list[str] = []
    inside = False
    for line in facts.splitlines():
        lowered = line.strip().casefold()
        if lowered.startswith(heading_prefix):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if not inside:
            continue
        if bullets_only and not line.strip().startswith(("-", "*")):
            continue
        items.extend(chunk.strip() for chunk in re.findall(r"`([^`]+)`", line) if chunk.strip())
    return items


def banned_phrases(facts: str) -> list[str]:
    """The backticked phrases in the "Banned phrases" section (the em dash is separate)."""
    phrases: list[str] = []
    for chunk in _section_backtick_items(facts, "## banned phrases"):
        for part in chunk.split("/"):
            part = part.strip()
            if not part or part == "—":
                continue
            phrases.append(part.casefold())
    return phrases


def student_markers(facts: str) -> list[str]:
    """Backticked items of the "Student-company markers" section."""
    return _section_backtick_items(facts, "## student-company markers", bullets_only=True)


def no_sales_markers(facts: str) -> list[str]:
    """Backticked entity names in the optional "No-sales markers" section."""
    return _section_backtick_items(facts, "## no-sales markers", bullets_only=True)


def student_fact_ids(draft: dict, facts: str = "") -> list[str]:
    """Facts tagged as student-run in ``facts.md`` (or under a Student section)."""
    tagged: set[str] = set()
    in_student_sec = False
    for line in (facts or "").splitlines():
        lowered = line.strip().casefold()
        if lowered.startswith("## "):
            in_student_sec = "student" in lowered and "marker" not in lowered
            continue
        for fid in re.findall(r"\b(F\d+)\b", line, re.IGNORECASE):
            if in_student_sec or "student-run" in lowered or "[student]" in lowered:
                tagged.add(fid.upper())

    out = []
    for fact in draft.get("facts_used") or []:
        text = str(fact).strip().upper()
        match = re.fullmatch(r"F(\d+)", text)
        if not match:
            continue
        if text in tagged:
            out.append(text)
    return out


STUDENT_PHRASES = ("student-run", "ที่บริหารโดยนักศึกษา")


def contact_bits(facts: str) -> tuple[str | None, str | None]:
    phone = re.search(r"0\d{1,2}-\d{3}-\d{4}", facts)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", facts)
    email_str = email.group(0) if email else None
    if email_str and email_str.casefold() == "you@example.com":
        email_str = None
    return (phone.group(0) if phone else None, email_str)


# --------------------------------------------------------------------------- #
# the checker
# --------------------------------------------------------------------------- #

def check(draft: dict, cfg, jd_text: str | None = None) -> list[str]:
    """Every violation of the hard rules, as messages a human can act on."""
    violations: list[str] = []
    subject = str(draft.get("subject") or "")
    body = str(draft.get("body") or "")
    text = f"{subject}\n{body}"
    facts = cfg.facts
    language = str(draft.get("language") or "en").lower()

    # a. numbers must exist in facts.md (skip only if facts.md has no numbers at all)
    allowed = numbers_in(facts)
    if allowed:
        for written, value in number_tokens(text):
            if value not in allowed:
                violations.append(f"Number \"{written}\" is not in facts.md")

    # b. banned phrases and the em dash
    lowered = text.casefold()
    for phrase in banned_phrases(facts):
        if phrase and phrase in lowered:
            violations.append(f"Banned phrase: \"{phrase}\"")
    if "—" in text:
        violations.append("Em dash (—) is not allowed; use a comma or a full stop")

    # c + h. student-company context: a marker from facts.md or a student fact ID
    # needs explicit "student-run" context so student club titles are never overstated.
    reasons: list[str] = []
    markers = [marker for marker in student_markers(facts)
               if marker and marker.casefold() in lowered]
    if markers:
        reasons.append(", ".join(f"\"{marker}\"" for marker in markers[:3]))
    facts_used = student_fact_ids(draft, facts)
    if facts_used:
        reasons.append("facts " + ", ".join(facts_used))
    if reasons and not any(phrase.casefold() in lowered for phrase in STUDENT_PHRASES):
        violations.append("Mentions student-company work (" + "; ".join(reasons)
                          + ") without saying student-run")

    # d. GPA only when the posting asks
    jd = jd_text or ""
    jd_asks_grades = re.search(r"GPA|GPAX|grade", jd, re.IGNORECASE)
    gpa_val = cfg.get("candidate.gpa")
    gpa_tokens = [r"\bGPAX?\b", "เกรด"]
    if gpa_val and float(gpa_val) > 0:
        gpa_tokens.append(re.escape(f"{float(gpa_val):.2f}"))
    if re.search("|".join(gpa_tokens), text, re.IGNORECASE) and not jd_asks_grades:
        violations.append("Mentions GPA/grades but the posting does not ask for them")

    # e. length
    if language == "th":
        if len(body) > 900:
            violations.append(f"Body is {len(body)} characters; the limit is 900")
    else:
        words = len(re.findall(r"\S+", body))
        if words > 230:
            violations.append(f"Body is {words} words; the limit is 230")

    # f. contact block
    phone, email = contact_bits(facts)
    if phone and phone not in body:
        violations.append(f"Contact block is missing the phone ({phone})")
    if email and email not in body:
        violations.append(f"Contact block is missing the email ({email})")

    # g. optional no-sales entities from facts.md ("## No-sales markers")
    no_sales = no_sales_markers(facts)
    if no_sales:
        words = re.findall(r"[\w']+", body.casefold())
        for entity in no_sales:
            token = re.findall(r"[\w']+", entity.casefold())
            if not token:
                continue
            head_word = token[0]
            for index, word in enumerate(words):
                if word != head_word:
                    continue
                window = words[max(0, index - 12): index + 13]
                if any(term in window for term in SALES_WORDS):
                    violations.append(
                        f"{entity} is described with sales/revenue wording; facts.md marks it no-sales"
                    )
                    break
    return violations


# --------------------------------------------------------------------------- #
# prompt, draft, save
# --------------------------------------------------------------------------- #

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")

THAILAND = ("", "thailand", "th")


def month_label(ym: str | None) -> str:
    """"2027-06" -> "June 2027"."""
    try:
        year, month = str(ym or "").split("-")[:2]
        return f"{MONTHS[int(month) - 1]} {int(year)}"
    except (ValueError, IndexError):
        return str(ym or "").strip()


def availability_line(cfg, score_row) -> str:
    """The one availability sentence the draft must use word for word."""
    country = ""
    if score_row is not None:
        facts = None
        try:
            facts = json.loads(score_row["facts_json"] or "{}")
        except (KeyError, TypeError, ValueError):
            facts = None
        if isinstance(facts, dict):
            country = str(facts.get("location_country") or "")
    key = ("gates.start.available_th" if country.strip().casefold() in THAILAND
           else "gates.start.available_abroad")
    avail = cfg.get(key, "flexible")
    if str(avail).strip().casefold() in ("immediate", "flexible", "now"):
        return f"Availability: {avail}"
    return f"Availability: available full-time from {month_label(avail)}"


def _field(row, key, default=None):
    """Read a column from a sqlite Row or a plain dict, without raising."""
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def build_prompt(cfg, job, score_row) -> str:
    parts = [
        cfg.cover.strip(), "",
        "## Approved facts (the only claims you may use)", cfg.facts.strip(), "",
        config_mod.candidate_block(cfg), "",
        "## The job",
        f"company: {job['company'] or 'unknown'}",
        f"title: {job['title']}",
        f"location: {job['location'] or 'unknown'}",
        f"apply url: {job['url'] or 'unknown'}",
        availability_line(cfg, score_row),
        # The posting is untrusted text: data to read, never instructions to follow.
        f'<job_posting id="{job["id"]}">',
    ]
    jd = (job["jd_text"] or job["email_snippet"] or "").strip()
    if len(jd) > JD_LIMIT:
        jd = jd[:JD_LIMIT] + " ... [truncated]"
    parts += [jd, "</job_posting>", "", "## How this job scored"]
    if score_row is not None:
        for circle in ("love", "good_at", "needs", "paid"):
            value = _field(score_row, circle)
            reason = _field(score_row, circle + "_reason")
            if reason:
                parts.append(f"- {circle} ({'?' if value is None else value}): {reason}")
        summary = _field(score_row, "summary")
        if summary:
            parts.append(f"- summary: {summary}")
        questions = store.load_json(_field(score_row, "gate_questions_json")) or []
        for question in questions:
            parts.append(f"- gate question: {question}")
    parts += ["", "Write the email now, following the output shape exactly."]
    return "\n".join(parts)


def draft(cfg, job, score_row, llm) -> dict:
    """One draft, checked; if it breaks a rule, regenerate once with the violations."""
    from . import validate as schema_check

    prompt = build_prompt(cfg, job, score_row)
    try:
        reply = llm.run(prompt)
    except Exception as exc:
        raise CoverError(str(exc)) from exc
    schema_check.validate(cfg.cover_schema(), reply)

    violations = check(reply, cfg, job["jd_text"])
    if violations:
        retry_prompt = prompt + "\n\n## Your previous draft broke these rules; fix all of them:\n" + \
            "\n".join(f"- {item}" for item in violations)
        try:
            reply = llm.run(retry_prompt)
            schema_check.validate(cfg.cover_schema(), reply)
        except Exception as exc:
            raise CoverError(f"regeneration failed: {exc}") from exc
        violations = check(reply, cfg, job["jd_text"])

    result = {
        "subject": reply["subject"],
        "body": reply["body"],
        "language": reply.get("language", "en"),
        "facts_used": list(reply.get("facts_used") or []),
        "violations": violations,
    }
    path = save(cfg, job, result)
    result["file"] = str(path)              # internal, absolute
    result["path"] = reference(cfg, path)   # what the API and next_step show
    return result


def slug(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u0e00-\u0e7f]+", "-", str(text or "")).strip("-")
    return (cleaned[:limit] or "job").casefold()


def drafts_dir(cfg) -> Path:
    return cfg.data_dir / "drafts"


def reference(cfg, path) -> str:
    """The path as the API and `next_step` show it: relative to the project folder."""
    absolute = Path(path)
    try:
        return f"{cfg.root.name}/{absolute.relative_to(cfg.data_dir.parent).as_posix()}"
    except ValueError:
        return absolute.name


def absolute(cfg, reference_or_path) -> Path:
    """Resolve a reference (or an absolute path) back to a file."""
    candidate = Path(reference_or_path)
    if candidate.is_absolute():
        return candidate
    text = str(candidate)
    prefix = f"{cfg.root.name}/"
    if text.startswith(prefix):
        text = text[len(prefix):]
    return cfg.data_dir.parent / text


def today_bangkok() -> date:
    from .sources.gmail import BANGKOK
    return datetime.now(timezone.utc).astimezone(BANGKOK).date()


def save(cfg, job, result: dict, today: date | None = None) -> Path:
    today = today or today_bangkok()
    directory = drafts_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{today.isoformat()}_{slug(job['company'])}_{slug(job['title'])}.md"
    path = directory / name
    if path.exists():
        # keep exactly one earlier draft beside the new one
        try:
            path.replace(path.with_suffix(".prev.md"))
        except OSError:
            pass
    model_name = cfg.get("llm.model") or cfg.get("llm.provider", "codex")
    front = [
        "---",
        f"job_id: {job['id']}",
        f"url: {json.dumps(job['url'] or '')}",
        f"created: {today.isoformat()}",
        f"model: {json.dumps(model_name)}",
        f"facts_used: {json.dumps(list(result.get('facts_used') or []), ensure_ascii=False)}",
        f"violations: {json.dumps(list(result.get('violations') or []), ensure_ascii=False)}",
        "---",
        "",
        f"Subject: {result['subject']}",
        "",
        result["body"],
        "",
    ]
    path.write_text("\n".join(front), encoding="utf-8")
    return path


def latest(cfg, job_id: int) -> dict | None:
    """The most recently saved draft for a job, if any."""
    directory = drafts_dir(cfg)
    if not directory.is_dir():
        return None
    best = None
    for path in directory.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            continue
        head, rest = match.group(1), match.group(2)
        if not re.search(rf"^job_id:\s*{job_id}\s*$", head, re.MULTILINE):
            continue
        stamp = path.stat().st_mtime
        if best is None or stamp > best[0]:
            best = (stamp, path, head, rest)
    if best is None:
        return None
    _stamp, path, head, rest = best
    match = re.match(r"\s*Subject:[ \t]*([^\n]*)\n(.*)", rest, re.DOTALL)
    if match:
        subject = match.group(1).strip()
        body = match.group(2).strip("\n")
    else:
        subject, body = "", rest.strip("\n")
    facts = re.search(r"^facts_used:\s*(.*)$", head, re.MULTILINE)
    violations = re.search(r"^violations:\s*(.*)$", head, re.MULTILINE)
    return {
        "subject": subject,
        "body": body,
        "path": reference(cfg, path),
        "file": str(path),
        "facts_used": json.loads(facts.group(1)) if facts else [],
        "violations": json.loads(violations.group(1)) if violations else [],
    }
