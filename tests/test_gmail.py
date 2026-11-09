import unittest
from datetime import datetime, timedelta

from _util import CFG

from jobhunt.sources import gmail

ALLOWED = {"login", "select", "search", "fetch", "logout"}


def raw_message(sender: str, subject: str = "Your job alerts") -> bytes:
    return (f"From: {sender}\r\nSubject: {subject}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n<html><body>jobs</body></html>"
            ).encode("utf-8")


class FakeImap:
    """A fake IMAP server that records every command it is asked to run."""

    def __init__(self, messages, unseen=2, unseen_after=None):
        self.calls = []
        self.messages = messages            # (uid bytes, internaldate str, raw bytes)
        self.unseen = unseen
        self.unseen_after = unseen_after
        self.unseen_reads = 0
        self.logged_in_as = None

    def login(self, user, password):
        self.calls.append(("login", user))
        self.logged_in_as = (user, password)
        return "OK", [b""]

    def select(self, mailbox="INBOX", readonly=True):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.calls.append(("search", charset) + tuple(criteria))
        if criteria and criteria[0] == "UNSEEN":
            self.unseen_reads += 1
            count = self.unseen if self.unseen_reads == 1 else (
                self.unseen if self.unseen_after is None else self.unseen_after)
            return "OK", [b" ".join(str(i).encode() for i in range(count))]
        return "OK", [b" ".join(uid for uid, _, _ in self.messages)]

    def fetch(self, message_set, message_parts):
        self.calls.append(("fetch", message_set, message_parts))
        for uid, stamp, raw in self.messages:
            if uid == message_set:
                head = (b'1 (INTERNALDATE "' + stamp.encode() + b'" BODY[] {%d}' % len(raw))
                return "OK", [(head, raw), b")"]
        return "OK", []

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]

    # commands that must never be used
    def store(self, *args):
        self.calls.append(("store",) + args)
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge",))
        return "OK", [b""]

    def append(self, *args):
        self.calls.append(("append",) + args)
        return "OK", [b""]


def make_reader(messages, unseen=2, unseen_after=None, now=None):
    imap = FakeImap(messages, unseen=unseen, unseen_after=unseen_after)
    reader = gmail.GmailReader(
        CFG,
        imap_factory=lambda host: imap,
        password="app-password",
        now=now or (lambda: datetime(2026, 11, 7, 12, 0, tzinfo=gmail.BANGKOK)),
    )
    return reader, imap


class Reading(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=gmail.BANGKOK)
        self.messages = [
            (b"1", "07-Nov-2026 09:12:00 +0700", raw_message("LinkedIn <jobalerts-noreply@linkedin.com>")),
            (b"2", "06-Nov-2026 10:00:00 +0700", raw_message("JobsDB <alerts@jobsdb.com>")),
            (b"3", "01-Nov-2026 10:00:00 +0700", raw_message("Some newsletter <news@example.com>")),
        ]

    def test_only_alert_senders_are_returned(self):
        reader, imap = make_reader(self.messages, now=lambda: self.now)
        found = reader.fetch_messages(3)
        self.assertEqual([m["uid"] for m in found], ["1", "2"])
        self.assertEqual(found[0]["subject"], "Your job alerts")

    def test_the_window_is_filtered_on_the_internal_date_in_bangkok(self):
        reader, imap = make_reader(self.messages, now=lambda: self.now)
        found = reader.fetch_messages(3)
        self.assertTrue(all(m["internal_date"] >= self.now - timedelta(days=3) for m in found))
        self.assertEqual(found[0]["internal_date"].tzinfo.utcoffset(None),
                         timedelta(hours=7))

    def test_the_search_asks_for_the_right_window(self):
        reader, imap = make_reader(self.messages, now=lambda: self.now)
        reader.fetch_messages(14)
        search_calls = [c for c in imap.calls if c[0] == "search" and "SINCE" in c]
        self.assertEqual(search_calls[0], ("search", None, "SINCE", "24-Oct-2026"))


