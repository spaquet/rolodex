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


if __name__ == "__main__":
    unittest.main()
