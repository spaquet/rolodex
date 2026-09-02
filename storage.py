"""SQLite output store. Local file is libsql-compatible (plain sqlite3 file format);
sync/import into Turso separately with `turso db shell <db> < dump` or `turso db import`."""
import csv
import sqlite3
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

TEMP_SCHEMA = """
CREATE TEMP TABLE contact_stage (
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    date TEXT NOT NULL
);
CREATE INDEX contact_stage_email_name ON contact_stage (email, name);
CREATE INDEX contact_stage_email_folder ON contact_stage (email, folder);
CREATE TEMP TABLE signature_candidates (
    sender TEXT NOT NULL,
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    date TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (sender, folder)
);
CREATE TEMP TABLE signature_targets (
    sender TEXT PRIMARY KEY,
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    date TEXT NOT NULL
);
CREATE INDEX signature_targets_folder_uid ON signature_targets (folder, uid);
"""


class ContactStore:
    """Stages (email, name, folder, date) observations and upserts them into
    a sqlite/libsql-compatible `contacts` table on flush().

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
        self._conn.execute("PRAGMA temp_store=FILE")
        self._conn.execute(SCHEMA)
        self._conn.execute(STATE_SCHEMA)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(contacts)")}
        for column in ("signature", "signature_seen"):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE contacts ADD COLUMN {column} TEXT")
        self._conn.commit()
        self._conn.executescript(TEMP_SCHEMA)
        self._contacts: list[tuple[str, str, str, str]] = []
        self._signatures: dict[str, tuple[str, str]] = {}
        self._candidate_ordinal = 0

    def record(
        self,
        email: str,
        name: str,
        folder: str,
        date: str = "",
        signature: str = "",
    ):
        """Register one occurrence of an address in a message.

        Buffered until the next stage() or flush() call.

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
        self._contacts.append((email, name, folder, date))
        previous = self._signatures.get(email)
        if signature and (not previous or (date and (not previous[1] or date >= previous[1]))):
            self._signatures[email] = (signature, date)

    def record_signature(self, email: str, signature: str, date: str = ""):
        """Register an extracted signature for an address without touching
        its message_count/folders/first_seen/last_seen (record() owns those).

        Used when signature extraction is deferred to a second pass over a
        subset of messages (e.g. only the newest per sender), separate from
        the header-only pass that drives record().
        """
        email = email.lower().strip()
        if not email or not signature:
            return
        previous = self._signatures.get(email)
        if not previous or (date and (not previous[1] or date >= previous[1])):
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

    def stage(self) -> int:
        """Move buffered contact observations into disk-backed temporary tables."""
        count = len(self._contacts)
        self._conn.executemany(
            "INSERT INTO contact_stage (email, name, folder, date) VALUES (?, ?, ?, ?)",
            self._contacts,
        )
        self._conn.commit()
        self._contacts.clear()
        return count

    def record_signature_target(self, sender: str, folder: str, uid: bytes, date: str):
        """Stage the newest signature candidate for one sender in one folder."""
        self._candidate_ordinal += 1
        self._conn.execute(
            """
            INSERT INTO signature_candidates (sender, folder, uid, date, ordinal)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sender, folder) DO UPDATE SET
                uid = excluded.uid,
                date = excluded.date,
                ordinal = excluded.ordinal
            WHERE excluded.date != '' AND
                (signature_candidates.date = '' OR excluded.date >= signature_candidates.date)
            """,
            (sender, folder, int(uid), date, self._candidate_ordinal),
        )

    def finalize_signature_targets(self):
        """Choose the single newest candidate per sender across all folders."""
        self._conn.executescript(
            """
            DELETE FROM signature_targets;
            INSERT INTO signature_targets (sender, folder, uid, date)
            SELECT sender, folder, uid, date
            FROM (
                SELECT sender, folder, uid, date,
                    ROW_NUMBER() OVER (
                        PARTITION BY sender
                        ORDER BY date != '' DESC, date DESC, ordinal DESC
                    ) AS rank
                FROM signature_candidates
            )
            WHERE rank = 1;
            DELETE FROM signature_candidates;
            """
        )

    @property
    def signature_target_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM signature_targets").fetchone()[0]

    def signature_target_folders(self) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT folder FROM signature_targets ORDER BY folder"
            )
        ]

    def signature_target_batches(self, folder: str, batch_size: int):
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT uid, sender, date FROM signature_targets WHERE folder = ? ORDER BY uid",
                (folder,),
            )
            while rows := cur.fetchmany(batch_size):
                yield [(str(uid).encode(), sender, date) for uid, sender, date in rows]

    def flush(self, checkpoint: tuple[str, str, str, str, int] | None = None):
        """Write all staged observations to the `contacts` table.

        Safe to call multiple times (e.g. once per folder): each call
        upserts on `email`, adding to message_count and expanding the
        `folders` list for addresses seen before.

        When supplied, the extraction checkpoint is committed atomically
        with the contacts so an interrupted retry cannot double-count them.
        """
        self.stage()
        with closing(self._conn.cursor()) as cur, closing(self._conn.cursor()) as staged:
            rows = staged.execute(
                """
                SELECT contacts.email,
                    (SELECT name FROM contact_stage AS names
                     WHERE names.email = contacts.email AND name != ''
                     GROUP BY name ORDER BY COUNT(*) DESC, name LIMIT 1),
                    COUNT(*),
                    (SELECT GROUP_CONCAT(folder, ',') FROM
                        (SELECT DISTINCT folder FROM contact_stage AS folders
                         WHERE folders.email = contacts.email ORDER BY folder)),
                    COALESCE(MIN(NULLIF(date, '')), ''),
                    COALESCE(MAX(NULLIF(date, '')), '')
                FROM contact_stage AS contacts
                GROUP BY contacts.email
                """
            )
            for email, best_name, count, folders, first_seen, last_seen in rows:
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
                        folders = CASE WHEN excluded.folders != '' THEN excluded.folders ELSE contacts.folders END,
                        first_seen = CASE WHEN excluded.first_seen != ''
                            THEN MIN(COALESCE(contacts.first_seen, excluded.first_seen), excluded.first_seen)
                            ELSE contacts.first_seen END,
                        last_seen = CASE WHEN excluded.last_seen != ''
                            THEN MAX(COALESCE(contacts.last_seen, excluded.last_seen), excluded.last_seen)
                            ELSE contacts.last_seen END,
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
                        folders or "",
                        first_seen,
                        last_seen,
                        None,
                        None,
                    ),
                )
            for email, (signature, signature_seen) in self._signatures.items():
                cur.execute(
                    """
                    INSERT INTO contacts (email, name, message_count, folders, first_seen,
                        last_seen, signature, signature_seen)
                    VALUES (?, NULL, 0, '', '', '', ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        signature = CASE
                            WHEN contacts.signature IS NULL OR
                                (excluded.signature_seen != '' AND
                                 (contacts.signature_seen IS NULL OR
                                  contacts.signature_seen = '' OR
                                  excluded.signature_seen >= contacts.signature_seen))
                            THEN excluded.signature ELSE contacts.signature END,
                        signature_seen = CASE
                            WHEN contacts.signature IS NULL OR
                                (excluded.signature_seen != '' AND
                                 (contacts.signature_seen IS NULL OR
                                  contacts.signature_seen = '' OR
                                  excluded.signature_seen >= contacts.signature_seen))
                            THEN excluded.signature_seen ELSE contacts.signature_seen END
                    """,
                    (email, signature, signature_seen),
                )
            cur.execute("DELETE FROM contact_stage")
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
        self._signatures.clear()

    def discard(self, folder: str | None = None):
        """Discard observations buffered since the last successful flush."""
        self._contacts.clear()
        self._signatures.clear()
        self._conn.execute("DELETE FROM contact_stage")
        if folder is not None:
            self._conn.execute("DELETE FROM signature_candidates WHERE folder = ?", (folder,))
        self._conn.commit()

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
