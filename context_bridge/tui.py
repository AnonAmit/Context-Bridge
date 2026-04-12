"""Full-screen Textual TUI for Context Bridge.

Provides an interactive terminal interface with:
  - Left panel: IDE selector + session list (scrollable)
  - Right panel: session detail view
  - Bottom bar: action buttons (Export, Bridge, Inspect)
  - Keyboard shortcuts: e=export, b=bridge, f=forensics, q=quit
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    LoadingIndicator,
    OptionList,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option


class ContextBridgeTUI(App):
    """Context Bridge interactive TUI application."""

    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 1fr;
    }

    #left-panel {
        width: 38;
        min-width: 30;
        border: solid $primary;
        padding: 0 1;
    }

    #right-panel {
        width: 1fr;
        border: solid $accent;
        padding: 0 1;
    }

    #ide-selector {
        height: 7;
        margin-bottom: 1;
    }

    #session-list {
        height: 1fr;
    }

    #detail-log {
        height: 1fr;
    }

    #bottom-bar {
        dock: bottom;
        height: 3;
        padding: 0 1;
        background: $panel;
    }

    #bottom-bar Button {
        margin: 0 1;
        min-width: 14;
    }

    .panel-title {
        text-style: bold;
        color: $text;
        padding: 0 0 1 0;
    }

    #status-label {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text-muted;
    }

    #loading {
        display: none;
        height: 3;
    }

    #loading.visible {
        display: block;
    }
    """

    TITLE = "Context Bridge"
    SUB_TITLE = "Transfer AI session context between IDEs"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("e", "export_session", "Export", show=True),
        Binding("f", "forensics", "Forensics", show=True),
        Binding("d", "detect", "Detect", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._current_ide: str | None = None
        self._sessions: list[dict[str, Any]] = []
        self._selected_session_id: str | None = None
        self._connectors_loaded = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-container"):
            with Vertical(id="left-panel"):
                yield Label("[b]IDE Selector[/b]", classes="panel-title")
                yield OptionList(
                    Option("Codex CLI", id="codex"),
                    Option("Cursor", id="cursor"),
                    Option("Antigravity", id="antigravity"),
                    Option("Claude Code", id="claude"),
                    id="ide-selector",
                )
                yield Label("[b]Sessions[/b]", classes="panel-title")
                yield DataTable(id="session-list")
                yield LoadingIndicator(id="loading")
            with Vertical(id="right-panel"):
                yield Label("[b]Session Detail[/b]", classes="panel-title")
                yield RichLog(id="detail-log", highlight=True, markup=True)
        with Horizontal(id="bottom-bar"):
            yield Button("Export [e]", id="btn-export", variant="primary")
            yield Button("Forensics [f]", id="btn-forensics", variant="warning")
            yield Button("Detect [d]", id="btn-detect", variant="default")
            yield Button("Quit [q]", id="btn-quit", variant="error")
        yield Label("Ready. Select an IDE to begin.", id="status-label")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize the session list table columns."""
        table = self.query_one("#session-list", DataTable)
        table.add_columns("#", "ID", "Title", "Msgs")
        table.cursor_type = "row"
        table.zebra_stripes = True

    # ── IDE Selection ────────────────────────────────────────────────────

    @on(OptionList.OptionSelected, "#ide-selector")
    def on_ide_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle IDE selection from the option list."""
        ide_id = str(event.option.id) if event.option.id else None
        if ide_id:
            self._current_ide = ide_id
            self._set_status(f"Loading {ide_id} sessions...")
            self._load_sessions(ide_id)

    @work(thread=True)
    def _load_sessions(self, ide_name: str) -> None:
        """Load sessions for the selected IDE in a background thread."""
        try:
            from context_bridge.normalizer import list_sessions
            sessions = list_sessions(ide_name, limit=50)
            self.call_from_thread(self._populate_sessions, sessions, ide_name)
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Failed to load sessions: {exc}")

    def _populate_sessions(self, sessions: list[dict], ide_name: str) -> None:
        """Populate the session table with results."""
        self._sessions = sessions
        table = self.query_one("#session-list", DataTable)
        table.clear()

        for i, s in enumerate(sessions, 1):
            sid = s.get("id", "?")
            sid_short = sid[:8] if len(sid) > 8 else sid
            title = s.get("title", "--") or "--"
            if len(title) > 22:
                title = title[:20] + ".."
            msgs = str(s.get("messages", "?"))
            table.add_row(str(i), sid_short, title, msgs, key=sid)

        self._set_status(f"Found {len(sessions)} session(s) in {ide_name}")

    # ── Session Selection ────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#session-list")
    def on_session_selected(self, event: DataTable.RowSelected) -> None:
        """Handle session selection from the data table."""
        if event.row_key and event.row_key.value:
            session_id = str(event.row_key.value)
            self._selected_session_id = session_id
            self._set_status(f"Loading session {session_id[:12]}...")
            self._load_session_detail(session_id)

    @work(thread=True)
    def _load_session_detail(self, session_id: str) -> None:
        """Load full session detail in a background thread."""
        if not self._current_ide:
            return
        try:
            from context_bridge.normalizer import normalize_session
            result = normalize_session(self._current_ide, session_id, redact=True)
            self.call_from_thread(self._display_session_detail, result)
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Extraction failed: {exc}")

    def _display_session_detail(self, result: Any) -> None:
        """Display session detail in the right panel RichLog."""
        log = self.query_one("#detail-log", RichLog)
        log.clear()

        session = result.session
        confidence = result.overall_confidence()

        log.write(f"[bold cyan]Session: {session.session_id}[/]")
        log.write(f"[bold]Source:[/] {session.source_ide}")
        if session.title:
            log.write(f"[bold]Title:[/] {session.title}")
        if session.model:
            log.write(f"[bold]Model:[/] {session.model}")
        if session.provider:
            log.write(f"[bold]Provider:[/] {session.provider}")
        if session.mode:
            log.write(f"[bold]Mode:[/] {session.mode}")
        if session.project_root:
            log.write(f"[bold]Project:[/] {session.project_root}")
        if session.created_at:
            log.write(f"[bold]Created:[/] {session.created_at}")

        log.write("")
        log.write(f"[bold green]Messages:[/] {session.message_count}")
        log.write(f"[bold yellow]Files:[/] {session.file_count}")
        log.write(f"[bold magenta]Confidence:[/] {confidence}")

        # Confidence details
        if result.confidence:
            log.write("")
            log.write("[bold]--- Confidence ---[/]")
            for c in result.confidence:
                icon = "[*]" if c.level == "HIGH" else "[~]" if c.level == "MEDIUM" else "[ ]"
                style = "green" if c.level == "HIGH" else "yellow" if c.level == "MEDIUM" else "red"
                log.write(f"  [{style}]{icon} {c.field_name}[/]: {c.source or '--'}")

        # Messages
        if session.messages:
            log.write("")
            log.write("[bold]--- Messages ---[/]")
            for i, msg in enumerate(session.messages[:15], 1):
                role_color = {
                    "user": "green", "assistant": "cyan",
                    "system": "yellow", "tool": "magenta"
                }.get(msg.role, "white")
                preview = msg.preview(80)
                log.write(f"  [{role_color}]{i}. [{msg.role}][/] {preview}")

        # Touched files
        if session.touched_files:
            log.write("")
            log.write("[bold]--- Touched Files ---[/]")
            for tf in session.touched_files[:20]:
                status_color = {
                    "created": "green", "edited": "yellow",
                    "deleted": "red", "referenced": "blue"
                }.get(tf.status, "white")
                log.write(f"  [{status_color}][{tf.status}][/] {tf.path}")

        # Context items
        if session.context_items:
            log.write("")
            log.write("[bold]--- Context Items ---[/]")
            for item in session.context_items[:20]:
                log.write(f"  [{item.type}] {item.value}")

        # Warnings
        if result.warnings:
            log.write("")
            log.write("[bold yellow]--- Warnings ---[/]")
            for w in result.warnings:
                log.write(f"  [yellow]! {w}[/]")

        if result.errors:
            log.write("")
            log.write("[bold red]--- Errors ---[/]")
            for e in result.errors:
                log.write(f"  [red]x {e}[/]")

        self._set_status(
            f"Session loaded: {session.message_count} messages, "
            f"{session.file_count} files, confidence: {confidence}"
        )

    # ── Actions ──────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-export")
    def on_export_pressed(self) -> None:
        self.action_export_session()

    @on(Button.Pressed, "#btn-forensics")
    def on_forensics_pressed(self) -> None:
        self.action_forensics()

    @on(Button.Pressed, "#btn-detect")
    def on_detect_pressed(self) -> None:
        self.action_detect()

    @on(Button.Pressed, "#btn-quit")
    def on_quit_pressed(self) -> None:
        self.exit()

    def action_export_session(self) -> None:
        """Export the currently selected session."""
        if not self._current_ide or not self._selected_session_id:
            self._set_status("[!] Select a session first")
            return
        self._set_status("Exporting session...")
        self._do_export(self._current_ide, self._selected_session_id)

    @work(thread=True)
    def _do_export(self, ide: str, session_id: str) -> None:
        """Export session in background thread."""
        try:
            from context_bridge.normalizer import normalize_session
            from context_bridge.emitter import export_session

            result = normalize_session(ide, session_id, redact=True)
            out_dir = Path("./context-bridge-exports")
            export_dir = out_dir / f"{ide}_{session_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            created = export_session(result.session, export_dir)

            files_str = ", ".join(created.keys())
            self.call_from_thread(
                self._show_in_log,
                f"\n[bold green][+] Exported to {export_dir}[/]\n  Files: {files_str}"
            )
            self.call_from_thread(
                self._set_status,
                f"Export complete: {export_dir}"
            )
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Export failed: {exc}")

    def action_forensics(self) -> None:
        """Run forensics on the selected IDE."""
        if not self._current_ide:
            self._set_status("[!] Select an IDE first")
            return
        self._set_status(f"Running forensics on {self._current_ide}...")
        self._do_forensics(self._current_ide)

    @work(thread=True)
    def _do_forensics(self, ide: str) -> None:
        """Run forensics in background thread."""
        try:
            from context_bridge.normalizer import get_connector
            import json

            connector = get_connector(ide)
            if hasattr(connector, "forensics_dump"):
                results = connector.forensics_dump()
            else:
                results = [{"key": "No forensics available", "classification": "UNKNOWN", "value_preview": "--"}]

            # Save report
            out_dir = Path("./context-bridge-exports")
            out_dir.mkdir(parents=True, exist_ok=True)
            report_path = out_dir / f"forensics-{ide}-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            report_path.write_text(
                json.dumps({"ide": ide, "results": results}, indent=2, default=str),
                encoding="utf-8",
            )

            # Display in log
            lines = [f"\n[bold red]--- Forensics: {ide} ({len(results)} entries) ---[/]"]
            for r in results[:50]:
                cls = r.get("classification", "?")
                cls_color = {
                    "CHAT": "green", "CONTEXT": "cyan",
                    "CONFIG": "yellow", "CACHE": "blue"
                }.get(cls, "red")
                lines.append(f"  [{cls_color}][{cls}][/] {r.get('key', '--')}")

            lines.append(f"\n[bold]Report saved: {report_path}[/]")
            self.call_from_thread(self._show_in_log, "\n".join(lines))
            self.call_from_thread(self._set_status, f"Forensics complete: {len(results)} entries, saved to {report_path}")
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Forensics failed: {exc}")

    def action_detect(self) -> None:
        """Detect all installed IDEs."""
        self._set_status("Detecting installed IDEs...")
        self._do_detect()

    @work(thread=True)
    def _do_detect(self) -> None:
        """Detect IDEs in background thread."""
        try:
            from context_bridge.normalizer import detect_all_ides
            results = detect_all_ides()

            lines = ["\n[bold]--- IDE Detection ---[/]"]
            for r in results:
                status = r.get("status", "unknown")
                icon = "[+]" if status == "found" else "[~]" if status == "partial" else "[-]"
                color = "green" if status == "found" else "yellow" if status == "partial" else "red"
                lines.append(f"  [{color}]{icon} {r.get('ide', '?')}[/]: {r.get('sessions', '--')} sessions")
                for p in r.get("paths", []):
                    lines.append(f"      {p}")

            self.call_from_thread(self._show_in_log, "\n".join(lines))
            found = sum(1 for r in results if r.get("status") in ("found", "partial"))
            self.call_from_thread(self._set_status, f"Detected {found} IDE(s)")
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Detection failed: {exc}")

    def action_refresh(self) -> None:
        """Refresh the current IDE session list."""
        if self._current_ide:
            self._set_status(f"Refreshing {self._current_ide}...")
            self._load_sessions(self._current_ide)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _set_status(self, text: str) -> None:
        """Update the status bar."""
        try:
            label = self.query_one("#status-label", Label)
            label.update(text)
        except NoMatches:
            pass

    def _show_in_log(self, text: str) -> None:
        """Write text to the detail log."""
        try:
            log = self.query_one("#detail-log", RichLog)
            log.write(text)
        except NoMatches:
            pass

    def _show_error(self, text: str) -> None:
        """Show an error in both status bar and log."""
        self._set_status(f"[-] {text}")
        self._show_in_log(f"\n[bold red][-] {text}[/]")
