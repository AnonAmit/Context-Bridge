"""Emitter: converts a BridgeSession into target-specific artifacts.

Generates:
  - bridge-session.json (universal format)
  - BOOTSTRAP.md (human-readable project context)
  - CONTEXT.md (full conversation digest)
  - FILES.md (touched files manifest)
  - Target-specific files for Codex, Cursor, Antigravity
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from context_bridge.models import BridgeSession, Provenance


# ── Main export function ─────────────────────────────────────────────────────

def export_session(
    session: BridgeSession,
    output_dir: Path,
    include_raw: bool = False,
) -> dict[str, Path]:
    """Export a BridgeSession to a directory.

    Creates:
      bridge-session.json — the universal session format
      bootstrap.md        — paste-ready context summary
      raw/                 — original source artifact (if include_raw)
      attachments/         — copies of referenced files (if accessible)

    Returns dict of created file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    created: dict[str, Path] = {}

    # 1. bridge-session.json
    session_json_path = output_dir / "bridge-session.json"
    session_data = session.model_dump(mode="json", exclude_none=True)

    # Add provenance block
    session_data["_provenance"] = {
        "extracted_at": datetime.now().isoformat(),
        "source_path": session.raw_source_path,
        "source_hash": session.extraction_hash,
        "tool_version": "1.0",
        "redacted": session.provenance.redacted if session.provenance else True,
    }

    session_json_path.write_text(
        json.dumps(session_data, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    created["bridge-session.json"] = session_json_path

    # 2. BOOTSTRAP.md
    bootstrap_path = output_dir / "bootstrap.md"
    bootstrap_path.write_text(
        generate_bootstrap_md(session),
        encoding="utf-8",
    )
    created["bootstrap.md"] = bootstrap_path

    # 3. Raw source copy
    if include_raw and session.raw_source_path:
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(exist_ok=True)
        src = Path(session.raw_source_path)
        if src.exists():
            dest = raw_dir / src.name
            try:
                shutil.copy2(src, dest)
                created["raw"] = dest
            except (PermissionError, OSError):
                pass  # couldn't copy, that's ok

    # 4. Attachments (copies of referenced files)
    if session.touched_files:
        attachments_dir = output_dir / "attachments"
        attachments_dir.mkdir(exist_ok=True)
        for tf in session.touched_files:
            src = Path(tf.path)
            if src.exists() and src.is_file():
                try:
                    dest = attachments_dir / src.name
                    shutil.copy2(src, dest)
                    created[f"attachment:{src.name}"] = dest
                except (PermissionError, OSError):
                    pass

    return created


# ── Target-specific import generation ────────────────────────────────────────

def generate_import_artifacts(
    session: BridgeSession,
    target: str,
    output_dir: Path,
    project_root: Path | None = None,
) -> dict[str, Path]:
    """Generate target-specific import artifacts.

    Args:
        session: The BridgeSession to import.
        target: Target IDE ("codex", "cursor", "antigravity", "universal").
        output_dir: Where to write output files.
        project_root: Project root override.

    Returns dict of created file paths.
    """
    target = target.lower().strip()

    if target == "codex":
        return _emit_codex(session, output_dir)
    elif target == "cursor":
        return _emit_cursor(session, output_dir, project_root)
    elif target == "antigravity":
        return _emit_antigravity(session, output_dir, project_root)
    elif target == "universal":
        created: dict[str, Path] = {}
        created.update(_emit_codex(session, output_dir / "codex"))
        created.update(_emit_cursor(session, output_dir / "cursor", project_root))
        created.update(_emit_antigravity(session, output_dir / "antigravity", project_root))
        return created
    else:
        raise ValueError(f"Unknown target: {target!r}")


# ── Codex emit ───────────────────────────────────────────────────────────────

def _emit_codex(session: BridgeSession, output_dir: Path) -> dict[str, Path]:
    """Generate a synthetic Codex rollout JSONL."""
    output_dir.mkdir(parents=True, exist_ok=True)
    created: dict[str, Path] = {}

    rollout_path = output_dir / f"rollout-{session.session_id[:8]}.jsonl"
    lines: list[str] = []

    # Session meta record
    meta = {
        "type": "session_meta",
        "session_meta": {
            "id": session.session_id,
            "cwd": session.project_root or "",
            "model": session.model or "",
            "model_provider": session.provider or "",
            "base_instructions": session.base_instructions or "",
            "imported_from": session.source_ide,
            "import_timestamp": datetime.now().isoformat(),
        },
    }
    lines.append(json.dumps(meta, default=str))

    # Message records
    for msg in session.messages:
        record = {
            "type": "event_msg",
            "message": {
                "id": msg.id,
                "role": msg.role,
                "content": msg.content,
                "model": msg.model or session.model or "",
            },
            "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
        }
        lines.append(json.dumps(record, default=str))

    rollout_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    created["codex:rollout"] = rollout_path

    return created


# ── Cursor emit ──────────────────────────────────────────────────────────────

def _emit_cursor(
    session: BridgeSession,
    output_dir: Path,
    project_root: Path | None = None,
) -> dict[str, Path]:
    """Generate Cursor-compatible context-bridge folder."""
    ctx_dir = output_dir / "context-bridge"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    created: dict[str, Path] = {}

    # CONTEXT.md — full conversation digest
    context_path = ctx_dir / "CONTEXT.md"
    context_path.write_text(
        _generate_context_md(session),
        encoding="utf-8",
    )
    created["cursor:CONTEXT.md"] = context_path

    # FILES.md — touched files manifest
    files_path = ctx_dir / "FILES.md"
    files_path.write_text(
        _generate_files_md(session),
        encoding="utf-8",
    )
    created["cursor:FILES.md"] = files_path

    # BOOTSTRAP.md — paste-ready system prompt
    bootstrap_path = ctx_dir / "BOOTSTRAP.md"
    bootstrap_path.write_text(
        generate_bootstrap_md(session, target_ide="Cursor"),
        encoding="utf-8",
    )
    created["cursor:BOOTSTRAP.md"] = bootstrap_path

    return created


# ── Antigravity emit ─────────────────────────────────────────────────────────

def _emit_antigravity(
    session: BridgeSession,
    output_dir: Path,
    project_root: Path | None = None,
) -> dict[str, Path]:
    """Generate Antigravity-compatible context-bridge folder."""
    ctx_dir = output_dir / "context-bridge"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    created: dict[str, Path] = {}

    # CONTEXT.md
    context_path = ctx_dir / "CONTEXT.md"
    context_path.write_text(
        _generate_context_md(session),
        encoding="utf-8",
    )
    created["antigravity:CONTEXT.md"] = context_path

    # FILES.md
    files_path = ctx_dir / "FILES.md"
    files_path.write_text(
        _generate_files_md(session),
        encoding="utf-8",
    )
    created["antigravity:FILES.md"] = files_path

    # BOOTSTRAP.md
    bootstrap_path = ctx_dir / "BOOTSTRAP.md"
    bootstrap_path.write_text(
        generate_bootstrap_md(session, target_ide="Antigravity"),
        encoding="utf-8",
    )
    created["antigravity:BOOTSTRAP.md"] = bootstrap_path

    return created


# ── Markdown generators ──────────────────────────────────────────────────────

def generate_bootstrap_md(
    session: BridgeSession,
    target_ide: str | None = None,
) -> str:
    """Generate the BOOTSTRAP.md paste-ready context summary."""
    target = target_ide or "your new IDE"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    lines.append("---")
    lines.append(f"# Project Context — Imported from {session.source_ide}")
    lines.append(f"Generated by Context Bridge on {now}")
    lines.append("")

    # Project section
    lines.append("## Project")
    lines.append(f"- **Root:** {session.project_root or 'Unknown'}")
    lines.append(f"- **Mode:** {session.mode or 'Unknown'}  |  **Model used:** {session.model or 'Unknown'}")
    if session.provider:
        lines.append(f"- **Provider:** {session.provider}")
    lines.append("")

    # Summary
    if session.summary:
        lines.append("## Summary")
        lines.append(session.summary)
        lines.append("")

    # Base instructions
    if session.base_instructions:
        lines.append("## Base Instructions")
        lines.append("```")
        lines.append(session.base_instructions)
        lines.append("```")
        lines.append("")

    # Context items
    if session.context_items:
        lines.append("## Files in Context")
        for item in session.context_items:
            label = f" ({item.label})" if item.label else ""
            lines.append(f"- `[{item.type}]` {item.value}{label}")
        lines.append("")

    # Touched files
    if session.touched_files:
        lines.append("## Files Touched by AI")
        lines.append("")
        lines.append("| Path | Status |")
        lines.append("|------|--------|")
        for tf in session.touched_files:
            lines.append(f"| `{tf.path}` | {tf.status} |")
        lines.append("")

    # Conversation digest
    if session.messages:
        lines.append("## Conversation Digest")
        lines.append("")
        for msg in session.messages:
            role_label = msg.role.capitalize()
            preview = msg.preview(200)
            lines.append(f"**{role_label}:** {preview}")
            lines.append("")

    # How to resume
    lines.append(f"## How to Resume in {target}")
    lines.append("Paste this file into a new chat and say:")
    lines.append(f'> "I\'m continuing a session from {session.source_ide}. Here is my project context."')
    lines.append("---")

    return "\n".join(lines)


def _generate_context_md(session: BridgeSession) -> str:
    """Generate CONTEXT.md — full conversation in Markdown."""
    lines: list[str] = []
    lines.append(f"# Conversation — {session.title or session.session_id}")
    lines.append(f"Source: {session.source_ide}  |  Session: {session.session_id}")
    lines.append(f"Date: {session.created_at or 'Unknown'}  |  Messages: {session.message_count}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for msg in session.messages:
        role = msg.role.upper()
        ts = f" ({msg.timestamp})" if msg.timestamp else ""
        model = f" [{msg.model}]" if msg.model else ""
        lines.append(f"### {role}{ts}{model}")
        lines.append("")
        lines.append(msg.content)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _generate_files_md(session: BridgeSession) -> str:
    """Generate FILES.md — touched files manifest."""
    lines: list[str] = []
    lines.append(f"# Files Manifest — {session.title or session.session_id}")
    lines.append(f"Source: {session.source_ide}  |  Session: {session.session_id}")
    lines.append("")

    if session.touched_files:
        lines.append("## Touched Files")
        lines.append("")
        lines.append("| # | Path | Status | Hash |")
        lines.append("|---|------|--------|------|")
        for i, tf in enumerate(session.touched_files, 1):
            hash_preview = tf.hash[:16] if tf.hash else "—"
            lines.append(f"| {i} | `{tf.path}` | {tf.status} | `{hash_preview}` |")
        lines.append("")
    else:
        lines.append("_No files tracked for this session._")
        lines.append("")

    if session.context_items:
        lines.append("## Context Items")
        lines.append("")
        lines.append("| Type | Value | Label |")
        lines.append("|------|-------|-------|")
        for item in session.context_items:
            lines.append(f"| {item.type} | `{item.value}` | {item.label or '—'} |")
        lines.append("")

    return "\n".join(lines)
