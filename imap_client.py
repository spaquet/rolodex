"""Generic IMAP connection, folder listing, and contact extraction."""
import base64
import binascii
import imaplib
import quopri
import re
import time
from dataclasses import dataclass
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

SKIP_HINTS = ("spam", "junk", "trash", "bin", "deleted")
FETCH_BATCH_SIZE = 1000
MESSAGE_BATCH_SIZE = 20

FOLDER_LINE_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)')
NOREPLY_RE = re.compile(
    r"(?:^|[._-])(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster)(?:[._-]|$)",
    re.IGNORECASE,
)
HEADER_FIELDS = "(FROM TO CC DATE LIST-UNSUBSCRIBE LIST-ID PRECEDENCE)"


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
            "fetch", id_set, f"(BODY.PEEK[HEADER.FIELDS {HEADER_FIELDS}])"
        )
        if status != "OK":
            raise ImapError("UID FETCH failed")
        headers = [
            part[1].decode(errors="replace")
            for part in data
            if isinstance(part, tuple) and len(part) > 1
        ]
        if len(headers) != len(batch):
            raise ImapError("UID FETCH returned an incomplete batch")
        yield batch, headers


def _parse_imap_value(raw: bytes, pos: int = 0):
    while pos < len(raw) and raw[pos : pos + 1].isspace():
        pos += 1
    if pos >= len(raw):
        raise ValueError("incomplete IMAP value")
    if raw[pos : pos + 1] == b"(":
        values = []
        pos += 1
        while True:
            while pos < len(raw) and raw[pos : pos + 1].isspace():
                pos += 1
            if pos >= len(raw):
                raise ValueError("incomplete IMAP list")
            if raw[pos : pos + 1] == b")":
                return values, pos + 1
            value, pos = _parse_imap_value(raw, pos)
            values.append(value)
    if raw[pos : pos + 1] == b'"':
        out = bytearray()
        pos += 1
        while pos < len(raw):
            char = raw[pos]
            pos += 1
            if char == 34:
                return out.decode(errors="replace"), pos
            if char == 92 and pos < len(raw):
                char = raw[pos]
                pos += 1
            out.append(char)
        raise ValueError("incomplete IMAP string")
    if raw[pos : pos + 1] == b"{":
        end = raw.find(b"}", pos)
        if end < 0:
            raise ValueError("incomplete IMAP literal")
        size = int(raw[pos + 1 : end])
        start = end + 1
        if raw[start : start + 2] == b"\r\n":
            start += 2
        if len(raw) < start + size:
            raise ValueError("incomplete IMAP literal")
        return raw[start : start + size].decode(errors="replace"), start + size
    end = pos
    while end < len(raw) and not raw[end : end + 1].isspace() and raw[end : end + 1] != b")":
        end += 1
    atom = raw[pos:end]
    if atom.upper() == b"NIL":
        return None, end
    return (int(atom) if atom.isdigit() else atom.decode(errors="replace")), end


def _bodystructure_records(data) -> tuple[dict[bytes, list], int, int]:
    records = {}
    pending = b""
    transferred = message_bytes = 0
    for item in data:
        if isinstance(item, tuple):
            piece = item[0] + b"\r\n" + item[1]
        elif isinstance(item, bytes):
            piece = item
        else:
            continue
        transferred += len(piece)
        if not pending and b"BODYSTRUCTURE" not in piece:
            continue
        pending = pending + b" " + piece if pending else piece
        marker = re.search(rb"\bUID\s+(\d+)\b.*?\bBODYSTRUCTURE\s+", pending, re.DOTALL)
        if not marker:
            continue
        try:
            structure, _ = _parse_imap_value(pending, marker.end())
        except ValueError:
            continue
        records[marker.group(1)] = structure
        size = re.search(rb"\bRFC822\.SIZE\s+(\d+)\b", pending)
        if size:
            message_bytes += int(size.group(1))
        pending = b""
    if pending:
        raise ImapError("UID FETCH returned an incomplete BODYSTRUCTURE")
    return records, transferred, message_bytes


def _params(values) -> dict[str, str]:
    if not isinstance(values, list):
        return {}
    return {
        str(values[i]).lower(): str(values[i + 1])
        for i in range(0, len(values) - 1, 2)
    }


def _preferred_text_part(structure: list):
    candidates = []

    def visit(part, path):
        if not isinstance(part, list) or not part:
            return
        if isinstance(part[0], list):
            children = []
            for value in part:
                if not isinstance(value, list):
                    break
                children.append(value)
            for index, child in enumerate(children, 1):
                visit(child, f"{path}.{index}" if path else str(index))
            return
        if len(part) < 7 or str(part[0]).lower() != "text":
            return
        subtype = str(part[1]).lower()
        params = _params(part[2])
        disposition = part[9] if len(part) > 9 else None
        disposition_params = _params(disposition[1]) if isinstance(disposition, list) and len(disposition) > 1 else {}
        if (
            subtype not in ("plain", "html")
            or "name" in params
            or "filename" in params
            or "filename" in disposition_params
            or isinstance(disposition, list)
            and disposition
            and str(disposition[0]).lower() == "attachment"
        ):
            return
        candidates.append((subtype != "plain", path or "1", subtype, params.get("charset", "utf-8"), str(part[5])))

    visit(structure, "")
    return min(candidates, default=None)


