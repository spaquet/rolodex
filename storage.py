"""SQLite output store. Local file is libsql-compatible (plain sqlite3 file format);
sync/import into Turso separately with `turso db shell <db> < dump` or `turso db import`."""
import sqlite3
from collections import defaultdict
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    email TEXT PRIMARY KEY,
    name TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    folders TEXT NOT NULL DEFAULT '',
    first_seen TEXT,
    last_seen TEXT
);
"""


class ContactStore:
    """Accumulates (email, name, folder, date) observations in memory and
    upserts them into a sqlite/libsql-compatible `contacts` table on flush().

    Name-conflict resolution is most-frequent-wins: if the same address
    appears with several display names, the one seen most often survives.
    """

    def __init__(self, db_path: str):
        """Open (creating if needed) the sqlite file and ensure the schema exists.

        Args:
            db_path: Path to the sqlite/libsql database file.
        """
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(SCHEMA)
        self._conn.commit()
        # per-email tally of {display_name: occurrence_count}, used to pick
        # the most-frequent name for that address at flush() time
        self._name_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._msg_count: dict[str, int] = defaultdict(int)
        self._folders: dict[str, set] = defaultdict(set)
        self._first_seen: dict[str, str] = {}
        self._last_seen: dict[str, str] = {}

    def record(self, email: str, name: str, folder: str, date: str = ""):
        """Register one occurrence of an address in a message.

        Buffered in memory only; call flush() to write to the database.

        Args:
            email: Address as found in a From/To/Cc header. Case-folded
                and stripped before use as the dedup key.
            name: Display name paired with the address in that header, if any.
            folder: IMAP folder the message was found in.
            date: ISO-8601 message date, if it could be parsed.
        """
        email = email.lower().strip()
        if not email:
            return
        if name:
            self._name_counts[email][name] += 1
        self._msg_count[email] += 1
        self._folders[email].add(folder)
        if date:
            if email not in self._first_seen or date < self._first_seen[email]:
                self._first_seen[email] = date
            if email not in self._last_seen or date > self._last_seen[email]:
                self._last_seen[email] = date

    def flush(self):
        """Write all buffered observations to the `contacts` table.

        Safe to call multiple times (e.g. once per folder): each call
        upserts on `email`, adding to message_count and expanding the
        `folders` list for addresses seen before.
        """
        with closing(self._conn.cursor()) as cur:
            for email, count in self._msg_count.items():
                names = self._name_counts.get(email, {})
                best_name = max(names.items(), key=lambda kv: kv[1])[0] if names else None
                folders = ",".join(sorted(self._folders[email]))
                cur.execute(
                    """
                    INSERT INTO contacts (email, name, message_count, folders, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        name = CASE WHEN excluded.name IS NOT NULL THEN excluded.name ELSE contacts.name END,
                        message_count = contacts.message_count + excluded.message_count,
                        folders = excluded.folders,
                        first_seen = MIN(COALESCE(contacts.first_seen, excluded.first_seen), excluded.first_seen),
                        last_seen = MAX(COALESCE(contacts.last_seen, excluded.last_seen), excluded.last_seen)
                    """,
                    (
                        email,
                        best_name,
                        count,
                        folders,
                        self._first_seen.get(email, ""),
                        self._last_seen.get(email, ""),
                    ),
                )
        self._conn.commit()

    def close(self):
        """Close the underlying sqlite connection."""
        self._conn.close()

    @property
    def contact_count(self) -> int:
        """Number of distinct email addresses recorded so far."""
        return len(self._msg_count)
