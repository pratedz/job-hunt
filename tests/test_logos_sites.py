"""M9 update: real logos from company sites, name matching, backfill and coverage.

Also covers the hardening done on handover: LinkedIn placeholders, https-only logo
URLs, capped hosts not counted as failures, strict served names, regions per tab.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _fake import FakeClock, FakeTransport, fixture
from _util import CFG, memory_conn, profile
from _vault import TODAY, make_vault

from jobhunt import board_api, logos, store
from jobhunt.http import PoliteFetcher
from jobhunt.sources import companies as companies_src, jobsdb, linkedin
from jobhunt.tracker import Tracker


def png(side: int) -> bytes:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    return header + side.to_bytes(4, "big") + side.to_bytes(4, "big") + b"\x08\x06" + b"\x00" * 40


def ico(*sides: int) -> bytes:
    data = b"\x00\x00\x01\x00" + len(sides).to_bytes(2, "little")
    for side in sides:
        data += bytes([side % 256, side % 256]) + b"\x00" * 14
    return data


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HOME = """<html><head>
<link rel="icon" type="image/svg+xml" href="/brand.svg">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-180.png">
<link rel="icon" sizes="32x32" href="/icon-32.png">
<link rel="icon" sizes="192x192" href="https://cdn.acme.test/icon-192.png">
</head><body>Acme</body></html>"""

COMPANIES = [
    {"name": "Unilever", "domain": "unilever.com"},
    {"name": "Unilever (Early Careers)", "domain": "unilever.com"},
    {"name": "Shopee", "domain": "shopee.co.th"},
    {"name": "TikTok / TikTok Shop / ByteDance", "domain": "tiktok.com"},
    {"name": "Marriott", "domain": "marriott.com"},
    {"name": "Central Group", "domain": "centralgroup.com"},
    {"name": "Sea", "domain": "sea.com"},
    {"name": "Grab"},                                      # no domain: never matched
]


def fetcher_for(transport):
    clock = FakeClock()
    return PoliteFetcher(profile(**{"limits.min_seconds_between_requests": 0}),
                         transport=transport, sleep=clock.sleep, now=clock.now)


class LinkedInLogos(unittest.TestCase):
    def test_every_search_card_logo_is_a_media_company_logo(self):
        cards = linkedin.parse_cards(fixture("linkedin_guest_search.html"))
        self.assertTrue(cards)
        for card in cards:
            self.assertTrue(card["logo_source_url"].startswith("https://media.licdn.com/"))
            self.assertIn("company-logo", card["logo_source_url"])
            self.assertNotIn("&amp;", card["logo_source_url"])

    def test_a_placeholder_only_card_has_no_logo(self):
        html = """<div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:1">
          <img data-delayed-url="https://static.licdn.com/aero-v1/sc/h/ghost"
               data-ghost-url="https://static.licdn.com/aero-v1/sc/h/ghost">
          <h3 class="base-search-card__title">Trainee</h3></div>"""
        self.assertIsNone(linkedin.parse_cards(html)[0]["logo_source_url"])

    def test_the_posting_logo_is_the_company_not_a_face_pile(self):
        logo = linkedin.parse_posting(fixture("linkedin_guest_jobposting.html"))["logo_source_url"]
        self.assertIn("company-logo_100_100", logo)
        self.assertTrue(logo.startswith("https://media.licdn.com/"))

    def test_the_jobsdb_job_page_logo_comes_from_ld_json(self):
        # the real page has no ld+json logo: it comes from the company profile's branding,
        # never from the ad's cover banner (a different image on the same CDN)
        logo = jobsdb.parse_job_page(fixture("jobsdb_job_95340875.html"))["logo_source_url"]
        self.assertEqual(logo, "https://image-service-cdn.seek.com.au/"
                               "d3b1665473f00605024574a18df267fc11498253/"
                               "f3c5292cec0e05e4272d9bf9146f390d366481d0")

    def test_an_ld_json_logo_still_wins(self):
        page = ('<script type="application/ld+json">{"@type": "JobPosting", '
                '"hiringOrganization": {"name": "B", "logo": "https://x.test/b.png"}}</script>')
        self.assertEqual(jobsdb.parse_job_page(page)["logo_source_url"], "https://x.test/b.png")


class DownloadHardening(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(tmp.name))
        self.conn = memory_conn(self)

    def test_a_non_https_url_is_refused_without_a_request(self):
        transport = FakeTransport({})
        for url in ("file:///etc/passwd", "http://media.licdn.com/x.png", "ftp://x/y.png"):
            self.assertIsNone(logos.download(self.cfg, self.conn, fetcher_for(transport),
                                             "acme", "Acme", url))
        self.assertEqual(transport.calls, [])
        self.assertIn("https", store.get_logo(self.conn, "acme")["error"])

    def test_a_capped_host_is_deferred_and_not_recorded_as_a_failure(self):
        for name in ("Acme", "Beta", "Gamma"):
            store.add_job(self.conn, {"source": "linkedin", "title": "T", "company": name,
                                      "logo_source_url": f"https://media.licdn.com/{name}.png"})
        transport = FakeTransport({"media.licdn.com": (200, png(100), {})})
        fetcher = fetcher_for(transport)
        fetcher.per_host_caps["media.licdn.com"] = 1
        report = logos.fetch_missing(self.cfg, self.conn, fetcher)
        self.assertEqual((report["stored"], report["deferred"], report["refused"]), (1, 2, 0))
        # the deferred two are still candidates for the next run
        self.assertEqual(len(store.logo_candidates(self.conn)), 2)

    def test_only_names_the_backend_writes_are_served(self):
        self.assertTrue(logos.stored_path(self.cfg, "0123456789abcdef.png"))
        for name in ("abc.png", "0123456789abcdef.svg", "../0123456789abcdef.png",
                     ".0123456789abcdef.png", "0123456789ABCDEF.png", "x/0123456789abcdef.png",
                     "0123456789abcdef.png\x00"):
            with self.assertRaises(ValueError, msg=name):
                logos.stored_path(self.cfg, name)


class Sizes(unittest.TestCase):
    def test_png_gif_and_ico_sizes(self):
        self.assertEqual(logos.image_size(png(180)), 180)
        self.assertEqual(logos.image_size(b"GIF89a" + (48).to_bytes(2, "little") * 2 + b"\0" * 8), 48)
        self.assertEqual(logos.image_size(ico(16, 32, 0)), 256)   # 0 means 256 px
        self.assertIsNone(logos.image_size(SVG))


class SiteIcons(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(tmp.name))
        self.conn = memory_conn(self)

    def test_candidates_are_ordered_and_svg_is_skipped(self):
        urls = logos.icon_candidates(HOME, "https://www.acme.test/")
        self.assertEqual(urls, ["https://www.acme.test/apple-180.png",
                                "https://cdn.acme.test/icon-192.png",
                                "https://www.acme.test/favicon.ico",
                                "https://www.acme.test/icon-32.png",
                                "https://www.acme.test/apple-touch-icon.png",
                                "https://www.acme.test/apple-touch-icon-precomposed.png"])

    def test_a_page_without_links_falls_back_to_favicon(self):
        self.assertEqual(logos.icon_candidates("", "https://acme.test/"),
                         ["https://acme.test/favicon.ico",
                          "https://acme.test/apple-touch-icon.png",
                          "https://acme.test/apple-touch-icon-precomposed.png"])

    def test_the_first_big_raster_wins_and_an_svg_body_is_never_stored(self):
        transport = FakeTransport({})
        transport.add("acme.test/apple-180.png", body=SVG)          # lies about its type
        transport.add("cdn.acme.test/icon-192.png", body=png(192))
        transport.add("acme.test/favicon.ico", body=ico(16))
        transport.add("https://acme.test/", body=HOME)
        name = logos.fetch_domain_icon(self.cfg, self.conn, fetcher_for(transport), "acme.test")
        self.assertTrue(name.endswith(".png"))
        stored = (self.cfg.data_dir / "logos" / name).read_bytes()
        self.assertEqual(logos.image_size(stored), 192)
        self.assertNotIn("https://acme.test/favicon.ico", transport.urls)     # stopped early
        self.assertEqual(store.get_logo(self.conn, "@acme.test")["file"], name)

    def test_a_configured_asset_is_used_before_the_home_page(self):
        transport = FakeTransport({})
        transport.add("cdn.acme.test/logo.png", body=png(128))
        name = logos.fetch_domain_icon(
            self.cfg, self.conn, fetcher_for(transport), "acme.test",
            assets={"acme.test": "https://cdn.acme.test/logo.png"})
        self.assertTrue(name.endswith(".png"))
        self.assertEqual(logos.image_size((self.cfg.data_dir / "logos" / name).read_bytes()), 128)
        self.assertNotIn("https://acme.test/", transport.urls)

    def test_a_homepage_that_redirects_to_another_brand_is_not_its_logo(self):
        parent = "<html><head><link rel=\"icon\" href=\"/favicon.ico\"></head></html>"
        transport = FakeTransport({})
        transport.add("https://moet.test/favicon.ico", body=png(64),
                      final_url="https://parent.test/favicon.ico")
        transport.add("https://moet.test/apple-touch-icon.png", body=png(64),
                      final_url="https://parent.test/apple-touch-icon.png")
        transport.add("https://moet.test/apple-touch-icon-precomposed.png", body=png(64),
                      final_url="https://parent.test/apple-touch-icon-precomposed.png")
        transport.add("https://www.moet.test/favicon.ico", body=png(64),
                      final_url="https://parent.test/favicon.ico")
        transport.add("https://www.moet.test/apple-touch-icon.png", body=png(64),
                      final_url="https://parent.test/apple-touch-icon.png")
        transport.add("https://www.moet.test/apple-touch-icon-precomposed.png", body=png(64),
                      final_url="https://parent.test/apple-touch-icon-precomposed.png")
        transport.add("https://www.moet.test/", body=parent, final_url="https://parent.test/")
        transport.add("https://moet.test/", body=parent, final_url="https://parent.test/")
        transport.add("https://parent.test/favicon.ico", body=png(180))
        name = logos.fetch_domain_icon(self.cfg, self.conn, fetcher_for(transport), "moet.test")
        self.assertIsNone(name)
        self.assertFalse(any("parent.test" in url for url in transport.urls))
        self.assertIn("no raster", store.get_logo(self.conn, "@moet.test")["error"])

    def test_a_cdn_link_on_the_company_page_is_kept(self):
        page = '<html><head><link rel="apple-touch-icon" href="https://cdn.other.test/logo.png"></head></html>'
        transport = FakeTransport({})
        transport.add("https://cdn.other.test/logo.png", body=png(128))
        transport.add("https://acme.test/", body=page)
        name = logos.fetch_domain_icon(self.cfg, self.conn, fetcher_for(transport), "acme.test")
        self.assertTrue(name.endswith(".png"))
        self.assertEqual(logos.image_size((self.cfg.data_dir / "logos" / name).read_bytes()), 128)

    def test_a_one_pixel_stub_is_not_stored(self):
        transport = FakeTransport({})
        transport.add("acme.test/favicon.ico", body=ico(1))
        transport.add("https://acme.test/", body="<html></html>")
        name = logos.fetch_domain_icon(self.cfg, self.conn, fetcher_for(transport), "acme.test")
        self.assertIsNone(name)
        self.assertIn("no raster", store.get_logo(self.conn, "@acme.test")["error"])

    def test_a_small_icon_is_kept_when_nothing_bigger_exists(self):
        transport = FakeTransport({})
        transport.add("acme.test/favicon.ico", body=ico(32))
        transport.add("https://acme.test/", body="<html></html>")
        name = logos.fetch_domain_icon(self.cfg, self.conn, fetcher_for(transport), "acme.test")
        self.assertTrue(name.endswith(".ico"))

    def test_a_site_without_a_raster_icon_is_remembered_for_a_week(self):
        transport = FakeTransport({})
        transport.add("https://acme.test/", body="<html></html>")
        report = logos.fetch_domains(self.cfg, self.conn, fetcher_for(transport), ["acme.test"])
        self.assertEqual(report["refused"], 1)
        again = logos.fetch_domains(self.cfg, self.conn, fetcher_for(FakeTransport({})),
                                    ["acme.test"])
        self.assertEqual(again["considered"], 0)

    def test_a_site_that_refuses_us_waits_a_week(self):
        transport = FakeTransport({"acme.test": (403, "", {})})
        report = logos.fetch_domains(self.cfg, self.conn, fetcher_for(transport), ["acme.test"])
        self.assertEqual(report["refused"], 1)
        self.assertIn("403", store.get_logo(self.conn, "@acme.test")["error"])
        again = FakeTransport({})
        logos.fetch_domains(self.cfg, self.conn, fetcher_for(again), ["acme.test"])
        self.assertEqual(again.calls, [])

    def test_a_capped_site_is_only_deferred(self):
        fetcher = fetcher_for(FakeTransport({}))
        fetcher.per_host_caps["acme.test"] = 0
        report = logos.fetch_domains(self.cfg, self.conn, fetcher, ["acme.test"])
        self.assertEqual(report["deferred"], 1)
        self.assertIsNone(store.get_logo(self.conn, "@acme.test"))


class Matching(unittest.TestCase):
    def setUp(self):
        self.matcher = logos.CompanyMatcher(COMPANIES)

    def test_local_entities_match_their_group(self):
        for company, domain in (("Unilever Thailand", "unilever.com"),
                                ("Unilever", "unilever.com"),
                                ("Shopee (Thailand) Co., Ltd.", "shopee.co.th"),
                                ("TikTok Shop", "tiktok.com"),
                                ("ByteDance", "tiktok.com"),
                                ("Marriott International", "marriott.com"),
                                ("Central Group", "centralgroup.com"),
                                ("Sea Limited", "sea.com")):
            self.assertEqual(self.matcher.domain_for(company), domain, company)

    def test_other_companies_do_not_match(self):
        for company in ("Central Retail", "Grab", "Unilab", "Seagate", "Shopify", None, ""):
            self.assertIsNone(self.matcher.domain_for(company), company)

    def test_matching_is_on_whole_tokens_only(self):
        """A configured short name never claims a longer one that starts with it."""
        for company in ("Sea Logistics", "Sea Logistics (Thailand) Co., Ltd.", "Unilever Foods",
                        "Marriott Vacations", "TikTok Ads Agency", "Shopee Food Partner",
                        "SEA Group Holdings Trading"):
            self.assertIsNone(self.matcher.domain_for(company), company)
        # while names that differ only by brackets, legal forms or places still match
        for company, domain in (("SEA LIMITED", "sea.com"), ("Sea (Singapore) Pte. Ltd.", "sea.com"),
                                ("Shopee / Monee", "shopee.co.th"),
                                ("Marriott International (Thailand)", "marriott.com")):
            self.assertEqual(self.matcher.domain_for(company), domain, company)

    def test_logo_domains_are_read_with_the_companies(self):
        cfg = replace(CFG, profile=profile(companies=COMPANIES, logo_domains={
            "Deloitte": "deloitte.com", "CP Axtra (Lotus's)": "lotuss.com",
            "Funding Societies | Modalku Group": "fundingsocieties.com",
            "Unilever": "not-used.example"}))
        sources = logos.logo_sources(cfg)
        matcher = logos.CompanyMatcher(sources)
        self.assertEqual(matcher.domain_for("Deloitte"), "deloitte.com")
        self.assertEqual(matcher.domain_for("DELOITTE"), "deloitte.com")
        self.assertEqual(matcher.domain_for("CP Axtra (Lotus's)"), "lotuss.com")
        self.assertEqual(matcher.domain_for("Funding Societies | Modalku Group"),
                         "fundingsocieties.com")
        self.assertIsNone(matcher.domain_for("Deloitte Digital"))         # whole tokens
        self.assertEqual(matcher.domain_for("Unilever Thailand"), "unilever.com")   # [[companies]] first
        self.assertIn("deloitte.com", logos.configured_domains(cfg))
        self.assertEqual(len(logos.configured_domains(cfg)),
                         len(set(logos.configured_domains(cfg))))

    def test_the_real_profile_tables_load(self):
        domains = logos.configured_domains(CFG)
        self.assertIn("unilever.com", domains)
        if CFG.get("logo_domains"):
            self.assertIn("deloitte.com", domains)

    def test_the_index_prefers_the_company_own_logo(self):
        conn = memory_conn(self)
        store.set_logo(conn, "@unilever.com", "aaaaaaaaaaaaaaaa.png", "https://unilever.com/")
        store.set_logo(conn, "unilever thailand", "bbbbbbbbbbbbbbbb.png", "https://media.licdn.com/x")
        index = logos.LogoIndex(conn, COMPANIES)
        self.assertEqual(index.url("Unilever Thailand"), "/logos/bbbbbbbbbbbbbbbb.png")
        self.assertEqual(index.url("Unilever (Thailand) Co., Ltd."), "/logos/aaaaaaaaaaaaaaaa.png")
        self.assertIsNone(index.url("Grab"))


class CompanySites(unittest.TestCase):
    def test_a_profile_logo_url_is_attached_to_the_company_jobs(self):
        conn = memory_conn(self)
        cfg = replace(CFG, profile=profile(companies=[
            {"name": "Acme", "adapter": "greenhouse", "board": "acme",
             "logo_url": "https://acme.test/logo.png"}]))
        job = {"source": "greenhouse", "source_job_id": "1", "url": "https://x/1",
               "company": "Acme", "title": "Trainee", "location": "Bangkok"}
        with mock.patch.dict(companies_src.API_ADAPTERS, {"greenhouse": lambda e, f: [dict(job)]}):
            jobs, _ = companies_src.collect(cfg, conn, fetcher=None, now="2026-11-08T00:00:00Z")
        self.assertEqual(jobs[0]["logo_source_url"], "https://acme.test/logo.png")


class Countries(unittest.TestCase):
    def country(self, location, facts=None):
        return board_api.country_of({"location": location,
                                     "facts_json": json.dumps(facts) if facts else None})

    def test_thai_provinces_and_industrial_areas(self):
        for location in ("Laem Chabang", "Chachoengsao", "Sathon, Bangkok", "Sriracha, Chon Buri",
                         "Map Ta Phut", "BANGKOK PLANT"):
            self.assertEqual(self.country(location), "Thailand", location)

    def test_abroad_cities_and_whole_words(self):
        self.assertEqual(self.country("Kuala Lumpur"), "Malaysia")
        self.assertEqual(self.country("Ho Chi Minh City, Viet Nam"), "Vietnam")
        self.assertEqual(self.country("Singapore, Singapore"), "Singapore")
        self.assertIsNone(self.country("Indianapolis, Indiana"))
        self.assertIsNone(self.country("Remote"))

    def test_the_scored_country_wins(self):
        self.assertEqual(self.country("Bangkok", {"location_country": "Singapore"}), "Singapore")
        self.assertEqual(board_api.region_of("TH"), "thailand")


class RegionsPerTab(unittest.TestCase):
    def test_the_region_counts_follow_the_selected_tab(self):
        conn = memory_conn(self)
        tracker = Tracker(make_vault(tempfile.mkdtemp()), today=lambda: TODAY)
        for title, country, verdict in (("A", "Thailand", "CENTRE"), ("B", "Singapore", "LOW"),
                                        ("C", "Thailand", "LOW")):
            job_id, _ = store.add_job(conn, {"source": "jobsdb", "title": title, "company": "Acme",
                                             "jd_text": "x"})
            store.insert_score(conn, job_id, {
                "status": "scored", "verdict": verdict, "gate_result": "pass",
                "love": 4, "good_at": 4, "needs": 4, "paid": 4, "jd_used": 1,
                "facts": {"location_country": country}, "weakest_circle": "love",
                "weakest_score": 4, "mean_score": 4.0,
                "created_at": store.utc_now(), "scored_at": store.utc_now()})
        self.assertEqual(board_api.stats(conn, tracker, tab="low")["regions"],
                         {"all": 2, "thailand": 1, "abroad": 1})
        self.assertEqual(board_api.stats(conn, tracker, tab="centre")["regions"],
                         {"all": 1, "thailand": 1, "abroad": 0})
        self.assertEqual(board_api.stats(conn, tracker, tab="applied")["regions"]["all"], 3)


class ContractClarifications(unittest.TestCase):
    """pair/M4-api.md §M9 last bullet: rank per region, JobDetail = JobRow + extras."""

    def setUp(self):
        self.conn = memory_conn(self)
        self.tracker = Tracker(make_vault(tempfile.mkdtemp()), today=lambda: TODAY)
        self.ids = {}
        for title, country, weakest in (("SG best", "Singapore", 5), ("TH good", "Thailand", 4),
                                        ("TH ok", "Thailand", 3)):
            job_id, _ = store.add_job(self.conn, {"source": "jobsdb", "title": title,
                                                  "company": "Unilever Thailand", "jd_text": "x"})
            store.insert_score(self.conn, job_id, {
                "status": "scored", "verdict": "CENTRE", "gate_result": "pass",
                "love": 5, "good_at": 5, "needs": 5, "paid": weakest, "jd_used": 1,
                "facts": {"location_country": country}, "weakest_circle": "paid",
                "weakest_score": weakest, "mean_score": (15 + weakest) / 4,
                "created_at": store.utc_now(), "scored_at": store.utc_now()})
            self.ids[title] = job_id

    def test_rank_restarts_within_the_region(self):
        everything = board_api.list_jobs(self.conn, self.tracker)["jobs"]
        self.assertEqual([(r["title"], r["rank"]) for r in everything],
                         [("SG best", 1), ("TH good", 2), ("TH ok", 3)])
        thai = board_api.list_jobs(self.conn, self.tracker, region="thailand")["jobs"]
        self.assertEqual([(r["title"], r["rank"]) for r in thai], [("TH good", 1), ("TH ok", 2)])

    def test_the_detail_carries_logo_url_and_country(self):
        store.set_logo(self.conn, "@unilever.com", "aaaaaaaaaaaaaaaa.png", "https://unilever.com/")
        detail = board_api.get_job(self.conn, self.tracker, self.ids["TH good"],
                                   companies=COMPANIES)
        self.assertEqual(detail["country"], "Thailand")
        self.assertEqual(detail["logo_url"], "/logos/aaaaaaaaaaaaaaaa.png")
        row = board_api.list_jobs(self.conn, self.tracker, companies=COMPANIES)["jobs"][1]
        self.assertTrue(set(row) <= set(detail))


class Backfill(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(tmp.name), profile=profile(companies=COMPANIES))
        self.conn = memory_conn(self)
        self.tracker = Tracker(make_vault(tmp.name), today=lambda: TODAY)

    def add(self, company, source="jobsdb", job_id=None, logo=None, verdict="CENTRE"):
        row_id, _ = store.add_job(self.conn, {
            "source": source, "source_job_id": job_id, "title": f"Trainee {company}",
            "company": company, "jd_text": "x", "logo_source_url": logo,
            "url": f"https://th.jobsdb.com/job/{job_id}" if source == "jobsdb" and job_id else None})
        store.insert_score(self.conn, row_id, {
            "status": "scored", "verdict": verdict, "gate_result": "pass",
            "love": 4, "good_at": 4, "needs": 4, "paid": 4, "jd_used": 1, "facts": {},
            "weakest_circle": "love", "weakest_score": 4, "mean_score": 4.0,
            "created_at": store.utc_now(), "scored_at": store.utc_now()})
        return row_id

    def test_every_path_and_the_coverage_numbers(self):
        self.add("Acme", logo="https://media.licdn.com/dms/acme/company-logo_100_100/x")
        self.add("Unilever Thailand")                                  # configured domain
        self.add("Beta Co., Ltd.", source="jobsdb", job_id="555")      # one posting fetch
        self.add("Nowhere", source="manual")                           # no path at all
        self.add("Filtered Inc", verdict="FILTERED")                  # not visible
        page = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "T", '
                '"hiringOrganization": {"name": "Beta", '
                '"logo": "https://image-service-cdn.seek.com.au/beta"}}</script>')
        transport = FakeTransport({})
        transport.add("media.licdn.com", body=png(100))
        transport.add("unilever.com/favicon.ico", body=ico(64))     # before the home page route
        transport.add("https://unilever.com/", body="<html></html>")
        transport.add("th.jobsdb.com/job/555", body=page)
        transport.add("image-service-cdn.seek.com.au/beta", body=png(120))
        companies = board_api.visible_companies(self.conn, self.tracker)
        names = [item["company"] for item in companies]
        self.assertNotIn("Filtered Inc", names)
        surfaced = [item["company"] for item in board_api.surfaced_companies(self.conn, self.tracker)]
        self.assertIn("Filtered Inc", surfaced)
        report = logos.backfill(self.cfg, self.conn, fetcher_for(transport), companies)
        visible_jobs_companies = {"Acme", "Unilever Thailand", "Beta Co., Ltd.", "Nowhere"}
        self.assertTrue(visible_jobs_companies <= set(names))
        self.assertEqual(report["stored"], {"url": 1, "domain": 1, "posting": 1})
        self.assertIn("Nowhere", report["missing"])
        self.assertEqual(report["with_logo"], report["visible"] - len(report["missing"]))
        # the board now shows each of the three
        jobs = {row["company"]: row["logo_url"] for row in
                board_api.list_jobs(self.conn, self.tracker, companies=COMPANIES)["jobs"]}
        for company in ("Acme", "Unilever Thailand", "Beta Co., Ltd."):
            self.assertTrue(jobs[company].startswith("/logos/"), company)
        self.assertIsNone(jobs["Nowhere"])
        # a second backfill asks nothing new for those three
        again = FakeTransport({})
        logos.backfill(self.cfg, self.conn, fetcher_for(again),
                       board_api.visible_companies(self.conn, self.tracker))
        self.assertEqual(again.calls, [])

    def test_a_thai_only_name_is_counted_without_changing_job_dedupe(self):
        thai = "บริษัท แฮปปี้ แคโนพี จำกัด"
        other = "บริษัท อื่น จำกัด"
        self.assertIsNone(store.normalise_text(thai))
        self.add(thai)
        key = logos.logo_storage_key(thai)
        self.assertEqual(key, logos.logo_storage_key("  " + thai + "  "))
        self.assertTrue(key.startswith("raw:"))
        self.assertNotEqual(key, logos.logo_storage_key(other))
        self.assertEqual(logos.logo_storage_key("Acme Ltd."), store.normalise_text("Acme Ltd."))
        stored = self.conn.execute(
            "SELECT company_norm FROM jobs WHERE company = ?", (thai,)).fetchone()
        self.assertIsNone(stored["company_norm"])
        surfaced = board_api.surfaced_companies(self.conn, self.tracker)
        item = next(row for row in surfaced if row["company"] == thai)
        self.assertEqual(item["company_norm"], key)
        store.set_logo(self.conn, key, "0123456789abcdef.png", "https://cdn.example.test/logo.png")
        shown = next(row for row in board_api.list_jobs(self.conn, self.tracker)["jobs"]
                     if row["company"] == thai)
        self.assertEqual(shown["logo_url"], "/logos/0123456789abcdef.png")

    def test_linkedin_posting_fetches_respect_the_budget(self):
        for index in range(3):
            self.add(f"Company {index}", source="linkedin", job_id=str(100 + index))
        transport = FakeTransport({})
        transport.add("jobs-guest/jobs/api/jobPosting", body="<html></html>")
        report = logos.backfill(self.cfg, self.conn, fetcher_for(transport),
                                board_api.visible_companies(self.conn, self.tracker),
                                linkedin_budget=2)
        posting_calls = [url for url in transport.urls if "jobPosting" in url]
        self.assertEqual(len(posting_calls), 2)
        self.assertEqual(report["with_logo"], 0)


if __name__ == "__main__":
    unittest.main()


class Relink(unittest.TestCase):
    """A stored site icon reaches every job/record whose company matches it (Douyin bug)."""

    DOUYIN = "Douyin Group (HK) Limited"
    ICON = "9bddfbecd993e5c7.png"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name), profile=profile(
            companies=COMPANIES, logo_domains={self.DOUYIN: "bytedance.com"}))
        self.conn = memory_conn(self)
        self.tracker = Tracker(make_vault(self.tmp.name), today=lambda: TODAY)
        self.job = Backfill.add(self, self.DOUYIN)
        store.set_logo(self.conn, "@bytedance.com", self.ICON, "https://bytedance.com/")
        self.sources = logos.logo_sources(self.cfg)

    def test_the_api_shows_a_logo_domains_icon(self):
        rows = board_api.list_jobs(self.conn, self.tracker, companies=self.sources)["jobs"]
        self.assertEqual(rows[0]["logo_url"], f"/logos/{self.ICON}")
        self.assertEqual(logos.check(self.conn, self.tracker, self.sources), [])

    def test_the_board_server_passes_logo_domains(self):
        import threading
        import urllib.request
        from jobhunt import board_server
        db = Path(self.tmp.name) / "board.sqlite"
        conn = store.connect(db) if hasattr(store, "connect") else None
        if conn is None:
            self.skipTest("no file-backed store.connect")
        Backfill.add(type("T", (), {"conn": conn})(), self.DOUYIN)
        store.set_logo(conn, "@bytedance.com", self.ICON, "https://bytedance.com/")
        conn.close()
        server = board_server.create_server(self.cfg, db_path=db,
                                            vault_path=self.tracker.vault.root, port=0,
                                            llm_factory=lambda: None, fetcher_factory=lambda: None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/jobs?tab=all"
        with urllib.request.urlopen(url, timeout=10) as response:
            rows = json.loads(response.read())["jobs"]
        self.assertEqual(rows[0]["logo_url"], f"/logos/{self.ICON}")

    def test_relink_writes_the_icon_under_the_company_key(self):
        before = store.logo_map(self.conn)
        self.assertNotIn("douyin group hk limited", before)        # an older board finds nothing
        report = logos.relink(self.conn, self.sources, logos.all_company_names(self.conn))
        self.assertEqual(report["linked"], 1)
        self.assertEqual(store.logo_map(self.conn)["douyin group hk limited"], self.ICON)
        again = logos.relink(self.conn, self.sources, logos.all_company_names(self.conn))
        self.assertEqual((again["linked"], again["updated"]), (0, 0))   # idempotent

    def test_relink_never_replaces_an_own_logo(self):
        store.set_logo(self.conn, "douyin group hk limited", "ffffffffffffffff.png",
                       "https://media.licdn.com/x")
        report = logos.relink(self.conn, self.sources, logos.all_company_names(self.conn))
        self.assertEqual(report["own"], 1)
        self.assertEqual(store.get_logo(self.conn, "douyin group hk limited")["file"],
                         "ffffffffffffffff.png")

    def test_an_own_logo_still_replaces_a_link_and_a_failure_keeps_it(self):
        logos.relink(self.conn, self.sources, logos.all_company_names(self.conn))
        self.conn.execute("UPDATE jobs SET logo_source_url = ? WHERE id = ?",
                          ("https://media.licdn.com/dms/douyin/company-logo_100_100/x", self.job))
        self.conn.commit()
        self.assertEqual(len(store.logo_candidates(self.conn)), 1)      # a link is no blocker
        failing = FakeTransport({"media.licdn.com": (404, "", {})})
        logos.fetch_missing(self.cfg, self.conn, fetcher_for(failing))
        row = store.get_logo(self.conn, "douyin group hk limited")
        self.assertEqual(row["file"], self.ICON)                         # still shown
        self.assertIn("404", row["error"])
        self.assertEqual(store.logo_candidates(self.conn), [])           # and waits a week
        self.conn.execute("UPDATE logos SET failed_at = '2026-01-01T00:00:00Z'")
        self.conn.commit()
        good = FakeTransport({"media.licdn.com": (200, png(100), {})})
        logos.fetch_missing(self.cfg, self.conn, fetcher_for(good))
        row = store.get_logo(self.conn, "douyin group hk limited")
        self.assertNotEqual(row["file"], self.ICON)
        self.assertFalse(row["source_url"].startswith("link:"))

    def test_the_check_reports_a_company_the_board_does_not_show(self):
        with mock.patch.object(board_api, "logo_urls", lambda conn, sources=None: None):
            gaps = logos.check(self.conn, self.tracker, self.sources)
        self.assertEqual([(g["company"], g["jobs"]) for g in gaps], [(self.DOUYIN, 1)])
