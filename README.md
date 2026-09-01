# Rolodex

TUI to scan your IMAP mailbox and extract every name + email address you've corresponded with, into a local sqlite/libsql file.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

## Flow

1. **Connect** — enter IMAP host, port, username, password (any provider; nothing is saved to disk).
2. **Folders** — pick which folders to scan. Folders that look like Spam/Trash/Junk/Bin are pre-unchecked but shown so you can override.
3. **Domain filter** — add/remove domains to exclude (e.g. `noreply.github.com`); saved for next run. Set the output db file path here too.
4. **Run** — live progress per folder + overall, then a summary of unique contacts written.

## Output

A sqlite file (default `./contacts.db`), libsql-compatible, with a `contacts` table:

| column | meaning |
|---|---|
| `email` | address, primary key |
| `name` | most-frequently-seen display name for that address |
| `message_count` | number of messages the address appeared in (From/To/Cc) |
| `folders` | comma-separated folders it was seen in |
| `first_seen` / `last_seen` | ISO timestamps from message `Date` headers |

To push into Turso cloud:

```bash
turso db shell <your-db> < <(sqlite3 contacts.db .dump)
```

## Notes

- Extracts from `From`, `To`, and `Cc` headers of every message in the selected folders.
- Credentials are prompted each run, never written to disk. Non-secret settings (excluded domains, last host/user, db path) persist in `~/.config/email_extract/config.json`.
