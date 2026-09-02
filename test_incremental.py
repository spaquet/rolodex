import re
import tempfile
import unittest
from pathlib import Path

import imap_client as im
from storage import ContactStore


class FakeImap:
    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"9 10 11 12"]
        return "OK", [
            (uid + b" FETCH", b"From: A <a@example.com>\r\n\r\n")
            for uid in args[0].split(b",")
        ]


class IncrementalExtractionTest(unittest.TestCase):
    def test_search_filters_reversed_uid_range_result(self):
        self.assertEqual(im.search_uids(FakeImap(), 10), [b"11", b"12"])

    def test_fetch_yields_completed_batches(self):
        batches = list(im.fetch_header_batches(FakeImap(), [b"1", b"2", b"3"], 2))
        self.assertEqual([batch for batch, _ in batches], [[b"1", b"2"], [b"3"]])
        self.assertEqual([len(headers) for _, headers in batches], [2, 1])

    def test_incomplete_header_batch_is_rejected(self):
        class IncompleteImap(FakeImap):
            def uid(self, command, *args):
                return "OK", []

        with self.assertRaises(im.ImapError):
            list(im.fetch_header_batches(IncompleteImap(), [b"1"]))

    def test_signature_fetch_reads_only_preferred_text_parts(self):
        structures = {
            b"1": b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 12 1 NIL NIL NIL NIL)',
            b"2": b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "BASE64" 8 1 NIL NIL NIL NIL) ("APPLICATION" "PDF" ("NAME" "large.pdf") NIL NIL "BASE64" 999999 NIL ("ATTACHMENT" ("FILENAME" "large.pdf")) NIL NIL) "MIXED" ("BOUNDARY" "x") NIL NIL NIL)',
            b"3": b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8" "NAME" "notes.txt") NIL NIL "7BIT" 10 1 NIL ("ATTACHMENT" ("FILENAME" "notes.txt")) NIL NIL) ("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 11 1 NIL NIL NIL NIL) "MIXED" ("BOUNDARY" "x") NIL NIL NIL)',
        }
        bodies = {
            (b"1", "1"): b"Hello=20one",
            (b"2", "1"): b"SGVsbG8gdHdv",
            (b"3", "2"): b"<b>Three</b>",
        }

        class MimeImap:
            def __init__(self):
                self.queries = []

            def uid(self, command, uid_set, query):
                self.queries.append(query)
                uids = uid_set.split(b",")
                if "BODYSTRUCTURE" in query:
                    return "OK", [
                        b"1 (UID " + uid + b" RFC822.SIZE 100000 BODYSTRUCTURE " + structures[uid] + b")"
                        for uid in uids
                    ]
                section = re.search(r"BODY\.PEEK\[([^]]+)\]", query).group(1)
                return "OK", [
                    (
                        b"1 (UID " + uid + f" BODY[{section}]".encode(),
                        bodies[(uid, section)],
                    )
                    for uid in uids
                ]

        conn = MimeImap()
        parts, metrics = im.fetch_text_parts(conn, [b"1", b"2", b"3"])
        self.assertEqual(
            parts,
            [
                (b"1", "Hello one", "plain"),
                (b"2", "Hello two", "plain"),
                (b"3", "<b>Three</b>", "html"),
            ],
        )
        self.assertEqual(metrics["body_bytes"], sum(map(len, bodies.values())))
        self.assertEqual(metrics["message_bytes"], 300000)
        self.assertEqual(
            conn.queries,
            ["(UID RFC822.SIZE BODYSTRUCTURE)", "(UID BODY.PEEK[1])", "(UID BODY.PEEK[2])"],
        )

    def test_bodystructure_literal_is_parsed(self):
        records, _, message_bytes = im._bodystructure_records(
            [
                (
                    b'1 (UID 7 RFC822.SIZE 42 BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" {5}',
                    b"UTF-8",
                ),
                b') NIL NIL "7BIT" 12 1 NIL NIL NIL NIL))',
            ]
        )
        self.assertEqual(message_bytes, 42)
        self.assertEqual(im._preferred_text_part(records[b"7"])[1:4], ("1", "plain", "UTF-8"))

    def test_contacts_and_checkpoint_commit_together(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ContactStore(str(Path(directory) / "contacts.db"))
            store.record("a@example.com", "A", "INBOX")
            store.flush(("imap.example.com", "me", "INBOX", "7", 12))
            self.assertEqual(store.contact_count, 1)
            self.assertEqual(
                store.checkpoint("imap.example.com", "me", "INBOX"), ("7", 12)
            )
            store.close()

    def test_batches_stage_on_disk_but_commit_only_with_folder_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "contacts.db")
            store = ContactStore(db_path)
            store.record("a@example.com", "Alice", "INBOX", "2026-01-02T00:00:00+00:00")
            store.record("a@example.com", "Alice", "INBOX", "2026-01-03T00:00:00+00:00")
            self.assertEqual(store.stage(), 2)
            store.record("a@example.com", "A", "INBOX", "2026-01-01T00:00:00+00:00")
            store.flush(("imap.example.com", "me", "INBOX", "7", 12))
            self.assertEqual(
                store._conn.execute(
                    "SELECT name, message_count, first_seen, last_seen FROM contacts"
                ).fetchone(),
                (
                    "Alice",
                    3,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-03T00:00:00+00:00",
                ),
            )
            store.close()

            store = ContactStore(db_path)
            store.record("lost@example.com", "Lost", "INBOX")
            store.stage()
            store.close()
            store = ContactStore(db_path)
            self.assertIsNone(
                store._conn.execute(
                    "SELECT email FROM contacts WHERE email = 'lost@example.com'"
                ).fetchone()
            )
            store.close()

    def test_signature_candidates_are_disk_backed_and_newest_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ContactStore(str(Path(directory) / "contacts.db"))
            store.record_signature_target("a@example.com", "INBOX", b"1", "2026-01-01")
            store.stage()
            store.record_signature_target("a@example.com", "Archive", b"2", "2026-01-02")
            store.record_signature_target("b@example.com", "INBOX", b"3", "")
            store.stage()
            store.finalize_signature_targets()
            self.assertEqual(store.signature_target_count, 2)
            self.assertEqual(
                list(store.signature_target_batches("Archive", 20)),
                [[(b"2", "a@example.com", "2026-01-02")]],
            )
            self.assertEqual(
                list(store.signature_target_batches("INBOX", 20)),
                [[(b"3", "b@example.com", "")]],
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
