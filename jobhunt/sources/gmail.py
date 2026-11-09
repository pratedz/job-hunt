"""Gmail over IMAP, read-only. Shell only until real alert samples exist.

WORKSPLIT constraint 1: the mailbox is opened read-only and nothing may change. That
is enforced in code, not only in tests:

* the connection is wrapped so ``STORE``, ``COPY``, ``MOVE``, ``EXPUNGE``,
  ``DELETE``, ``APPEND`` and the other mutating commands raise immediately;
* ``select`` refuses ``readonly=False`` (so the wire command is always ``EXAMINE``,
  never ``SELECT``);
* ``fetch`` refuses any fetch that is not ``BODY.PEEK[...]``, because a plain
  ``BODY[]`` would set ``\\Seen``;
* the UNSEEN count is read before and after, and a change aborts the run.

The app password comes from the macOS Keychain and is never logged, never stored
and never written into a fixture.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
import subprocess
from datetime import datetime, timedelta, timezone

BANGKOK = timezone(timedelta(hours=7))
SECURITY = "/usr/bin/security"
FORBIDDEN = {
    "store", "copy", "move", "expunge", "delete", "append", "create", "rename",
    "subscribe", "unsubscribe", "setacl", "deleteacl", "setquota", "setannotation",
    "starttls", "shutdown", "open", "recent",
}
_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')


class GmailError(RuntimeError):
    pass


def keychain_password(service: str, account: str, runner=None) -> str:
    """Read the app password from the Keychain. Never echo it."""
    runner = runner or subprocess.run
    argv = [SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"]
    try:
        completed = runner(argv, capture_output=True, text=True)
    except OSError as exc:
        raise GmailError(f"could not run the keychain lookup: {exc}") from exc
    if getattr(completed, "returncode", 1) != 0:
        raise GmailError(
            "no Gmail app password in the Keychain for service "
            f"{service!r}/{account!r} (see WORKSPLIT open question 1)")
    return (completed.stdout or "").strip()


class ReadOnlyConnection:
    """Wraps an IMAP connection and refuses anything that could change the mailbox."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        if name.lower() in FORBIDDEN:
            raise GmailError(f"refusing IMAP {name.upper()}: this client is read-only")
        return getattr(self._conn, name)

    def select(self, mailbox: str = "INBOX", readonly: bool = True):
        if not readonly:
            raise GmailError("refusing SELECT: only EXAMINE (readonly=True) is allowed")
        return self._conn.select(mailbox, readonly=True)

    def fetch(self, message_set, message_parts="(BODY.PEEK[])"):
        if "PEEK" not in str(message_parts).upper():
            raise GmailError("refusing fetch without PEEK: it would mark mail as read")
        return self._conn.fetch(message_set, message_parts)

    def logout(self):
        return self._conn.logout()


def _parse_fetch(items) -> tuple[datetime, bytes] | None:
    """Pull (internal date, raw message) out of one imaplib fetch response."""
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        head, raw = item
        match = _INTERNALDATE.search(head if isinstance(head, bytes) else str(head).encode())
        if match and isinstance(raw, bytes):
            stamp = email.utils.parsedate_to_datetime(match.group(1).decode())
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=BANGKOK)
            return stamp, raw
    return None


def _sender_allowed(sender: str, domains: list[str]) -> bool:
    lowered = (sender or "").casefold()
    return any(domain.casefold() in lowered for domain in domains or [])


class GmailReader:
    def __init__(self, cfg, imap_factory=None, password=None, password_reader=None,
                 now=None, log=None):
        gmail = cfg.profile.get("gmail", {})
        self.account = gmail.get("account")
        self.host = gmail.get("imap_host", "imap.gmail.com")
        self.service = gmail.get("keychain_service", "jobhunt-gmail")
        self.domains = gmail.get("alert_from_domains", [])
        self.imap_factory = imap_factory or (lambda host: imaplib.IMAP4_SSL(host))
        self.password = password
        self.password_reader = password_reader or keychain_password
        self.now = now or (lambda: datetime.now(BANGKOK))
        self.log = log or (lambda *a, **k: None)

    def _password(self) -> str:
        if self.password is None:
            self.password = self.password_reader(self.service, self.account)
        return self.password

    def _unseen_count(self, conn) -> int:
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            raise GmailError("could not count unread mail")
        return len((data or [b""])[0].split())

    def fetch_messages(self, days: float, mailbox: str = "INBOX",
                       include_all: bool = False) -> list[dict]:
        """Emails inside the window, never modified.

        By default only the job-alert senders are returned (discovery); ``include_all``
        returns everything, which the status scan needs because employer mail does not
        come from the alert senders.
        """
        now = self.now()
        cutoff = now - timedelta(days=float(days))
        conn = ReadOnlyConnection(self.imap_factory(self.host))
        try:
            conn.login(self.account, self._password())
            status, _ = conn.select(mailbox, readonly=True)
            if status != "OK":
                raise GmailError(f"could not EXAMINE {mailbox}")
            unseen_before = self._unseen_count(conn)

            status, data = conn.search(None, "SINCE", cutoff.strftime("%d-%b-%Y"))
            if status != "OK":
                raise GmailError("IMAP SEARCH failed")
            uids = (data or [b""])[0].split()

            messages = []
            for uid in uids:
                status, items = conn.fetch(uid, "(INTERNALDATE BODY.PEEK[])")
                if status != "OK":
                    continue
                parsed = _parse_fetch(items)
                if parsed is None:
                    continue
                internal_date, raw = parsed
                if internal_date < cutoff:
                    continue
                message = email.message_from_bytes(raw)
                sender = message.get("From", "")
                if not include_all and not _sender_allowed(sender, self.domains):
                    continue
                messages.append({
                    "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "internal_date": internal_date.astimezone(BANGKOK),
                    "sender": sender,
                    "subject": message.get("Subject", ""),
                    "raw": raw,
                })

            unseen_after = self._unseen_count(conn)
            if unseen_after != unseen_before:
                raise GmailError(
                    f"unread count changed during the read ({unseen_before} -> {unseen_after}); "
                    "the mailbox was modified, aborting")
            self.log(f"gmail: {len(messages)} alert messages in the last {days:g} days")
            return messages
        finally:
            try:
                conn.logout()
            except Exception:  # logout must never mask the real error
                pass
