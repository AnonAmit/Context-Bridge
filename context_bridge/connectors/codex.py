"""OpenAI Codex CLI connector.

Extracts sessions from the Codex CLI storage:
  - session_index.jsonl (session list)
  - sessions/YYYY/MM/DD/rollout-*.jsonl (event streams)
  - state_5.sqlite (fallback index)
  - memories/ (persistent context)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from context_bridge.connectors.base import IDEConnector
from context_bridge.extractors.jsonl_parser import (
    find_rollout_files,
    parse_jsonl,
    parse_rollout_events,
    parse_session_index,
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


class CodexConnector(IDEConnector):
    """Connector for OpenAI Codex CLI."""

    @property
    def ide_name(self) -> str:
        return "Codex CLI"

    @property
    def storage_paths(self) -> dict[str, Path]:
        home = self.user_profile()
        return {
            "index": home / ".codex" / "session_index.jsonl",
            "sessions": home / ".codex" / "sessions",
            "sqlite": home / ".codex" / "state_5.sqlite",
            "logs_db": home / ".codex" / "logs_2.sqlite",
            "memories": home / ".codex" / "memories",
            "global_state": home / ".codex" / ".codex-global-state.json",
        }

    def detect(self) -> dict[str, Any]:
        paths = self.storage_paths
        existing = [str(p) for p in paths.values() if p.exists()]

        if not existing:
            return {
                "found": False,
                "status": "not_found",
                "paths": [],
                "sessions_estimate": None,
                "details": "No Codex CLI storage found",
            }

        # Count sessions from index if available
        sessions_count = None
        if paths["index"].exists():
            try:
                sessions = parse_session_index(paths["index"])
                sessions_count = len(sessions)
            except Exception:
                pass

        if sessions_count is None and paths["sessions"].exists():
            rollouts = find_rollout_files(paths["sessions"])
            sessions_count = len(rollouts)

        status = "found" if paths["index"].exists() or paths["sessions"].exists() else "partial"

        return {
            "found": True,
            "status": status,
            "paths": existing,
            "sessions_estimate": sessions_count,
            "details": f"Found {sessions_count or '?'} sessions",
        }

    def list_sessions(
        self,
        limit: int = 20,
        project_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        paths = self.storage_paths
        sessions: list[dict[str, Any]] = []

        # Primary: session_index.jsonl
        if paths["index"].exists():
            try:
                index_entries = parse_session_index(paths["index"])
                for entry in index_entries:
                    cwd = entry.get("cwd", "")
                    if project_filter and project_filter.lower() not in (cwd or "").lower():
                        continue
                    sessions.append(
                        {
                            "id": entry["id"],
                            "title": entry.get("thread_name") or "Untitled",
                            "date": entry.get("updated_at") or entry.get("created_at"),
                            "messages": "?",
                            "project": cwd or "—",
                        }
                    )
            except Exception:
                pass

        # Fallback: scan rollout files directly
        if not sessions and paths["sessions"].exists():
            rollouts = find_rollout_files(paths["sessions"])
            for rollout_path in rollouts:
                try:
                    parsed = parse_rollout_events(rollout_path)
                    meta = parsed["meta"]
                    cwd = meta.get("cwd", "")
                    if project_filter and project_filter.lower() not in (cwd or "").lower():
                        continue
                    sid = meta.get("session_id") or rollout_path.stem
                    sessions.append(
                        {
                            "id": sid,
                            "title": rollout_path.stem,
                            "date": _file_mtime(rollout_path),
                            "messages": str(len(parsed["messages"])),
                            "project": cwd or "—",
                            "_rollout_path": str(rollout_path),
                        }
                    )
                except Exception:
                    continue

        return sessions[:limit]

    def extract_session(self, session_id: str) -> ExtractionResult:
        """Extract a full session from Codex storage."""
        paths = self.storage_paths
        confidence: list[FieldConfidence] = []
        warnings: list[str] = []
        errors: list[str] = []

        # Find the rollout file for this session
        rollout_path = self._find_rollout(session_id)
        if not rollout_path:
            errors.append(f"Could not find rollout file for session: {session_id}")
            return ExtractionResult(
                session=BridgeSession(
                    source_ide="codex",
                    session_id=session_id,
                    messages=[],
                    context_items=[],
                    touched_files=[],
                ),
                confidence=confidence,
                warnings=warnings,
                errors=errors,
            )

        # Parse the rollout events
        parsed = parse_rollout_events(rollout_path)
        meta = parsed["meta"]

        # Build messages
        messages: list[Message] = []
        for i, msg_data in enumerate(parsed["messages"]):
            messages.append(
                Message(
                    id=msg_data.get("id") or f"msg-{i}",
                    role=_normalize_role(msg_data.get("role", "user")),
                    content=msg_data.get("content", ""),
                    timestamp=_parse_ts(msg_data.get("timestamp")),
                    model=msg_data.get("model"),
                )
            )

        # Build touched files
        touched: list[TouchedFile] = []
        for fp in parsed["file_paths"]:
            touched.append(
                TouchedFile(
                    path=fp,
                    status="referenced",
                    conversation_id=session_id,
                )
            )

        # Look up title from session index
        title = self._find_title(session_id)

        # Build confidence
        confidence.append(
            FieldConfidence(
                field_name="messages",
                level="HIGH",
                source="rollout JSONL",
            )
        )
        confidence.append(
            FieldConfidence(
                field_name="project_root",
                level="HIGH" if meta.get("cwd") else "LOW",
                source="session_meta.cwd",
            )
        )
        confidence.append(
            FieldConfidence(
                field_name="model",
                level="HIGH" if meta.get("model") else "MEDIUM",
                source="session_meta / turn_context",
            )
        )
        confidence.append(
            FieldConfidence(
                field_name="title",
                level="HIGH" if title else "LOW",
                source="session_index.jsonl",
            )
        )
        confidence.append(
            FieldConfidence(
                field_name="touched_files",
                level="MEDIUM",
                source="heuristic path extraction",
                note="Derived from tool events",
            )
        )

        # Timestamps
        created_at = None
        updated_at = None
        if messages:
            timestamps = [m.timestamp for m in messages if m.timestamp]
            if timestamps:
                created_at = min(timestamps)
                updated_at = max(timestamps)

        session = BridgeSession(
            source_ide="codex",
            session_id=session_id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            project_root=meta.get("cwd"),
            mode="agent",
            model=meta.get("model"),
            provider=meta.get("model_provider"),
            base_instructions=meta.get("base_instructions"),
            messages=messages,
            context_items=[],
            touched_files=touched,
            raw_source_path=str(rollout_path),
            extraction_hash=parsed["file_hash"],
            provenance=Provenance(
                source_path=str(rollout_path),
                source_hash=parsed["file_hash"],
            ),
        )

        return ExtractionResult(
            session=session,
            confidence=confidence,
            warnings=warnings,
            errors=errors,
        )

    # ── Private helpers ──────────────────────────────────────────────────

    def _find_rollout(self, session_id: str) -> Path | None:
        """Find the rollout file matching a session ID."""
        paths = self.storage_paths

        # Search in sessions directory
        if paths["sessions"].exists():
            for rollout in find_rollout_files(paths["sessions"]):
                # Check if the session_id is in the filename
                if session_id in rollout.stem:
                    return rollout
                # Parse first few lines to check the id field
                try:
                    records = parse_jsonl(rollout)
                    for rec in records[:5]:
                        meta = rec.get("session_meta", rec)
                        rid = meta.get("id") or meta.get("session_id") or ""
                        if rid == session_id:
                            return rollout
                except Exception:
                    continue

        return None

    def _find_title(self, session_id: str) -> str | None:
        """Look up a session title from the session index."""
        paths = self.storage_paths
        if not paths["index"].exists():
            return None
        try:
            entries = parse_session_index(paths["index"])
            for entry in entries:
                if entry["id"] == session_id:
                    return entry.get("thread_name")
        except Exception:
            pass
        return None


def _normalize_role(role: str) -> str:
    """Normalize a role string to one of the standard roles."""
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
    """Try to parse a timestamp from various formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _file_mtime(path: Path) -> str:
    """Get file modification time as ISO string."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception:
        return ""
