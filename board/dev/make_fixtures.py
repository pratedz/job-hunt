"""Build the board's dev fixtures (mock mode) from the M1 seed data plus synthetic rows.

Run:  python3 -I board/dev/make_fixtures.py
Writes board/dev/fixtures/*.json:
  _db.json                       master state the in-browser mock server works on
  stats.json                     GET /api/stats
  jobs-<tab>.json                GET /api/jobs?tab=<tab>   (all centre hold low filtered skipped)
  job-<id>.json                  GET /api/jobs/<id>
  applications-to_apply.json     GET /api/applications?status=to_apply
  applications-applied.json      GET /api/applications?status=applied
  site-changes.json              GET /api/site-changes
Synthetic rows are dev data only (ids >= 100); seeds keep their M1 ids (1-7).
"""
import json
import os
import urllib.parse
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, "fixtures")
TODAY = date(2026, 11, 7)
CIRCLES = ["love", "good_at", "needs", "paid"]
VERDICT_ORDER = {"CENTRE": 0, "HOLD": 1, "LOW": 2, "FILTERED": 3, None: 4}


def obsidian(file):
    return "obsidian://open?vault=Career-Wiki&file=" + urllib.parse.quote(file, safe="")


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def derive(job):
    """Fill weakest / mean / verdict like the M1 rules."""
    sc = job["scores"]
    known = [(c, sc[c]) for c in CIRCLES if sc[c] is not None]
    if known:
        lo = min(v for _, v in known)
        circle = next(c for c, v in known if v == lo)
        job["weakest"] = {"circle": circle, "score": lo}
        job["mean"] = round(sum(v for _, v in known) / len(known), 4)
    else:
        job["weakest"], job["mean"] = None, None
    if job.get("_unscored"):
        job["verdict"] = None
    elif job["gate"]["result"] == "fail" or job["prefilter_reason"]:
        job["verdict"] = "FILTERED"
    elif not known:
        job["verdict"] = "HOLD"
    elif any(v < 3 for _, v in known):
        job["verdict"] = "LOW"
    elif len(known) < 4:
        job["verdict"] = "HOLD"
    else:
        job["verdict"] = "CENTRE"
    job.pop("_unscored", None)
    return job


def sort_key(j):
    w = j["weakest"]["score"] if j["weakest"] else -1
    m = j["mean"] if j["mean"] is not None else -1
    return (VERDICT_ORDER[j["verdict"]], -w, -m, -datetime.fromisoformat(j["first_seen_at"].replace("Z", "+00:00")).timestamp())


def from_seed(s):
    sc = s["score"]
    facts = json.loads(sc["facts_json"]) if sc.get("facts_json") else {}
    paid_mid = sc.get("paid_thb_month_mid")
    salary = None
    if paid_mid:
        salary = {"min": None, "max": None, "currency": "THB", "period": "month",
                  "stated": not sc["paid_estimated"], "thb_month_mid": paid_mid}
        if s["id"] == 5:
            salary.update({"min": 25000, "max": 30000})
    return {
        "id": s["id"], "rank": None, "company": s["company"], "title": s["title"], "location": s["location"],
        "source": s["source"], "url": s["url"], "posted_at": s["posted_at"], "first_seen_at": s["first_seen_at"],
        "verdict": sc["verdict"], "status": "new",
        "scores": {c: sc[c] for c in CIRCLES}, "paid_estimated": bool(sc["paid_estimated"]),
        "weakest": None, "mean": None,
        "gate": {"result": sc["gate_result"], "fails": json.loads(sc["gate_reasons_json"] or "[]"),
                 "questions": json.loads(sc["gate_questions_json"] or "[]")},
        "prefilter_reason": sc["prefilter_rule"], "tracked": None, "error": sc["error"],
        "reasons": {c: sc[c + "_reason"] for c in CIRCLES} if sc["status"] == "scored" else {c: None for c in CIRCLES},
        "summary": sc["summary"], "facts": facts, "salary": salary,
        "jd_text": s["jd_text"], "jd_missing": bool(s["jd_missing"]), "email_snippet": s["email_snippet"],
    }


FACTS_BASE = {"min_years_experience": 0, "gpa_floor": None, "start_kind": "fixed_date", "start_date": "2027-06",
              "graduation_years": [2027], "degree_completed_required": False, "required_languages": ["Thai", "English"],
              "required_test_scores": [], "location_city": "Bangkok", "location_country": "Thailand", "remote": False,
              "job_type": "graduate_programme", "other_hard_requirements": []}

