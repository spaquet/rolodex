"""SQLite output store. Local file is libsql-compatible (plain sqlite3 file format);
sync/import into Turso separately with `turso db shell <db> < dump` or `turso db import`."""
import csv
import sqlite3
from collections import defaultdict
from contextlib import closing

SORTABLE_COLUMNS = {"name", "email", "message_count", "first_seen", "last_seen"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    email TEXT PRIMARY KEY,
    name TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    folders TEXT NOT NULL DEFAULT '',
    first_seen TEXT,
    last_seen TEXT,
    signature TEXT,
    signature_seen TEXT
);
"""

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_state (
    host TEXT NOT NULL,
    username TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity TEXT NOT NULL,
    last_uid INTEGER NOT NULL,
    PRIMARY KEY (host, username, folder)
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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(SCHEMA)
        self._conn.execute(STATE_SCHEMA)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(contacts)")}
        for column in ("signature", "signature_seen"):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE contacts ADD COLUMN {column} TEXT")
        self._conn.commit()
        # per-email tally of {display_name: occurrence_count}, used to pick
        # the most-frequent name for that address at flush() time
        self._name_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._msg_count: dict[str, int] = defaultdict(int)
        self._folders: dict[str, set] = defaultdict(set)
        self._first_seen: dict[str, str] = {}
        self._last_seen: dict[str, str] = {}
        self._signatures: dict[str, tuple[str, str]] = {}

    def record(
        self,
        email: str,
        name: str,
        folder: str,
        date: str = "",
        signature: str = "",
    ):
        """Register one occurrence of an address in a message.

        Buffered in memory only; call flush() to write to the database.

        Args:
            email: Address as found in a From/To/Cc header. Case-folded
                and stripped before use as the dedup key.
            name: Display name paired with the address in that header, if any.
            folder: IMAP folder the message was found in.
            date: ISO-8601 message date, if it could be parsed.
            signature: Signature extracted from a message sent by this address.
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
        previous = self._signatures.get(email)
        if signature and (not previous or (date and (not previous[1] or date >= previous[1]))):
            self._signatures[email] = (signature, date)

    def checkpoint(self, host: str, username: str, folder: str) -> tuple[str, int] | None:
        """Return the saved (UIDVALIDITY, last UID) for one account folder."""
        return self._conn.execute(
            """
            SELECT uidvalidity, last_uid FROM extraction_state
            WHERE host = ? AND username = ? AND folder = ?
            """,
            (host, username, folder),
        ).fetchone()

    def flush(self, checkpoint: tuple[str, str, str, str, int] | None = None):
        """Write all buffered observations to the `contacts` table.

        Safe to call multiple times (e.g. once per folder): each call
        upserts on `email`, adding to message_count and expanding the
        `folders` list for addresses seen before.

        When supplied, the extraction checkpoint is committed atomically
        with the contacts so an interrupted retry cannot double-count them.
        """
        with closing(self._conn.cursor()) as cur:
            for email, count in self._msg_count.items():
                names = self._name_counts.get(email, {})
                best_name = max(names.items(), key=lambda kv: kv[1])[0] if names else None
                folders = ",".join(sorted(self._folders[email]))
                signature, signature_seen = self._signatures.get(email, (None, None))
                cur.execute(
                    """
                    INSERT INTO contacts (
                        email, name, message_count, folders, first_seen, last_seen,
                        signature, signature_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        name = CASE WHEN excluded.name IS NOT NULL THEN excluded.name ELSE contacts.name END,
                        message_count = contacts.message_count + excluded.message_count,
                        folders = excluded.folders,
                        first_seen = MIN(COALESCE(contacts.first_seen, excluded.first_seen), excluded.first_seen),
                        last_seen = MAX(COALESCE(contacts.last_seen, excluded.last_seen), excluded.last_seen),
                        signature = CASE
                            WHEN excluded.signature IS NULL THEN contacts.signature
                            WHEN contacts.signature IS NULL THEN excluded.signature
                            WHEN excluded.signature_seen != '' AND
                                 (contacts.signature_seen IS NULL OR
                                  contacts.signature_seen = '' OR
                                  excluded.signature_seen >= contacts.signature_seen)
                            THEN excluded.signature
                            ELSE contacts.signature
                        END,
                        signature_seen = CASE
                            WHEN excluded.signature IS NULL THEN contacts.signature_seen
                            WHEN contacts.signature IS NULL THEN excluded.signature_seen
                            WHEN excluded.signature_seen != '' AND
                                 (contacts.signature_seen IS NULL OR
                                  contacts.signature_seen = '' OR
                                  excluded.signature_seen >= contacts.signature_seen)
                            THEN excluded.signature_seen
                            ELSE contacts.signature_seen
                        END
                    """,
                    (
                        email,
                        best_name,
                        count,
                        folders,
                        self._first_seen.get(email, ""),
                        self._last_seen.get(email, ""),
                        signature,
                        signature_seen,
                    ),
                )
            if checkpoint:
                cur.execute(
                    """
                    INSERT INTO extraction_state (host, username, folder, uidvalidity, last_uid)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(host, username, folder) DO UPDATE SET
                        uidvalidity = excluded.uidvalidity,
                        last_uid = excluded.last_uid
                    """,
                    checkpoint,
                )
        self._conn.commit()
        self.discard()

    def discard(self):
        """Discard observations buffered since the last successful flush."""
        self._name_counts.clear()
        self._msg_count.clear()
        self._folders.clear()
        self._first_seen.clear()
        self._last_seen.clear()
        self._signatures.clear()

    def close(self):
        """Close the underlying sqlite connection."""
        self._conn.close()

    @property
    def contact_count(self) -> int:
        """Number of distinct email addresses written to the db so far (flushed rows)."""
        return self._conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

    def distinct_folders(self) -> list[str]:
        """All distinct folder names present across stored contacts, sorted."""
        rows = self._conn.execute("SELECT DISTINCT folders FROM contacts WHERE folders != ''").fetchall()
        names: set[str] = set()
        for (folders,) in rows:
            names.update(folders.split(","))
        return sorted(names)

    @staticmethod
    def _build_where(search: str, date_from: str, date_to: str, folder: str):
        """Build a WHERE clause + params for the contacts table filters shared
        by search() and export_csv()."""
        clauses = []
        params: list = []
        if search:
            clauses.append("(name LIKE ? OR email LIKE ?)")
            like = f"%{search}%"
            params += [like, like]
        if date_from:
            clauses.append("last_seen >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("last_seen <= ?")
            params.append(date_to)
        if folder:
            clauses.append("(',' || folders || ',') LIKE ?")
            params.append(f"%,{folder},%")
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where_sql, params

    def search(
        self,
        search: str = "",
        date_from: str = "",
        date_to: str = "",
        folder: str = "",
        order_by: str = "last_seen",
        descending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[tuple], int]:
        """Query stored contacts with optional text/date/folder filters, paginated.

        Args:
            search: Substring matched (case-sensitive per sqlite LIKE) against
                name or email.
            date_from / date_to: Inclusive ISO-8601 bounds on last_seen.
            folder: Restrict to contacts seen in this exact folder name.
            order_by: Column to sort by; must be one of SORTABLE_COLUMNS.
            descending: Sort direction.
            limit / offset: Page window.

        Returns:
            (rows, total) where rows are
            (email, name, message_count, folders, first_seen, last_seen)
            tuples for the requested page, and total is the full match count
            across all pages.
        """
        if order_by not in SORTABLE_COLUMNS:
            order_by = "last_seen"
        where_sql, params = self._build_where(search, date_from, date_to, folder)
        direction = "DESC" if descending else "ASC"
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM contacts{where_sql}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"""
            SELECT email, name, message_count, folders, first_seen, last_seen
            FROM contacts{where_sql}
            ORDER BY {order_by} {direction}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return rows, total

    def export_csv(
        self,
        path: str,
        search: str = "",
        date_from: str = "",
        date_to: str = "",
        folder: str = "",
    ) -> int:
        """Write every contact matching the given filters (all pages) to a CSV file.

        Returns:
            Number of rows written.
        """
        where_sql, params = self._build_where(search, date_from, date_to, folder)
        rows = self._conn.execute(
            f"""
            SELECT email, name, message_count, folders, first_seen, last_seen,
                   signature, signature_seen
            FROM contacts{where_sql}
            ORDER BY name
            """,
            params,
        ).fetchall()
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "email", "name", "message_count", "folders", "first_seen",
                "last_seen", "signature", "signature_seen",
            ])
            writer.writerows(rows)
        return len(rows)
