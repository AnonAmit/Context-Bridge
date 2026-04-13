"""Context Bridge CLI — main entry point.

All commands use Typer for argument parsing and Rich for output.
Every command shows a header panel, uses spinners for work,
and displays results in Rich tables.

Commands:
  cb detect     — detect installed IDEs
  cb scan       — list sessions for an IDE
  cb inspect    — show session detail
  cb export     — export session to bridge-session.json
  cb import     — import bridge session into target IDE
  cb bridge     — one-shot export→import pipeline
  cb forensics  — deep diagnostic dump
  cb tui        — launch interactive TUI
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.prompt import Prompt

from context_bridge import __version__
from context_bridge import ui
from context_bridge.emitter import export_session, generate_import_artifacts
from context_bridge.normalizer import (
    detect_all_ides,
    get_all_connectors,
    get_connector,
    list_sessions,
    normalize_session,
)
from context_bridge.extractors.cdp_extractor import extract_live_session

app = typer.Typer(
    name="cb",
    help="Context Bridge -- Transfer AI session context between coding IDEs",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# ── 0. live-extract ──────────────────────────────────────────────────────────

@app.command("live-extract")
def live_extract(port: int = typer.Option(9222, help="Chromium DevTools remote debugging port")):
    """Force extract a loaded session out of an IDE's active memory via CDP WebSockets.
    
    This bypasses local SQLite/Protobuf encryptions entirely by hijacking the Chrome Engine
    inspector. Before running, ensure you close your IDE completely and run it via the
    command line with `--remote-debugging-port=9222`.
    """
    ui.print_header("live-extract")
    
    ui.console.print("\n[cb.warning]⚠️ EXPERIMENTAL CDP MEMORY HOOK ⚠️[/]")
    ui.console.print("This command taps directly into the active Chromium RAM of the IDE.")
    ui.console.print(f"You MUST have launched the IDE with `--remote-debugging-port={port}`.")
    
    confirm = Prompt.ask("\nHave you properly launched the IDE with this flag?", choices=["y", "n"], default="n")
    if confirm.lower() != "y":
        ui.console.print("\n[cb.info]Aborting. Please restart the IDE with the port flag and try again.[/]")
        raise typer.Exit()
        
    with ui.spinner("Searching for local Chromium DevTools targets..."):
        session = extract_live_session()
        
    if not session:
        ui.console.print("\n[cb.error]Extraction Failed![/]")
        ui.console.print(f"Could not connect to localhost:{port}. Is the IDE running locally?")
        raise typer.Exit(1)
        
    ui.console.print("\n[cb.success]DOM Successfully Extracted and Parsed![/]")
    
    with ui.spinner("Formatting and saving universal session marker..."):
        out_dir = export_session(session)
        
    ui.console.print(f"\n[cb.accent]  [+] Exported bridge-session.json[/]")
    ui.console.print(f"[cb.key]  Path:[/] {out_dir}\n")
    ui.console.print("[cb.success]Mission Accomplished! You can now run `cb import` on that folder.[/]")



# ── 1. detect ────────────────────────────────────────────────────────────────

@app.command()
def detect():
    """Scan the local machine for installed AI coding IDEs."""
    ui.header_panel("detect")

    with ui.spinner("Scanning for installed IDEs..."):
        results = detect_all_ides()

    if not results:
        ui.warning("No IDEs detected on this machine.")
        raise typer.Exit(1)

    ui.detection_table(results)

    found = sum(1 for r in results if r.get("status") in ("found", "partial"))
    ui.success(
        f"Detected {found} IDE(s)",
        detail="Run `cb scan --ide <name>` to list sessions.",
    )


# ── 2. scan ──────────────────────────────────────────────────────────────────

@app.command()
def scan(
    ide: str = typer.Option(..., "--ide", "-i", help="IDE to scan (codex, cursor, antigravity, claude)"),
    project_filter: Optional[str] = typer.Option(None, "--project-filter", "-p", help="Filter by project path substring"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum sessions to show"),
):
    """List all sessions found for a specific IDE."""
    ui.header_panel("scan")

    try:
        with ui.spinner(f"Scanning {ide} sessions..."):
            sessions = list_sessions(ide, limit=limit, project_filter=project_filter)
    except ValueError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from exc
    except Exception as exc:
        ui.error(f"Scan failed: {exc}", tip="Check if the IDE storage paths exist.")
        raise typer.Exit(1) from exc

    if not sessions:
        ui.warning(f"No sessions found for {ide}.")
        if project_filter:
            ui.info(f"Project filter was: {project_filter}")
        raise typer.Exit(0)

    ui.session_table(sessions, ide_name=ide)
    ui.success(f"Found {len(sessions)} session(s)", detail=f"IDE: {ide}")


# ── 3. inspect ───────────────────────────────────────────────────────────────

@app.command()
def inspect(
    ide: str = typer.Option(..., "--ide", "-i", help="Source IDE"),
    session: str = typer.Option(..., "--session", "-s", help="Session ID to inspect"),
):
    """Show full detail of one session."""
    ui.header_panel("inspect")

    try:
        with ui.spinner(f"Extracting session {session[:8]}... from {ide}"):
            result = normalize_session(ide, session, redact=True)
    except ValueError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from exc
    except Exception as exc:
        ui.error(f"Extraction failed: {exc}")
        raise typer.Exit(1) from exc

    ui.session_detail(result)

    if result.errors:
        ui.error(f"{len(result.errors)} error(s) during extraction")
    elif result.warnings:
        ui.warning(f"{len(result.warnings)} warning(s) during extraction")
    else:
        ui.success(
            f"Session inspected: {result.session.message_count} messages",
            detail=f"Overall confidence: {result.overall_confidence()}",
        )


# ── 4. export ────────────────────────────────────────────────────────────────

@app.command(name="export")
def export_cmd(
    ide: str = typer.Option(..., "--ide", "-i", help="Source IDE"),
    session: str = typer.Option(..., "--session", "-s", help="Session ID to export"),
    out: Path = typer.Option(
        Path("./context-bridge-exports"),
        "--out", "-o",
        help="Output directory",
    ),
    include_raw: bool = typer.Option(False, "--include-raw", help="Include unredacted raw copies"),
):
    """Export a session to bridge-session.json."""
    ui.header_panel("export")

    try:
        # Step 1: Extract
        progress = ui.create_progress()
        with progress:
            task = progress.add_task("Extracting session...", total=4)

            result = normalize_session(ide, session, redact=not include_raw)
            progress.update(task, advance=1, description="Session extracted")

            # Step 2: Export
            progress.update(task, description="Writing bridge-session.json...")
            export_dir = out / f"{ide}_{session[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            created = export_session(result.session, export_dir, include_raw=include_raw)
            progress.update(task, advance=1)

            # Step 3: Show results
            progress.update(task, description="Finalizing...")
            progress.update(task, advance=1)

            progress.update(task, description="Done!", advance=1)

    except ValueError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from exc
    except Exception as exc:
        ui.error(f"Export failed: {exc}")
        raise typer.Exit(1) from exc

    # Report
    ui.console.print()
    for name, path in created.items():
        size = path.stat().st_size if path.exists() else 0
        ui.success(f"Exported: {name}", detail=f"{path} ({size:,} bytes)")

    session_obj = result.session
    ui.console.print()
    ui.success(
        "Export complete!",
        detail=f'Session: "{session_obj.title or "Untitled"}" | '
               f'{session_obj.message_count} messages | '
               f'{session_obj.file_count} files',
    )


# ── 5. import ────────────────────────────────────────────────────────────────

@app.command(name="import")
def import_cmd(
    target: str = typer.Option(
        "universal",
        "--target", "-t",
        help="Target IDE (codex, cursor, antigravity, universal)",
    ),
    bridge: Path = typer.Option(..., "--bridge", "-b", help="Path to bridge-session.json"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output directory override"),
):
    """Import a bridge-session.json into target IDE format."""
    ui.header_panel("import")

    if not bridge.exists():
        ui.error(f"Bridge file not found: {bridge}")
        raise typer.Exit(1)

    # Load bridge session
    try:
        with ui.spinner("Loading bridge session..."):
            from context_bridge.models import BridgeSession
            data = json.loads(bridge.read_text(encoding="utf-8"))
            # Remove provenance (not part of the model)
            data.pop("_provenance", None)
            session = BridgeSession.model_validate(data)
    except Exception as exc:
        ui.error(f"Failed to load bridge file: {exc}")
        raise typer.Exit(1) from exc

    ui.info(f'Loaded session: "{session.title or "Untitled"}" ({session.message_count} messages)')

    # Generate import artifacts
    output_dir = out or Path("./context-bridge-imports")
    try:
        with ui.spinner(f"Generating {target} import artifacts..."):
            created = generate_import_artifacts(
                session,
                target=target,
                output_dir=output_dir / f"{target}_{session.session_id[:8]}",
                project_root=Path(session.project_root) if session.project_root else None,
            )
    except ValueError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from exc
    except Exception as exc:
        ui.error(f"Import generation failed: {exc}")
        raise typer.Exit(1) from exc

    # Report
    ui.console.print()
    for name, path in created.items():
        ui.step_done(name, str(path))

    ui.console.print()
    ui.success(f"Import artifacts generated for: {target}")

    # Print target-specific instructions
    if target in ("codex", "universal"):
        ui.console.print()
        ui.info("To use in Codex CLI:")
        ui.console.print(f"  [cb.accent]codex resume {session.session_id}[/]")

    if target in ("cursor", "universal"):
        ui.console.print()
        ui.info("To use in Cursor:")
        ui.console.print("  1. Open the BOOTSTRAP.md file")
        ui.console.print("  2. Copy its contents")
        ui.console.print("  3. Paste into a new Cursor chat")

    if target in ("antigravity", "universal"):
        ui.console.print()
        ui.info("To use in Antigravity:")
        ui.console.print("  1. Open the BOOTSTRAP.md file")
        ui.console.print("  2. Paste into a new Antigravity conversation")


# ── 6. bridge ────────────────────────────────────────────────────────────────

@app.command()
def bridge(
    from_ide: str = typer.Option(..., "--from", "-f", help="Source IDE"),
    to_ide: str = typer.Option(..., "--to", "-t", help="Target IDE"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Session ID (interactive picker if omitted)"),
    out: Path = typer.Option(
        Path("./context-bridge-exports"),
        "--out", "-o",
        help="Output directory",
    ),
):
    """One-shot pipeline: detect -> export -> normalize -> import."""
    ui.header_panel("bridge")

    # Step 1: Validate
    ui.step_done("Validating IDEs")
    try:
        src_connector = get_connector(from_ide)
        _ = get_connector(to_ide) if to_ide not in ("universal",) else None
    except ValueError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from exc

    # Step 2: Interactive session selection if needed
    if not session:
        ui.info(f"Scanning {from_ide} for sessions...")
        with ui.spinner("Listing sessions..."):
            sessions = src_connector.list_sessions(limit=20)

        if not sessions:
            ui.error(f"No sessions found in {from_ide}")
            raise typer.Exit(1)

        ui.session_table(sessions, ide_name=from_ide)
        ui.console.print()

        choice = Prompt.ask(
            "[cb.accent]Select session number[/]",
            choices=[str(i) for i in range(1, len(sessions) + 1)],
            console=ui.console,
        )
        session = sessions[int(choice) - 1]["id"]

    ui.step_done("Session selected", session[:16])

    # Step 3: Extract
    try:
        with ui.spinner(f"Extracting from {from_ide}..."):
            result = normalize_session(from_ide, session, redact=True)
        ui.step_done(
            "Extraction complete",
            f'{result.session.message_count} messages, confidence: {result.overall_confidence()}',
        )
    except Exception as exc:
        ui.step_fail("Extraction failed", str(exc))
        raise typer.Exit(1) from exc

    # Step 4: Export
    try:
        export_dir = out / f"bridge_{from_ide}_to_{to_ide}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        with ui.spinner("Exporting bridge session..."):
            exported = export_session(result.session, export_dir)
        ui.step_done("Exported bridge-session.json")
    except Exception as exc:
        ui.step_fail("Export failed", str(exc))
        raise typer.Exit(1) from exc

    # Step 5: Generate import artifacts
    try:
        with ui.spinner(f"Generating {to_ide} artifacts..."):
            imported = generate_import_artifacts(
                result.session,
                target=to_ide,
                output_dir=export_dir,
            )
        ui.step_done(f"Generated {to_ide} import artifacts")
    except Exception as exc:
        ui.step_fail("Import generation failed", str(exc))
        raise typer.Exit(1) from exc

    # Summary
    ui.console.print()
    ui.success(
        f"Bridge complete: {from_ide} -> {to_ide}",
        detail=f'Session: "{result.session.title or "Untitled"}" | '
               f"Output: {export_dir}",
    )


# ── 7. forensics ─────────────────────────────────────────────────────────────

@app.command()
def forensics(
    ide: str = typer.Option(..., "--ide", "-i", help="IDE to analyze"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Session ID (optional)"),
    out: Path = typer.Option(
        Path("./context-bridge-exports"),
        "--out", "-o",
        help="Output directory for forensics report",
    ),
):
    """Deep read-only diagnostic dump of IDE storage."""
    ui.header_panel("forensics")

    try:
        connector = get_connector(ide)
    except ValueError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from exc

    with ui.spinner(f"Running deep forensics on {ide}..."):
        try:
            # Use forensics_dump if available
            if hasattr(connector, "forensics_dump"):
                results = connector.forensics_dump()
            else:
                # Fallback: basic detection info
                detection = connector.detect()
                results = [{
                    "key": f"Detection: {ide}",
                    "classification": "CONFIG",
                    "value_preview": json.dumps(detection, default=str),
                }]
        except Exception as exc:
            ui.error(f"Forensics failed: {exc}")
            raise typer.Exit(1) from exc

    if not results:
        ui.warning(f"No forensics data found for {ide}")
        raise typer.Exit(0)

    # Display results
    ui.forensics_table(results, title=f"[x] Forensics -- {ide}")

    # Show schemas for any tables found
    for r in results:
        if "schema" in r and r.get("schema"):
            ui.schema_table(r.get("table", "?"), r["schema"])

    # Save report
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / f"forensics-{ide}-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_data = {
        "ide": ide,
        "timestamp": datetime.now().isoformat(),
        "tool_version": __version__,
        "results": results,
    }
    report_path.write_text(
        json.dumps(report_data, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    ui.console.print()
    ui.success(f"Forensics report saved", detail=str(report_path))


# ── 8. tui ───────────────────────────────────────────────────────────────────

@app.command()
def tui():
    """Launch the interactive TUI (Terminal User Interface)."""
    ui.header_panel("tui")

    try:
        from context_bridge.tui import ContextBridgeTUI
        app_tui = ContextBridgeTUI()
        app_tui.run()
    except ImportError:
        ui.error(
            "TUI requires the 'textual' library.",
            tip="Install with: pip install textual",
        )
        raise typer.Exit(1)
    except Exception as exc:
        ui.error(f"TUI failed: {exc}")
        raise typer.Exit(1) from exc


# ── Version callback ─────────────────────────────────────────────────────────

def version_callback(value: bool):
    if value:
        ui.console.print(f"[cb.header][>>] Context Bridge[/] v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None, "--version", "-V",
        help="Show version and exit",
        callback=version_callback,
        is_eager=True,
    ),
):
    """Context Bridge -- Transfer AI session context between coding IDEs."""
    pass


if __name__ == "__main__":
    app()