REASON = {
    "love": {5: "Commercial rotation in FMCG / e-commerce, one of his preferred tracks.",
             4: "Customer-facing commercial work close to his preferred Key Account track.",
             3: "Partly commercial; much of the role is back-office support.",
             2: "Mostly operations or admin work outside his preferred tracks.",
             1: "Role is far from the tracks he wants (e.g. engineering or pure audit)."},
    "good_at": {5: "F10 and F21 match the partnership and seller work; F26 shows the reporting the JD asks for.",
                4: "F20 and F25 show leadership and communication in a student-run company; F26 shows Excel reporting.",
                3: "Some overlap via F26 (reporting); the domain experience the JD wants is not evidenced.",
                2: "The JD asks for sales quota experience that facts.md does not evidence.",
                1: "Requires technical skills not in facts.md."},
    "needs": {5: "Structured rotations and a published path to manager at a growing regional company.",
              4: "Established company with formal training for graduates.",
              3: "Stable employer, but the JD names no development path.",
              2: "Small team with no visible training or growth path.",
              1: "Short contract with no growth path."},
    "paid": {5: "THB 65,000+ per month stated in the posting.",
             4: "THB 50,000-65,000 per month.",
             3: "Estimate about THB 40,000-50,000 per month for a Bangkok graduate programme.",
             2: "Estimate about THB 30,000-40,000 per month.",
             1: "THB 18,000-25,000 per month stated in the posting."},
}

