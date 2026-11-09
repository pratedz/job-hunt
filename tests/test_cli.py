import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _util import ROOT

from jobhunt.cli import main


def run_cli(*args):
    """Run the CLI, returning (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class AddCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.sqlite")

    def test_json_file_is_ingested_once_and_prefiltered(self):
        path = self.dir / "jobs.json"
        path.write_text(json.dumps([
            {"source": "manual", "title": "Management Trainee", "company": "Acme",
             "location": "Bangkok", "jd_text": "A rotational programme."},
            {"source": "linkedin", "source_job_id": "1", "title": "Senior Analyst", "company": "Beta"},
        ]), encoding="utf-8")
        code, out, _ = run_cli("--db", self.db, "add", "--json", str(path))
        self.assertEqual(code, 0)
        self.assertIn("2 added", out)
        self.assertIn("1 pre-filtered", out)
        code, out, _ = run_cli("--db", self.db, "add", "--json", str(path))
        self.assertIn("0 added", out)
        self.assertIn("2 already known", out)

    def test_the_lead_check_set_loads(self):
        code, out, _ = run_cli("--db", self.db, "add", "--json", str(ROOT / "tests" / "fixtures" / "checkset.json"))
        self.assertEqual(code, 0)
        self.assertIn("7 added", out)
        self.assertIn("1 pre-filtered", out)

    def test_single_job_from_a_jd_file(self):
        jd = self.dir / "jd.txt"
        jd.write_text("Management Trainee, Bangkok, 2027 graduates.", encoding="utf-8")
        code, out, _ = run_cli("--db", self.db, "add", "--url", "https://example.com/jobs/1",
                               "--title", "Management Trainee", "--company", "Acme",
                               "--location", "Bangkok", "--jd-file", str(jd))
        self.assertEqual(code, 0)
        self.assertIn("1 added", out)
        code, out, _ = run_cli("--db", self.db, "add", "--url", "https://www.example.com/jobs/1/?utm_source=x",
                               "--title", "Management Trainee", "--company", "Acme",
                               "--location", "Bangkok")
        self.assertIn("0 added", out)
        self.assertIn("already known", out)

    def test_title_is_required_for_a_single_job(self):
        jd = self.dir / "jd.txt"
        jd.write_text("x", encoding="utf-8")
        code, _, err = run_cli("--db", self.db, "add", "--jd-file", str(jd))
        self.assertEqual(code, 2)
        self.assertIn("--title is required", err)

    def test_a_bad_source_is_rejected_before_storage(self):
        path = self.dir / "bad.json"
        path.write_text('[{"source": "monster", "title": "X"}]', encoding="utf-8")
        code, _, err = run_cli("--db", self.db, "add", "--json", str(path))
        self.assertEqual(code, 2)
        self.assertIn("job_input.schema.json", err)

    def test_a_missing_json_file_is_reported(self):
        code, _, err = run_cli("--db", self.db, "add", "--json", str(self.dir / "nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)


class FetchJdCommand(unittest.TestCase):
    """fetch-jd against a fake transport: no network, no real data folder."""

    def setUp(self):
        import jobhunt.cli as cli
        from _fake import FakeClock, FakeTransport, fixture
        from jobhunt.http import PoliteFetcher

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.sqlite")

        transport = FakeTransport({"th.jobsdb.com/job/": (200, fixture("jobsdb_job_95340875.html"), {})})
        clock = FakeClock()
        original = cli._fetcher
        cli._fetcher = lambda cfg: PoliteFetcher(cfg.profile, transport=transport,
                                                 sleep=clock.sleep, now=clock.now)
        self.addCleanup(setattr, cli, "_fetcher", original)
        self.transport = transport

        jobs = self.dir / "jobs.json"
        jobs.write_text(json.dumps([{
            "source": "jobsdb", "source_job_id": "95340875",
            "title": "Fresh Graduate Management Trainee Program (MT)",
            "company": "Deliveree (Thailand) Limited", "location": "Bangkok",
        }]), encoding="utf-8")
        run_cli("--db", self.db, "add", "--json", str(jobs))

    def test_the_backlog_is_fetched_and_reported(self):
        code, out, _ = run_cli("--db", self.db, "fetch-jd")
        self.assertEqual(code, 0)
        self.assertIn("1 attempted, 1 fetched, 0 missing", out)
        self.assertIn("jobsdb: fetched 1, missing 0", out)
        self.assertIn("0 job(s) still without a description", out)

    def test_the_source_filter_keeps_other_sources_alone(self):
        code, out, _ = run_cli("--db", self.db, "fetch-jd", "--source", "linkedin")
        self.assertEqual(code, 0)
        self.assertIn("nothing to fetch", out)
        self.assertEqual(self.transport.calls, [])


class ListAndShow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.sqlite")
        run_cli("--db", self.db, "add", "--json", str(ROOT / "tests" / "fixtures" / "checkset.json"))

    def test_list_is_empty_of_scores_before_scoring_but_shows_the_prefiltered_job(self):
        code, out, _ = run_cli("--db", self.db, "list")
        self.assertEqual(code, 0)
        self.assertIn("FILTERED", out)
        self.assertEqual(out.count("\n"), 2)   # header plus the one pre-filtered job

    def test_fake_score_then_list_json(self):
        code, out, _ = run_cli("--db", self.db, "score", "--fake")
        self.assertEqual(code, 0)
        self.assertIn("6 scored", out)
        code, out, _ = run_cli("--db", self.db, "list", "--json")
        rows = json.loads(out)
        self.assertEqual(len(rows), 7)
        self.assertEqual(sum(1 for r in rows if r["verdict"] == "FILTERED"), 1)
        self.assertTrue(all(r["love"] is None for r in rows))

    def test_score_is_idempotent(self):
        run_cli("--db", self.db, "score", "--fake")
        code, out, _ = run_cli("--db", self.db, "score", "--fake")
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out)

    def test_fetch_jd_with_an_empty_backlog_makes_no_request(self):
        # every job in the checkset already carries a description
        code, out, _ = run_cli("--db", self.db, "fetch-jd")
        self.assertEqual(code, 0)
        self.assertIn("nothing to fetch", out)

    def test_fetch_jd_takes_a_source_and_a_limit(self):
        from jobhunt.cli import build_parser
        args = build_parser().parse_args(["fetch-jd", "--source", "jobsdb", "--limit", "5"])
        self.assertEqual((args.source, args.limit), ("jobsdb", 5))

    def test_show_reports_an_unknown_job(self):
        code, _, err = run_cli("--db", self.db, "show", "999")
        self.assertEqual(code, 2)
        self.assertIn("no job 999", err)


if __name__ == "__main__":
    unittest.main()
