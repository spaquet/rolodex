"""Rolodex: TUI to connect to any IMAP server, pick folders, filter domains,
and extract contacts to sqlite."""
import imaplib
import re
import threading
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    ProgressBar,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

import config as cfgmod
import imap_client as im
from storage import ContactStore

PAGE_SIZE = 50
SEARCH_OPERATORS = ["folder:", "after:", "before:"]


def default_db_name(username: str, host: str) -> str:
    """Suggest a per-account db filename, e.g. alice_imap.example.com.db.

    Keeps contacts from different mailboxes in separate files by default,
    since the `contacts` table dedupes globally on email address.
    """
    local = username.split("@")[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{local}_{host}").strip("_")
    return f"{slug}.db"


def format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_query(text: str) -> dict:
    """Turn a Gmail-style single search string into store.search() filter kwargs.

    Recognizes `folder:NAME`, `after:DATE`, `before:DATE` tokens anywhere in
    the string; everything else is treated as free-text name/email search.
    """
    search_terms = []
    date_from = date_to = folder = ""
    for tok in text.split():
        key, sep, val = tok.partition(":")
        if sep and val and key.lower() == "folder":
            folder = val
        elif sep and val and key.lower() == "after":
            date_from = val
        elif sep and val and key.lower() == "before":
            date_to = val
        else:
            search_terms.append(tok)
    return {
        "search": " ".join(search_terms),
        "date_from": date_from,
        "date_to": date_to,
        "folder": folder,
    }


class StartScreen(Screen):
    """Entry screen: extract fresh contacts from IMAP, or browse an existing db."""

    BINDINGS = [("escape", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="start-panel", classes="panel"):
            yield Static("Rolodex", classes="title")
            yield Static("Extract and browse email contacts.", classes="hint")
            yield Button("Extract from IMAP", id="extract", variant="primary")
            yield Button("Browse contacts db", id="browse")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "extract":
            self.app.push_screen(ConnectScreen())
        elif event.button.id == "browse":
            self.app.push_screen(BrowseScreen())


class ConnectScreen(Screen):
    """First screen: collect IMAP host/port/username/password and connect.

    Non-secret fields are pre-filled from config.load(); password is never
    pre-filled or persisted. Connection runs on a background thread so the
    UI stays responsive.
    """

    BINDINGS = [("escape", "app.quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.cfg = cfgmod.load()

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="form", classes="panel"):
            yield Static("IMAP Connection", classes="title")
            yield Label("Host")
            yield Input(value=self.cfg["last_host"], placeholder="imap.example.com", id="host")
            yield Label("Port")
            yield Input(value=str(self.cfg["last_port"]), placeholder="993", id="port")
            yield Label("Username")
            yield Input(value=self.cfg["last_username"], placeholder="you@example.com", id="username")
            yield Label("Password")
            yield Input(password=True, id="password")
            yield Checkbox("Use SSL", value=self.cfg["last_use_ssl"], id="use_ssl")
            yield Button("Connect", id="connect", variant="primary")
            yield Static("", id="status")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect":
            self.try_connect()

    def try_connect(self) -> None:
        """Validate the form, then attempt the IMAP connection off the UI thread."""
        host = self.query_one("#host", Input).value.strip()
        port_raw = self.query_one("#port", Input).value.strip()
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        use_ssl = self.query_one("#use_ssl", Checkbox).value
        status = self.query_one("#status", Static)

        if not host or not port_raw or not username or not password:
            status.update("[red]All fields required.[/red]")
            return
        try:
            port = int(port_raw)
        except ValueError:
            status.update("[red]Port must be a number.[/red]")
            return

        status.update("Connecting...")
        self.query_one("#connect", Button).disabled = True

        def work():
            try:
                conn = im.connect(host, port, username, password, use_ssl)
                folders = im.list_folders(conn)
            except im.ImapError as e:
                self.app.call_from_thread(self._on_fail, str(e))
                return
            self.app.call_from_thread(self._on_success, conn, folders, host, port, username, use_ssl)

        threading.Thread(target=work, daemon=True).start()

    def _on_fail(self, msg: str) -> None:
        """UI-thread callback: show the connection error and re-enable the form."""
        self.query_one("#status", Static).update(f"[red]{msg}[/red]")
        self.query_one("#connect", Button).disabled = False

    def _on_success(self, conn, folders, host, port, username, use_ssl) -> None:
        """UI-thread callback: save non-secret fields and advance to folder selection."""
        cfgmod.save(
            {
                **self.cfg,
                "last_host": host,
                "last_port": port,
                "last_username": username,
                "last_use_ssl": use_ssl,
            }
        )
        self.app.push_screen(FolderScreen(conn, folders, username, host))


class FolderScreen(Screen):
    """Second screen: pick which mailboxes to scan.

    Folders that look like Spam/Trash/Junk (Folder.looks_excludable) start
    unchecked; every folder is shown so the user can override.
    """

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, conn: imaplib.IMAP4, folders: list[im.Folder], username: str, host: str):
        super().__init__()
        self.conn = conn
        self.folders = folders
        self.username = username
        self.host = host

    def compose(self) -> ComposeResult:
        yield Header()
        all_folder = next(
            (folder for folder in self.folders if "\\all" in folder.flags.lower().split()),
            None,
        )
        yield Static(
            "Select folders to scan. A server-designated All Mail folder is selected alone "
            "for speed; otherwise spam/trash-like folders are pre-unchecked.",
            classes="title",
        )
        selections = [
            Selection(
                folder.name,
                folder.name,
                folder is all_folder if all_folder else not folder.looks_excludable,
            )
            for folder in self.folders
        ]
        yield SelectionList[str](*selections, id="folders")
        yield Button("Continue", id="continue", variant="primary")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue":
            selected = self.query_one("#folders", SelectionList).selected
            if not selected:
                return
            self.app.push_screen(DomainScreen(self.conn, list(selected), self.username, self.host))


class DomainScreen(Screen):
    """Third screen: maintain the excluded-domain list and output db path.

    The domain list is seeded from and saved back to config, so it persists
    across runs.
    """

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("d", "remove_selected", "Remove domain")]

    def __init__(self, conn: imaplib.IMAP4, folders: list[str], username: str, host: str):
        super().__init__()
        self.conn = conn
        self.folders = folders
        self.username = username
        self.host = host
        self.cfg = cfgmod.load()
        self.domains = list(self.cfg["excluded_domains"])
        self.suggested_db = default_db_name(username, host)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Excluded domains (contacts from these are skipped)", classes="title")
        with Horizontal(id="add-row"):
            yield Input(placeholder="example.com", id="new_domain")
            yield Button("Add", id="add_domain")
        yield ListView(*[ListItem(Label(d)) for d in self.domains], id="domain_list")
        yield Static("Select a row + press 'd' to remove.", classes="hint")
        yield Label("Output sqlite/libsql file path")
        yield Static(
            "Different mailboxes should usually use different db files "
            "(contacts dedupe globally by email address within one file).",
            classes="hint",
        )
        db_default = self.cfg["db_path"] if self.cfg["db_path"] not in ("", "contacts.db", "test.db") else self.suggested_db
        yield Input(value=db_default, id="db_path")
        yield Button("Start scan", id="continue", variant="primary")
        yield Footer()

    def action_remove_selected(self) -> None:
        """Remove the currently highlighted domain row (bound to 'd')."""
        lv = self.query_one("#domain_list", ListView)
        if lv.index is not None and 0 <= lv.index < len(self.domains):
            del self.domains[lv.index]
            lv.remove_children()
            for d in self.domains:
                lv.append(ListItem(Label(d)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add_domain":
            inp = self.query_one("#new_domain", Input)
            val = inp.value.strip().lower()
            if val and val not in self.domains:
                self.domains.append(val)
                self.query_one("#domain_list", ListView).append(ListItem(Label(val)))
            inp.value = ""
        elif event.button.id == "continue":
            db_path = self.query_one("#db_path", Input).value.strip() or self.cfg["db_path"]
            cfgmod.save({**self.cfg, "excluded_domains": self.domains, "db_path": db_path})
            self.app.push_screen(
                RunScreen(
                    self.conn,
                    self.folders,
                    self.domains,
                    db_path,
                    self.host,
                    self.username,
                )
            )


class ConfirmQuitScreen(ModalScreen[bool]):
    """Modal asking to confirm abandoning an in-progress extraction."""

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-panel", classes="panel"):
            yield Static("Extraction is still running.", classes="title")
            yield Static("Contacts saved so far stay in the db. Quit anyway?", classes="hint")
            with Horizontal():
                yield Button("Quit", id="yes", variant="error")
                yield Button("Cancel", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class RunScreen(Screen):
    """Final screen: runs the extraction and shows live progress.

    Extraction runs on a background thread; all widget updates are
    marshalled back to the UI thread via `app.call_from_thread`. The thread
    keeps running even if this screen is covered by another (e.g. Browse),
    so results already flushed to the db can be searched mid-run.
    """

    BINDINGS = [("q", "try_quit", "Quit"), ("b", "browse", "Browse (bg)")]

    def __init__(
        self,
        conn: imaplib.IMAP4,
        folders: list[str],
        excluded_domains: list[str],
        db_path: str,
        host: str,
        username: str,
    ):
        super().__init__()
        self.conn = conn
        self.folders = folders
        self.excluded_domains = set(excluded_domains)
        self.db_path = db_path
        self.host = host
        self.username = username
        self.done = False
        self.start_time = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="folder_status", classes="title")
        with Horizontal(id="progress-row"):
            yield ProgressBar(id="progress", total=100, show_eta=False)
            yield Static("00:00", id="elapsed")
        with VerticalScroll(id="log-wrap"):
            yield RichLog(id="log", wrap=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        """Kick off extraction as soon as this screen is shown."""
        threading.Thread(target=self.run_extraction, daemon=True).start()
        self.set_interval(1, self._tick_elapsed)

    def _tick_elapsed(self) -> None:
        if not self.done:
            self.query_one("#elapsed", Static).update(format_elapsed(time.monotonic() - self.start_time))

    def action_try_quit(self) -> None:
        if self.done:
            self.app.exit()
            return

        def check(quit_anyway: bool | None) -> None:
            if quit_anyway:
                self.app.exit()

        self.app.push_screen(ConfirmQuitScreen(), check)

    def action_browse(self) -> None:
        """Open the contacts browser on top of this screen without stopping extraction."""
        self.app.push_screen(BrowseScreen())

    def append_log(self, msg: str) -> None:
        """Append a line to the on-screen log (thread-safe)."""
        self.app.call_from_thread(self.query_one("#log", RichLog).write, msg)

    def run_extraction(self) -> None:
        """Worker: scan each selected folder, extract contacts, write to storage.

        Runs entirely off the UI thread; progress and log updates are
        marshalled back via `app.call_from_thread`. Flushes after each
        folder so a crash mid-run only loses the folder in progress, not
        everything scanned so far.
        """
        store = ContactStore(self.db_path)
        progress = self.query_one("#progress", ProgressBar)
        status = self.query_one("#folder_status", Static)

        total_folders = len(self.folders)
        for fi, folder in enumerate(self.folders, start=1):
            self.app.call_from_thread(status.update, f"[{fi}/{total_folders}] Scanning: {folder}")
            try:
                uidvalidity = im.select_folder(self.conn, folder)
            except im.ImapError as e:
                self.append_log(f"[red]Skip {folder}: {e}[/red]")
                continue

            checkpoint = store.checkpoint(self.host, self.username, folder)
            if checkpoint and checkpoint[0] != uidvalidity:
                self.append_log(
                    f"[red]Skip {folder}: UIDVALIDITY changed; use a fresh database "
                    "to avoid double-counting.[/red]"
                )
                continue

            last_uid = checkpoint[1] if checkpoint else 0
            try:
                uids = im.search_uids(self.conn, last_uid)
            except im.ImapError as e:
                self.append_log(f"[red]Skip {folder}: {e}[/red]")
                continue

            self.append_log(f"{folder}: {len(uids)} new messages")
            self.app.call_from_thread(progress.update, total=len(uids) or 1, progress=0)
            done = 0
            try:
                for batch, headers in im.fetch_header_batches(self.conn, uids):
                    for raw_headers in headers:
                        date = im.parse_date(raw_headers)
                        for name, addr in im.parse_addresses(raw_headers):
                            domain = addr.split("@")[-1].lower() if "@" in addr else ""
                            if domain in self.excluded_domains:
                                continue
                            store.record(addr, name, folder, date)
                    done += len(batch)
                    self.app.call_from_thread(progress.update, progress=done)
            except im.ImapError as e:
                store.discard()
                self.append_log(f"[red]Skip {folder}: {e}[/red]")
                continue

            self.app.call_from_thread(progress.update, progress=len(uids) or 1)
            newest_uid = int(uids[-1]) if uids else last_uid
            store.flush((self.host, self.username, folder, uidvalidity, newest_uid))

        count = store.contact_count
        store.close()
        self.done = True
        try:
            self.conn.logout()
        except imaplib.IMAP4.error:
            pass

        self.app.call_from_thread(
            status.update, f"[green]Done. {count} unique contacts written to {self.db_path}[/green]"
        )
        self.append_log("Press q to quit.")


class BrowseScreen(Screen):
    """Browse, search, filter, and export contacts from a local db."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self):
        super().__init__()
        self.cfg = cfgmod.load()
        self.store: ContactStore | None = None
        self.page_offset = 0
        self.total = 0
        self._suggest_prefix = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="db-row", classes="toolbar"):
            yield Input(value=self.cfg["db_path"], placeholder="contacts.db", id="db_path")
            yield Button("Load", id="load", variant="primary")
        with Vertical(id="search-wrap"):
            with Horizontal(classes="toolbar"):
                yield Input(
                    placeholder="Search contacts…  try folder: after: before:",
                    id="query",
                )
                yield Button("Search", id="search_btn", variant="primary")
            yield OptionList(id="suggestions")
        yield DataTable(id="table", zebra_stripes=True, cursor_type="row")
        with Horizontal(id="page-row", classes="toolbar"):
            yield Button("< Prev", id="prev")
            yield Static("", id="page_info")
            yield Button("Next >", id="next")
        with Horizontal(id="export-row", classes="toolbar"):
            yield Input(value="export.csv", id="export_path")
            yield Button("Export matches to CSV", id="export")
        yield Static("", id="browse_status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Email", "Name", "Msgs", "Folders", "First seen", "Last seen")
        self.query_one("#suggestions", OptionList).display = False
        self.load_db()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "load":
            self.load_db()
        elif button_id == "search_btn":
            self.query_one("#suggestions", OptionList).display = False
            self.page_offset = 0
            self.run_search()
        elif button_id == "prev":
            self.page_offset = max(0, self.page_offset - PAGE_SIZE)
            self.run_search()
        elif button_id == "next":
            if self.page_offset + PAGE_SIZE < self.total:
                self.page_offset += PAGE_SIZE
                self.run_search()
        elif button_id == "export":
            self.export_matches()

    def load_db(self) -> None:
        db_path = self.query_one("#db_path", Input).value.strip()
        status = self.query_one("#browse_status", Static)
        if not db_path:
            status.update("[red]Enter a db path.[/red]")
            return
        if self.store:
            self.store.close()
        self.store = ContactStore(db_path)
        cfgmod.save({**self.cfg, "db_path": db_path})
        self.page_offset = 0
        self.run_search()

    def _filters(self) -> dict:
        return parse_query(self.query_one("#query", Input).value.strip())

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "query":
            self._update_suggestions(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "query":
            self.query_one("#suggestions", OptionList).display = False
            self.page_offset = 0
            self.run_search()

    def _update_suggestions(self, value: str) -> None:
        """Gmail-style operator/value suggestions for the last word being typed."""
        space = value.rfind(" ")
        self._suggest_prefix = value[: space + 1]
        current = value[space + 1 :]

        options: list[str] = []
        if ":" not in current:
            options = [op for op in SEARCH_OPERATORS if op.startswith(current)] if current else SEARCH_OPERATORS
        elif current.lower().startswith("folder:") and self.store:
            val = current[len("folder:") :]
            options = [f"folder:{f}" for f in self.store.distinct_folders() if f.lower().startswith(val.lower())]

        suggestions = self.query_one("#suggestions", OptionList)
        suggestions.clear_options()
        if options:
            for opt in options:
                suggestions.add_option(Option(opt, id=opt))
            suggestions.display = True
        else:
            suggestions.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "suggestions":
            return
        chosen = str(event.option.id)
        inp = self.query_one("#query", Input)
        inp.value = self._suggest_prefix + chosen + ("" if chosen.endswith(":") else " ")
        inp.cursor_position = len(inp.value)
        self.query_one("#suggestions", OptionList).display = False
        inp.focus()

    def run_search(self) -> None:
        status = self.query_one("#browse_status", Static)
        if not self.store:
            return
        rows, total = self.store.search(
            **self._filters(), limit=PAGE_SIZE, offset=self.page_offset
        )
        self.total = total
        table = self.query_one("#table", DataTable)
        table.clear()
        for email, name, count, folders, first_seen, last_seen in rows:
            table.add_row(email, name or "", str(count), folders, first_seen or "", last_seen or "")

        page = self.page_offset // PAGE_SIZE + 1
        last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.query_one("#page_info", Static).update(f"Page {page}/{last_page} ({total} contacts)")
        self.query_one("#prev", Button).disabled = self.page_offset == 0
        self.query_one("#next", Button).disabled = self.page_offset + PAGE_SIZE >= total
        status.update("")

    def export_matches(self) -> None:
        status = self.query_one("#browse_status", Static)
        if not self.store:
            status.update("[red]Load a db first.[/red]")
            return
        path = self.query_one("#export_path", Input).value.strip()
        if not path:
            status.update("[red]Enter an export path.[/red]")
            return
        count = self.store.export_csv(path, **self._filters())
        status.update(f"[green]Exported {count} contacts to {path}[/green]")


class RolodexApp(App):
    """Rolodex: entry point. Pushes StartScreen, which leads to either the
    Connect -> Folder -> Domain -> Run extraction chain, or BrowseScreen."""

    TITLE = "Rolodex"
    CSS = """
    Screen {
        align: center middle;
        background: $surface;
    }
    RunScreen, BrowseScreen, FolderScreen, DomainScreen {
        align: left top;
        background: $surface;
    }

    .title {
        padding: 1 2 0 2;
        text-style: none;
        color: $text;
    }
    .hint {
        padding: 0 2 1 2;
        color: $text-muted;
        text-style: italic;
    }

    .panel {
        width: 72;
        border: round $primary-muted;
        background: $panel;
        padding: 1 3;
    }
    #start-panel {
        width: 46;
        align: center middle;
        border: round $primary-muted;
    }
    #start-panel .title {
        text-align: center;
        text-style: bold;
        color: $accent;
        width: 1fr;
    }
    #start-panel Button {
        width: 1fr;
        margin: 1 0 0 0;
        border: none;
    }

    #form { padding: 1 3; }
    #form Label { color: $text-muted; padding: 1 0 0 0; }

    Input {
        border: round $primary-muted;
        background: $boost;
    }
    Input:focus {
        border: round $accent;
    }

    Button {
        border: none;
        min-width: 12;
    }

    SelectionList {
        border: round $primary-muted;
        background: $panel;
        margin: 1 0;
    }

    #add-row { height: 3; padding: 0 0 1 0; }
    #new_domain { width: 1fr; margin-right: 1; }
    ListView {
        border: round $primary-muted;
        background: $panel;
        height: auto;
        max-height: 12;
        margin: 0 0 1 0;
    }

    #log-wrap {
        height: 1fr;
        border: round $primary-muted;
        margin: 0 2 1 2;
    }
    #log { background: $panel; }
    #folder_status { padding: 1 2; }
    #progress-row { height: 1; margin: 0 2 1 2; }
    #progress-row ProgressBar { width: 1fr; }
    #elapsed { width: 8; content-align: right middle; color: $text-muted; }
    #confirm-panel { width: 60; align: center middle; }
    #confirm-panel Button { width: 1fr; margin: 1 1 0 0; }

    .toolbar { height: 3; padding: 0 1; margin-bottom: 1; }
    .toolbar Input { width: 1fr; margin-right: 1; }

    #search-wrap { padding: 0 1; height: auto; }
    #suggestions {
        border: round $accent;
        background: $panel;
        height: auto;
        max-height: 8;
        margin: 0 1 1 1;
    }

    #table {
        height: 1fr;
        margin: 0 1;
        border: round $primary-muted;
        background: $panel;
    }
    #page_info { width: 1fr; content-align: center middle; color: $text-muted; }
    #browse_status { padding: 0 2 1 2; color: $text-muted; }
    """

    def get_system_commands(self, screen: Screen):
        for command in super().get_system_commands(screen):
            if "screenshot" in command.title.lower():
                continue
            yield command

    def on_mount(self) -> None:
        self.push_screen(StartScreen())


if __name__ == "__main__":
    RolodexApp().run()