# id, company, title, location, source, scores (L,G,N,P), paid_est, gate, extra
SYN = [
    (101, "Unilever Thailand", "Unilever Future Leaders Programme 2027 - Customer Development", "Bangkok, Thailand", "linkedin", (5, 4, 5, 4), True, "pass", {}),
    (102, "Nestlé Thailand", "Management Trainee - Sales (Modern Trade)", "Bangkok, Thailand", "jobsdb", (5, 4, 4, 3), True, "pass", {}),
    (103, "L'Oréal Thailand", "SeedZ Management Trainee - E-Commerce", "Bangkok, Thailand", "linkedin", (5, 4, 4, 4), True,
     ("ask", ["The posting asks for TOEIC 750 or IELTS 6.5. Do you have a recent score?"]), {}),
    (104, "Shopee", "Seller Relationship Management (MKP) - Fresh Graduate", "Bangkok, Thailand", "linkedin", (4, 5, 4, 3), True,
     ("ask", ["Hiring immediately; you are available from 2027-06. Will they hold the role until then?",
              "Based in Bangkok with occasional travel to the Singapore office. Is that acceptable?",
              "The posting also requires: own transport for warehouse visits. Do you meet it?"]), {}),
    (105, "Grab Thailand", "Associate, Key Account Management (GrabFood)", "Bangkok, Thailand", "jobsdb", (5, 4, 3, 3), False, "pass",
     {"salary": (40000, 45000, True)}),
    (106, "Mondelēz Thailand", "Taste The Future Management Trainee - Commercial", "Bangkok, Thailand", "linkedin", (5, 3, 4, None), True, "pass",
     {"paid_reason": None}),
    (107, "บริษัท ไทยเบฟเวอเรจ จำกัด (มหาชน)", "เจ้าหน้าที่บริหารลูกค้าองค์กร (Key Account Associate) - ช่องทางโมเดิร์นเทรด", "กรุงเทพมหานคร", "jobsdb", (4, 3, 4, 3), True, "pass",
     {"jd": "ตำแหน่งนี้ดูแลลูกค้ากลุ่มโมเดิร์นเทรด วางแผนโปรโมชั่นร่วมกับคู่ค้า ติดตามยอดขายรายสัปดาห์ คุณสมบัติ: ปริญญาตรีทุกสาขา จบปี 2569-2570 สื่อสารภาษาไทยและอังกฤษได้ดี ใช้ Excel ได้คล่อง"}),
    (108, "Siam Piwat", "Management Trainee Program - Retail & Leasing", "Bangkok, Thailand", "manual", (None, 4, 4, None), True, "pass", {}),
    (109, "DKSH Thailand", "Management Trainee - Client Management, Consumer Goods Business Unit, Bangkok Head Office (2027 intake, rotations across Sales, Marketing and Supply Chain)", "Bangkok, Thailand", "linkedin", (4, 4, 4, 3), True, "pass", {}),
    (110, "Lazada Thailand", "LazMall Brand Partnership Associate", "Bangkok, Thailand", "gmail", (5, 4, 3, 3), True,
     ("ask", ["Hiring immediately; you are available from 2027-06. Will they hold the role until then?"]),
     {"jd_missing": True, "snippet": "LazMall Brand Partnership Associate - Lazada Thailand - Bangkok. Work with flagship brands on campaign plans and store growth. Apply by 30 Nov."}),
    (111, "ShopBack", "Management Associate - SEA Regional Track", "Singapore", "greenhouse", (5, 4, 5, 5), False, "pass",
     {"salary": (4200, 4800, True, "SGD", 108000)}),
    (112, "P&G Thailand", "Account Manager, Customer Business Development", "Bangkok, Thailand", "workday", (5, 5, 5, 4), True, "pass", {"tracked": True}),
    (113, "Ninja Van Thailand", "Sales Executive (B2B Logistics)", "Bangkok, Thailand", "jobsdb", (3, 3, 2, 2), False, "pass",
     {"salary": (25000, 32000, True)}),
    (114, "Krungsri (Bank of Ayudhya)", "Relationship Manager Trainee - SME Banking", "Bangkok, Thailand", "linkedin", (3, 3, 4, 2), True,
     ("ask", ["The posting asks for an investment consultant licence (IC). Can you get it before the start date?"]), {}),
    (115, "Thai Union Group", "Global Management Associate Program", "Samut Sakhon, Thailand", "smartrecruiters", (4, 3, 4, 3), True,
     ("ask", ["Based in Samut Sakhon (upcountry). Is a daily commute or relocation acceptable?"]), {}),
    (116, "Example Logistics Co.", "Warehouse Admin Officer", "Chonburi, Thailand", "jobsdb", (1, 2, 2, 1), False, "pass",
     {"salary": (15000, 18000, True)}),
    (117, "Agoda", "Commercial Finance Analyst", "Bangkok, Thailand", "greenhouse", (3, 4, 4, 3), True, "pass", {}),
    (118, "Central Retail", "Data Engineer Graduate", "Bangkok, Thailand", "lever", (2, 1, 4, 3), True, "pass", {}),
    (119, "Boon Rawd (Singha)", "Singha Management Trainee 2027", "Bangkok, Thailand", "manual", (4, 3, 4, 2), True, "pass", {}),
    (120, "Accenture", "Strategy Analyst - Commodity Markets", "Bangkok, Thailand", "linkedin", (4, 3, 4, 4), True,
     ("ask", ["Start date not stated. Will they hold a June 2027 start for a May 2027 graduate?"]), {}),
    # filtered by gate
    (130, "Bangkok Bank", "Management Trainee 2027", "Bangkok, Thailand", "linkedin", (3, 3, 4, 2), True,
     ("fail", ["Requires a minimum GPA of 3.25; your GPAX is 2.48 (transcript)."]), {}),
    (131, "McKinsey & Company", "Business Analyst - Bangkok", "Bangkok, Thailand", "linkedin", (4, 3, 5, 5), True,
     ("fail", ["Requires the degree to be completed when you apply; yours is conferred after May 2027."]), {}),
    (132, "HEINEKEN Thailand", "Key Account Manager - On Trade", "Bangkok, Thailand", "jobsdb", (5, 3, 4, 4), False,
     ("fail", ["Requires at least 3 years of experience; you have 0 years (student-run companies are not corporate experience)."]),
     {"salary": (50000, 60000, True)}),
    (133, "Mars Wrigley Vietnam", "Graduate Trainee - Sales", "Ho Chi Minh City, Vietnam", "workday", (4, 3, 4, 3), True,
     ("fail", ["Requires fluent Vietnamese; your languages are Thai and English."]), {}),
    (134, "SCB", "Management Trainee Program (Business track)", "Bangkok, Thailand", "manual", (3, 3, 4, 3), True,
     ("fail", ["Requires a minimum GPA of 3.00; your GPAX is 2.48 (transcript).", "Accepts graduation years 2025-2026 only; you graduate in 2027."]), {}),
    # pre-filtered (no codex call)
    (140, "Example FMCG Co.", "Senior Brand Manager", "Bangkok, Thailand", "linkedin", (None, None, None, None), False, None,
     {"prefilter": "title_exclude: \\bsenior\\b"}),
    (141, "Shopee", "Lead Software Engineer, Payments", "Singapore", "linkedin", (None, None, None, None), False, None,
     {"prefilter": "title_exclude: \\b(engineer|developer)\\b"}),
    (142, "Example Co.", "Store Supervisor (posted 2025)", "Phuket, Thailand", "jobsdb", (None, None, None, None), False, None,
     {"prefilter": "posted_within_days: posted 412 days ago (limit 45)"}),
    (143, "Lotus's", "Area Manager", "Khon Kaen, Thailand", "jobsdb", (None, None, None, None), False, None,
     {"prefilter": "title_exclude: \\bmanager\\b (without trainee)"}),
    # unscored / error
    (150, "TikTok", "Creator Operation Graduate (TikTok LIVE) 2027", "Bangkok, Thailand", "linkedin", (None, None, None, None), False, None,
     {"unscored": True, "error": "codex timeout after 120 s; will retry next run"}),
    (151, "LINE MAN Wongnai", "Key Account Executive - Food AMG", "Bangkok, Thailand", "jobsdb", (None, None, None, None), False, None,
     {"unscored": True}),
    (152, "Monee (ShopeePay)", "Business Development - Digital Products", "Bangkok, Thailand", "gmail", (None, None, None, None), False, None,
     {"unscored": True, "error": "codex reply failed schema validation (scores.paid: 7 is above maximum 5)"}),
    # skipped
    (160, "Cartier Thailand", "Commercial - Retail Trainee", "Bangkok, Thailand", "linkedin", (3, 3, 3, 3), True, "pass", {"status": "skipped"}),
    (161, "Prudential Thailand", "Agency Development Management Associate", "Bangkok, Thailand", "jobsdb", (2, 3, 3, 3), True, "pass", {"status": "skipped"}),
    (162, "Example Insurance", "Telesales Officer", "Bangkok, Thailand", "jobsdb", (None, None, None, None), False, None,
     {"status": "skipped", "prefilter": "title_exclude: \\btelesales\\b"}),
    # more for volume
    (170, "Beiersdorf (NIVEA)", "WINNERSHIP Graduate Program - Sales", "Bangkok, Thailand", "linkedin", (5, 3, 4, 3), True, "pass", {}),
    (171, "Suntory PepsiCo", "Management Trainee (Sales - Commercial)", "Bangkok, Thailand", "jobsdb", (5, 4, 4, 3), True, "pass", {}),
    (172, "JD Central", "Category Management Associate", "Bangkok, Thailand", "jobsdb", (4, 3, 3, 2), True, "pass", {}),
    (173, "Minor Food", "Management Trainee - Restaurants", "Bangkok, Thailand", "manual", (3, 2, 3, 2), True, "pass", {}),
    (174, "Kraft Heinz Thailand", "E-commerce Sales Coordinator", "Bangkok, Thailand", "linkedin", (4, 4, 3, 2), True,
     ("ask", ["Contract role (12 months). Is a fixed-term contract acceptable?"]), {}),
    (175, "Gulf Development", "GULF Graduate Associate Program", "Bangkok, Thailand", "linkedin", (3, 3, 5, None), True, "pass", {}),
    (176, "Michelin Thailand", "APAC Graduate Program - Marketing & Sales", "Bangkok, Thailand", "workday", (4, 3, 5, 3), True, "pass", {}),
]


