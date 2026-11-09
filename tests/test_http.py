import unittest
from pathlib import Path

from _fake import FakeClock, FakeTransport
from _util import profile

from jobhunt.http import (CURL, CurlTransport, FetchError, HostStopped, PoliteFetcher,
                          USER_AGENT)


def fetcher(routes=None, **overrides):
    clock = FakeClock()
    transport = FakeTransport(routes)
    overrides.setdefault("limits.min_seconds_between_requests", 3)
    p = PoliteFetcher(profile(**overrides), transport=transport, sleep=clock.sleep, now=clock.now)
    return p, transport, clock


class Politeness(unittest.TestCase):
    def test_requests_to_the_same_host_are_spaced(self):
        f, transport, clock = fetcher({"example.com": (200, "ok", {})})
        f.get("https://example.com/a")
        self.assertEqual(clock.sleeps, [])
        f.get("https://example.com/b")
        self.assertEqual(clock.sleeps, [3.0])
        self.assertEqual(len(transport.calls), 2)

    def test_a_slow_caller_does_not_wait(self):
        p, transport, clock = fetcher({"example.com": (200, "ok", {})})
        p.get("https://example.com/a")
        clock.time += 10          # the caller did something slow in between
        p.get("https://example.com/b")
        self.assertEqual(clock.sleeps, [])

    def test_different_hosts_do_not_wait_for_each_other(self):
        p, transport, clock = fetcher({"example.com": (200, "ok", {})})
        p.get("https://example.com/a")
        p.get("https://other.test/b")
        self.assertEqual(clock.sleeps, [])


class HostCaps(unittest.TestCase):
    def test_the_default_cap_stops_the_host(self):
        p, transport, clock = fetcher({"example.com": (200, "ok", {})},
                                      **{"limits.max_requests_per_host_per_run": 2})
        p.get("https://example.com/1")
        p.get("https://example.com/2")
        with self.assertRaises(HostStopped) as ctx:
            p.get("https://example.com/3")
        self.assertIn("cap", ctx.exception.reason)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(p.stopped_hosts(), {"example.com": "request cap reached (2)"})

    def test_a_per_host_override_allows_more(self):
        p, transport, clock = fetcher({"linkedin.com": (200, "ok", {})},
                                      **{"limits.max_requests_per_host_per_run": 1,
                                         "limits.per_host": {"www.linkedin.com": 3}})
        for i in range(3):
            p.get(f"https://www.linkedin.com/{i}")
        self.assertEqual(len(transport.calls), 3)
        with self.assertRaises(HostStopped):
            p.get("https://www.linkedin.com/4")

    def test_the_override_does_not_leak_to_other_hosts(self):
        p, transport, clock = fetcher({"example.com": (200, "ok", {})},
                                      **{"limits.max_requests_per_host_per_run": 1,
                                         "limits.per_host": {"www.linkedin.com": 150}})
        p.get("https://example.com/1")
        with self.assertRaises(HostStopped):
            p.get("https://example.com/2")

    def test_a_stopped_host_is_never_asked_again(self):
        p, transport, clock = fetcher({"example.com": (429, "slow down", {})})
        with self.assertRaises(HostStopped):
            p.get("https://example.com/a")
        with self.assertRaises(HostStopped):
            p.get("https://example.com/b")
        self.assertEqual(len(transport.calls), 1)


class StopConditions(unittest.TestCase):
    def assert_stops(self, status, body="", final_url=None):
        p, transport, clock = fetcher({"example.com": (status, body, {}, final_url)})
        with self.assertRaises(HostStopped) as ctx:
            p.get("https://example.com/a")
        self.assertIn("example.com", str(ctx.exception))
        self.assertEqual(transport.urls, ["https://example.com/a"])

    def test_429_stops_the_host(self):
        self.assert_stops(429)

    def test_999_stops_the_host(self):
        self.assert_stops(999)

    def test_403_stops_the_host(self):
        self.assert_stops(403)

    def test_an_authwall_page_stops_the_host(self):
        self.assert_stops(200, "<html><body>Authwall: sign in to continue</body></html>")

    def test_a_challenge_page_stops_the_host(self):
        self.assert_stops(200, "<html><body>Please verify you are a human</body></html>")

    def test_a_page_that_only_embeds_recaptcha_does_not_stop_the_host(self):
        # real career pages load reCAPTCHA; stopping on the bare word would lose them
        p, transport, clock = fetcher({"example.com": (
            200, '<html><script src="https://www.google.com/recaptcha/api.js"></script></html>', {})})
        response = p.get("https://example.com/a")
        self.assertEqual(response.status, 200)
        self.assertEqual(p.stopped_hosts(), {})

    def test_a_redirect_to_a_login_path_stops_the_host(self):
        self.assert_stops(200, "<html>ok</html>",
                          final_url="https://www.linkedin.com/authwall?trk=x")

    def test_a_500_does_not_stop_the_host(self):
        p, transport, clock = fetcher({"example.com": (500, "boom", {})})
        response = p.get("https://example.com/a")
        self.assertEqual(response.status, 500)
        self.assertFalse(response.ok)
        p.get("https://example.com/b")
        self.assertEqual(len(transport.calls), 2)

    def test_a_normal_page_is_returned(self):
        p, transport, clock = fetcher({"example.com": (200, "hello", {})})
        response = p.get("https://example.com/a")
        self.assertTrue(response.ok)
        self.assertEqual(response.text, "hello")


