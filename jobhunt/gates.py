"""Eligibility gates: pure functions over the JD facts and profile.toml.

Codex extracts the facts; this module applies the rules, so the same facts always
give the same outcome. Every rule contributes at most one hit. The job's gate
result is the worst hit (fail > ask > pass), and every non-pass hit keeps its
message: fail reasons in ``reasons``, asks in ``questions``. A fail does not
suppress the questions, so a job can be FILTERED and still carry "will you hold
the role for a June 2027 start?".
"""

from __future__ import annotations

WORST = {"pass": 0, "ask": 1, "fail": 2}

EXPERIENCE = "experience"
GPA = "gpa"
START = "start"
GRADUATION = "graduation"
DEGREE = "degree"
LANGUAGE = "language"
LOCATION = "location"
JOB_TYPE = "job_type"
OTHER = "other"

THAILAND = {"thailand", "th"}


def _hit(rule: str, outcome: str, message: str | None = None) -> dict:
    return {"rule": rule, "outcome": outcome, "message": message}


def _policy(profile: dict, section: str, key: str, default: str = "pass") -> str:
    value = profile.get("gates", {}).get(section, {}).get(key, default)
    return value if value in WORST else default


def _month_index(ym: str | None) -> int | None:
    if not isinstance(ym, str):
        return None
    parts = ym.strip().split("-")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    month = int(parts[1])
    if not 1 <= month <= 12:
        return None
    return int(parts[0]) * 12 + (month - 1)


