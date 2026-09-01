# Rolodex

![Rolodex — email contacts organized into a private local address book](assets/rolodex-og.webp)

TUI to scan your IMAP mailbox and extract every name + email address you've corresponded with, into a local sqlite/libsql file.

## Features

- Connects to any IMAP server; credentials are never saved.
- Extracts contacts from `From`, `To`, and `Cc` headers and uses Talon to detect sender signatures.
- Resumes scans incrementally using per-folder UID checkpoints.
- Supports folder selection and persistent domain exclusions.
- Deduplicates contacts, tracks message counts and dates, and keeps the most frequently seen name.
- Searches and filters local databases by name, email, folder, or date, with paginated results and CSV export.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python app.py
```

## Flow

Start screen offers two paths:

- **Extract from IMAP**
  1. **Connect** — enter IMAP host, port, username, password (any provider; nothing is saved to disk).
  2. **Folders and database** — choose the output database and folders to scan. A server-designated All Mail folder is selected alone by default for speed; otherwise Spam/Trash/Junk/Bin-like folders are pre-unchecked.
  3. **Domain filter** — add/remove domains to exclude (e.g. `noreply.github.com`); saved for next run.
  4. **Run** — live progress per folder + overall, then a summary of unique contacts written.
- **Browse contacts db** — open an existing output db, search by name/email, filter by date range or folder, page through results, and export the current filtered set to CSV.

Change theme any time via the command palette (`ctrl+p`).

## Database

The output is a SQLite file in WAL mode and is libsql-compatible. Use a separate file per mailbox because contacts are deduplicated across the entire database. It contains two tables.

### `contacts`

| column | meaning |
|---|---|
| `email` | address, primary key |
| `name` | most-frequently-seen display name for that address |
| `message_count` | number of messages the address appeared in (From/To/Cc) |
| `folders` | comma-separated folders it was seen in |
| `first_seen` / `last_seen` | ISO timestamps from message `Date` headers |
| `signature` | latest dated signature Talon extracted for this sender |
| `signature_seen` | ISO timestamp of the message that supplied the signature |

### `extraction_state`

Internal checkpoints used to make later scans incremental:

| column | meaning |
|---|---|
| `host` | IMAP server hostname |
| `username` | IMAP account username |
| `folder` | mailbox name |
| `uidvalidity` | server generation identifier for the mailbox |
| `last_uid` | highest message UID successfully extracted |

`host`, `username`, and `folder` form the primary key. Contact updates and their checkpoint are committed in one transaction, so an interrupted scan cannot advance past unsaved contacts. If `uidvalidity` changes, the folder is skipped and the app asks for a fresh database rather than risking duplicate counts.

No passwords, message bodies, raw messages, or raw headers are stored in either table.

To push into Turso cloud:

```bash
turso db shell <your-db> < <(sqlite3 contacts.db .dump)
```

## Notes

- Extracts contacts and sender signatures from every message in the selected folders.
- Existing incremental databases only scan signatures in new messages; use a fresh database to backfill historical signatures.
- Credentials are prompted each run, never written to disk. Non-secret settings (excluded domains, last host/user, db path) persist in `~/.config/email_extract/config.json`.