def synth(entry, n):
    jid, company, title, loc, source, scores, est, gate, x = entry
    gate_result, fails, questions = None, [], []
    if isinstance(gate, tuple):
        gate_result, items = gate
        if gate_result == "fail":
            fails = items
        else:
            questions = items
    elif gate:
        gate_result = gate
    sc = dict(zip(CIRCLES, scores))
    prefilter = x.get("prefilter")
    scored = not prefilter and not x.get("unscored")
    seen = datetime(2026, 11, 7, 0, 5, tzinfo=timezone.utc) - timedelta(hours=7 * n)
    posted = None if n % 4 == 0 else iso(seen - timedelta(days=n % 9))
    reasons = {c: (REASON[c][sc[c]] if scored and sc[c] is not None else None) for c in CIRCLES}
    if scored:
        for c in CIRCLES:
            if sc[c] is None:
                reasons[c] = "The posting does not say enough to score this circle."
    salary = None
    if "salary" in x:
        s = x["salary"]
        cur = s[3] if len(s) > 3 else "THB"
        mid = s[4] if len(s) > 4 else (s[0] + s[1]) / 2
        salary = {"min": s[0], "max": s[1], "currency": cur, "period": "month", "stated": s[2], "thb_month_mid": mid}
        if scored:
            reasons["paid"] = f"{cur} {s[0]:,}-{s[1]:,} per month stated in the posting -> about THB {mid:,.0f}/month."
    elif scored and sc["paid"] is not None:
        if est:
            reasons["paid"] = "Estimate: the posting states no salary. " + REASON["paid"][sc["paid"]].replace("Estimate about ", "about ").replace(" stated in the posting", "") + " for a comparable Bangkok graduate role."
        salary = {"min": None, "max": None, "currency": "THB", "period": "month", "stated": False,
                  "thb_month_mid": {5: 70000, 4: 55000, 3: 42500, 2: 35000, 1: 25000}[sc["paid"]]}
    facts = dict(FACTS_BASE)
    city = loc.split(",")[0]
    facts.update({"location_city": city, "location_country": loc.split(", ")[-1] if "," in loc else "Thailand"})
    if "Hiring immediately" in " ".join(questions):
        facts.update({"start_kind": "immediate", "start_date": None})
    if any("experience" in f for f in fails):
        facts["min_years_experience"] = 3 if "3 years" in fails[0] else 2
    if any("GPA" in f for f in fails):
        facts["gpa_floor"] = 3.25 if "3.25" in fails[0] else 3.0
    jd = x.get("jd") or (
        None if x.get("jd_missing") else
        f"{company} is hiring for {title} in {loc}. You will work with cross-functional teams, analyse sales and "
        f"customer data, prepare weekly reports and present recommendations to managers. Requirements: bachelor's "
        f"degree in any field (2026-2027 graduates welcome); good command of English and Thai; strong Excel; "
        f"a team player who learns quickly. We offer structured training, mentoring and a clear career path.")
    job = {
        "id": jid, "rank": None, "company": company, "title": title, "location": loc, "source": source,
        "url": f"https://example.invalid/jobs/{jid}" if source in ("manual", "gmail") else {
            "linkedin": f"https://www.linkedin.com/jobs/view/44{jid:08d}",
            "jobsdb": f"https://th.jobsdb.com/job/9{jid:07d}",
            "greenhouse": f"https://job-boards.greenhouse.io/example/jobs/8{jid:06d}",
            "lever": f"https://jobs.lever.co/example/{jid:04d}-0000",
            "workday": f"https://example.wd3.myworkdayjobs.com/en-US/careers/job/{jid}",
            "smartrecruiters": f"https://jobs.smartrecruiters.com/Example/{jid}"}[source],
        "posted_at": posted, "first_seen_at": iso(seen), "verdict": None, "status": x.get("status", "new"),
        "scores": sc, "paid_estimated": bool(est and sc["paid"] is not None and "salary" not in x),
        "weakest": None, "mean": None,
        "gate": {"result": gate_result if scored else None, "fails": fails, "questions": questions},
        "prefilter_reason": prefilter, "tracked": None, "error": x.get("error"),
        "reasons": reasons,
        "summary": None if not scored else (
            "Strong fit on role and growth; confirm the open questions before applying." if questions else
            "Eligibility barrier: see the gate reasons." if fails else
            "Good commercial fit with a clear growth path." if min(v for v in scores if v) >= 3 else
            "Weak on pay and growth compared with the graduate programmes on the board."),
        "facts": facts if scored else {}, "salary": salary,
        "jd_text": jd, "jd_missing": bool(x.get("jd_missing")), "email_snippet": x.get("snippet"),
        "_unscored": bool(x.get("unscored")),
    }
    if x.get("tracked"):
        f = f"61 Application Records/{company} — ⭐ {title}.md"
        job["tracked"] = {"file": f, "obsidian_url": obsidian(f), "my_status": "To apply"}
    return job


