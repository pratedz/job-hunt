"""M9: logos (raster only, cached per company) and the Thailand/Abroad region filter."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _fake import FakeTransport
from _util import CFG, memory_conn, profile
from _vault import TODAY, make_vault, seeded_db

from jobhunt import board_api, board_server, logos, store
from jobhunt.http import PoliteFetcher
from jobhunt.sources import jobsdb, linkedin
from jobhunt.tracker import Tracker

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
ICO = b"\x00\x00\x01\x00" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body>not an image</body></html>"
BIG = PNG + b"\x00" * (201 * 1024)


def _solid_png(side: int) -> bytes:
    """A real RGB PNG large enough that the logo cap has to scale it."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + (b"\xff\x80\x00" * side) for _ in range(side))
    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b"")


class Images(unittest.TestCase):
    def test_raster_types_are_recognised_by_magic_bytes(self):
        for data, kind in ((PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (WEBP, "webp"), (ICO, "ico")):
            self.assertEqual(logos.image_kind(data), kind)

    def test_svg_is_refused(self):
        self.assertIsNone(logos.image_kind(SVG))

    def test_html_named_as_an_image_is_refused(self):
        self.assertIsNone(logos.image_kind(HTML))

    def test_an_empty_body_is_refused(self):
        self.assertIsNone(logos.image_kind(b""))


class Fetching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name))
        self.conn = memory_conn(self)
        for title in ("Graduate Programme", "Another Role"):
            store.add_job(self.conn, {
                "source": "linkedin", "title": title, "company": "Acme",
                "logo_source_url": "https://media.licdn.com/logo/aman.png",
                "jd_text": "A programme."})

    def fetcher(self, body=PNG, status=200, content_type="image/png"):
        self.transport = FakeTransport({"media.licdn.com": (status, "", {})})
        self.transport.routes = [("media.licdn.com", (status, body, {}))]
        return PoliteFetcher(self.cfg.profile, transport=self.transport,
                             sleep=lambda s: None, now=lambda: 0.0)

    def test_a_png_is_stored_once_per_company(self):
        report = logos.fetch_missing(self.cfg, self.conn, self.fetcher())
        self.assertEqual(report["considered"], 1)         # one company, two jobs
        self.assertEqual(report["stored"], 1)
        row = store.get_logo(self.conn, "acme")
        self.assertTrue(row["file"].endswith(".png"))
        self.assertTrue((self.cfg.data_dir / "logos" / row["file"]).exists())

    def test_the_cache_is_reused_on_the_next_run(self):
        logos.fetch_missing(self.cfg, self.conn, self.fetcher())
        again = logos.fetch_missing(self.cfg, self.conn, self.fetcher())
        self.assertEqual(again["considered"], 0)

    def test_an_svg_is_never_stored(self):
        report = logos.fetch_missing(self.cfg, self.conn, self.fetcher(body=SVG))
        self.assertEqual(report["stored"], 0)
        self.assertEqual(report["refused"], 1)
        self.assertIsNone(store.get_logo(self.conn, "acme")["file"])
        self.assertIn("raster", store.get_logo(self.conn, "acme")["error"])
        self.assertEqual(list((self.cfg.data_dir / "logos").glob("*")), [])

    def test_html_disguised_as_a_png_is_refused(self):
        report = logos.fetch_missing(self.cfg, self.conn, self.fetcher(body=HTML))
        self.assertEqual(report["stored"], 0)

    def test_an_oversized_image_is_refused(self):
        # PNG magic plus padding is not a decodable raster. sips cannot shrink it,
        # so the 200 KB cap still refuses it.
        report = logos.fetch_missing(self.cfg, self.conn, self.fetcher(body=BIG))
        self.assertEqual(report["stored"], 0)
        self.assertIn("too large", store.get_logo(self.conn, "acme")["error"])
        self.assertIsNone(logos._shrink_raster(BIG))

    def test_a_real_oversized_png_is_scaled_and_stored(self):
        body = _solid_png(360)
        self.assertGreater(len(body), logos.MAX_BYTES)
        self.assertFalse(logos.raster_decodes(BIG))
        self.assertTrue(logos.raster_decodes(body))
        # A 32-bit ICO whose mask makes Pillow reject the buffer still has a real directory.
        tight_ico = (b"\x00\x00\x01\x00\x01\x00" + bytes([32, 32, 0, 0, 1, 0, 32, 0])
                     + (40).to_bytes(4, "little") + (22).to_bytes(4, "little")
                     + b"\x28\x00\x00\x00" + b"\x00" * 36)
        self.assertTrue(logos.raster_decodes(tight_ico))
        # Pillow is the portable shrink. sips must not be required.
        with mock.patch("jobhunt.logos.subprocess.run", side_effect=FileNotFoundError):
            report = logos.fetch_missing(self.cfg, self.conn, self.fetcher(body=body))
        self.assertEqual(report["stored"], 1, store.get_logo(self.conn, "acme")["error"])
        row = store.get_logo(self.conn, "acme")
        stored = (self.cfg.data_dir / "logos" / row["file"]).read_bytes()
        self.assertLessEqual(len(stored), logos.MAX_BYTES)
        self.assertTrue(logos.raster_decodes(stored))
        self.assertEqual(logos.image_kind(stored), "png")
        self.assertLessEqual(logos.image_size(stored), 256)
        self.assertGreaterEqual(logos.image_size(stored), 16)

    def test_a_failed_download_is_remembered(self):
        logos.fetch_missing(self.cfg, self.conn, self.fetcher(status=404))
        row = store.get_logo(self.conn, "acme")
        self.assertIsNone(row["file"])
        self.assertIn("404", row["error"])
        # and is not retried straight away
        self.assertEqual(logos.fetch_missing(self.cfg, self.conn, self.fetcher())["considered"], 0)

    def test_the_served_url(self):
        logos.fetch_missing(self.cfg, self.conn, self.fetcher())
        self.assertTrue(logos.url_for(self.conn, "acme").startswith("/logos/"))
        self.assertIsNone(logos.url_for(self.conn, "nobody"))

    def test_a_name_cannot_escape_the_logo_folder(self):
        with self.assertRaises(ValueError):
            logos.stored_path(self.cfg, "../../data/jobhunt.sqlite")


