"""Generic IMAP connection, folder listing, and header extraction."""
import imaplib
import re
from dataclasses import dataclass
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime

SKIP_HINTS = ("spam", "junk", "trash", "bin", "deleted")
FETCH_BATCH_SIZE = 1000

FOLDER_LINE_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)')


@dataclass
class Folder:
    """One IMAP mailbox as returned by LIST, with its raw flags."""

    name: str
    flags: str

    @property
    def looks_excludable(self) -> bool:
        """Whether the folder name/flags suggest Spam/Trash/Junk/Bin/Deleted.

        Used only to pre-uncheck likely-unwanted folders in the folder
        picker; the user can still select them explicitly.
        """
        low = (self.name + " " + self.flags).lower()
        return any(h in low for h in SKIP_HINTS)


class ImapError(Exception):
    """Raised for any IMAP failure (connect, login, LIST/SELECT/SEARCH/FETCH)."""


def connect(host: str, port: int, username: str, password: str, use_ssl: bool) -> imaplib.IMAP4:
    """Open and authenticate an IMAP connection.

    Args:
        host: IMAP server hostname.
        port: IMAP server port (e.g. 993 for implicit TLS, 143 plaintext).
        username: Login username.
        password: Login password. Never persisted by callers of this module.
        use_ssl: Use IMAP4_SSL if True, plain IMAP4 otherwise.

    Returns:
        An authenticated, ready-to-use IMAP4 connection.

    Raises:
        ImapError: On any connection or login failure.
    """
    try:
        conn = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        conn.login(username, password)
    except (imaplib.IMAP4.error, OSError) as e:
        raise ImapError(str(e)) from e
    return conn


def list_folders(conn: imaplib.IMAP4) -> list[Folder]:
    """Fetch and parse the server's folder list.

    Args:
        conn: An authenticated IMAP connection.

    Returns:
        All mailboxes reported by LIST, in server order.

    Raises:
        ImapError: If the LIST command fails.
    """
    status, data = conn.list()
    if status != "OK":
        raise ImapError(f"LIST failed: {data}")
    folders = []
    for raw in data:
        if raw is None:
            continue
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        m = FOLDER_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        folders.append(Folder(name=name, flags=m.group("flags")))
    return folders


def _decode(value: str) -> str:
    """Decode a raw (possibly RFC 2047 encoded-word) header value to text.

    Args:
        value: Raw header value, e.g. `"=?UTF-8?B?...?= <a@b.com>"`.

    Returns:
        Plain-text version with each encoded word decoded; falls back to
        UTF-8 with replacement characters on an unknown/invalid charset.
    """
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def select_folder(conn: imaplib.IMAP4, folder: str) -> str:
    """Select a folder read-only and return its UIDVALIDITY value.

    Args:
        conn: An authenticated IMAP connection.
        folder: Folder name to select, exactly as returned by list_folders().

    Raises:
        ImapError: If SELECT fails or the server omits UIDVALIDITY.
    """
    status, _ = conn.select(f'"{folder}"', readonly=True)
    if status != "OK":
        raise ImapError(f"SELECT failed for {folder}")
    _, data = conn.response("UIDVALIDITY")
    if not data or not data[0]:
        raise ImapError(f"UIDVALIDITY missing for {folder}")
    value = data[0]
    return value.decode() if isinstance(value, bytes) else str(value)


def search_uids(conn: imaplib.IMAP4, after_uid: int = 0) -> list[bytes]:
    """Return message UIDs newer than after_uid in the selected folder."""
    criteria = ("ALL",) if not after_uid else ("UID", f"{after_uid + 1}:*")
    status, data = conn.uid("search", None, *criteria)
    if status != "OK":
        raise ImapError("UID SEARCH failed")
    # IMAP ranges reverse when their start exceeds *, so filter the last UID
    # that some servers return when the mailbox has no newer messages.
    return sorted((uid for uid in data[0].split() if int(uid) > after_uid), key=int)


def fetch_header_batches(
    conn: imaplib.IMAP4, msg_uids: list[bytes], batch_size: int = FETCH_BATCH_SIZE
):
    """Fetch From/To/Cc/Date headers for message UIDs, yielding completed batches.

    Uses BODY.PEEK so messages are not marked \\Seen. The currently
    selected folder (from select_folder) is used implicitly.

    Args:
        msg_uids: Message UIDs to fetch, as returned by search_uids().
        batch_size: Messages per FETCH command, to keep individual
            IMAP responses a manageable size.

    Yields:
        (requested UIDs, raw header texts) for each completed batch.
    """
    for i in range(0, len(msg_uids), batch_size):
        batch = msg_uids[i : i + batch_size]
        id_set = b",".join(batch)
        status, data = conn.uid(
            "fetch", id_set, "(BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE)])"
        )
        if status != "OK":
            raise ImapError("UID FETCH failed")
        yield batch, [
            part[1].decode(errors="replace")
            for part in data
            if isinstance(part, tuple) and len(part) > 1
        ]


def parse_addresses(raw_headers: str) -> list[tuple[str, str]]:
    """Extract every (display_name, email) pair from From/To/Cc header text.

    Handles folded (multi-line, indented) headers and RFC 2047 encoded
    display names, and splits comma-separated address lists within a
    single header.

    Args:
        raw_headers: Raw header block returned by fetch_header_batches().

    Returns:
        (name, email) pairs, one per address found. name may be "".
    """
    lines = {"from": [], "to": [], "cc": []}
    current_key = None
    buf = []
    for line in raw_headers.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(From|To|Cc):\s?(.*)$", line, re.IGNORECASE)
        if m:
            if current_key and buf:
                lines[current_key].append(" ".join(buf))
            current_key = m.group(1).lower()
            buf = [m.group(2)]
        elif line.startswith((" ", "\t")) and current_key:
            buf.append(line.strip())
    if current_key and buf:
        lines[current_key].append(" ".join(buf))

    results = []
    for key in ("from", "to", "cc"):
        for entry in lines[key]:
            decoded = _decode(entry)
            for name, addr in getaddresses([decoded]):
                if addr:
                    results.append((name.strip(), addr.strip()))
    return results


def parse_date(raw_headers: str) -> str:
    """Extract and normalize the message Date header.

    Args:
        raw_headers: Raw header block returned by fetch_header_batches().

    Returns:
        ISO-8601 timestamp, or "" if no Date header is present or it
        fails to parse.
    """
    m = re.search(r"^Date:\s?(.*)$", raw_headers, re.IGNORECASE | re.MULTILINE)
    if not m:
        return ""
    try:
        dt = parsedate_to_datetime(m.group(1).strip())
        return dt.isoformat()
    except (TypeError, ValueError):
        return ""