def _fmt_month(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _fold(value) -> str | None:
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def check(facts: dict | None, profile: dict) -> dict:
    """Return ``{result, reasons, questions, hits}``."""
    facts = facts or {}
    candidate = profile.get("candidate", {})
    hits: list[dict] = []

    # --- experience -------------------------------------------------------- #
    years = facts.get("min_years_experience")
    pass_max = profile.get("gates", {}).get("experience", {}).get("pass_max_years", 0)
    ask_max = profile.get("gates", {}).get("experience", {}).get("ask_max_years", 1)
    have_years = candidate.get("fulltime_experience_years", 0)
    if years is None:
        hits.append(_hit(EXPERIENCE, "pass"))
    elif years <= pass_max:
        hits.append(_hit(EXPERIENCE, "pass"))
    elif years <= ask_max:
        hits.append(_hit(
            EXPERIENCE, "ask",
            f"The posting asks for {years} year of experience; you have {have_years} years of "
            "full-time experience. Do they consider fresh graduates?",
        ))
    else:
        hits.append(_hit(
            EXPERIENCE, "fail",
            f"Requires at least {years} years of experience; you have {have_years} years "
            "(student-run companies are not corporate experience).",
        ))

    # --- GPA --------------------------------------------------------------- #
    floor = facts.get("gpa_floor")
    gpa = candidate.get("gpa")
    if floor is None or gpa is None or float(gpa) <= 0:
        hits.append(_hit(GPA, "pass"))
    elif floor > float(gpa):
        hits.append(_hit(
            GPA, "fail",
            f"Requires a minimum GPA of {floor:.2f}; your GPAX is {float(gpa):.2f} (transcript).",
        ))
    else:
        hits.append(_hit(GPA, "pass"))

    # --- start date -------------------------------------------------------- #
    start_cfg = profile.get("gates", {}).get("start", {})
    remote = bool(facts.get("remote"))
    country = _fold(facts.get("location_country"))
    needs_visa = bool(country) and country not in THAILAND and not remote
    available = start_cfg.get("available_abroad" if needs_visa else "available_th", "2027-06")
    kind = facts.get("start_kind") or "unknown"
    start_date = facts.get("start_date")
    avail_idx = _month_index(available)
    start_idx = _month_index(start_date)
    late_months = start_cfg.get("late_start_after_months", 2)

    if kind == "immediate":
        hits.append(_hit(START, start_cfg.get("immediate", "ask"),
                         f"Hiring immediately; you are available from {available}. "
                         "Will they hold the role until then?"))
    elif kind == "fixed_date" and start_idx is not None and avail_idx is not None:
        if start_idx < avail_idx:
            hits.append(_hit(START, start_cfg.get("fixed_date_before_available", "fail"),
                             f"Starts {_fmt_month(start_idx)}, before you are available ({available})."))
        elif start_idx > avail_idx + int(late_months):
            hits.append(_hit(START, start_cfg.get("late_start", "ask"),
                             f"Programme starts {_fmt_month(start_idx)}; that is more than "
                             f"{late_months} months after {available}. Are you happy to wait that long?"))
        else:
            hits.append(_hit(START, "pass"))
    elif kind == "flexible":
        hits.append(_hit(START, start_cfg.get("flexible", "pass")))
    else:
        hits.append(_hit(START, start_cfg.get("unknown", "pass")))

    # --- graduation year --------------------------------------------------- #
    years_ok = facts.get("graduation_years") or []
    grad_year = candidate.get("graduation_year")
    if years_ok and grad_year not in years_ok:
        listed = ", ".join(str(y) for y in sorted(years_ok))
        hits.append(_hit(GRADUATION, "fail",
                         f"Accepts graduates of {listed}; you graduate in {grad_year}."))
    else:
        hits.append(_hit(GRADUATION, "pass"))

    # --- degree already completed ------------------------------------------ #
    if facts.get("degree_completed_required") and not candidate.get("degree_completed", False):
        grad_note = f" (expected {grad_year})" if grad_year else ""
        hits.append(_hit(DEGREE, _policy(profile, "degree", "completed_required", "fail"),
                         f"Requires the degree to be completed when you apply; yours is not yet conferred{grad_note}."))
    else:
        hits.append(_hit(DEGREE, "pass"))

    # --- languages and test scores ----------------------------------------- #
    language_cfg = profile.get("gates", {}).get("language", {})
    have = {_fold(x) for x in language_cfg.get("have", candidate.get("languages", []))}
    other_policy = language_cfg.get("other_required", "fail")
    for language in facts.get("required_languages") or []:
        if _fold(language) in have:
            continue
        hits.append(_hit(LANGUAGE, other_policy,
                         f"Requires {language}, which is not one of your languages "
                         f"({', '.join(candidate.get('languages', []))})."))
    for score in facts.get("required_test_scores") or []:
        hits.append(_hit(LANGUAGE, language_cfg.get("test_score_required", "ask"),
                         f"The posting asks for {score} and you have no test score yet. "
                         "Would they accept a score taken after you apply?"))
    if not any(h["rule"] == LANGUAGE for h in hits):
        hits.append(_hit(LANGUAGE, "pass"))

    # --- location ---------------------------------------------------------- #
    location_cfg = profile.get("gates", {}).get("location", {})
    city = _fold(facts.get("location_city"))
    pass_cities = [_fold(c) for c in location_cfg.get("pass_cities", [])]
    pass_countries = [_fold(c) for c in location_cfg.get("pass_countries", [])]
    where = ", ".join(x for x in [facts.get("location_city"), facts.get("location_country")] if x)
    if remote:
        hits.append(_hit(LOCATION, location_cfg.get("remote", "pass")))
    elif country in pass_countries or city in pass_cities:
        hits.append(_hit(LOCATION, "pass"))
    elif not city and not country:
        hits.append(_hit(LOCATION, location_cfg.get("unknown", "pass")))
    elif country in THAILAND and not city:
        # "Thailand" with no city named: nothing to judge, so treat it like unknown.
        hits.append(_hit(LOCATION, location_cfg.get("unknown", "pass")))
    elif country in THAILAND:
        # Thailand but a city outside the Bangkok metro: allowed, but only because
        # the rule for "upcountry" is a question, not a filter, further down.
        hits.append(_hit(LOCATION, location_cfg.get("other", "ask"),
                         f"Based in {where or 'Thailand'}, outside the Bangkok metro. "
                         "Would you relocate or commute?"))
    else:
        hits.append(_hit(LOCATION, location_cfg.get("other", "ask"),
                         f"Based in {where}; that needs a visa and relocation. Is that acceptable?"))

    # --- job type ---------------------------------------------------------- #
    job_type = facts.get("job_type") or "unknown"
    type_policy = _policy(profile, "job_type", job_type, "pass")
    messages = {
        "internship": "Internship rather than a full-time role: is it worth it as a way in?",
        "contract": "Contract rather than permanent: is that acceptable?",
        "part_time": "Part-time role, not full-time: this does not fit the plan.",
    }
    if type_policy == "pass":
        hits.append(_hit(JOB_TYPE, "pass"))
    else:
        hits.append(_hit(JOB_TYPE, type_policy,
                         messages.get(job_type, f"Job type is {job_type}: is that acceptable?")))

    # --- other hard requirements ------------------------------------------- #
    other_policy = _policy(profile, "other", "hard_requirements", "ask")
    for item in facts.get("other_hard_requirements") or []:
        requirement = item.strip().rstrip(".")
        hits.append(_hit(OTHER, other_policy, f"The posting also requires: {requirement}. Do you meet it?"))
    if not any(h["rule"] == OTHER for h in hits):
        hits.append(_hit(OTHER, "pass"))

    result = "pass"
    for hit in hits:
        if WORST[hit["outcome"]] > WORST[result]:
            result = hit["outcome"]

    reasons = [h["message"] for h in hits if h["outcome"] == "fail"]
    questions = [h["message"] for h in hits if h["outcome"] == "ask"]
    return {"result": result, "reasons": reasons, "questions": questions, "hits": hits}