class Parsers(unittest.TestCase):
    def test_the_linkedin_card_carries_the_company_logo(self):
        cards = linkedin.parse_cards(
            (Path("tests/fixtures/linkedin_guest_search.html")).read_text(encoding="utf-8"))
        self.assertTrue(cards[0]["logo_source_url"].startswith("https://media.licdn.com/"))

    def test_the_jobsdb_search_carries_the_branding_logo(self):
        jobs = jobsdb.parse_search(json.loads(
            (Path("tests/fixtures/jobsdb_search_v5.json")).read_text(encoding="utf-8")))
        self.assertIn("seek.com.au", jobs[0]["logo_source_url"])

    def test_a_stored_job_keeps_the_logo_url(self):
        conn = memory_conn(self)
        job_id, _ = store.add_job(conn, {"source": "linkedin", "title": "T", "company": "Acme",
                                         "logo_source_url": "https://x.test/logo.png"})
        self.assertEqual(store.get_job(conn, job_id)["logo_source_url"], "https://x.test/logo.png")


class Regions(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.tracker = Tracker(make_vault(tempfile.mkdtemp()), today=lambda: TODAY)

    def add(self, title, facts_country=None, location=None, verdict="CENTRE"):
        job_id, _ = store.add_job(self.conn, {
            "source": "jobsdb", "title": title, "company": "Acme", "location": location,
            "jd_text": "A description."})
        facts = {"job_type": "graduate_programme"}
        if facts_country:
            facts["location_country"] = facts_country
        store.insert_score(self.conn, job_id, {
            "status": "scored", "verdict": verdict, "gate_result": "pass",
            "love": 4, "good_at": 4, "needs": 4, "paid": 4, "facts": facts, "jd_used": 1,
            "weakest_circle": "love", "weakest_score": 4, "mean_score": 4.0,
            "created_at": store.utc_now(), "scored_at": store.utc_now()})
        return job_id

    def test_facts_come_first(self):
        self.add("Thai role", facts_country="Thailand", location="Somewhere else")
        self.add("Singapore role", facts_country="Singapore", location="Bangkok")
        thai = board_api.list_jobs(self.conn, self.tracker, region="thailand")["jobs"]
        abroad = board_api.list_jobs(self.conn, self.tracker, region="abroad")["jobs"]
        self.assertEqual([row["title"] for row in thai], ["Thai role"])
        self.assertEqual([row["title"] for row in abroad], ["Singapore role"])

    def test_the_location_is_the_fallback(self):
        self.add("Bangkok role", location="Sathon, Bangkok")
        self.add("Hong Kong role", location="Hong Kong")
        self.assertEqual([row["country"] for row in
                          board_api.list_jobs(self.conn, self.tracker, region="thailand")["jobs"]],
                         ["Thailand"])
        self.assertEqual([row["country"] for row in
                          board_api.list_jobs(self.conn, self.tracker, region="abroad")["jobs"]],
                         ["Hong Kong"])

    def test_an_unknown_country_is_only_under_all(self):
        self.add("Mystery role", location=None)
        row = board_api.list_jobs(self.conn, self.tracker, region="all")["jobs"][0]
        self.assertIsNone(row["country"])
        self.assertEqual(board_api.list_jobs(self.conn, self.tracker, region="thailand")["jobs"], [])
        self.assertEqual(board_api.list_jobs(self.conn, self.tracker, region="abroad")["jobs"], [])

    def test_an_unknown_region_is_a_400(self):
        with self.assertRaises(board_api.ApiError):
            board_api.list_jobs(self.conn, self.tracker, region="moon")

    def test_stats_follow_the_region(self):
        self.add("Thai one", facts_country="Thailand")
        self.add("Thai two", facts_country="Thailand", verdict="LOW")
        self.add("Abroad one", facts_country="Singapore")
        everything = board_api.stats(self.conn, self.tracker)
        thai = board_api.stats(self.conn, self.tracker, region="thailand")
        abroad = board_api.stats(self.conn, self.tracker, region="abroad")
        self.assertEqual(everything["tabs"]["all"], 3)
        self.assertEqual(thai["tabs"]["all"], 2)
        self.assertEqual(abroad["tabs"]["all"], 1)
        self.assertEqual(everything["regions"], {"all": 3, "thailand": 2, "abroad": 1})
        # the region counts describe the tabs, so they do not change with the filter
        self.assertEqual(thai["regions"], {"all": 3, "thailand": 2, "abroad": 1})

    def test_the_shortfall_bars_follow_the_region(self):
        job_id = self.add("Thai one", facts_country="Thailand")
        store.insert_score(self.conn, job_id, {
            "status": "scored", "verdict": "CENTRE", "gate_result": "pass",
            "love": 4, "good_at": 2, "needs": 4, "paid": 5, "jd_used": 1,
            "facts": {"location_country": "Thailand"},
            "weakest_circle": "good_at", "weakest_score": 2, "mean_score": 3.75,
            "created_at": store.utc_now(), "scored_at": store.utc_now()})
        self.add("Abroad one", facts_country="Singapore")
        self.assertEqual(board_api.stats(self.conn, self.tracker, "thailand")["shortfall"]["good_at"], 1)
        self.assertEqual(board_api.stats(self.conn, self.tracker, "abroad")["shortfall"]["good_at"], 0)


class Serving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name))
        self.vault = make_vault(self.tmp.name)
        self.db = Path(self.tmp.name) / "board.sqlite"
        conn, _ids = seeded_db(self, path=self.db)
        conn.close()
        directory = self.cfg.data_dir / "logos"
        directory.mkdir(parents=True, exist_ok=True)
        self.real_png = _solid_png(8)
        (directory / "0123456789abcdef.png").write_bytes(self.real_png)
        self.server = board_server.create_server(self.cfg, db_path=self.db,
                                                 vault_path=self.vault.root, port=0,
                                                 llm_factory=lambda: None,
                                                 fetcher_factory=lambda: None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10)

    def test_a_logo_is_served_with_nosniff_and_a_cache_header(self):
        with self.get("/logos/0123456789abcdef.png") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["Cache-Control"], "max-age=604800")
            body = response.read()
            self.assertEqual(body, self.real_png)
            # Same origin: the URL is a path on this server, and the bytes are a real PNG.
            self.assertEqual(logos.image_kind(body), "png")
            self.assertEqual(logos.image_size(body), 8)
            self.assertTrue(response.geturl().startswith(f"http://127.0.0.1:{self.port}/logos/"))

    def test_an_unknown_logo_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/logos/nope.png")
        self.assertEqual(ctx.exception.code, 404)

    def test_a_traversal_attempt_is_refused(self):
        for path in ("/logos/../../data/jobhunt.sqlite", "/logos/..%2F..%2Fetc%2Fpasswd"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertIn(ctx.exception.code, (403, 404), path)

    def test_jobs_and_stats_accept_the_region(self):
        for path in ("/api/jobs?tab=all&region=thailand", "/api/stats?region=abroad"):
            with self.get(path) as response:
                payload = json.loads(response.read())
                self.assertIn("regions", payload if path.startswith("/api/stats") else {"regions": 1})


if __name__ == "__main__":
    unittest.main()