def to_row(j):
    return {k: v for k, v in j.items() if k not in ("reasons", "summary", "facts", "salary", "jd_text", "jd_missing", "email_snippet")}


def tab_jobs(jobs, tab):
    want = {"centre": "CENTRE", "hold": "HOLD", "low": "LOW", "filtered": "FILTERED"}
    if tab == "skipped":
        sel = [j for j in jobs if j["status"] == "skipped"]
    else:
        # all = CENTRE + HOLD + LOW only (contract change 7 Nov 2026); unscored jobs only count in stats.unscored
        sel = [j for j in jobs if j["status"] == "new" and (j["verdict"] in ("CENTRE", "HOLD", "LOW") if tab == "all" else j["verdict"] == want[tab])]
    sel.sort(key=sort_key)
    out, r = [], 0
    for j in sel:
        row = to_row(j)
        if j["verdict"] is not None:
            r += 1
            row["rank"] = r
        out.append(row)
    return out


PG_DRAFT_PATH = "job-hunt/data/drafts/2026-11-07_p-g-thailand_account-manager-customer-business-development.md"
# Saved draft for the tracked P&G job (GET /api/jobs/112/cover-email). Only facts.md wording (F01, F02, F21, F25, F04).
PG_DRAFT = {
    "subject": "Application: Account Manager, Customer Business Development – Alex Rivera",
    "body": ("Dear Hiring Team,\n\nI am a final-year Finance student at Bangkok Metropolitan University, graduating in May 2027 "
             "and available full-time from June 2027, and I am applying for the Account Manager, Customer Business Development role.\n\n"
             "At Campus Co, a student-run company, I negotiated and won MetroMart as a modern-trade retail partner and helped develop an SME shelf "
             "for local suppliers, now in 100 MetroMart branches. In the same student-run company I built a 30-session sales training program "
             "for 383 reps (120 certified) and delivered 6 sessions personally.\n\nThe role's focus on growing business with retail customers "
             "is the work I want to build a career in. My CV is attached, and I would welcome a conversation.\n\nBest regards,\n"
             "Alex Rivera\n081-555-0199\nalex.rivera@example.com\nlinkedin.com/in/alex-rivera-demo"),
    "path": PG_DRAFT_PATH, "violations": [], "facts_used": ["F01", "F02", "F21", "F25", "F04"],
}


def app_row(file, company, program, status, applied, next_step, link, jid=None, section="Apply now"):
    days = (TODAY - date.fromisoformat(applied)).days if applied else None
    return {"record_file": "61 Application Records/" + file, "obsidian_url": obsidian("61 Application Records/" + file),
            "company": company, "program": program, "my_status": status, "section": section,
            "date_applied": applied, "days_waiting": days, "next_step": next_step, "jobhunt_id": jid, "link": link,
            "outcome_stage": None, "outcome_reason_category": None, "outcome_reason": None, "outcome_date": None}


