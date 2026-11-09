"""Source sightings: exact identity, same-source requisition guard, one-time backfill.

Temp DB only. Coverage query state is not stored here.
"""

import sqlite3
import unittest

from _util import memory_conn

from jobhunt import store


def posting(**kw):
    base = {
        "title": "Key Account",
        "company": "Acme",
        "location": "Bangkok",
    }
    base.update(kw)
    return base


class Sightings(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)

    def test_two_sources_stay_one_job_and_a_renamed_title_still_matches(self):
        workday, created = store.add_job(self.conn, posting(
            source="company_site", source_job_id="R1",
            url="https://acme.wd1.myworkdayjobs.com/en-US/job/R1",
        ), now="2026-01-01T00:00:00Z")
        linkedin, linkedin_created = store.add_job(self.conn, posting(
            source="linkedin", source_job_id="101",
            url="https://www.linkedin.com/jobs/view/101",
        ), now="2026-01-02T00:00:00Z")
        self.assertTrue(created)
        self.assertFalse(linkedin_created)
        self.assertEqual(workday, linkedin)

        renamed, renamed_created = store.add_job(self.conn, posting(
            source="linkedin", source_job_id="101", title="Key Account Executive",
            url="https://www.linkedin.com/jobs/view/101",
        ), now="2026-01-03T00:00:00Z")
        self.assertFalse(renamed_created)
        self.assertEqual(renamed, workday)

        other, other_created = store.add_job(self.conn, posting(
            source="company_site", source_job_id="R2",
            url="https://acme.wd1.myworkdayjobs.com/en-US/job/R2",
        ))
        self.assertTrue(other_created)
        self.assertNotEqual(other, workday)

        row = store.get_job(self.conn, workday)
        self.assertEqual(row["source"], "company_site")
        self.assertEqual(row["source_job_id"], "R1")
        sights = store.job_sightings(self.conn, workday)
        self.assertTrue(all(isinstance(item, dict) for item in sights))
        self.assertEqual(
            [(item["source"], item["source_job_id"]) for item in sights],
            [("company_site", "R1"), ("linkedin", "101")],
        )
        linkedin_sight = sights[1]
        self.assertEqual(linkedin_sight["last_seen_at"], "2026-01-03T00:00:00Z")
        self.assertEqual(linkedin_sight["first_seen_at"], "2026-01-02T00:00:00Z")
        self.assertIn("101", linkedin_sight["url"])

    def test_same_source_requisitions_do_not_merge_by_title(self):
        first, _ = store.add_job(self.conn, posting(
            source="linkedin", source_job_id="101",
            url="https://www.linkedin.com/jobs/view/101",
        ))
        second, created = store.add_job(self.conn, posting(
            source="linkedin", source_job_id="202",
            url="https://www.linkedin.com/jobs/view/202",
        ))
        self.assertTrue(created)
        self.assertNotEqual(first, second)
        self.assertEqual(len(store.job_sightings(self.conn, first)), 1)
        self.assertEqual(store.job_sightings(self.conn, second)[0]["source_job_id"], "202")

    def test_manual_row_keeps_an_empty_primary_id_when_linkedin_matches(self):
        manual, _ = store.add_job(self.conn, posting(
            source="manual", url="https://example.com/careers/acme",
        ))
        again, created = store.add_job(self.conn, posting(
            source="linkedin", source_job_id="55",
            url="https://www.linkedin.com/jobs/view/55",
        ))
        self.assertFalse(created)
        self.assertEqual(again, manual)
        row = store.get_job(self.conn, manual)
        self.assertEqual(row["source"], "manual")
        self.assertIsNone(row["source_job_id"])
        sights = store.job_sightings(self.conn, manual)
        self.assertEqual(sights[1]["source"], "linkedin")
        self.assertEqual(sights[1]["source_job_id"], "55")

    def test_a_shared_url_keeps_both_sources_and_does_not_copy_ids(self):
        manual, _ = store.add_job(self.conn, posting(
            source="manual", url="https://example.com/jobs/9?utm_source=x",
        ), now="2026-02-01T00:00:00Z")
        site, created = store.add_job(self.conn, posting(
            source="company_site", source_job_id="WD-1",
            url="https://www.example.com/jobs/9/",
        ), now="2026-02-02T00:00:00Z")
        self.assertFalse(created)
        self.assertEqual(site, manual)
        self.assertIsNone(store.get_job(self.conn, manual)["source_job_id"])
        by_source = {item["source"]: item for item in store.job_sightings(self.conn, manual)}
        self.assertEqual(set(by_source), {"manual", "company_site"})
        self.assertIsNone(by_source["manual"]["source_job_id"])
        self.assertEqual(by_source["company_site"]["source_job_id"], "WD-1")
        self.assertEqual(by_source["manual"]["url_canonical"], by_source["company_site"]["url_canonical"])
        again, again_created = store.add_job(self.conn, posting(
            source="company_site", url="https://example.com/jobs/9",
        ))
        self.assertFalse(again_created)
        self.assertEqual(again, manual)
        # The LinkedIn URL is only a sighting. The jobs row still has the manual URL.
        linkedin, linkedin_created = store.add_job(self.conn, posting(
            source="linkedin", source_job_id="55",
            url="https://www.linkedin.com/jobs/view/55",
        ), now="2026-02-03T00:00:00Z")
        self.assertFalse(linkedin_created)
        self.assertEqual(store.find_job(self.conn, posting(
            source="jobsdb", title="Something else", company="Other",
            url="https://www.linkedin.com/jobs/view/55?trk=public",
        )), manual)

    def test_a_repeated_manual_add_without_id_or_url_keeps_one_sighting(self):
        first, _ = store.add_job(self.conn, posting(source="manual", url=None),
                                 now="2026-03-01T00:00:00Z")
        second, created = store.add_job(self.conn, posting(source="manual", url=None),
                                        now="2026-03-02T00:00:00Z")
        self.assertFalse(created)
        self.assertEqual(first, second)
        sights = store.job_sightings(self.conn, first)
        self.assertEqual(len(sights), 1)
        self.assertEqual(sights[0]["source"], "manual")
        self.assertIsNone(sights[0]["source_job_id"])
        self.assertIsNone(sights[0]["url_canonical"])
        self.assertEqual(sights[0]["first_seen_at"], "2026-03-01T00:00:00Z")
        self.assertEqual(sights[0]["last_seen_at"], "2026-03-02T00:00:00Z")

    def test_same_source_can_fill_an_empty_primary_id(self):
        first, _ = store.add_job(self.conn, posting(source="linkedin", url=None))
        self.assertIsNone(store.get_job(self.conn, first)["source_job_id"])
        again, created = store.add_job(self.conn, posting(
            source="linkedin", source_job_id="7",
            url="https://www.linkedin.com/jobs/view/7",
        ))
        self.assertFalse(created)
        self.assertEqual(store.get_job(self.conn, again)["source_job_id"], "7")