def _decode_text_part(raw: bytes, encoding: str, charset: str) -> str:
    if encoding.lower() == "base64":
        raw = base64.b64decode(raw, validate=False)
    elif encoding.lower() == "quoted-printable":
        raw = quopri.decodestring(raw)
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def fetch_text_parts(conn: imaplib.IMAP4, msg_uids: list[bytes]):
    """Fetch only each message's preferred non-attachment plain or HTML MIME part."""
    started = time.monotonic()
    status, data = conn.uid(
        "fetch", b",".join(msg_uids), "(UID RFC822.SIZE BODYSTRUCTURE)"
    )
    structure_s = time.monotonic() - started
    if status != "OK":
        raise ImapError("UID BODYSTRUCTURE FETCH failed")
    structures, structure_bytes, message_bytes = _bodystructure_records(data)
    if set(structures) != set(msg_uids):
        raise ImapError("UID FETCH returned an incomplete BODYSTRUCTURE batch")

    selected = {}
    groups = {}
    for uid in msg_uids:
        part = _preferred_text_part(structures[uid])
        if part:
            _, section, subtype, charset, encoding = part
            selected[uid] = (subtype, charset, encoding)
            groups.setdefault(section, []).append(uid)

    bodies = {}
    body_s = body_bytes = 0
    for section, uids in groups.items():
        started = time.monotonic()
        status, data = conn.uid(
            "fetch", b",".join(uids), f"(UID BODY.PEEK[{section}])"
        )
        body_s += time.monotonic() - started
        if status != "OK":
            raise ImapError("UID text-part FETCH failed")
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            match = re.search(rb"\bUID\s+(\d+)\b", item[0])
            if match:
                bodies[match.group(1)] = item[1]
                body_bytes += len(item[1])
        if any(uid not in bodies for uid in uids):
            raise ImapError("UID FETCH returned an incomplete text-part batch")

    parts = []
    for uid in msg_uids:
        if uid not in selected:
            parts.append((uid, "", ""))
            continue
        subtype, charset, encoding = selected[uid]
        try:
            body = _decode_text_part(bodies[uid], encoding, charset)
        except (binascii.Error, ValueError):
            body = ""
        parts.append((uid, body, subtype))
    return parts, {
        "structure_seconds": structure_s,
        "body_seconds": body_s,
        "structure_bytes": structure_bytes,
        "message_bytes": message_bytes,
        "body_bytes": body_bytes,
        "requests": 1 + len(groups),
    }


def parse_message(raw_message: bytes) -> tuple[list[tuple[str, str]], str, str, str, str]:
    """Return addresses, date, sender address, body text, and body subtype."""
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    addresses = []
    for key in ("from", "to", "cc"):
        addresses.extend(
            (name.strip(), addr.strip())
            for name, addr in getaddresses([str(value) for value in message.get_all(key, [])])
            if addr
        )
    senders = getaddresses([str(value) for value in message.get_all("from", [])])
    sender = senders[0][1].strip() if senders else ""
    date = ""
    if message.get("date"):
        try:
            date = parsedate_to_datetime(str(message["date"])).isoformat()
        except (TypeError, ValueError):
            pass
    part = message.get_body(preferencelist=("plain", "html"))
    if not part:
        return addresses, date, sender, "", ""
    body = part.get_content()
    return addresses, date, sender, body if isinstance(body, str) else "", part.get_content_subtype()


def extract_signature(body: str, subtype: str, sender: str) -> str:
    """Extract the current sender's signature from decoded plain text or HTML."""
    from talon import quotations, signature
    from talon.utils import html_to_text

    if subtype == "html":
        body = html_to_text(body) or ""
    if not body:
        return ""
    body = quotations.extract_from_plain(body)
    _, found = signature.extract(body, sender=sender)
    return found.strip() if found else ""


def _split_header_lines(raw_headers: str) -> dict[str, list[str]]:
    """Split raw From/To/Cc header text into unfolded per-header line lists."""
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
    return lines


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
    lines = _split_header_lines(raw_headers)
    results = []
    for key in ("from", "to", "cc"):
        for entry in lines[key]:
            decoded = _decode(entry)
            for name, addr in getaddresses([decoded]):
                if addr:
                    results.append((name.strip(), addr.strip()))
    return results


def parse_sender(raw_headers: str) -> str:
    """Extract the first From address from raw header text, or "" if absent."""
    for entry in _split_header_lines(raw_headers)["from"]:
        addrs = getaddresses([_decode(entry)])
        if addrs and addrs[0][1]:
            return addrs[0][1].strip()
    return ""


def is_noreply_sender(addr: str) -> bool:
    """Whether an address's local part looks like an automated no-reply sender."""
    local = addr.split("@", 1)[0] if addr else ""
    return bool(NOREPLY_RE.search(local))


def is_bulk_message(raw_headers: str) -> bool:
    """Whether raw header text carries standard bulk/mailing-list markers.

    Checks List-Unsubscribe/List-Id (RFC 2369/8058) and Precedence: bulk|list,
    all included in the header fields fetch_header_batches() requests.
    """
    for line in raw_headers.splitlines():
        low = line.lower()
        if low.startswith("list-unsubscribe:") or low.startswith("list-id:"):
            return True
        if low.startswith("precedence:") and ("bulk" in low or "list" in low):
            return True
    return False


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
