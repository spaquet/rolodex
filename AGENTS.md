# AGENTS.md

Rolodex — Python 3.11+ TUI (Textual). No secrets on disk — password prompted at runtime, never saved.
Config (`~/.config/email_extract/config.json`): excluded domains, last host/port/user, db path only.

Files:
- `app.py` — Textual screens: Start → (Connect → Folder select → Domain filter → Run) or Browse (search/filter/paginate/export)
- `imap_client.py` — generic IMAP: connect, list folders, incrementally fetch messages by UID, parse contacts, extract signatures with Talon
- `storage.py` — sqlite/libsql-compatible contacts and extraction checkpoints
- `config.py` — non-secret settings persistence

Database (SQLite WAL, libsql-compatible; use one file per mailbox):
- `contacts` — `email TEXT PRIMARY KEY`, `name`, `message_count`, comma-separated `folders`, `first_seen`, `last_seen`, `signature`, `signature_seen`; most-frequent name wins and latest dated signature wins.
- `extraction_state` — `host`, `username`, `folder`, `uidvalidity`, `last_uid`; primary key is (`host`, `username`, `folder`). This is internal scan state, not contact data.
- Contact changes and their extraction checkpoint must commit in the same transaction. Never advance `last_uid` separately.
- Never store passwords, message bodies, raw messages, or raw headers.
- A message is skipped entirely (no contact, no signature) when it's from the connected account itself, from a no-reply/automated address (`imap_client.is_noreply_sender`), or bulk/mailing-list mail identified via `List-Unsubscribe`/`List-Id`/`Precedence` headers (`imap_client.is_bulk_message`) — see SIGNATURE_EXTRACTION.md.
- Signature extraction runs once per sender per whole run (newest-dated message only, across all selected folders), not once per message — see SIGNATURE_EXTRACTION.md's "Performance Rework" section.

Run: `pip install -r requirements.txt && python app.py`

Conventions: stdlib only besides `textual` and the pinned Talon fork with its ML dependencies. No comments unless explaining non-obvious why.
Don't add error handling for cases that can't happen; keep IMAP/parsing code generic (any provider).
