# AGENTS.md

Rolodex — Python 3.11+ TUI (Textual). No secrets on disk — password prompted at runtime, never saved.
Config (`~/.config/email_extract/config.json`): excluded domains, last host/port/user, db path only.

Files:
- `app.py` — Textual screens: Connect → Folder select → Domain filter → Run (extraction + progress)
- `imap_client.py` — generic IMAP: connect, list folders, fetch From/To/Cc/Date headers
- `storage.py` — sqlite/libsql-compatible output, upsert contacts (most-frequent name wins)
- `config.py` — non-secret settings persistence

Run: `pip install -r requirements.txt && python app.py`

Conventions: stdlib only besides `textual`. No comments unless explaining non-obvious why.
Don't add error handling for cases that can't happen; keep IMAP/parsing code generic (any provider).