class Migration(unittest.TestCase):
    def test_backfill_uses_the_job_timestamp_once(self):
        conn = memory_conn(self)
        conn.execute("DROP TABLE job_sightings")
        cur = conn.execute(
            """INSERT INTO jobs
                   (dedupe_key, source, source_job_id, url, url_canonical,
                    company, company_norm, title, title_norm, location, location_norm,
                    first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "src:jobsdb:R9", "jobsdb", "R9",
                "https://th.jobsdb.com/job/R9", "https://th.jobsdb.com/job/R9",
                "Acme", "acme", "Key Account", "key account", "Bangkok", "bangkok",
                "2024-05-01T00:00:00Z", "2024-06-01T00:00:00Z",
            ),
        )
        job_id = int(cur.lastrowid)
        store.insert_score(conn, job_id, {
            "status": "prefiltered",
            "prefilter_rule": "test",
            "verdict": "FILTERED",
            "gate_reasons": [],
            "gate_questions": [],
        })
        store.set_decision(conn, job_id, "skip", note="later")
        conn.commit()

        store.ensure_schema(conn)
        sights = store.job_sightings(conn, job_id)
        self.assertEqual(len(sights), 1)
        self.assertEqual(sights[0]["source"], "jobsdb")
        self.assertEqual(sights[0]["source_job_id"], "R9")
        self.assertEqual(sights[0]["first_seen_at"], "2024-05-01T00:00:00Z")
        self.assertEqual(sights[0]["last_seen_at"], "2024-06-01T00:00:00Z")
        self.assertEqual(store.current_score(conn, job_id)["verdict"], "FILTERED")
        self.assertEqual(store.get_decision(conn, job_id)["action"], "skip")

        store.ensure_schema(conn)
        again = store.job_sightings(conn, job_id)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["first_seen_at"], "2024-05-01T00:00:00Z")
        self.assertEqual(again[0]["last_seen_at"], "2024-06-01T00:00:00Z")
        self.assertEqual(store.get_job(conn, job_id)["source"], "jobsdb")

    def test_migrate_drops_a_unique_url_index_and_keeps_a_nonunique_one(self):
        conn = memory_conn(self)
        conn.execute("DROP INDEX IF EXISTS job_sightings_by_url")
        conn.execute("CREATE UNIQUE INDEX job_sightings_url ON job_sightings (url_canonical)")
        store.ensure_schema(conn)
        unique = {
            row["name"]: row["unique"]
            for row in conn.execute("PRAGMA index_list('job_sightings')")
        }
        self.assertNotIn("job_sightings_url", unique)
        self.assertEqual(unique["job_sightings_by_url"], 0)
        self.assertEqual(unique["job_sightings_source_url"], 1)

        conn.execute("CREATE INDEX job_sightings_url ON job_sightings (url_canonical)")
        store.ensure_schema(conn)
        kept = {
            row["name"]: row["unique"]
            for row in conn.execute("PRAGMA index_list('job_sightings')")
        }
        self.assertEqual(kept["job_sightings_url"], 0)
        self.assertEqual(kept["job_sightings_by_url"], 0)

    def test_a_live_insert_is_not_restamped_by_ensure_schema(self):
        conn = memory_conn(self)
        job_id, _ = store.add_job(conn, posting(
            source="jobsdb", source_job_id="1",
            url="https://th.jobsdb.com/job/1",
        ), now="2024-01-01T00:00:00Z")
        store.ensure_schema(conn)
        sight = store.job_sightings(conn, job_id)[0]
        self.assertEqual(sight["first_seen_at"], "2024-01-01T00:00:00Z")
        self.assertEqual(sight["last_seen_at"], "2024-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
