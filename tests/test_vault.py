import tempfile
import unittest
from collections import Counter
from pathlib import Path

from _vault import copy_real_vault, make_vault, record_text

from jobhunt import vault as vault_mod
from jobhunt.vault import Vault, parse_record


def changed_keys(before: list[str], after: list[str]) -> list[str]:
    """Keys whose lines differ, robust to a key being inserted (line numbers shift)."""
    removed = Counter(before) - Counter(after)
    added = Counter(after) - Counter(before)
    keys = set()
    for line in list(removed) + list(added):
        keys.add(line.split(":")[0].strip())
    return sorted(keys)


class Reading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = make_vault(self.tmp.name)

    def test_records_are_parsed_from_frontmatter(self):
        records = {record.get("company"): record for record in self.vault.records()}
        acme = records["Acme"]
        self.assertEqual(acme.get("my_status"), "To apply")
        self.assertEqual(acme.get("program"), "Management Trainee")
        self.assertEqual(acme.get("tags"), ["type/record", "db/applications"])
        self.assertIn("Record in [[2027 Applications]]", acme.body)

    def test_the_relative_path_is_reported_from_the_records_folder(self):
        acme = self.vault.records()[0]
        self.assertTrue(acme.rel.startswith("61 Application Records/"))
        self.assertTrue(acme.rel.endswith(".md"))

    def test_the_obsidian_url_is_escaped(self):
        record = self.vault.records()[0]
        url = record.obsidian_url()
        self.assertTrue(url.startswith("obsidian://open?vault=Career-Wiki&file="))
        self.assertIn("%20", url)

    def test_a_note_without_frontmatter_is_ignored(self):
        (self.vault.path / "notes.md").write_text("just text", encoding="utf-8")
        self.assertNotIn("notes.md", [r.file_name for r in self.vault.records()])

    def test_the_records_folder_accepts_the_profile_form(self):
        # config/profile.toml writes "wiki/61 Application Records"
        prefixed = Vault(self.vault.root, "wiki/61 Application Records")
        self.assertEqual(prefixed.path, self.vault.path)
        self.assertEqual(len(prefixed.records()), len(self.vault.records()))
        self.assertEqual(prefixed.record(prefixed.records()[0].rel).get("company"), "Acme")

    def test_missing_folder_gives_no_records(self):
        self.assertEqual(Vault(Path(self.tmp.name) / "nowhere").records(), [])


