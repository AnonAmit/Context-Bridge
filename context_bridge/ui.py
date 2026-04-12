"""Rich UI components for Context Bridge.

Provides consistent, beautiful terminal output using Rich panels,
tables, spinners, progress bars, and styled text. Every CLI command
uses these helpers for uniform visual presentation.

NOTE: On Windows, we force_terminal=True so Rich uses VT100 escape
sequences instead of the legacy Win32 console API, avoiding cp1252
encoding errors with Unicode characters.
"""

from __future__ import annotations

import os
import platform
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    from collections.abc import Generator

    from context_bridge.models import (
        BridgeSession,
        ExtractionResult,
        FieldConfidence,
    )

# ── Ensure VT100 mode on Windows ─────────────────────────────────────────────
# The legacy Windows console uses cp1252 encoding and cannot render Unicode
# characters like braille spinners. We enable VT processing at the OS level
# so Rich renders via ANSI escape sequences instead.

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # STD_OUTPUT_HANDLE = -11, STD_ERROR_HANDLE = -12
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass  # non-fatal: Rich will fall back to its own renderer

# ── Theme ────────────────────────────────────────────────────────────────────

CB_THEME = Theme(
    {
        "cb.success": "bold green",
        "cb.warning": "bold yellow",
        "cb.error": "bold red",
        "cb.info": "bold blue",
        "cb.dim": "dim",
        "cb.header": "bold magenta",
        "cb.accent": "bold cyan",
        "cb.key": "bold white",
        "cb.value": "white",
        "cb.high": "bold green",
        "cb.medium": "bold yellow",
        "cb.low": "bold red",
    }
)

# force_terminal=True tells Rich this is a real terminal (not piped).
# force_jupyter=False prevents Rich from trying Jupyter HTML output.
console = Console(theme=CB_THEME, force_terminal=True, force_jupyter=False)
err_console = Console(theme=CB_THEME, stderr=True, force_terminal=True, force_jupyter=False)

# ASCII-safe spinner name (avoids braille characters that crash cp1252)
_SPINNER = "line"


# ── Icons (ASCII-safe) ──────────────────────────────────────────────────────
# Using plain ASCII / basic symbols to avoid cp1252 encoding crashes.

_ICON_BRIDGE = "[>>]"
_ICON_SEARCH = "[?]"
_ICON_LIST = "[=]"
_ICON_META = "[i]"
_ICON_MSG = "[>]"
_ICON_CLIP = "[+]"
_ICON_FOLDER = "[d]"
_ICON_TARGET = "[*]"
_ICON_FORENSIC = "[x]"
_ICON_SCHEMA = "[#]"

# ── Header Panel ─────────────────────────────────────────────────────────────

def header_panel(command: str) -> None:
    """Display the standard Context Bridge header panel."""
    py_version = f"Python {sys.version_info.major}.{sys.version_info.minor}"
    os_name = platform.system()

    title_line = Text()
    title_line.append(f"{_ICON_BRIDGE} ", style="bold")
    title_line.append("Context Bridge", style="cb.header")
    title_line.append(" v1.0", style="cb.dim")
    title_line.append("  |  ", style="cb.dim")
    title_line.append("command: ", style="cb.dim")
    title_line.append(command, style="cb.accent")

    subtitle = Text()
    subtitle.append("Read-only mode", style="cb.info")
    subtitle.append(" - ", style="cb.dim")
    subtitle.append(os_name, style="cb.value")
    subtitle.append(" - ", style="cb.dim")
    subtitle.append(py_version, style="cb.value")

    panel_content = Text()
    panel_content.append_text(title_line)
    panel_content.append("\n")
    panel_content.append_text(subtitle)

    console.print()
    console.print(
        Panel(
            panel_content,
            border_style="magenta",
            padding=(0, 2),
        )
    )
    console.print()


# ── IDE Detection Table ──────────────────────────────────────────────────────

