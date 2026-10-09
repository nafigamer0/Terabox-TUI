"""Modal dialogs: confirm, text input, settings, transfer progress."""
from __future__ import annotations

import threading

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ProgressBar, Static

from .config import parse_curl


class CurlInput(Input):
    """Input for curl commands: multiline pastes are joined into one line."""

    def _on_paste(self, event: events.Paste) -> None:
        if event.text:
            lines = event.text.splitlines()
            if len(lines) > 1:
                event.text = " ".join(lines)
        super()._on_paste(event)


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
        background: $background 60%;
    }
    #confirm-box {
        width: 70;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title { text-style: bold; color: $accent; width: 100%; }
    #confirm-message { margin: 1 0; width: 100%; }
    #confirm-buttons { height: 3; align: center middle; }
    #confirm-buttons Button { margin: 0 1; min-width: 10; }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("left", "focus_yes", "Yes"),
        ("right", "focus_no", "No"),
    ]

    def __init__(self, message: str, title: str = "Confirm", danger: bool = True):
        super().__init__()
        self._message = message
        self._title = title
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._title, id="confirm-title")
            yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button(
                    "Yes",
                    variant="error" if self._danger else "primary",
                    id="yes",
                )
                yield Button("No", id="no")

    def on_mount(self) -> None:
        btn = self.query_one_optional("#no", Button)
        if btn is not None:
            btn.focus()

    @on(Button.Pressed, "#yes")
    def accept(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def reject(self) -> None:
        self.dismiss(False)

    def action_focus_yes(self) -> None:
        btn = self.query_one_optional("#yes", Button)
        if btn is not None:
            btn.focus()

    def action_focus_no(self) -> None:
        btn = self.query_one_optional("#no", Button)
        if btn is not None:
            btn.focus()

    def action_cancel(self) -> None:
        self.dismiss(False)


class InputScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    InputScreen {
        align: center middle;
        background: $background 60%;
    }
    #input-box {
        width: 80;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #input-title { text-style: bold; color: $accent; }
    #input-prompt { margin: 1 0; color: $text-muted; }
    #input-field { margin: 1 0; }
    #input-buttons { height: 3; align: center middle; }
    #input-buttons Button { margin: 0 1; min-width: 10; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self, title: str, prompt: str = "", default: str = "", placeholder: str = ""
    ):
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._default = default
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box"):
            yield Label(self._title, id="input-title")
            if self._prompt:
                yield Static(self._prompt, id="input-prompt")
            yield Input(
                value=self._default,
                placeholder=self._placeholder,
                id="input-field",
            )
            with Horizontal(id="input-buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        field = self.query_one_optional("#input-field", Input)
        if field is None:
            return
        field.focus()
        field.cursor_position = len(field.value)

    @on(Input.Submitted, "#input-field")
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    @on(Button.Pressed, "#ok")
    def accept(self) -> None:
        self.dismiss(self.query_one("#input-field", Input).value)

    @on(Button.Pressed, "#cancel")
    def reject(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen[dict | None]):
    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
        background: $background 60%;
    }
    #settings-box {
        width: 110;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #settings-title { text-style: bold; color: $accent; }
    #settings-hint { color: $text-muted; margin: 1 0; }
    .settings-label { margin-top: 1; color: $text; }
    #settings-buttons { height: 3; align: center middle; margin-top: 1; }
    #settings-buttons Button { margin: 0 1; min-width: 10; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, cfg: dict):
        super().__init__()
        self._cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-box"):
            yield Label("TeraBox credentials", id="settings-title")
            yield Static(
                "Paste the full curl command of any dm.terabox.com request "
                "(from browser DevTools), or fill the fields below.",
                id="settings-hint",
            )
            yield CurlInput(placeholder="curl 'https://dm.terabox.com/...jsToken=...'", id="curl")
            yield Button("Parse Creds", id="parse")
            yield Label("Cookie (ndus=...)", classes="settings-label")
            yield Input(value=self._cfg.get("cookie", ""), id="cookie")
            yield Label("jsToken", classes="settings-label")
            yield Input(value=self._cfg.get("js_token", ""), id="jstoken")
            yield Label("bdstoken", classes="settings-label")
            yield Input(value=self._cfg.get("bdstoken", ""), id="bdstoken")
            yield Static(
                f"Saved to {self._cfg.get('_path', '~/.config/terabox-tui/config.json')}",
                id="settings-path",
            )
            with Horizontal(id="settings-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        curl = self.query_one_optional("#curl", Input)
        if curl is not None:
            curl.focus()

    @on(Button.Pressed, "#parse")
    def parse(self) -> None:
        raw = self.query_one("#curl", Input).value.strip()
        if not raw:
            self.app.notify("Nothing to parse", severity="warning")
            return
        parsed = parse_curl(raw)
        if not parsed:
            self.app.notify("Could not find jsToken / bdstoken / Cookie", severity="error")
            return
        if parsed.get("cookie"):
            self.query_one("#cookie", Input).value = parsed["cookie"]
        if parsed.get("js_token"):
            self.query_one("#jstoken", Input).value = parsed["js_token"]
        if parsed.get("bdstoken"):
            self.query_one("#bdstoken", Input).value = parsed["bdstoken"]
        found = ", ".join(k for k in ("cookie", "js_token", "bdstoken") if parsed.get(k))
        self.app.notify(f"Parsed: {found}")

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        cookie = self.query_one("#cookie", Input).value.strip()
        js_token = self.query_one("#jstoken", Input).value.strip()
        bdstoken = self.query_one("#bdstoken", Input).value.strip()
        if not cookie or not js_token:
            self.app.notify("Cookie and jsToken are required", severity="error")
            return
        cfg = dict(self._cfg)
        cfg.update(
            {"cookie": cookie, "js_token": js_token, "bdstoken": bdstoken}
        )
        self.dismiss(cfg)

    @on(Button.Pressed, "#cancel")
    def reject(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TransferScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    TransferScreen {
        align: center middle;
        background: $background 60%;
    }
    #transfer-box {
        width: 72;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #transfer-title { text-style: bold; color: $accent; }
    #transfer-detail { margin: 1 0; min-height: 2; }
    #transfer-bar { margin: 1 0; }
    #transfer-buttons { height: 3; align: center middle; }
    #transfer-buttons Button { min-width: 12; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str):
        super().__init__()
        self._title = title
        self.cancel_event = threading.Event()

    def compose(self) -> ComposeResult:
        with Vertical(id="transfer-box"):
            yield Label(self._title, id="transfer-title")
            yield Static("Starting...", id="transfer-detail")
            yield ProgressBar(total=1.0, show_eta=False, id="transfer-bar")
            with Horizontal(id="transfer-buttons"):
                yield Button("Cancel", variant="error", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        if not self.cancel_event.is_set():
            self.cancel_event.set()
            btn = self.query_one("#cancel", Button)
            btn.disabled = True
            self.query_one("#transfer-detail", Static).update("Cancelling...")

    def set_progress(self, fraction: float, detail: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#transfer-bar", ProgressBar).update(
            progress=max(0.0, min(1.0, fraction))
        )
        self.query_one("#transfer-detail", Static).update(detail)

    def set_detail(self, detail: str) -> None:
        if self.is_attached:
            self.query_one("#transfer-detail", Static).update(detail)