def main():
    seeds = json.load(open(os.path.join(HERE, "seeds.json"), encoding="utf-8"))
    jobs = [derive(dict(s)) for s in seeds]
    # The vault already has a record for this Agoda posting (same link), so the server reports it as tracked.
    agoda = next(j for j in jobs if j["id"] == 2)
    f = "61 Application Records/Agoda — Associate Analyst, Marketing (Bangkok).md"
    agoda["tracked"] = {"file": f, "obsidian_url": obsidian(f), "my_status": "To apply"}
    jobs += [derive(synth(e, i + 1)) for i, e in enumerate(SYN)]

    # Sample vault records so the Applied tab shows realistic tracker stages in mock mode.
    applied = [
        app_row("Shopee - Monee — ⭐ Graduate Development Program (GDP) 2027.md", "Shopee / Monee", "⭐ Graduate Development Program (GDP) 2027", "Interview", "2026-10-23",
                "Invited to behavioural interview (told 5 Nov 2026). Confirm date/time and format from the invite email; prep interview stories.", "https://www.linkedin.com/jobs/view/4455619735"),
        app_row("Sea Limited — Global Management Associate Program 2027 (Shopee-Monee track).md", "Sea Limited", "Global Management Associate Program 2027 (Shopee/Monee track)", "Interview", "2026-10-24",
                "Invited to behavioural interview (told 5 Nov 2026). Confirm date/time and format from the invite email.", "https://www.linkedin.com/jobs/view/4455578347"),
        app_row("Deutsche Bank (Singapore) — 2027 Graduate Programme – Corporate Bank.md", "Deutsche Bank (Singapore)", "2027 Graduate Programme – Corporate Bank", "Test", "2026-10-23",
                "Online test submitted 1 Nov; invited to job simulation round (told 5 Nov 2026). Check the invite email for the deadline and complete it.", "https://www.linkedin.com/jobs/view/4469303814/"),
        app_row("CP Axtra (Makro) — ⭐ Future Leader Program 21 (Makro).md", "CP Axtra (Makro)", "⭐ Future Leader Program 21 (Makro)", "Applied", "2026-10-23",
                "Applied Oct 23, 2026 via LinkedIn, marked as top choice. Watch email for the online test or assessment.", "https://www.linkedin.com/jobs/view/4475738833"),
        app_row("CP Axtra (Lotus's) — ⭐ Future Leader Program 21 (Lotus's).md", "CP Axtra (Lotus's)", "⭐ Future Leader Program 21 (Lotus's)", "Applied", "2026-10-23",
                "Applied Oct 23, 2026 via LinkedIn. Watch email for the online test or assessment.", "https://www.linkedin.com/jobs/view/4471315797"),
        app_row("CPF (CP Group) — ⭐ Future Leader Program 21.md", "CPF (CP Group)", "⭐ Future Leader Program 21", "Applied", "2026-10-23",
                "Watch alex.rivera@example.com (incl. spam) for Test Set 1 email; applied Commercial track, start after graduation (Jun 2027)", "https://www.linkedin.com/jobs/view/4466833156"),
        app_row("AlphaSights (Hong Kong) — Associate, Client Service, Thai Speaker, 2027.md", "AlphaSights (Hong Kong)", "Associate, Client Service, Thai Speaker, 2027", "Rejected", "2026-10-09",
                "Closed — not passed (stated 1 Nov, reconfirmed 5 Nov 2026). Reason not given.", "https://grnh.se/qiox333l1us"),
    ]
    alpha = next(a for a in applied if a["company"].startswith("AlphaSights"))
    alpha.update({"outcome_stage": "unknown", "outcome_reason_category": "no_reason_given", "outcome_reason": "Closed, not passed. Reason not given.", "outcome_date": "2026-11-01"})
    # Synthetic records (dev only) that the M6 suggestion cards point at; real records never get fake employer emails.
    applied += [
        app_row("Example Retail Co. — Future Leader Program.md", "Example Retail Co.", "Future Leader Program", "Applied", "2026-10-20",
                "Watch email for the online test.", "https://example.invalid/jobs/retail"),
        app_row("Example Bank (Singapore) — Graduate Programme.md", "Example Bank (Singapore)", "Graduate Programme", "Applied", "2026-10-18",
                "Watch email for the next step.", "https://example.invalid/jobs/bank"),
        app_row("Example FMCG Co. — Management Trainee (Sales).md", "Example FMCG Co.", "Management Trainee (Sales)", "Test", "2026-10-12",
                "Online test submitted 30 Oct.", "https://example.invalid/jobs/fmcg"),
        app_row("Example Logistics Co. — MT Program.md", "Example Logistics Co.", "MT Program", "Applied", "2026-10-02",
                "Watch email for a reply.", "https://example.invalid/jobs/logistics"),
        dict(app_row("Example Consulting — Business Analyst.md", "Example Consulting", "Business Analyst", "Rejected", "2026-09-28",
                     "Closed.", "https://example.invalid/jobs/consulting"), section="Archive",
             outcome_stage="online_test", outcome_reason_category="test_score", outcome_reason="Did not pass the numerical test cut-off.", outcome_date="2026-10-15",
             _ikigai="L4 G3 N4 P4e", _weakest="good_at"),
        dict(app_row("Example Telecom — Graduate MT.md", "Example Telecom", "Graduate MT", "Ghosted", "2026-09-20",
                     "Closed: no reply after 30 days.", "https://example.invalid/jobs/telecom"), section="Archive",
             outcome_stage="cv_screen", outcome_reason_category="ghosted", outcome_reason="No reply 30 days after applying.", outcome_date="2026-10-20",
             _ikigai="L3 G3 N3 P2e", _weakest="paid"),
    ]
    applied.sort(key=lambda a: a["date_applied"] or "", reverse=True)
    suggestions = [
        {"id": 31, "record_file": "61 Application Records/Example Retail Co. — Future Leader Program.md", "company": "Example Retail Co.", "program": "Future Leader Program",
         "type": "rejection", "proposed": {"my_status": "Rejected", "outcome_stage": "cv_screen", "outcome_reason_category": "start_date", "next_step": "None, closed."},
         "excerpt": "Thank you for your interest. We are looking for candidates who can start in January 2027, so we will not move forward with your application at this time.",
         "email": {"from": "Talent Team <careers@example-retail.invalid>", "subject": "Your application: Future Leader Program", "date": "2026-11-06T09:12:00Z"},
         "detected_at": "2026-11-07T00:01:00Z"},
        {"id": 32, "record_file": "61 Application Records/Example Bank (Singapore) — Graduate Programme.md", "company": "Example Bank (Singapore)", "program": "Graduate Programme",
         "type": "test_invite", "proposed": {"my_status": "Test", "outcome_stage": "online_test", "outcome_reason_category": None, "next_step": "Complete the SOVA assessment by 14 Nov."},
         "excerpt": "We are pleased to invite you to complete our online assessment. Please complete it by 14 November 2026, 23:59 SGT.",
         "email": {"from": "Example Bank Graduate Recruitment <noreply@sova.invalid>", "subject": "Next step: online assessment", "date": "2026-11-06T02:40:00Z"},
         "detected_at": "2026-11-07T00:01:00Z"},
        {"id": 33, "record_file": "61 Application Records/Example FMCG Co. — Management Trainee (Sales).md", "company": "Example FMCG Co.", "program": "Management Trainee (Sales)",
         "type": "interview_invite", "proposed": {"my_status": "Interview", "outcome_stage": "interview", "outcome_reason_category": None, "next_step": "Book an interview slot (link in the email)."},
         "excerpt": "Congratulations, you have passed the online test. Please choose a time for a 30-minute video interview using the link below.",
         "email": {"from": "Example FMCG Careers <talent@example-fmcg.invalid>", "subject": "Interview invitation", "date": "2026-11-05T11:05:00Z"},
         "detected_at": "2026-11-07T00:01:00Z"},
        {"id": 34, "record_file": "61 Application Records/Example Logistics Co. — MT Program.md", "company": "Example Logistics Co.", "program": "MT Program",
         "type": "ghosted", "proposed": {"my_status": "Ghosted", "outcome_stage": "cv_screen", "outcome_reason_category": "ghosted", "next_step": "None, closed."},
         "excerpt": None, "email": None, "detected_at": "2026-11-07T00:01:00Z"},
    ]
    def lesson(a, ikigai=None, weakest=None):
        return {"company": a["company"], "program": a["program"], "my_status": a["my_status"], "outcome_stage": a.get("outcome_stage"),
                "outcome_reason_category": a.get("outcome_reason_category"), "outcome_reason": a.get("outcome_reason"),
                "outcome_date": a.get("outcome_date"), "ikigai": ikigai, "weakest_circle": weakest}
    lessons = [lesson(a, a.get("_ikigai"), a.get("_weakest")) for a in applied if a["my_status"] in ("Rejected", "Ghosted")]
    public = lambda rows: [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]

    to_apply = [
        app_row("P&G Thailand — ⭐ Account Manager, Customer Business Development.md", "P&G Thailand", "⭐ Account Manager, Customer Business Development", "To apply", None,
                "Submit via the apply link. Cover email draft: " + PG_DRAFT_PATH, "https://example.wd3.myworkdayjobs.com/en-US/careers/job/112", 112),
        app_row("Agoda — Associate Analyst, Marketing (Bangkok).md", "Agoda", "Associate Analyst, Marketing", "To apply", None,
                "GATE: ask if a June 2027 start is possible before applying", "https://job-boards.greenhouse.io/agoda/jobs/8194690", 2),
        app_row("BCG — ⭐ Associate, Thailand.md", "BCG", "⭐ Associate, Thailand", "To apply", None,
                "Request alumni referral first, then apply on BCG careers; ask about a Bangkok June 2027 start", "https://www.linkedin.com/jobs/view/4423910715"),
        app_row("Alvarez & Marsal — Graduate hires – Performance Improvement.md", "Alvarez & Marsal", "Graduate hires – Performance Improvement", "To apply", None,
                "Request alumni referral first, then submit; ask about a June 2027 start", "https://www.linkedin.com/jobs/view/4440998313"),
    ]
    # A real "To apply" record outside "Apply now": excluded from the To apply tab (contract change 7 Nov 2026).
    blocked = app_row("ABeam Consulting — Graduate Analyst Program (July 2027 intake).md", "ABeam Consulting", "Graduate Analyst Program (July 2027 intake)", "To apply", None,
                      "GATE: GPA ≥2.75 – check transcript first; if below, ask ABeam about an exception before applying", "https://www.linkedin.com/jobs/view/4469393745", section="Blocked")
    for j in jobs:
        if j["id"] == 112:
            j["status"] = "to_apply"
    site_changes = [
        {"id": 7, "company": "CP Axtra", "url": "https://www.cpaxtra.com/en/careers", "detected_at": "2026-11-07T00:02:00Z",
         "new_links": [{"href": "https://www.cpaxtra.com/en/careers/future-leader-22", "text": "Future Leader Program 22 - register interest"}, {"href": "https://careers.example-retail.invalid/job/Bangkok-Retail-Management-Trainee-13976/59380144/", "text": "59380144"}]},
        {"id": 8, "company": "Boon Rawd Brewery", "url": "https://www.boonrawd.co.th/careers", "detected_at": "2026-11-06T00:03:00Z", "new_links": []},
    ]
    last_run = {"at": "2026-11-07T00:00:41Z", "sources": {
        "gmail": {"found": 23, "new": 9, "prefiltered": 3, "scored": 6, "errors": 0},
        "linkedin": {"found": 61, "new": 14, "prefiltered": 6, "scored": 8, "errors": 1},
        "jobsdb": {"found": 48, "new": 11, "prefiltered": 4, "scored": 7, "errors": 0},
        "greenhouse": {"found": 112, "new": 3, "prefiltered": 2, "scored": 1, "errors": 0},
        "workday": {"found": 0, "new": 0, "prefiltered": 0, "scored": 0, "errors": 1}}}

    os.makedirs(OUT, exist_ok=True)
    for name in os.listdir(OUT):
        if name.endswith(".json"):
            os.remove(os.path.join(OUT, name))

    def dump(name, obj):
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
            fh.write("\n")

    dump("cover-email-112.json", {"ok": True, "cover_email": PG_DRAFT})
    dump("apply-created.json", {"ok": True, "created": True, "already_tracked": False,
                                "record_file": "61 Application Records/P&G Thailand — ⭐ Account Manager, Customer Business Development.md",
                                "obsidian_url": obsidian("61 Application Records/P&G Thailand — ⭐ Account Manager, Customer Business Development.md"),
                                "cover_email": PG_DRAFT})
    dump("apply-created-cover-failed.json", {"ok": True, "created": True, "already_tracked": False,
                                             "record_file": "61 Application Records/Example.md", "obsidian_url": obsidian("61 Application Records/Example.md"),
                                             "cover_email": None, "cover_email_error": "codex is unreachable"})
    def agg(key):
        out = {}
        for l in lessons:
            k = l[key] or ("unknown" if key != "weakest_circle" else "null")
            out[k] = out.get(k, 0) + 1
        return out
    lessons_res = {"lessons": lessons, "by_stage": agg("outcome_stage"), "by_category": agg("outcome_reason_category"), "by_weakest_circle": agg("weakest_circle")}
    by_file = {a["record_file"]: a for a in applied}
    for sg in suggestions:
        sg["current_status"] = by_file.get(sg["record_file"], {}).get("my_status")
    dump("suggestions.json", {"items": suggestions})
    dump("lessons.json", lessons_res)
    dump("_db.json", {"suggestions": suggestions, "drafts": {"112": PG_DRAFT}, "jobs": jobs, "applications": to_apply + [blocked] + applied, "site_changes": site_changes, "last_run": last_run})
    tabs = {}
    for t in ("all", "centre", "hold", "low", "filtered", "skipped"):
        rows = tab_jobs(jobs, t)
        tabs[t] = len(rows)
        dump(f"jobs-{t}.json", {"total": len(rows), "jobs": rows[:100]})
    tabs["to_apply"], tabs["applied"] = len(to_apply), len(applied)
    tabs["lessons"] = sum(1 for a in applied if a["my_status"] in ("Rejected", "Ghosted"))
    for j in jobs:
        dump(f"job-{j['id']}.json", j)
    dump("applications-to_apply.json", {"applications": public(to_apply)})
    dump("applications-applied.json", {"applications": public(applied)})
    dump("site-changes.json", {"items": site_changes})
    live = [j for j in jobs if j["status"] == "new" and j["verdict"] in ("CENTRE", "HOLD", "LOW")]
    reasons = {}
    for j in jobs:
        if j["status"] == "new" and j["verdict"] == "FILTERED":
            for r in (j["gate"]["fails"] or [j["prefilter_reason"]]):
                key = r.split(";")[0]
                reasons[key] = reasons.get(key, 0) + 1
    dump("stats.json", {
        "last_run": last_run, "tabs": tabs,
        "shortfall": {c: sum(1 for j in live if j["scores"][c] is not None and j["scores"][c] < 3) for c in CIRCLES},
        "filtered_top_reasons": [{"reason": r, "count": n} for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:3]],
        "site_changes_unseen": len(site_changes), "suggestions_pending": len(suggestions),
        "unscored": sum(1 for j in jobs if j["verdict"] is None)})
    print(f"wrote {len(os.listdir(OUT))} files to {OUT}; tabs={tabs}")


if __name__ == "__main__":
    main()
