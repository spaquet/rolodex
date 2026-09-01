# Talon Signature Extraction

## Goal

Extract the sender's signature from each email, keep the extracted signature with
the sender's contact, and discard the rest of the message body immediately.

This feature does not attempt to interpret a signature into phone, company,
title, or address fields. Talon only identifies the signature boundary; the
extracted signature text is the result.

## Scope

The first version will:

- Run locally with Talon's bundled SVM classifier.
- Prefer the `text/plain` MIME part and support HTML-only messages as a fallback.
- Remove quoted replies before signature detection.
- Associate a signature only with the address in the message's `From` header.
- Store the latest dated non-empty signature and its message date.
- Include the signature in CSV exports.
- Never store message bodies, raw messages, raw headers, passwords, or model
  training data.

The first version will not:

- Train or fine-tune Talon's classifier.
- Split signatures into structured fields.
- Send content to an external AI service.
- Scan attachments or images for signatures.
- Backfill an existing database whose UID checkpoints have already advanced.

Use a fresh database for the first signature-enabled scan. This avoids adding a
second checkpoint system or double-counting existing contacts.

## Why Talon

[Mailgun Talon](https://github.com/mailgun/talon) exposes the exact operation we
need:

```python
import talon
from talon import signature

talon.init()
_, extracted_signature = signature.extract(body, sender=sender)
```

The classifier runs locally. Talon ships its trained classifier and uses a
scikit-learn `LinearSVC` to classify candidate lines near the end of a message.
No mailbox-specific training is required for the initial implementation.

Talon's PyPI package is stale, while its repository contains newer Python and
scikit-learn work. We will therefore install a reviewed Git commit rather than
`talon` from PyPI.

## Tiny Talon Fork

### Purpose

The fork provides an owner-controlled, immutable source while Talon's PyPI
release remains stale. Upstream commit
`365cbd9f881462bb95e87ef5a5f7796a4994ff2b` already removes the unused
`chardet` and `cchardet` requirements. The fork adds one compatibility commit:
it pins scikit-learn 1.9.0 and regenerates the bundled classifier with 1.9.0 so
loading the model does not cross scikit-learn serialization versions.

### Create the fork

1. Fork `mailgun/talon` into the GitHub account or organization that owns
   Rolodex. The current fork is
   [`spaquet/talon`](https://github.com/spaquet/talon).
2. Clone the fork and retain Mailgun as the `upstream` remote:

   ```bash
   git clone https://github.com/<github-account>/talon.git
   cd talon
   git remote add upstream https://github.com/mailgun/talon.git
   git fetch upstream
   git switch master
   ```

3. Confirm that the incompatible dependencies are absent:

   ```bash
   git grep -n -E '(^|[^c])chardet|cchardet' -- talon tests
   ```

4. Pin scikit-learn 1.9.0 in `setup.py` and `requirements.txt`, then regenerate
   `talon/signature/data/classifier` with the same version.

   ```bash
   python -c 'from talon.signature.learning import classifier; classifier.train(classifier.init(), "talon/signature/data/train.data", "talon/signature/data/classifier")'
   ```

### Validate the fork

Create a clean Python 3.11 environment and install the fork normally, including
its declared dependencies:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

Run upstream's tests when they support the current test runner, then run this
required smoke check regardless:

```bash
.venv/bin/python - <<'PY'
import talon
from talon import signature

talon.init()
body, found = signature.extract(
    "Thanks Sasha, I can't go any higher and is why I limited it to the\n"
    "homepage.\n\nJohn Doe\nvia mobile",
    sender="john.doe@example.com",
)
assert body == (
    "Thanks Sasha, I can't go any higher and is why I limited it to the\n"
    "homepage."
)
assert found == "John Doe\nvia mobile"
PY
```

Test installation and the smoke check on macOS and Linux if both are supported
deployment targets.

### Publish and pin

Push the branch, then record its full 40-character commit SHA:

```bash
git rev-parse HEAD
```

Add the fork to Rolodex's `requirements.txt` using that immutable SHA:

```text
talon @ git+https://github.com/spaquet/talon.git@330b960c20e8634a60578346d94c229957e09d2e
```

Do not use `@master` or `@main`. Moving branches make
installs non-reproducible and can silently replace the classifier.

### Maintain the fork

Only update the fork deliberately:

```bash
git fetch upstream
git switch master
git merge upstream/master
git push origin master
```

After an upstream update:

1. Recreate the Python 3.11 environment.
2. Run upstream tests and the signature smoke check.
3. Test Rolodex's extraction tests.
4. Pin Rolodex to the new commit SHA in a separate reviewed change.

Delete the fork and switch back to upstream when upstream removes the two
dependencies and publishes or tags a commit that passes our checks.

## Dependency Policy

This feature intentionally changes the project's previous “stdlib plus
Textual” dependency rule. Talon's ML extraction requires NumPy, SciPy,
scikit-learn, joblib, regex, lxml, cssselect, html5lib, and six.

For the first version:

- Verify that the unused `chardet` and `cchardet` requirements remain absent.
- Keep scikit-learn pinned to the version used to serialize the classifier.
- Keep the other version ranges from the chosen Talon commit.
- Do not independently upgrade Textual or unrelated Rolodex dependencies.
- Do not add spaCy, transformers, an LLM client, or another signature parser.
- Treat a failure to load Talon's bundled classifier as a startup error for an
  extraction run, not as “no signature found.”

If a future scikit-learn release cannot load the bundled classifier, pin the
last verified compatible scikit-learn version or regenerate the classifier in
the fork. Do not retrain it merely to refresh a dependency.

## Email Processing

### IMAP fetch

The existing extraction fetches headers only. Signature extraction needs MIME
content, so the signature-enabled path will fetch complete messages with
`BODY.PEEK[]` in small batches. `PEEK` preserves the message's unread state.

Fetching complete messages is the simplest provider-independent implementation.
It may transfer attachments, but the bytes remain in memory and are discarded
after parsing. If mailbox measurements show that attachment traffic is a real
problem, add `BODYSTRUCTURE`-guided text-part fetching later.

Header-only extraction remains available only if the UI retains an explicit
“scan signatures” choice. Otherwise the existing fetch function will be
replaced by a message fetch function; two parallel extraction pipelines are not
needed.

### MIME decoding

Parse each fetched message with the standard library:

```python
from email import policy
from email.parser import BytesParser

message = BytesParser(policy=policy.default).parsebytes(raw_message)
part = message.get_body(preferencelist=("plain", "html"))
```

- Ignore attachments.
- Decode the selected part using the `email` package's content manager.
- Prefer plain text even when an HTML alternative is also present.
- For HTML-only mail, convert the chosen part with Talon's existing HTML-to-text
  utility before classification.
- If there is no usable text part, record no signature and continue.

### Extraction order

For each message:

1. Parse `From` and `Date` from the parsed `EmailMessage`.
2. Select and decode the preferred text part.
3. Remove quoted history with `talon.quotations.extract_from_plain`.
4. Call `talon.signature.extract(unquoted_body, sender=from_address)`.
5. If Talon returns a signature, trim surrounding whitespace and record it for
   the `From` contact.
6. Drop all message and body references before processing the next message.

Quotation removal is required even though only the signature is retained. It
prevents a signature in an older quoted message from being attributed to the
current sender.

Only `From` receives the signature. Addresses in `To` and `Cc` continue to be
recorded as contacts, but the current message provides no evidence about their
signatures.

## Storage

Add two nullable columns to `contacts`:

| column | meaning |
|---|---|
| `signature` | latest dated non-empty signature extracted for this sender |
| `signature_seen` | ISO timestamp of the message that supplied `signature` |

Schema creation includes the columns for new databases. Opening an older
database may add the nullable columns with `ALTER TABLE`, but it does not rescan
old UIDs. The UI must tell the user to select a fresh database when historical
signatures are wanted.

`ContactStore.record` accepts an optional signature only for the sender. During
an upsert, replace the stored signature only when:

- the new signature is non-empty; and
- its parsed message date is at least as recent as `signature_seen`.

If the message has no valid date, use the new signature only when the contact
does not already have one. This prevents an undated old message from replacing
a known recent signature.

Signature updates and the folder's extraction checkpoint must commit in the
same transaction, preserving the existing crash-safety rule. Bodies and raw
messages never enter SQLite.

CSV export adds `signature` and `signature_seen` columns. Python's CSV writer
already handles multiline quoted fields. Rendering multiline signatures in the
contact table is deferred; the database and CSV are the initial output surfaces.

## Application Changes

Implementation should touch the fewest existing files:

- `requirements.txt`: add the pinned Talon fork.
- `imap_client.py`: fetch raw messages and parse their sender, date, and preferred
  body.
- `storage.py`: add signature columns, buffering, upsert behavior, and CSV fields.
- `app.py`: initialize Talon once and pass extracted sender signatures to the
  store.
- `test_signature.py`: one focused end-to-end extraction check using a fake IMAP
  response and a temporary database.
- `README.md` and `AGENTS.md`: document the new dependency exception, stored
  columns, body privacy rule, and fresh-database backfill behavior.

Do not introduce a parser interface, model abstraction, provider-specific IMAP
code, background model service, or training command.

## Failure Behavior

- Talon initialization failure stops the run before any folder checkpoint moves.
- IMAP fetch or MIME parsing failure follows the existing folder failure path:
  discard buffered changes and do not advance that folder's checkpoint.
- A valid message with no detectable signature is normal and does not fail the
  run.
- Talon's per-message `(body, None)` result records no signature.
- Passwords, raw messages, and bodies are never logged.

## Acceptance Check

One runnable test should prove the complete feature:

1. A fake IMAP message contains a plain-text reply, a quoted older reply, and a
   sender signature.
2. Talon extracts the current sender's signature, not the quoted signature.
3. The contact row stores the signature and its date.
4. No body or raw-message column exists in SQLite.
5. The contact change and checkpoint appear together after `flush`.
6. CSV export contains the multiline signature.

Also run the existing suite:

```bash
python -m unittest
```

## Rollback

Rollback is mechanical:

1. Remove the Talon VCS requirement.
2. Restore header-only fetching.
3. Stop writing and exporting the two signature columns.

Existing databases remain readable because the added columns are nullable and
SQLite tolerates unused columns. No destructive migration is required.

## Status

Implemented and closed out:

- Talon extraction, SQLite storage/migration, CSV export, MIME parsing.
- `RunScreen` logs a warning per folder when a prior checkpoint exists
  (`last_uid > 0`), telling the user historical signatures before that UID
  were not backfilled and a fresh database is needed for those.
- Per-message parsing/extraction is wrapped in `try/except` in
  `run_extraction`; a message that fails to parse is logged and skipped,
  the folder scan continues, and the checkpoint still advances past it.
- `test_signature.py` covers the extraction/storage/CSV path end-to-end and
  the skip-on-malformed-message path.

Known remaining gap: no test against a live/real IMAP server — all IMAP
interaction is exercised through fakes, consistent with the rest of the
test suite (`test_incremental.py`).
