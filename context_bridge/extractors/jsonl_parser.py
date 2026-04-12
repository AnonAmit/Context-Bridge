"""JSONL parser for Codex rollout files and session indices.

Handles line-by-line parsing of JSONL event streams, including
session_meta, event_msg, and turn_context records used by
OpenAI Codex CLI.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class JSONLParseError(Exception):
    """Raised when a JSONL file cannot be parsed."""


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return f"sha256:{sha.hexdigest()}"


def parse_jsonl(file_path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of dicts.

    Skips blank lines and lines that fail JSON parsing (with warnings).
    """
    if not file_path.exists():
        raise JSONLParseError(f"File not found: {file_path}")

    records: list[dict[str, Any]] = []
    errors: list[str] = []

    with open(file_path, encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    obj["_line_num"] = line_num
                    records.append(obj)
                else:
                    errors.append(f"Line {line_num}: not a JSON object")
            except json.JSONDecodeError as exc:
                errors.append(f"Line {line_num}: {exc}")

    return records


def parse_session_index(file_path: Path) -> list[dict[str, Any]]:
    """Parse the Codex session_index.jsonl file.

    Each line has: {id, thread_name, updated_at, ...}
    Returns sorted by updated_at descending.
    """
    records = parse_jsonl(file_path)

    sessions = []
    for rec in records:
        session_id = rec.get("id") or rec.get("session_id")
        if not session_id:
            continue
        sessions.append(
            {
                "id": session_id,
                "thread_name": rec.get("thread_name") or rec.get("name"),
                "updated_at": rec.get("updated_at"),
                "created_at": rec.get("created_at"),
                "model": rec.get("model"),
                "cwd": rec.get("cwd"),
                "_raw": rec,
            }
        )

    # Sort by updated_at descending (most recent first)
    sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return sessions


def parse_rollout_events(file_path: Path) -> dict[str, Any]:
    """Parse a Codex rollout-*.jsonl event stream.

    Returns a structured dict with:
      - meta: session metadata from session_meta records
      - messages: list of message dicts from event_msg records
      - context: list of context items from turn_context records
      - file_paths: set of file paths mentioned in tool events
      - raw_events: all parsed records
    """
    records = parse_jsonl(file_path)

    meta: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    context: list[dict[str, Any]] = []
    file_paths: set[str] = set()

    for rec in records:
        event_type = rec.get("type") or rec.get("event_type") or ""

        if event_type == "session_meta" or "session_meta" in rec:
            # Extract session metadata
            session_data = rec.get("session_meta", rec)
            meta.update(
                {
                    "cwd": session_data.get("cwd") or session_data.get("project_root"),
                    "model_provider": session_data.get("model_provider"),
                    "model": session_data.get("model"),
                    "base_instructions": session_data.get("base_instructions")
                    or session_data.get("instructions"),
                    "session_id": session_data.get("id") or session_data.get("session_id"),
                }
            )

        elif event_type in ("event_msg", "response_item") or "message" in rec:
            # Extract message content
            payload = rec.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            msg_data = payload.get("message", payload) if payload else rec.get("message", rec)
            if not isinstance(msg_data, dict):
                msg_data = {}
            role = msg_data.get("role", "")
            content = msg_data.get("content", "")

            # Handle content that is a list of parts (OpenAI format)
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        text_parts.append(part.get("text", str(part)))
                    else:
                        text_parts.append(str(part))
                content = "\n".join(text_parts)
            elif isinstance(content, dict):
                content = content.get("text") or str(content)

            if role and content:
                messages.append(
                    {
                        "role": role,
                        "content": str(content),
                        "timestamp": rec.get("timestamp") or rec.get("created_at"),
                        "model": rec.get("model") or msg_data.get("model") or payload.get("model"),
                        "id": msg_data.get("id") or rec.get("id", ""),
                    }
                )

        elif event_type == "turn_context" or "context" in rec:
            # Extract context / model info
            ctx_data = rec.get("context", rec)
            if isinstance(ctx_data, dict):
                if ctx_data.get("model"):
                    meta["model"] = ctx_data["model"]
                context.append(ctx_data)

        # Extract file paths from tool calls / results
        _extract_file_paths(rec, file_paths)

    return {
        "meta": meta,
        "messages": messages,
        "context": context,
        "file_paths": file_paths,
        "raw_events": records,
        "file_hash": compute_file_hash(file_path),
    }


def _extract_file_paths(record: dict, paths: set[str]) -> None:
    """Recursively extract file paths from event records."""
    for key, value in record.items():
        if isinstance(value, str):
            # Look for file-like patterns in values
            if key in ("path", "file", "filename", "file_path", "target"):
                paths.add(value)
            elif "/" in value or "\\" in value:
                # Heuristic: if it looks like a path
                parts = value.replace("\\", "/").split("/")
                if len(parts) >= 2 and any(
                    p.endswith(ext)
                    for p in parts
                    for ext in (".py", ".js", ".ts", ".json", ".md", ".yaml", ".toml", ".css",
                                ".html", ".jsx", ".tsx", ".go", ".rs", ".java", ".cpp", ".h")
                ):
                    paths.add(value)
        elif isinstance(value, dict):
            _extract_file_paths(value, paths)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _extract_file_paths(item, paths)


def find_rollout_files(sessions_dir: Path) -> list[Path]:
    """Recursively find all rollout-*.jsonl files in the sessions directory."""
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