class Writing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = make_vault(self.tmp.name)

    def test_an_update_changes_only_the_given_keys(self):
        record = parse_record(self.vault.path / "Acme — Management Trainee.md")
        before = (self.vault.path / "Acme — Management Trainee.md").read_text(encoding="utf-8").splitlines()
        self.vault.update_keys(record, {"my_status": "Applied", "date_applied": "2026-11-07",
                                        "updated": "2026-11-07"})
        after = (self.vault.path / "Acme — Management Trainee.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(after), len(before) + 1)          # one new key line added
        self.assertEqual(changed_keys(before, after),
                         ["date_applied", "my_status", "updated"])

    def test_a_key_that_does_not_exist_is_appended(self):
        record = parse_record(self.vault.path / "Beta Corp — Analyst.md")
        self.assertIsNone(record.get("deadline"))
        self.vault.update_keys(record, {"deadline": "2026-12-31"})
        again = parse_record(self.vault.path / "Beta Corp — Analyst.md")
        self.assertEqual(again.get("deadline"), "2026-12-31")
        self.assertEqual(again.get("my_status"), "Applied")   # untouched

    def test_the_body_survives_an_edit(self):
        record = parse_record(self.vault.path / "Acme — Management Trainee.md")
        self.vault.update_keys(record, {"my_status": "Applied"})
        text = (self.vault.path / "Acme — Management Trainee.md").read_text(encoding="utf-8")
        self.assertTrue(text.rstrip().endswith("Record in [[2027 Applications]] (Applications)."))

    def test_no_temp_files_are_left_behind(self):
        record = parse_record(self.vault.path / "Acme — Management Trainee.md")
        self.vault.update_keys(record, {"my_status": "Applied"})
        names = [path.name for path in self.vault.path.iterdir()]
        self.assertFalse([name for name in names if name.startswith(".jobhunt-")], names)

    def test_a_new_record_uses_the_contract_key_order(self):
        fields = {
            "title": "Acme — New Role", "type": "record", "folder": "61 Application Records",
            "status": "current", "updated": "2026-11-07", "summary": "s", "database": "Applications",
            "link": "u", "apply_link": "u", "company": "Acme", "program": "New Role",
            "section": "Apply now", "my_status": "To apply", "next_step": "n", "do_by": "2026-11-10",
            "note": "n", "job_platform": "LinkedIn", "location": "Bangkok", "job_type": "Full-time",
            "ikigai": "L5 G4 N4 P3e", "ikigai_verdict": "CENTRE", "jobhunt_id": 7,
            "tags": ["type/record", "db/applications"],
        }
        record = self.vault.new_record(fields)
        keys = [line.split(":")[0] for line in record.lines]
        self.assertEqual(keys, [key for key in vault_mod.RECORD_KEYS if key in fields])
        # deadline is omitted when the JD states none, and the rest keep their order
        self.assertNotIn("deadline", keys)

    def test_a_colliding_filename_gets_a_number(self):
        fields = {"title": "Acme — Management Trainee", "company": "Acme", "program": "X"}
        first = self.vault.new_record(dict(fields))
        self.vault.write_record(first)
        self.assertEqual(first.path.name, "Acme — Management Trainee (2).md")
        self.vault.write_record(first)
        second = self.vault.new_record(dict(fields))
        self.assertEqual(second.path.name, "Acme — Management Trainee (3).md")
        self.assertNotEqual(first.path, second.path)

    def test_writing_outside_the_records_folder_is_refused(self):
        outside = vault_mod.Record(path=Path(self.tmp.name) / "elsewhere.md", fields={}, lines=[], body="")
        with self.assertRaises(vault_mod.VaultError):
            self.vault.write_record(outside)

    def test_reading_outside_the_records_folder_is_refused(self):
        with self.assertRaises(vault_mod.VaultError):
            self.vault.record("61 Application Records/../../secret.md")

    def test_nothing_is_ever_deleted(self):
        before = sorted(path.name for path in self.vault.path.iterdir())
        record = parse_record(self.vault.path / "Acme — Management Trainee.md")
        self.vault.update_keys(record, {"my_status": "Applied"})
        self.vault.write_record(self.vault.new_record({"title": "Another", "company": "X"}))
        after = sorted(path.name for path in self.vault.path.iterdir())
        self.assertEqual(set(before) - set(after), set())


class RealVaultCopy(unittest.TestCase):
    """The real records, copied first: proof that an edit is surgical."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = copy_real_vault(self.tmp.name)
        if self.vault is None:
            self.skipTest("real vault not present")

    def test_all_real_records_parse(self):
        records = self.vault.records()
        self.assertGreater(len(records), 50)
        for record in records:
            self.assertTrue(record.get("title"), record.path.name)

    def test_marking_one_real_record_submitted_touches_four_keys(self):
        target = next(r for r in self.vault.records() if str(r.get("my_status")) == "To apply")
        before = target.path.read_text(encoding="utf-8").splitlines()
        updated = self.vault.update_keys(target, {
            "my_status": "Applied", "date_applied": "2026-11-07", "updated": "2026-11-07",
            "summary": vault_mod.summary_line(vault_mod.Record(
                path=target.path,
                fields={**target.fields, "my_status": "Applied"},
                lines=target.lines)),
        })
        after = updated.path.read_text(encoding="utf-8").splitlines()
        before_keys = {line.split(":")[0].strip() for line in before}
        self.assertEqual(len(after), len(before) + (0 if "date_applied" in before_keys else 1))
        self.assertEqual(changed_keys(before, after),
                         ["date_applied", "my_status", "summary", "updated"])


if __name__ == "__main__":
    unittest.main()
