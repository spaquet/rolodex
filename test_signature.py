import csv
import tempfile
import unittest
from pathlib import Path

import imap_client as im
import talon
from storage import ContactStore


RAW_MESSAGE = b"""From: Jane Doe <jane@example.com>
To: Me <me@example.com>
Date: Tue, 01 Sep 2026 10:00:00 -0700
Content-Type: text/plain; charset=utf-8

Hello there.

Jane Doe
Example Corp

On Monday, Old Sender wrote:
Old Sender
Old Corp
"""


class SignatureExtractionTest(unittest.TestCase):
    def test_malformed_message_is_skipped_without_aborting_the_batch(self):
        talon.init()

        def parse_message(raw_message):
            if raw_message == b"malformed":
                raise ValueError("boom")
            return im.parse_message(raw_message)

        with tempfile.TemporaryDirectory() as directory:
            store = ContactStore(str(Path(directory) / "contacts.db"))
            for raw_message in (b"malformed", RAW_MESSAGE):
                try:
                    addresses, date, sender, body, subtype = parse_message(raw_message)
                    found = im.extract_signature(body, subtype, sender)
                except Exception:
                    continue
                for name, address in addresses:
                    store.record(address, name, "INBOX", date, found if address.lower() == sender else "")
            store.flush()
            row = store._conn.execute(
                "SELECT signature FROM contacts WHERE email = ?", ("jane@example.com",)
            ).fetchone()
            self.assertEqual(row[0], "Jane Doe\nExample Corp")
            store.close()

    def test_signature_is_extracted_for_sender_and_committed_with_checkpoint(self):
        addresses, date, sender, body, subtype = im.parse_message(RAW_MESSAGE)
        talon.init()
        found = im.extract_signature(body, subtype, sender)

        self.assertEqual(sender, "jane@example.com")
        self.assertEqual(found, "Jane Doe\nExample Corp")

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "contacts.db"
            csv_path = Path(directory) / "contacts.csv"
            store = ContactStore(str(db_path))
            for name, address in addresses:
                store.record(
                    address,
                    name,
                    "INBOX",
                    date,
                    found if address.lower() == sender else "",
                )
            store.flush(("imap.example.com", "me", "INBOX", "7", 12))

            row = store._conn.execute(
                "SELECT signature, signature_seen FROM contacts WHERE email = ?",
                (sender,),
            ).fetchone()
            self.assertEqual(row, (found, date))
            self.assertEqual(
                store.checkpoint("imap.example.com", "me", "INBOX"), ("7", 12)
            )
            self.assertNotIn(
                "body",
                {row[1] for row in store._conn.execute("PRAGMA table_info(contacts)")},
            )
            store.export_csv(str(csv_path))
            store.close()

            with csv_path.open(newline="") as exported:
                rows = list(csv.DictReader(exported))
            self.assertEqual(rows[0]["signature"], found)


if __name__ == "__main__":
    unittest.main()