def detection_table(
    rows: list[dict],
) -> None:
    """Display a table of detected IDEs.

    Each row dict: {ide, status, paths, sessions}
    """
    table = Table(
        title=f"{_ICON_SEARCH} Detected IDEs",
        title_style="cb.header",
        border_style="blue",
        show_lines=True,
        pad_edge=True,
    )
    table.add_column("#", style="cb.dim", width=3, justify="right")
    table.add_column("IDE", style="cb.key", min_width=14)
    table.add_column("Status", min_width=12)
    table.add_column("Storage Paths", style="cb.value", min_width=30)
    table.add_column("Sessions", justify="right", min_width=8)

    for i, row in enumerate(rows, 1):
        status = row.get("status", "unknown")
        if status == "found":
            status_text = Text("[+] Found", style="cb.success")
        elif status == "partial":
            status_text = Text("[~] Partial", style="cb.warning")
        else:
            status_text = Text("[-] Not found", style="cb.error")

        paths = row.get("paths", [])
        paths_text = "\n".join(paths) if paths else "--"
        sessions = str(row.get("sessions", "--"))

        table.add_row(str(i), row.get("ide", "?"), status_text, paths_text, sessions)

    console.print(table)


# ── Session List Table ───────────────────────────────────────────────────────

def session_table(
    sessions: list[dict],
    ide_name: str = "",
) -> None:
    """Display a table of sessions.

    Each row dict: {id, title, date, messages, project}
    """
    table = Table(
        title=f"{_ICON_LIST} Sessions -- {ide_name}" if ide_name else f"{_ICON_LIST} Sessions",
        title_style="cb.header",
        border_style="cyan",
        show_lines=True,
        pad_edge=True,
    )
    table.add_column("#", style="cb.dim", width=3, justify="right")
    table.add_column("Session ID", style="cb.accent", min_width=10)
    table.add_column("Title", style="cb.key", min_width=20, max_width=40)
    table.add_column("Date", style="cb.value", min_width=12)
    table.add_column("Msgs", justify="right", min_width=5)
    table.add_column("Project Root", style="cb.dim", min_width=20, max_width=50)

    for i, s in enumerate(sessions, 1):
        sid = s.get("id", "?")
        # Truncate session ID to 8 chars for display
        sid_display = sid[:8] + "..." if len(sid) > 8 else sid
        title = s.get("title", "--") or "--"
        date = s.get("date", "--") or "--"
        msgs = str(s.get("messages", "--"))
        project = s.get("project", "--") or "--"

        table.add_row(str(i), sid_display, title, str(date), msgs, project)

    console.print(table)


# ── Session Detail Panels ────────────────────────────────────────────────────