class ReadOnlyGuarantees(unittest.TestCase):
    def setUp(self):
        self.messages = [
            (b"1", "07-Nov-2026 09:12:00 +0700", raw_message("LinkedIn <jobalerts-noreply@linkedin.com>")),
        ]

    def test_the_command_allow_list(self):
        reader, imap = make_reader(self.messages)
        reader.fetch_messages(3)
        used = {call[0] for call in imap.calls}
        self.assertTrue(used <= ALLOWED, f"unexpected IMAP commands: {used - ALLOWED}")

    def test_the_mailbox_is_opened_with_examine(self):
        reader, imap = make_reader(self.messages)
        reader.fetch_messages(3)
        selects = [c for c in imap.calls if c[0] == "select"]
        self.assertEqual(selects, [("select", "INBOX", True)])

    def test_every_fetch_uses_peek(self):
        reader, imap = make_reader(self.messages)
        reader.fetch_messages(3)
        fetches = [c for c in imap.calls if c[0] == "fetch"]
        self.assertTrue(fetches)
        for call in fetches:
            self.assertIn("BODY.PEEK[", call[2])

    def test_the_unread_count_is_checked_before_and_after(self):
        reader, imap = make_reader(self.messages)
        reader.fetch_messages(3)
        unseen_calls = [c for c in imap.calls if c[0] == "search" and "UNSEEN" in c]
        self.assertEqual(len(unseen_calls), 2)

    def test_a_changed_unread_count_aborts(self):
        reader, imap = make_reader(self.messages, unseen=2, unseen_after=1)
        with self.assertRaises(gmail.GmailError) as ctx:
            reader.fetch_messages(3)
        self.assertIn("unread count changed", str(ctx.exception))

    def test_the_wrapper_refuses_a_mutating_call(self):
        conn = gmail.ReadOnlyConnection(FakeImap([]))
        for name in ("store", "expunge", "append", "copy", "delete"):
            with self.assertRaises(gmail.GmailError):
                getattr(conn, name)
        self.assertEqual(conn._conn.calls, [])

    def test_the_wrapper_refuses_select_without_readonly(self):
        conn = gmail.ReadOnlyConnection(FakeImap([]))
        with self.assertRaises(gmail.GmailError):
            conn.select("INBOX", readonly=False)

    def test_the_wrapper_refuses_a_fetch_without_peek(self):
        conn = gmail.ReadOnlyConnection(FakeImap([]))
        conn.login("me@example.com", "x")
        with self.assertRaises(gmail.GmailError):
            conn.fetch(b"1", "(BODY[])")
        with self.assertRaises(gmail.GmailError):
            conn.fetch(b"1", "(RFC822)")


class Password(unittest.TestCase):
    def test_the_keychain_value_is_used_and_trimmed(self):
        calls = []

        class Done:
            returncode = 0
            stdout = "abcd efgh ijkl mnop\n"

        def runner(argv, **kwargs):
            calls.append(argv)
            return Done()

        password = gmail.keychain_password("jobhunt-gmail", "alex.rivera@example.com", runner)
        self.assertEqual(password, "abcd efgh ijkl mnop")
        self.assertEqual(calls[0][:2], [gmail.SECURITY, "find-generic-password"])
        self.assertIn("-w", calls[0])

    def test_a_missing_keychain_entry_is_reported_without_the_password(self):
        class Done:
            returncode = 44
            stdout = "hunter2"

        with self.assertRaises(gmail.GmailError) as ctx:
            gmail.keychain_password("svc", "acct", lambda argv, **kw: Done())
        self.assertIn("Keychain", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_the_password_reaches_login_but_is_never_returned(self):
        imap = FakeImap([])
        reader = gmail.GmailReader(CFG, imap_factory=lambda host: imap,
                                   password_reader=lambda service, account: "s3cret")
        messages = reader.fetch_messages(3)
        self.assertEqual(messages, [])
        self.assertEqual(imap.logged_in_as, (CFG.profile["gmail"]["account"], "s3cret"))
        self.assertNotIn("s3cret", str(messages))


if __name__ == "__main__":
    unittest.main()