class NoCookies(unittest.TestCase):
    def test_no_cookie_header_is_ever_sent(self):
        p, transport, clock = fetcher({"example.com": (200, "ok", {})})
        p.get("https://example.com/a")
        headers = {k.lower(): v for k, v in transport.calls[0]["headers"].items()}
        self.assertNotIn("cookie", headers)
        self.assertEqual(headers["user-agent"], USER_AGENT)

    def test_the_user_agent_is_the_honest_one(self):
        self.assertEqual(USER_AGENT, "job-hunt/1.0 (local job search tool)")


class Events(unittest.TestCase):
    def test_waits_and_stops_are_logged(self):
        p, transport, clock = fetcher({"example.com": (200, "ok", {})})
        p.get("https://example.com/a")
        p.get("https://example.com/b")
        events = [event["event"] for event in p.events]
        self.assertEqual(events.count("request"), 2)
        self.assertEqual(events.count("wait"), 1)

    def test_stats_report_requests_per_host(self):
        p, transport, clock = fetcher({"example.com": (200, "ok", {})})
        p.get("https://example.com/a")
        self.assertEqual(p.stats()["example.com"], {"requests": 1, "stopped": None})


class CurlArgv(unittest.TestCase):
    """The command line curl is given, built without starting it."""

    def setUp(self):
        self.transport = CurlTransport()
        self.headers_file = Path("/tmp/h.txt")
        self.body_file = Path("/tmp/b.bin")

    def argv(self, url="https://example.com/a", method="GET", data_file=None, headers=None):
        return self.transport.argv(url, headers or {"Accept": "text/html"},
                                   method, self.headers_file, self.body_file, data_file)

    def test_curl_is_the_binary_and_q_comes_first(self):
        argv = self.argv()
        self.assertEqual(argv[0], CURL)
        self.assertEqual(argv[1], "-q")          # ignores ~/.curlrc, so no user config leaks in

    def test_the_required_flags(self):
        argv = self.argv()
        self.assertIn("-sS", argv)
        self.assertEqual(argv[argv.index("--max-time") + 1], "20")
        self.assertIn("--compressed", argv)
        self.assertIn("-L", argv)
        self.assertEqual(argv[argv.index("--max-redirs") + 1], "5")

    def test_output_files_and_the_status_format(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("-D") + 1], "/tmp/h.txt")
        self.assertEqual(argv[argv.index("-o") + 1], "/tmp/b.bin")
        self.assertEqual(argv[argv.index("-w") + 1], "%{http_code} %{url_effective}")

    def test_the_user_agent_goes_through_dash_a_once(self):
        argv = self.argv(headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        self.assertEqual(argv[argv.index("-A") + 1], USER_AGENT)
        self.assertFalse(any("User-Agent:" in part for part in argv if part != argv[argv.index("-A") + 1]))
        self.assertEqual(sum(1 for part in argv if part == "-A"), 1)

    def test_no_cookie_flags_are_ever_passed(self):
        for method in ("GET", "POST"):
            argv = self.argv(method=method, data_file=Path("/tmp/d.bin"))
            for forbidden in ("-b", "-c", "--cookie", "--cookie-jar", "--junk-session-cookies",
                              "--cookie-file", "-j"):
                self.assertNotIn(forbidden, argv, f"{forbidden} must never reach curl ({method})")

    def test_post_adds_the_method_and_the_body_file(self):
        data_file = Path("/tmp/d.bin")
        argv = self.argv(method="POST", data_file=data_file)
        self.assertEqual(argv[argv.index("-X") + 1], "POST")
        self.assertEqual(argv[argv.index("--data-binary") + 1], "@/tmp/d.bin")

    def test_a_get_has_no_body(self):
        argv = self.argv()
        self.assertNotIn("-X", argv)
        self.assertNotIn("--data-binary", argv)

    def test_the_url_is_last(self):
        self.assertEqual(self.argv()[-1], "https://example.com/a")


class CurlErrors(unittest.TestCase):
    """curl exit codes become typed errors; nothing escapes as a raw OSError."""

    class Done:
        def __init__(self, returncode=0, stdout="200 https://example.com/a", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def run_with(self, done, write_files=True):
        def runner(argv, **kwargs):
            if write_files:
                Path(argv[argv.index("-D") + 1]).write_text("HTTP/1.1 200 OK\r\nX-Test: 1\r\n\r\n")
                Path(argv[argv.index("-o") + 1]).write_bytes(b"hello")
            return done
        return CurlTransport(runner=runner)

    def test_a_good_response_is_parsed(self):
        status, url, headers, body = self.run_with(self.Done())( "https://example.com/a", {})
        self.assertEqual(status, 200)
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(headers["x-test"], "1")
        self.assertEqual(body, b"hello")

    def test_a_timeout_is_typed(self):
        with self.assertRaises(FetchError) as ctx:
            self.run_with(self.Done(28, "", "Operation timed out"))("https://e.com", {})
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_connect_and_tls_failures_are_typed(self):
        self.assertEqual(self._kind(7), "connect")
        self.assertEqual(self._kind(35), "tls")
        self.assertEqual(self._kind(6), "resolve")

    def _kind(self, code):
        with self.assertRaises(FetchError) as ctx:
            self.run_with(self.Done(code, "", "boom"))("https://e.com", {})
        return ctx.exception.kind

    def test_a_missing_curl_binary_is_typed(self):
        transport = CurlTransport(curl_path="/nonexistent/curl")
        with self.assertRaises(FetchError) as ctx:
            transport("https://example.com", {})
        self.assertEqual(ctx.exception.kind, "spawn")

    def test_a_missing_status_line_is_reported(self):
        with self.assertRaises(FetchError):
            self.run_with(self.Done(0, "garbage"))("https://example.com", {})


class CurlAgainstALocalServer(unittest.TestCase):
    """One real curl run against 127.0.0.1: no external network, real plumbing."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, body):
                self.send_response(200)
                self.send_header("X-Test", "local")
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._reply(b"hello from local")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                self._reply(b"posted:" + body)

            def log_message(self, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path="/x"):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_a_real_get(self):
        status, url, headers, body = CurlTransport()(self.url(), {"Accept": "text/plain"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-test"), "local")
        self.assertEqual(body, b"hello from local")

    def test_a_real_post(self):
        status, _, _, body = CurlTransport()(self.url(), {"Accept": "text/plain"},
                                             method="POST", data=b'{"a": 1}')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'posted:{"a": 1}')

    def test_the_fetcher_wraps_it(self):
        p = PoliteFetcher(profile(**{"limits.min_seconds_between_requests": 0}))
        response = p.get(self.url())
        self.assertTrue(response.ok)
        self.assertIn("hello", response.text)


class PostThroughTheFetcher(unittest.TestCase):
    def test_post_passes_method_body_and_content_type(self):
        p, transport, clock = fetcher({"example.com": (200, "{}", {})})
        p.post("https://example.com/api", {"query": "x"})
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["data"], b'{"query": "x"}')
        self.assertEqual(call["headers"]["Content-Type"], "application/json")

    def test_a_timeout_is_retried_once(self):
        attempts = []

        def transport(url, headers, method="GET", data=None, timeout=None):
            attempts.append(url)
            raise FetchError("timeout", "curl gave up")

        p = PoliteFetcher(profile(), transport=transport, sleep=lambda s: None, now=lambda: 0.0)
        with self.assertRaises(FetchError):
            p.get("https://example.com/a")
        self.assertEqual(len(attempts), 2)                  # one retry, then it gives up
        self.assertEqual(p.stopped_hosts(), {})             # a timeout never stops the host
        self.assertEqual(p.stats()["example.com"]["requests"], 2)

    def test_a_connect_error_is_not_retried(self):
        attempts = []

        def transport(url, headers, method="GET", data=None, timeout=None):
            attempts.append(url)
            raise FetchError("connect", "connection refused")

        p = PoliteFetcher(profile(), transport=transport, sleep=lambda s: None, now=lambda: 0.0)
        with self.assertRaises(FetchError):
            p.get("https://example.com/a")
        self.assertEqual(len(attempts), 1)

    def test_a_retry_that_succeeds_returns_the_response(self):
        attempts = []

        def transport(url, headers, method="GET", data=None, timeout=None):
            attempts.append(url)
            if len(attempts) == 1:
                raise FetchError("timeout", "curl gave up")
            return 200, url, {}, b"ok"

        p = PoliteFetcher(profile(), transport=transport, sleep=lambda s: None, now=lambda: 0.0)
        response = p.get("https://example.com/a")
        self.assertEqual(response.text, "ok")
        self.assertEqual(len(attempts), 2)

    def test_the_timeout_follows_the_host(self):
        profiles = profile(**{"limits.timeout_per_host": {"api.lever.co": 60}})
        seen = []

        def transport(url, headers, method="GET", data=None, timeout=None):
            seen.append(timeout)
            return 200, url, {}, b"ok"

        p = PoliteFetcher(profiles, transport=transport, sleep=lambda s: None, now=lambda: 0.0)
        p.get("https://api.lever.co/v0/postings/lalamove?mode=json")
        p.get("https://th.jobsdb.com/api/jobsearch/v5/search")
        self.assertEqual(seen, [60.0, 20.0])


if __name__ == "__main__":
    unittest.main()
