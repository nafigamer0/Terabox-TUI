"""TeraBox file manager Textual application."""
from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, ProgressBar, Static

from .api import FileEntry, TeraBoxClient, TeraBoxError, TransferCancelled, human_size
from .config import CONFIG_PATH, config_ready, load_config, save_config
from .dialogs import ConfirmScreen, InputScreen, SettingsScreen, TransferScreen

SORT_NAMES = {1: "name", 2: "size", 3: "mtime"}
SELECTION_STYLE = "bold reverse"
ROW_COLUMNS = ("sel", "name", "size", "mtime", "type")


def _join(directory: str, name: str) -> str:
    return (directory.rstrip("/") or "") + "/" + name


class TeraBoxApp(App):
    TITLE = "TeraBox TUI"
    CSS = """
    Screen { background: $background; }
    #main { height: 1fr; }
    #sidebar {
        width: 30;
        border-right: solid $primary-darken-2;
        padding: 1 1;
    }
    #sidebar .side-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #quota-bar { margin: 1 0; }
    #quota-text { color: $text-muted; }
    #side-help {
        color: $text-muted;
        margin-top: 2;
    }
    #listing { border: none; }
    #status {
        height: 1;
        padding: 0 1;
        background: $primary-darken-2;
        color: $text;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("backspace", "go_parent", "Parent"),
        ("h", "go_parent", "Parent"),
        ("space", "toggle_select", "Select"),
        ("escape", "clear_selection", "Clear"),
        ("enter", "open_item", "Open"),
        ("u", "upload", "Upload"),
        ("d", "delete", "Delete"),
        ("r", "rename", "Rename"),
        ("m", "mkdir", "New folder"),
        ("R", "refresh", "Refresh"),
        ("s", "settings", "Settings"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg: dict = load_config()
        self.client = TeraBoxClient(self.cfg)
        self._cwd = "/"
        self._entries: dict[str, FileEntry] = {}
        self._row_order: list[str] = []
        self._raw_entries: list[FileEntry] = []
        self._selected: set[str] = set()
        self._sort_key = "name"
        self._sort_desc = False
        self._columns_ready = False
        self._cursor_path: str | None = None

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("Storage", classes="side-title")
                yield ProgressBar(total=1.0, show_eta=False, id="quota-bar")
                yield Static("—", id="quota-text")
                yield Static(
                    "Keys\n"
                    "  ↑↓ move   enter open\n"
                    "  space select   esc clear\n"
                    "  backspace parent\n"
                    "  d delete   r rename\n"
                    "  m mkdir   u upload\n"
                    "  R refresh   s settings\n"
                    "  q quit",
                    id="side-help",
                )
            yield DataTable(id="listing", cursor_type="row", zebra_stripes=True)
        yield Static("Loading...", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#listing", DataTable)
        table.add_column("", key="sel")
        table.add_column("Name", key="name")
        table.add_column("Size", key="size")
        table.add_column("Modified", key="mtime")
        table.add_column("Type", key="type")
        self._columns_ready = True
        table.focus()
        self.sub_title = "/"
        self.call_after_refresh(self._start_bootstrap)

    def _start_bootstrap(self) -> None:
        self.run_worker(self._bootstrap(), exclusive=True, group="bootstrap")

    async def _bootstrap(self) -> None:
        if not config_ready(self.cfg):
            await self._settings_flow(first_time=True)
        if config_ready(self.cfg):
            self._load(self._cwd)
        else:
            self._set_status(
                "No credentials — press s to open settings (paste curl or fill fields)"
            )

    # ------------------------------------------------------------ helpers

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _cursor_entry(self) -> FileEntry | None:
        table = self.query_one("#listing", DataTable)
        if not self._row_order:
            return None
        row = table.cursor_row
        if row < 0 or row >= len(self._row_order):
            return None
        return self._entries.get(self._row_order[row])

    def _update_status(self) -> None:
        parts = [self._cwd, f"{len(self._row_order)} items"]
        if self._selected:
            parts.append(f"{len(self._selected)} selected")
        self._set_status("  •  ".join(parts))
        self.sub_title = self._cwd

    def _update_quota(self, quota: dict) -> None:
        total = quota.get("total") or 0
        used = quota.get("used") or 0
        frac = (used / total) if total else 0.0
        bar = self.query_one("#quota-bar", ProgressBar)
        bar.update(total=1.0, progress=frac)
        text = f"{human_size(used)} / {human_size(total)}"
        if total:
            text += f"  ({frac * 100:.1f}%)"
        self.query_one("#quota-text", Static).update(text)

    # ---------------------------------------------------------- table fill

    @staticmethod
    def _row_cells(entry: FileEntry, mark: str) -> list[str]:
        label = ("📁 " if entry.isdir else "📄 ") + entry.name + ("/" if entry.isdir else "")
        size = "—" if entry.isdir else human_size(entry.size)
        mtime = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
            if entry.mtime
            else "—"
        )
        kind = "folder" if entry.isdir else (Path(entry.name).suffix.lstrip(".").upper() or "file")
        return [mark, label, size, mtime, kind]

    def _apply_row_style(
        self, entry: FileEntry, mark: str, selected: bool
    ) -> None:
        table = self.query_one("#listing", DataTable)
        cells = self._row_cells(entry, mark)
        if selected:
            cells = [Text(c, style=SELECTION_STYLE) for c in cells]
        for column, value in zip(ROW_COLUMNS, cells):
            table.update_cell(entry.path, column, value, update_width=False)

    def _fill_table(self, entries: list[FileEntry]) -> None:
        table = self.query_one("#listing", DataTable)
        self._raw_entries = list(entries)
        dirs = sorted(
            (e for e in entries if e.isdir), key=lambda e: e.name.lower()
        )
        files = [e for e in entries if not e.isdir]
        keyfn = {
            "name": lambda e: e.name.lower(),
            "size": lambda e: e.size,
            "mtime": lambda e: e.mtime,
        }[self._sort_key]
        files.sort(key=keyfn, reverse=self._sort_desc)

        self._entries = {e.path: e for e in dirs + files}
        self._row_order = [e.path for e in dirs + files]
        # drop selections that no longer exist
        self._selected &= set(self._row_order)

        table.clear()
        for entry in dirs + files:
            selected = entry.path in self._selected
            mark = "●" if selected else ""
            cells = self._row_cells(entry, mark)
            if selected:
                cells = [Text(c, style=SELECTION_STYLE) for c in cells]
            table.add_row(*cells, key=entry.path)

        if self._row_order:
            row = 0
            if self._cursor_path in self._row_order:
                row = self._row_order.index(self._cursor_path)
            table.move_cursor(row=row)
        self._update_status()

    # ------------------------------------------------------------- loading

    def _load(self, path: str) -> None:
        self._cwd = path
        self.sub_title = path
        self._set_status(f"Loading {path}…")
        self._load_worker(path)

    @work(thread=True, group="list", exclusive=True, exit_on_error=False)
    def _load_worker(self, path: str) -> None:
        entries: list[FileEntry] = []
        quota: dict | None = None
        error: TeraBoxError | None = None
        try:
            entries = self.client.list_dir(path)
            try:
                quota = self.client.quota()
            except TeraBoxError:
                quota = None
        except TeraBoxError as exc:
            error = exc
        self.call_from_thread(self._after_load, path, entries, quota, error)

    def _after_load(
        self,
        path: str,
        entries: list[FileEntry],
        quota: dict | None,
        error: TeraBoxError | None,
    ) -> None:
        if path != self._cwd:
            return  # stale result, a newer navigation superseded it
        if error is not None:
            self._set_status(f"Load failed: {error.message}")
            self.notify(f"Load failed: {error.message}", severity="error", timeout=8)
            if error.is_auth:
                self.call_after_refresh(self._open_settings)
            return
        self._fill_table(entries)
        if quota:
            self._update_quota(quota)

    # ---------------------------------------------------------- navigation

    def _navigate(self, path: str) -> None:
        if path != self._cwd:
            self._selected.clear()
            self._cursor_path = None
        self._load(path)

    def action_go_parent(self) -> None:
        if self._cwd == "/":
            return
        parent = str(Path(self._cwd).parent)
        self._navigate(parent if parent != "." else "/")

    @on(DataTable.RowSelected)
    def row_selected(self, event: DataTable.RowSelected) -> None:
        path = str(event.row_key.value)
        entry = self._entries.get(path)
        if entry is None:
            return
        if entry.isdir:
            self._cursor_path = None
            self._navigate(entry.path)
        else:
            self._download_flow(entry)

    def action_open_item(self) -> None:
        entry = self._cursor_entry()
        if entry is None:
            return
        if entry.isdir:
            self._cursor_path = None
            self._navigate(entry.path)
        else:
            self._download_flow(entry)

    def action_refresh(self) -> None:
        self._load(self._cwd)

    # ----------------------------------------------------------- selection

    def action_toggle_select(self) -> None:
        entry = self._cursor_entry()
        if entry is None:
            return
        if entry.path in self._selected:
            self._selected.discard(entry.path)
        else:
            self._selected.add(entry.path)
        self._apply_row_style(
            entry, "●" if entry.path in self._selected else "",
            entry.path in self._selected,
        )
        self._update_status()

    def action_clear_selection(self) -> None:
        if not self._selected:
            return
        for path in list(self._selected):
            entry = self._entries.get(path)
            if entry is not None:
                self._apply_row_style(entry, "", False)
        self._selected.clear()
        self._update_status()

    # --------------------------------------------------------------- sort

    @on(DataTable.HeaderSelected)
    def header_selected(self, event: DataTable.HeaderSelected) -> None:
        key = SORT_NAMES.get(event.column_index)
        if key is None:
            return
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = key != "name"
        entry = self._cursor_entry()
        self._cursor_path = entry.path if entry else None
        self._fill_table(self._raw_entries)

    # ------------------------------------------------------------- download

    def _download_flow(self, entry: FileEntry) -> None:
        self.run_worker(
            self._download_dialog(entry), exclusive=True, group="flow"
        )

    async def _download_dialog(self, entry: FileEntry) -> None:
        default_dest = str(Path.cwd() / entry.name)
        dest = await self.push_screen_wait(
            InputScreen(
                "Download",
                prompt=f"Download {entry.name} to:",
                default=default_dest,
            )
        )
        if dest is None or not dest.strip():
            return
        dest_path = Path(dest.strip()).expanduser()
        screen = TransferScreen(f"Downloading {entry.name}")
        self.push_screen(screen)
        self._download_worker(entry.path, entry.name, dest_path, screen)

    @work(thread=True, group="transfer", exclusive=True, exit_on_error=False)
    def _download_worker(
        self, path: str, name: str, dest: Path, screen: TransferScreen
    ) -> None:
        def progress(done: int, total: int) -> None:
            frac = (done / total) if total else 0.0
            detail = f"{human_size(done)} / {human_size(total)}"
            if total:
                detail += f"  ({frac * 100:.0f}%)"
            try:
                self.call_from_thread(screen.set_progress, frac, detail)
            except Exception:
                pass

        error: str | None = None
        message: str | None = None
        try:
            self.client.download(
                path,
                dest,
                progress=progress,
                should_cancel=screen.cancel_event.is_set,
            )
        except TransferCancelled:
            message = f"Download of {name} cancelled"
        except TeraBoxError as exc:
            error = f"Download failed: {exc.message}"
        except OSError as exc:
            error = f"Download failed: {exc}"
        else:
            message = f"Downloaded {name} → {dest}"
        self.call_from_thread(self._transfer_done, screen, error, message)

    # --------------------------------------------------------------- upload

    def action_upload(self) -> None:
        self.run_worker(self._upload_dialog(), exclusive=True, group="flow")

    async def _upload_dialog(self) -> None:
        default = str(Path.cwd())
        local = await self.push_screen_wait(
            InputScreen(
                "Upload",
                prompt=f"Local file or folder to upload into {self._cwd}:",
                default=default,
            )
        )
        if local is None or not local.strip():
            return
        local_path = Path(local.strip()).expanduser()
        if not local_path.exists():
            self.notify(f"Not found: {local_path}", severity="error", timeout=8)
            return
        screen = TransferScreen(f"Uploading {local_path.name} → {self._cwd}")
        self.push_screen(screen)
        self._upload_worker(local_path, self._cwd, screen)

    @work(thread=True, group="transfer", exclusive=True, exit_on_error=False)
    def _upload_worker(
        self, local: Path, target_dir: str, screen: TransferScreen
    ) -> None:
        def progress(done: int, total: int) -> None:
            frac = (done / total) if total else 0.0
            detail = f"{human_size(done)} / {human_size(total)}"
            if total:
                detail += f"  ({frac * 100:.0f}%)"
            try:
                self.call_from_thread(screen.set_progress, frac, detail)
            except Exception:
                pass

        def on_file(name: str, index: int, count: int) -> None:
            try:
                self.call_from_thread(
                    screen.set_detail, f"file {index}/{count}: {name}"
                )
            except Exception:
                pass

        error: str | None = None
        message: str | None = None
        refresh = False
        try:
            self.client.upload(
                local,
                target_dir,
                progress=progress,
                should_cancel=screen.cancel_event.is_set,
                on_file=on_file,
            )
        except TransferCancelled:
            message = f"Upload of {local.name} cancelled"
        except TeraBoxError as exc:
            error = f"Upload failed: {exc.message}"
        except OSError as exc:
            error = f"Upload failed: {exc}"
        else:
            message = f"Uploaded {local.name} → {target_dir}"
            refresh = True
        self.call_from_thread(
            self._transfer_done, screen, error, message, refresh
        )

    def _transfer_done(
        self,
        screen: TransferScreen,
        error: str | None,
        message: str | None,
        refresh: bool = False,
    ) -> None:
        if screen.is_attached:
            screen.dismiss(None)
        if error:
            self.notify(error, severity="error", timeout=8)
        elif message:
            self.notify(message)
        self.query_one("#listing", DataTable).focus()
        if refresh:
            self._load(self._cwd)

    # -------------------------------------------------------- write actions

    def action_delete(self) -> None:
        self.run_worker(self._delete_dialog(), exclusive=True, group="flow")

    async def _delete_dialog(self) -> None:
        if self._selected:
            targets = sorted(self._selected)
        else:
            entry = self._cursor_entry()
            targets = [entry.path] if entry else []
        if not targets:
            self.notify("Nothing selected", severity="warning")
            return
        names = ", ".join(Path(p).name or p for p in targets[:5])
        if len(targets) > 5:
            names += f" … (+{len(targets) - 5} more)"
        ok = await self.push_screen_wait(
            ConfirmScreen(
                f"Delete {len(targets)} item(s)?\n\n{names}\n\nThis cannot be undone.",
                title="Delete",
            )
        )
        if not ok:
            return
        self._write_worker("delete", targets)

    def action_rename(self) -> None:
        self.run_worker(self._rename_dialog(), exclusive=True, group="flow")

    async def _rename_dialog(self) -> None:
        entry = self._cursor_entry()
        if entry is None:
            self.notify("Nothing to rename", severity="warning")
            return
        new_name = await self.push_screen_wait(
            InputScreen("Rename", prompt="New name:", default=entry.name)
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or "/" in new_name:
            self.notify("Invalid name", severity="error")
            return
        if new_name == entry.name:
            return
        self._write_worker("rename", [entry.path, new_name])

    def action_mkdir(self) -> None:
        self.run_worker(self._mkdir_dialog(), exclusive=True, group="flow")

    async def _mkdir_dialog(self) -> None:
        name = await self.push_screen_wait(
            InputScreen(
                "New folder",
                prompt=f"Create folder in {self._cwd}:",
                placeholder="folder-name",
            )
        )
        if name is None:
            return
        name = name.strip()
        if not name or "/" in name:
            if name:
                self.notify("Invalid folder name", severity="error")
            return
        self._write_worker("mkdir", _join(self._cwd, name))

    @work(thread=True, group="write", exclusive=True, exit_on_error=False)
    def _write_worker(self, kind: str, payload) -> None:
        error: TeraBoxError | None = None
        try:
            if kind == "delete":
                self.client.delete(payload)
            elif kind == "mkdir":
                self.client.mkdir(payload)
            elif kind == "rename":
                self.client.rename(payload[0], payload[1])
        except TeraBoxError as exc:
            error = exc
        self.call_from_thread(self._write_done, kind, error)

    def _write_done(self, kind: str, error: TeraBoxError | None) -> None:
        if error is not None:
            self.notify(f"{kind} failed: {error.message}", severity="error", timeout=8)
            if error.is_auth:
                self.call_after_refresh(self._open_settings)
        else:
            self.notify(f"{kind.capitalize()} OK")
        if kind == "delete":
            self._selected.clear()
        self._load(self._cwd)

    # ------------------------------------------------------------ settings

    def action_settings(self) -> None:
        self.run_worker(
            self._settings_flow(first_time=False), exclusive=True, group="flow"
        )

    def _open_settings(self) -> None:
        self.run_worker(
            self._settings_flow(first_time=False), exclusive=True, group="flow"
        )

    async def _settings_flow(self, first_time: bool = False) -> None:
        shown_cfg = {**self.cfg, "_path": str(CONFIG_PATH)}
        cfg = await self.push_screen_wait(SettingsScreen(shown_cfg))
        if cfg is None:
            if first_time:
                self.notify(
                    "No credentials saved — press s to open settings later",
                    severity="warning",
                    timeout=8,
                )
            return
        cfg.pop("_path", None)
        path = save_config(cfg)
        self.cfg = load_config()
        self.client.update_config(self.cfg)
        self.notify(f"Saved {path}")
        self._load(self._cwd)
