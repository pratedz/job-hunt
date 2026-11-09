"""Tests for Career Wiki onboarding, resume text extraction, and OAuth provider selection."""

from __future__ import annotations

import base64
import io
import shutil
import tempfile
import tomllib
import unittest
import zipfile
import zlib
from pathlib import Path

from _util import ROOT

from jobhunt import config as config_mod
from jobhunt import resume as resume_mod
from jobhunt import validate as schema_check
from jobhunt import wiki_setup
from jobhunt.board_server import BoardApp
from jobhunt.llm import FakeLLM


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    buf = io.BytesIO()
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml.encode("utf-8"))
    return buf.getvalue()


def _make_flate_pdf_bytes(text_line: str) -> bytes:
    stream_body = f"BT /F1 12 Tf 72 720 Td ({text_line}) Tj ET".encode("latin-1")
    compressed = zlib.compress(stream_body)
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Length "
        + str(len(compressed)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n%%EOF\n"
    )


def _sample_ai_wiki_reply() -> dict:
    return {
        "candidate": {
            "name": "Nicha Srisawat",
            "email": "nicha@example.com",
            "degree": "B.Eng., Computer Engineering",
            "gpa": 3.45,
            "graduation_year": 2026,
            "degree_completed": True,
            "fulltime_experience_years": 1.5,
            "languages": ["Thai", "English"],
            "test_scores": [{"exam": "IELTS", "score": 7.5}],
            "nationality": "Thai",
            "based_in": "Bangkok, Thailand",
        },
        "gates": {
            "pass_max_years": 2,
            "ask_max_years": 4,
            "available_th": "2027-01",
            "available_abroad": "2027-02",
            "pass_cities": ["Bangkok"],
            "pass_countries": ["Thailand", "Singapore"],
            "remote": "pass",
            "internship": "fail",
            "contract": "ask",
        },
        "paid": {
            "anchors_thb_month": [45000, 60000, 80000, 105000],
        },
        "search": {
            "keywords": ["product manager", "associate product manager", "business analyst"],
            "title_exclude_extra": ["intern", "telesales"],
        },
        "facts_md": (
            "# Verified candidate facts\n\n"
            "## Identity & Contact\n"
            "- **F01**: Nicha Srisawat — B.Eng. Computer Engineering graduate based in Bangkok.\n"
            "- **F02**: Contact: nicha@example.com\n\n"
            "## Experience & Projects\n"
            "- **F10**: Associate Product Analyst at CloudCart (18 months): shipped checkout redesign lifting conversion by 14%.\n\n"
            "## Skills & Education\n"
            "- **F30**: SQL, Python, Amplitude, bilingual Thai and English (IELTS 7.5).\n\n"
            "## Banned phrases\n"
            "- `passionate self-starter` / `synergy` / `rockstar` / `engineered`\n"
        ),
        "rubric_md": (
            "# Ikigai Scoring Rubric\n\n"
            "## What you love (1 to 5)\n"
            "- **5**: Product Management or Product Analytics at consumer tech platforms.\n"
            "- **1**: Cold-calling sales or pure IT support.\n\n"
            "## What you are good at (1 to 5)\n"
            "- **5**: Directly matches F10 and F30 product analytics & funnel experimentation.\n\n"
            "## What the world needs (1 to 5)\n"
            "- **5**: Strong product mentorship, clear PM career ladder, cross-functional ownership.\n"
        ),
        "wiki_summary": "Built your Career Wiki from your CloudCart product experience, set Bangkok & Singapore gates, and tuned Ikigai anchors to Product Management roles.",
        "follow_up_questions": [
            "Did your checkout redesign at CloudCart impact monthly GMV or order volume with a specific number we can add to F10?",
            "Are hybrid roles requiring 4+ days in office acceptable, or do you prefer flexible hybrid?",
        ],
    }


class ResumeExtraction(unittest.TestCase):
    def test_plain_text_and_markdown(self):
        raw = b"# Jane Doe\nProduct Manager with 3 years of SQL and experimentation experience."
        b64 = base64.b64encode(raw).decode("ascii")
        decoded = resume_mod.decode_base64_upload(f"data:text/markdown;base64,{b64}")
        text = resume_mod.extract_text("resume.md", decoded)
        self.assertIn("Jane Doe", text)
        self.assertIn("Product Manager", text)

    def test_docx_extraction_uses_stdlib_zip_xml(self):
        raw = _make_docx_bytes([
            "Nicha Srisawat — Bangkok, Thailand",
            "CloudCart: Shipped checkout funnel improvements (+14% conversion).",
        ])
        text = resume_mod.extract_text("cv.docx", raw)
        self.assertIn("Nicha Srisawat", text)
        self.assertIn("+14% conversion", text)

    def test_pdf_stdlib_flate_stream_extractor(self):
        raw = _make_flate_pdf_bytes("Nicha Srisawat - Associate Product Manager in Bangkok Thailand")
        text = resume_mod._extract_pdf_stdlib(raw)
        self.assertIn("Nicha Srisawat", text)
        self.assertIn("Associate Product Manager", text)

    def test_empty_or_oversized_upload_raises_resume_error(self):
        with self.assertRaises(resume_mod.ResumeError):
            resume_mod.decode_base64_upload("")
        with self.assertRaises(resume_mod.ResumeError):
            resume_mod.extract_text("tiny.txt", b"too short")


class OnboardingSchemaAndBoardRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "workspace"
        (self.project / "config").mkdir(parents=True)
        (self.project / "schemas").mkdir(parents=True)
        (self.project / "board").mkdir(parents=True)
        for p in (ROOT / "config").glob("*.example.*"):
            shutil.copy2(p, self.project / "config" / p.name)
        shutil.copy2(ROOT / "config" / "employer-universe.json", self.project / "config" / "employer-universe.json")
        for p in (ROOT / "schemas").glob("*.json"):
            shutil.copy2(p, self.project / "schemas" / p.name)

        self.cfg = config_mod.load(self.project)
        self.db_path = self.project / "data" / "jobhunt.sqlite"

        class FakeProbe:
            returncode = 0
            stdout = '{"loggedIn": true, "authMethod": "oauth"}'
            stderr = ""

        self.reply = _sample_ai_wiki_reply()
        self.fake_llm = FakeLLM([self.reply])
        self.app = BoardApp(
            self.cfg,
            self.db_path,
            llm_factory=lambda: self.fake_llm,
            llm_probe_runner=lambda *a, **k: FakeProbe(),
        )
        self.addCleanup(self.app.pool.close)

    def test_sample_reply_conforms_to_onboarding_schema(self):
        schema = self.cfg.onboarding_schema()
        schema_check.validate(schema, self.reply)

    def test_initial_get_wiki_is_unconfigured_and_lists_three_providers(self):
        res = self.app.handle("GET", "/api/wiki", {}, None)
        self.assertTrue(res["ok"])
        self.assertFalse(res["configured"])
        self.assertTrue(res["vault"]["builtin"])
        self.assertEqual([p["id"] for p in res["llm"]["providers"]], ["codex", "claude", "gemini"])

    def test_post_provider_persists_single_ai_subscription_and_preserves_companies(self):
        res = self.app.handle("POST", "/api/wiki/provider", {}, {"provider": "claude", "model": ""})
        self.assertTrue(res["ok"])
        self.assertEqual(res["llm"]["provider"], "claude")
        parsed = tomllib.loads((self.project / "config" / "profile.toml").read_text(encoding="utf-8"))
        self.assertEqual(parsed["llm"]["provider"], "claude")
        self.assertGreater(len(parsed.get("companies", [])), 5)

    def test_extract_generate_and_save_wiki_end_to_end(self):
        docx_b64 = base64.b64encode(_make_docx_bytes([
            "Nicha Srisawat | nicha@example.com | Bangkok",
            "Associate Product Analyst at CloudCart — lifted checkout conversion by 14%.",
        ])).decode("ascii")
        ext = self.app.handle("POST", "/api/wiki/extract-resume", {}, {
            "filename": "Nicha_Resume.docx",
            "data_base64": docx_b64,
        })
        self.assertTrue(ext["ok"])
        self.assertIn("CloudCart", ext["text"])

        gen = self.app.handle("POST", "/api/wiki/generate", {}, {
            "resume_text": ext["text"],
            "qa": {
                "target_roles": "Product Manager and Associate Product Manager roles in consumer tech",
                "compensation": "At least 60,000 THB/month, targeting 80,000+ THB/month",
            },
        })
        self.assertTrue(gen["ok"])
        generated = gen["generated"]
        self.assertEqual(generated["candidate"]["name"], "Nicha Srisawat")
        self.assertEqual(generated["candidate"]["test_scores"], {"IELTS": 7.5})

        saved = self.app.handle("POST", "/api/wiki/save", {}, {
            "llm": {"provider": "gemini", "model": ""},
            "candidate": generated["candidate"],
            "gates": generated["gates"],
            "paid": generated["paid"],
            "search": generated["search"],
            "facts_md": generated["facts_md"],
            "rubric_md": generated["rubric_md"],
        })
        self.assertTrue(saved["ok"])
        self.assertTrue(saved["configured"])
        self.assertEqual(saved["candidate"]["name"], "Nicha Srisawat")
        self.assertEqual(saved["llm"]["provider"], "gemini")
        self.assertTrue((self.project / "config" / "facts.md").is_file())
        self.assertTrue((self.project / "config" / "rubric.md").is_file())
        self.assertTrue((self.project / "data" / "wiki" / "61 Application Records").is_dir())


if __name__ == "__main__":
    unittest.main()
