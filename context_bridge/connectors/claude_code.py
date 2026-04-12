"""Claude Code connector.

Extracts session data from Claude Code / Anthropic Claude storage:
  - %LOCALAPPDATA%\AnthropicClaude\
  - %USERPROFILE%\.claude\settings.json
  - Scans for JSONL files in subdirs
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from context_bridge.connectors.base import IDEConnector
from context_bridge.extractors.jsonl_parser import (
    compute_file_hash,
    parse_jsonl,
    parse_rollout_events,
)
from context_bridge.models import (
    BridgeSession,
    ContextItem,
    ExtractionResult,
    FieldConfidence,
    Message,
    Provenance,
    TouchedFile,
)


class ClaudeCodeConnector(IDEConnector):
    """Connector for Claude Code / Anthropic Claude."""

    @property
    def ide_name(self) -> str:
        return "Claude Code"

    @property
    def storage_paths(self) -> dict[str, Path]:
        home = self.user_profile()
        local_appdata = self.local_appdata()
        return {
            "app_dir": local_appdata / "AnthropicClaude",
            "settings": home / ".claude" / "settings.json",
            "claude_dir": home / ".claude",
            "backups": home / ".claude" / "backups",
        }

    def detect(self) -> dict[str, Any]:
        existing = self.existing_paths()
        if not existing:
            return {
                "found": False,
                "status": "not_found",
                "paths": [],
                "sessions_estimate": None,
                "details": "No Claude Code storage found",
            }

        sessions_count = None
        paths = self.storage_paths

        # Count JSONL files
        jsonl_files = self._find_all_jsonl()
        if jsonl_files:
            sessions_count = len(jsonl_files)

        status = "found" if len(existing) >= 2 else "partial"
        return {
            "found": True,
            "status": status,
            "paths": existing,
            "sessions_estimate": sessions_count,
            "details": f"Found {sessions_count or '?'} session files",
        }

    def list_sessions(
        self,
        limit: int = 20,
        project_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []

        # Get project root hints from settings
        project_roots = self._get_project_roots()

        # Scan for JSONL session files
        for jsonl_path in self._find_all_jsonl():
            try:
                parsed = parse_rollout_events(jsonl_path)
                meta = parsed.get("meta", {})
                cwd = meta.get("cwd") or ""

                if project_filter and project_filter.lower() not in cwd.lower():
                    continue

                sid = meta.get("session_id") or jsonl_path.stem
                sessions.append({
                    "id": sid,
                    "title": jsonl_path.stem,
                    "date": _file_mtime(jsonl_path),
                    "messages": str(len(parsed.get("messages", []))),
                    "project": cwd or "—",
                    "_path": str(jsonl_path),
                })
            except Exception:
                # Fallback: just list the file
                sessions.append({
                    "id": jsonl_path.stem,
                    "title": jsonl_path.name,
                    "date": _file_mtime(jsonl_path),
                    "messages": "?",
                    "project": "—",
                    "_path": str(jsonl_path),
                })

        # Also scan for JSON conversation files
        for json_path in self._find_json_conversations():
            try:
                with open(json_path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                if isinstance(data, dict) and ("messages" in data or "conversation" in data):
                    msgs = data.get("messages") or data.get("conversation") or []
                    sessions.append({
                        "id": json_path.stem,
                        "title": data.get("title") or json_path.stem,
                        "date": _file_mtime(json_path),
                        "messages": str(len(msgs)),
                        "project": data.get("project_root") or data.get("cwd") or "—",
                        "_path": str(json_path),
                    })
            except (json.JSONDecodeError, OSError):
                continue

        sessions.sort(key=lambda s: s.get("date") or "", reverse=True)
        return sessions[:limit]

    def extract_session(self, session_id: str) -> ExtractionResult:
        """Extract a session from Claude Code storage."""
        confidence: list[FieldConfidence] = []
        warnings: list[str] = []
        errors: list[str] = []

        messages: list[Message] = []
        title: str | None = None
        model: str | None = None
        project_root: str | None = None
        created_at: datetime | None = None
        source_path: str | None = None
        source_hash: str | None = None
        context_items: list[ContextItem] = []
        touched: list[TouchedFile] = []

        # Find the session file
        session_file = self._find_session_file(session_id)
        if not session_file:
            errors.append(f"Could not find session file for: {session_id}")
            return ExtractionResult(
                session=BridgeSession(
                    source_ide="claude_code",
                    session_id=session_id,
                    messages=[], context_items=[], touched_files=[],
                ),
                confidence=confidence, warnings=warnings, errors=errors,
            )

        source_path = str(session_file)
        source_hash = compute_file_hash(session_file)

        # Parse based on file type
        if session_file.suffix == ".jsonl":
            try:
                parsed = parse_rollout_events(session_file)
                meta = parsed.get("meta", {})

                for i, msg_data in enumerate(parsed.get("messages", [])):
                    messages.append(Message(
                        id=msg_data.get("id") or f"claude-{i}",
                        role=_normalize_role(msg_data.get("role", "user")),
                        content=msg_data.get("content", ""),
                        timestamp=_parse_ts(msg_data.get("timestamp")),
                        model=msg_data.get("model"),
                    ))

                for fp in parsed.get("file_paths", set()):
                    touched.append(TouchedFile(
                        path=fp, status="referenced", conversation_id=session_id
                    ))

                model = meta.get("model")
                project_root = meta.get("cwd")
                title = session_file.stem

                confidence.append(FieldConfidence(
                    field_name="messages", level="HIGH", source="JSONL event stream"
                ))
            except Exception as exc:
                errors.append(f"JSONL parse error: {exc}")
                confidence.append(FieldConfidence(
                    field_name="messages", level="LOW", source="JSONL (failed)"
                ))

        elif session_file.suffix == ".json":
            try:
                with open(session_file, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)

                raw_msgs = data.get("messages") or data.get("conversation") or []
                for i, msg in enumerate(raw_msgs):
                    if isinstance(msg, dict):
                        content = msg.get("content") or msg.get("text") or ""
                        if isinstance(content, list):
                            parts = [p.get("text", str(p)) if isinstance(p, dict) else str(p) for p in content]
                            content = "\n".join(parts)
                        if content:
                            messages.append(Message(
                                id=msg.get("id") or f"claude-{i}",
                                role=_normalize_role(msg.get("role", "user")),
                                content=str(content),
                                timestamp=_parse_ts(msg.get("timestamp")),
                                model=msg.get("model"),
                            ))

                title = data.get("title") or session_file.stem
                model = data.get("model")
                project_root = data.get("project_root") or data.get("cwd")

                confidence.append(FieldConfidence(
                    field_name="messages", level="HIGH", source="JSON conversation file"
                ))
            except (json.JSONDecodeError, OSError) as exc:
                errors.append(f"JSON parse error: {exc}")

        # Timestamps from messages
        if messages:
            timestamps = [m.timestamp for m in messages if m.timestamp]
            if timestamps:
                created_at = min(timestamps)

        confidence.append(FieldConfidence(
            field_name="model", level="HIGH" if model else "LOW", source="session metadata"
        ))
        confidence.append(FieldConfidence(
            field_name="project_root", level="HIGH" if project_root else "LOW", source="session metadata"
        ))

        session = BridgeSession(
            source_ide="claude_code",
            session_id=session_id,
            title=title,
            created_at=created_at,
            project_root=project_root,
            mode="agent",
            model=model,
            provider="anthropic",
            messages=messages,
            context_items=context_items,
            touched_files=touched,
            raw_source_path=source_path,
            extraction_hash=source_hash,
            provenance=Provenance(
                source_path=source_path,
                source_hash=source_hash,
            ),
        )

        return ExtractionResult(
            session=session,
            confidence=confidence, warnings=warnings, errors=errors,
        )

    # ── Private helpers ──────────────────────────────────────────────────

    def _get_project_roots(self) -> list[str]:
        """Extract project root hints from .claude/settings.json."""
        settings_path = self.storage_paths["settings"]
        if not settings_path.exists():
            return []
        try:
            with open(settings_path, encoding="utf-8") as f:
                data = json.load(f)
            roots = data.get("projectRoots") or data.get("projects") or []
            if isinstance(roots, list):
                return [str(r) for r in roots]
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _find_all_jsonl(self) -> list[Path]:
        """Find all JSONL files in Claude storage dirs."""
        files: list[Path] = []
        for key in ("app_dir", "claude_dir", "backups"):
            path = self.storage_paths.get(key)
            if path and path.exists():
                files.extend(path.rglob("*.jsonl"))
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def _find_json_conversations(self) -> list[Path]:
        """Find JSON files that look like conversations."""
        files: list[Path] = []
        for key in ("app_dir", "claude_dir"):
            path = self.storage_paths.get(key)
            if path and path.exists():
                for f in path.rglob("*.json"):
                    if f.name != "settings.json" and f.stat().st_size > 100:
                        files.append(f)
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def _find_session_file(self, session_id: str) -> Path | None:
        """Find the file matching a session ID."""
        # Check JSONL files
        for f in self._find_all_jsonl():
            if session_id in f.stem:
                return f
            # Check inside the file
            try:
                records = parse_jsonl(f)
                for rec in records[:5]:
                    if rec.get("id") == session_id or rec.get("session_id") == session_id:
                        return f
            except Exception:
                continue

        # Check JSON files
        for f in self._find_json_conversations():
            if session_id in f.stem:
                return f

        return None

    def forensics_dump(self) -> list[dict[str, Any]]:
        """Diagnostic dump of Claude Code storage."""
        results: list[dict[str, Any]] = []
        paths = self.storage_paths

        for name, path in paths.items():
            if path.exists():
                if path.is_file():
                    results.append({
                        "key": f"[FILE] {path.name}",
                        "classification": "CONFIG" if "settings" in path.name else "CONTEXT",
                        "value_preview": f"{path.stat().st_size} bytes",
                    })
                elif path.is_dir():
                    file_count = sum(1 for _ in path.rglob("*") if _.is_file())
                    results.append({
                        "key": f"[DIR] {name}: {path}",
                        "classification": "CONTEXT",
                        "value_preview": f"{file_count} files",
                    })

        return results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_role(role: str) -> str:
    role = role.lower().strip()
    if role in ("user", "human"):
        return "user"
    if role in ("assistant", "ai", "bot", "model"):
        return "assistant"
    if role in ("system",):
        return "system"
    if role in ("tool", "function", "tool_result"):
        return "tool"
    return "assistant"


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            if value > 1e12:
                value = value / 1000
            return datetime.fromtimestamp(value)
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _file_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception:
        return ""
