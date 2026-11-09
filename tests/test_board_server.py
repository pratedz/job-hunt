"""The real HTTP layer: routes, the POST guard, static files, and the paste path."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from _fake import FakeTransport
from _util import CFG
from _vault import TODAY, make_vault, scored_reply, seeded_db

from jobhunt import board_server
from jobhunt.http import PoliteFetcher
from jobhunt.llm import FakeLLM


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.vault = make_vault(self.dir)
        self.db = self.dir / "board.sqlite"
        self.conn, self.ids = seeded_db(self, path=self.db)

        self.llm = FakeLLM([lambda prompt, ids: scored_reply(ids)])
        self.transport = FakeTransport({"th.jobsdb.com/job/": (404, "", {})})
        self.server = board_server.create_server(
            CFG, db_path=self.db, vault_path=self.vault.root, port=0,
            llm_factory=lambda: self.llm,
            fetcher_factory=lambda: PoliteFetcher(CFG.profile, transport=self.transport,
                                                  sleep=lambda s: None, now=lambda: 0.0),
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as response:
            return response.status, response.headers.get("Content-Type"), response.read()

    def post(self, path, body=None, header=True):
        data = json.dumps(body or {}).encode()
        request = urllib.request.Request(self.url(path), data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        if header:
            request.add_header("X-Requested-With", "jobhunt")
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())

    def expect_error(self, path, method="GET", body=None, header=True):
        data = json.dumps(body or {}).encode() if method == "POST" else None
        request = urllib.request.Request(self.url(path), data=data, method=method)
        if header:
            request.add_header("X-Requested-With", "jobhunt")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class StaticFiles(ServerCase):
    def test_the_board_index_is_served(self):
        status, content_type, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"<", body)

    def test_javascript_is_served_with_the_right_type(self):
        status, content_type, body = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/javascript; charset=utf-8")
        self.assertGreater(len(body), 1000)

    def test_the_css_is_served(self):
        status, content_type, _ = self.get("/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)

    def test_a_missing_file_is_a_json_404(self):
        status, payload = self.expect_error("/nope.js")
        self.assertEqual(status, 404)
        self.assertEqual(payload["ok"], False)

    def test_a_path_traversal_is_refused(self):
        for path in ("/../pair/M1.md", "/..%2F..%2Fetc%2Fpasswd", "/%2e%2e/CLAUDE.md"):
            status, payload = self.expect_error(path)
            self.assertIn(status, (403, 404), path)
            self.assertEqual(payload["ok"], False, path)

    def test_only_get_is_allowed_for_files(self):
        status, payload = self.expect_error("/index.html", method="POST", header=True)
        self.assertEqual(status, 405)


class Api(ServerCase):
    def test_stats(self):
        status, content_type, body = self.get("/api/stats")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(payload["tabs"]["all"], 5)
        self.assertEqual(payload["unscored"], 3)
        self.assertIn("shortfall", payload)

    def test_jobs_list_and_detail(self):
        payload = json.loads(self.get("/api/jobs?tab=centre")[2])
        self.assertEqual(payload["total"], 2)
        job_id = payload["jobs"][0]["id"]
        detail = json.loads(self.get(f"/api/jobs/{job_id}")[2])
        self.assertEqual(detail["reasons"]["love"], "love reason")

    def test_an_unknown_tab_is_a_400_with_a_message(self):
        status, payload = self.expect_error("/api/jobs?tab=nonsense")
        self.assertEqual(status, 400)
        self.assertIn("Unknown tab", payload["error"])

    def test_an_unknown_job_is_a_404(self):
        status, payload = self.expect_error("/api/jobs/9999")
        self.assertEqual(status, 404)

    def test_skip_needs_the_guard_header(self):
        status, payload = self.expect_error(f"/api/jobs/{self.ids['low']}/skip", method="POST",
                                            header=False)
        self.assertEqual(status, 403)
        self.assertIn("X-Requested-With", payload["error"])

    def test_skip_with_the_header_works(self):
        status, payload = self.post(f"/api/jobs/{self.ids['low']}/skip")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "status": "skipped"})
        skipped = json.loads(self.get("/api/jobs?tab=skipped")[2])
        self.assertIn(self.ids["low"], [job["id"] for job in skipped["jobs"]])

    def test_apply_then_submitted_over_http(self):
        status, payload = self.post(f"/api/jobs/{self.ids['low']}/apply")
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        record_file = payload["record_file"]

        status, done = self.post("/api/applications/submitted", {"record_file": record_file})
        self.assertEqual((status, done), (200, {"ok": True}))
        applied = json.loads(self.get("/api/applications?status=applied")[2])["applications"]
        self.assertIn("Acme", [row["company"] for row in applied])

    def test_paste_over_http_uses_the_injected_model(self):
        status, payload = self.post("/api/paste", {"jd_text": "A graduate programme in Bangkok.",
                                                   "title": "Pasted Trainee",
                                                   "company": "Pasted Co"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "scored")
        self.assertTrue(payload["job_id"])
        self.assertEqual(len(self.llm.calls), 1)

    def test_paste_without_anything_is_a_400(self):
        status, payload = self.expect_error("/api/paste", method="POST", body={})
        self.assertEqual(status, 400)
        self.assertIn("URL", payload["error"])

    def test_a_bad_json_body_is_a_400(self):
        request = urllib.request.Request(self.url("/api/jobs/1/skip"), data=b"{nope",
                                         method="POST")
        request.add_header("X-Requested-With", "jobhunt")
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("expected an error")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertFalse(json.loads(exc.read())["ok"])

    def test_site_changes_round_trip(self):
        from jobhunt import store
        conn = store.connect(self.db)
        change_id = store.add_site_change(conn, "CP Axtra", "https://example.test/careers",
                                          [{"href": "https://example.test/flp", "text": "FLP"}])
        conn.close()
        items = json.loads(self.get("/api/site-changes")[2])["items"]
        self.assertEqual(items[0]["id"], change_id)
        status, payload = self.post(f"/api/site-changes/{change_id}/seen")
        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertEqual(json.loads(self.get("/api/site-changes")[2])["items"], [])

    def test_unknown_api_routes_are_json_404s(self):
        status, payload = self.expect_error("/api/nothing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["ok"], False)


class Binding(unittest.TestCase):
    def test_the_server_only_binds_localhost(self):
        server = board_server.create_server(
            CFG, db_path=Path(tempfile.mkdtemp()) / "x.sqlite",
            vault_path=Path(tempfile.mkdtemp()), port=0, host="0.0.0.0")
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()

class DailyRoutes(ServerCase):
    def test_daily_post_requires_same_guard_as_other_mutations(self):
        code, result = self.expect_error('/api/daily', method='POST', header=False)
        self.assertEqual(code, 403)

    def test_daily_routes_use_background_control_without_real_run(self):
        from dataclasses import replace
        from jobhunt.board_daily import DailyControl
        started = threading.Event()
        release = threading.Event()
        def runner(log):
            started.set()
            release.wait(3)
        control = DailyControl(replace(CFG, data_root=self.dir), self.db, self.vault.root, runner=runner)
        self.server.app.daily = control
        status, _, body = self.get('/api/daily')
        self.assertEqual(json.loads(body)['phase'], 'not_run')
        status, body = self.post('/api/daily')
        self.assertEqual(body['phase'], 'running')
        self.assertTrue(started.wait(1))
        self.assertEqual(self.post('/api/daily')[1]['phase'], 'running')
        release.set()
        control.thread.join(3)