def session_detail(result: ExtractionResult) -> None:
    """Display full detail of an extracted session."""
    session = result.session

    # ── Metadata panel ──
    meta_lines = []
    meta_lines.append(f"[cb.key]Session ID:[/]  {session.session_id}")
    meta_lines.append(f"[cb.key]Source IDE:[/]   {session.source_ide}")
    if session.title:
        meta_lines.append(f"[cb.key]Title:[/]       {session.title}")
    if session.model:
        meta_lines.append(f"[cb.key]Model:[/]       {session.model}")
    if session.provider:
        meta_lines.append(f"[cb.key]Provider:[/]    {session.provider}")
    if session.mode:
        meta_lines.append(f"[cb.key]Mode:[/]        {session.mode}")
    if session.created_at:
        meta_lines.append(f"[cb.key]Created:[/]     {session.created_at}")
    if session.updated_at:
        meta_lines.append(f"[cb.key]Updated:[/]     {session.updated_at}")
    if session.project_root:
        meta_lines.append(f"[cb.key]Project:[/]     {session.project_root}")
    meta_lines.append(f"[cb.key]Messages:[/]    {session.message_count}")
    meta_lines.append(f"[cb.key]Files:[/]       {session.file_count}")

    console.print(
        Panel(
            "\n".join(meta_lines),
            title=f"{_ICON_META} Session Metadata",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    # ── Messages table (first 10) ──
    if session.messages:
        msg_table = Table(
            title=f"{_ICON_MSG} Messages (first 10)",
            title_style="cb.header",
            border_style="blue",
            show_lines=True,
        )
        msg_table.add_column("#", style="cb.dim", width=3, justify="right")
        msg_table.add_column("Role", style="cb.accent", min_width=10)
        msg_table.add_column("Preview", style="cb.value", min_width=40, max_width=70)
        msg_table.add_column("Timestamp", style="cb.dim", min_width=12)

        for i, msg in enumerate(session.messages[:10], 1):
            role_style = {
                "user": "bold green",
                "assistant": "bold cyan",
                "system": "bold yellow",
                "tool": "bold magenta",
            }.get(msg.role, "white")
            role_text = Text(msg.role, style=role_style)
            ts = str(msg.timestamp) if msg.timestamp else "--"
            msg_table.add_row(str(i), role_text, msg.preview(70), ts)

        console.print(msg_table)

    # ── Context items ──
    if session.context_items:
        ctx_table = Table(
            title=f"{_ICON_CLIP} Context Items",
            title_style="cb.header",
            border_style="green",
            show_lines=True,
        )
        ctx_table.add_column("Type", style="cb.accent", min_width=8)
        ctx_table.add_column("Value", style="cb.value", min_width=30)
        ctx_table.add_column("Label", style="cb.dim", min_width=15)

        for item in session.context_items:
            ctx_table.add_row(item.type, item.value, item.label or "--")

        console.print(ctx_table)

    # ── Touched files ──
    if session.touched_files:
        file_table = Table(
            title=f"{_ICON_FOLDER} Touched Files",
            title_style="cb.header",
            border_style="yellow",
            show_lines=True,
        )
        file_table.add_column("Path", style="cb.value", min_width=30)
        file_table.add_column("Status", min_width=10)
        file_table.add_column("Hash", style="cb.dim", min_width=10)

        for f in session.touched_files:
            status_style = {
                "created": "bold green",
                "edited": "bold yellow",
                "deleted": "bold red",
                "referenced": "bold blue",
            }.get(f.status, "white")
            status_text = Text(f.status, style=status_style)
            file_table.add_row(f.path, status_text, (f.hash or "--")[:16])

        console.print(file_table)

    # ── Confidence indicators ──
    if result.confidence:
        confidence_panel(result.confidence)

    # ── Warnings ──
    if result.warnings:
        for w in result.warnings:
            warning(w)

    if result.errors:
        for e in result.errors:
            error(e)


# ── Confidence Panel ─────────────────────────────────────────────────────────

def confidence_panel(fields: list[FieldConfidence]) -> None:
    """Display confidence indicators for extracted fields."""
    table = Table(
        title=f"{_ICON_TARGET} Extraction Confidence",
        title_style="cb.header",
        border_style="magenta",
        show_lines=False,
    )
    table.add_column("Field", style="cb.key", min_width=18)
    table.add_column("Confidence", min_width=10)
    table.add_column("Source", style="cb.dim", min_width=20)
    table.add_column("Note", style="cb.dim", min_width=20)

    for f in fields:
        if f.level == "HIGH":
            indicator = Text("[*] HIGH", style="cb.high")
        elif f.level == "MEDIUM":
            indicator = Text("[~] MEDIUM", style="cb.medium")
        else:
            indicator = Text("[ ] LOW", style="cb.low")

        table.add_row(f.field_name, indicator, f.source or "--", f.note or "--")

    console.print(table)


# ── Progress ─────────────────────────────────────────────────────────────────

def create_progress(description: str = "Processing...") -> Progress:
    """Create a styled Rich progress bar."""
    return Progress(
        SpinnerColumn(_SPINNER, style="cb.info"),
        TextColumn("[cb.info]{task.description}"),
        BarColumn(bar_width=40, style="blue", complete_style="bold blue"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )


@contextmanager
def spinner(message: str) -> Generator[None, None, None]:
    """Context manager that shows a spinner while work is in progress."""
    with console.status(f"[cb.info]{message}[/]", spinner=_SPINNER):
        yield


# ── Status Messages ──────────────────────────────────────────────────────────

def success(message: str, detail: str | None = None) -> None:
    """Display a success message."""
    console.print(f"[cb.success][+] {message}[/]")
    if detail:
        console.print(f"  [cb.dim]{detail}[/]")


def warning(message: str, tip: str | None = None) -> None:
    """Display a warning message."""
    console.print(f"[cb.warning][!] Warning:[/] {message}")
    if tip:
        console.print(f"  [cb.dim]Tip: {tip}[/]")


def error(message: str, tip: str | None = None) -> None:
    """Display an error message."""
    console.print(f"[cb.error][-] Error:[/] {message}")
    if tip:
        console.print(f"  [cb.dim]Tip: {tip}[/]")


def info(message: str) -> None:
    """Display an info message."""
    console.print(f"[cb.info][i] {message}[/]")


def step_done(step: str, detail: str | None = None) -> None:
    """Display a completed step with checkmark."""
    console.print(f"  [cb.success][+][/] {step}")
    if detail:
        console.print(f"    [cb.dim]{detail}[/]")


def step_fail(step: str, detail: str | None = None) -> None:
    """Display a failed step."""
    console.print(f"  [cb.error][-][/] {step}")
    if detail:
        console.print(f"    [cb.dim]{detail}[/]")


# ── Forensics Table ──────────────────────────────────────────────────────────

def forensics_table(
    rows: list[dict],
    title: str = "[x] Forensics Report",
) -> None:
    """Display a forensics diagnostic table.

    Each row dict: {key, classification, value_preview, table}
    """
    table = Table(
        title=title,
        title_style="cb.header",
        border_style="red",
        show_lines=True,
        pad_edge=True,
    )
    table.add_column("#", style="cb.dim", width=4, justify="right")
    table.add_column("Key / Table", style="cb.accent", min_width=25, max_width=50)
    table.add_column("Classification", min_width=10)
    table.add_column("Preview", style="cb.value", min_width=30, max_width=60)

    for i, row in enumerate(rows, 1):
        classification = row.get("classification", "UNKNOWN")
        cls_style = {
            "CHAT": "bold green",
            "CONTEXT": "bold cyan",
            "CONFIG": "bold yellow",
            "CACHE": "bold blue",
            "UNKNOWN": "bold red",
        }.get(classification, "white")
        cls_text = Text(classification, style=cls_style)

        preview = row.get("value_preview", "--")
        if len(preview) > 60:
            preview = preview[:57] + "..."

        table.add_row(
            str(i),
            row.get("key", "--"),
            cls_text,
            preview,
        )

    console.print(table)


# ── Schema Table ─────────────────────────────────────────────────────────────

def schema_table(table_name: str, columns: list[dict]) -> None:
    """Display SQLite table schema.

    Each column dict: {name, type, notnull, pk}
    """
    table = Table(
        title=f"{_ICON_SCHEMA} Schema: {table_name}",
        title_style="cb.header",
        border_style="cyan",
        show_lines=False,
    )
    table.add_column("Column", style="cb.key", min_width=20)
    table.add_column("Type", style="cb.accent", min_width=12)
    table.add_column("Not Null", style="cb.value", min_width=8)
    table.add_column("PK", style="cb.warning", min_width=4)

    for col in columns:
        table.add_row(
            col.get("name", "?"),
            col.get("type", "?"),
            "YES" if col.get("notnull") else "--",
            "[+]" if col.get("pk") else "--",
        )

    console.print(table)
