"""Cursor IDE connector.

Three-layer extraction approach:
  LAYER 1: state.vscdb → cursorDiskKV table (composerData, chat data)
  LAYER 2: agent-transcripts directory (per-project transcripts)
  LAYER 3: ai-code-tracking.db (touched files, conversation summaries)
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from context_bridge.connectors.base import IDEConnector
from context_bridge.extractors.sqlite_reader import SafeSQLiteReader, SQLiteReadError
from context_bridge.models import (
    BridgeSession,
    ContextItem,
    ExtractionResult,
    FieldConfidence,
    Message,
    Provenance,
    TouchedFile,
)


class CursorConnector(IDEConnector):
    """Connector for Cursor IDE."""

    @property
    def ide_name(self) -> str:
        return "Cursor"

    @property
    def storage_paths(self) -> dict[str, Path]:
        appdata = self.appdata()
        home = self.user_profile()
        return {
            "state_db": appdata / "Cursor" / "User" / "globalStorage" / "state.vscdb",
            "projects": home / ".cursor" / "projects",
            "ai_tracking": home / ".cursor" / "ai-tracking" / "ai-code-tracking.db",
            "ide_state": home / ".cursor" / "ide_state.json",
        }

    def detect(self) -> dict[str, Any]:
        existing = self.existing_paths()
        if not existing:
            return {
                "found": False,
                "status": "not_found",
                "paths": [],
                "sessions_estimate": None,
                "details": "No Cursor storage found",
            }

        sessions_count = None
        paths = self.storage_paths
        if paths["state_db"].exists():
            try:
                sessions_count = self._count_sessions_from_state_db()
            except Exception:
                pass

        status = "found" if paths["state_db"].exists() else "partial"
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
        sessions: list[dict[str, Any]] = []
        paths = self.storage_paths

        # LAYER 1: state.vscdb
        if paths["state_db"].exists():
            try:
                sessions.extend(self._sessions_from_state_db(project_filter))
            except (SQLiteReadError, Exception) as exc:
                pass

        # LAYER 2: agent-transcripts
        if paths["projects"].exists():
            try:
                transcript_sessions = self._sessions_from_transcripts(project_filter)
                # Merge, avoiding duplicates by ID
                existing_ids = {s["id"] for s in sessions}
                for ts in transcript_sessions:
                    if ts["id"] not in existing_ids:
                        sessions.append(ts)
            except Exception:
                pass

        # Sort by date descending (handle mixed int/str date types)
        sessions.sort(key=lambda s: str(s.get("date") or ""), reverse=True)
        return sessions[:limit]

    def extract_session(self, session_id: str) -> ExtractionResult:
        """Extract a full session using the three-layer approach."""
        confidence: list[FieldConfidence] = []
        warnings: list[str] = []
        errors: list[str] = []
        paths = self.storage_paths

        messages: list[Message] = []
        context_items: list[ContextItem] = []
        touched: list[TouchedFile] = []
        title: str | None = None
        mode: str | None = None
        model: str | None = None
        project_root: str | None = None
        created_at: datetime | None = None
        summary: str | None = None
        source_hash: str | None = None

        # ── LAYER 1: state.vscdb ──
        if paths["state_db"].exists():
            try:
                layer1 = self._extract_layer1(session_id)
                if layer1:
                    messages = layer1.get("messages", [])
                    title = layer1.get("title")
                    mode = layer1.get("mode")
                    model = layer1.get("model")
                    project_root = layer1.get("project_root")
                    created_at = layer1.get("created_at")
                    source_hash = layer1.get("source_hash")

                    for fi in layer1.get("file_selections", []):
                        context_items.append(
                            ContextItem(type="file", value=fi, label=Path(fi).name)
                        )

                    confidence.append(
                        FieldConfidence(field_name="messages", level="HIGH", source="state.vscdb composerData")
                    )
                    confidence.append(
                        FieldConfidence(field_name="title", level="HIGH" if title else "LOW", source="state.vscdb")
                    )
                    confidence.append(
                        FieldConfidence(field_name="model", level="HIGH" if model else "MEDIUM", source="state.vscdb modelConfig")
                    )
                else:
                    warnings.append("Session not found in state.vscdb")
                    confidence.append(
                        FieldConfidence(field_name="messages", level="LOW", source="state.vscdb")
                    )
            except SQLiteReadError as exc:
                errors.append(f"state.vscdb: {exc}")

        # ── LAYER 2: agent-transcripts ──
        if paths["projects"].exists() and not messages:
            try:
                layer2 = self._extract_layer2(session_id)
                if layer2:
                    if not messages:
                        messages = layer2.get("messages", [])
                    if not title:
                        title = layer2.get("title")
                    confidence.append(
                        FieldConfidence(
                            field_name="messages",
                            level="MEDIUM",
                            source="agent-transcripts",
                            note="Supplemental transcript data",
                        )
                    )
            except Exception as exc:
                warnings.append(f"agent-transcripts: {exc}")

        # ── LAYER 3: ai-code-tracking.db ──
        if paths["ai_tracking"].exists():
            try:
                layer3 = self._extract_layer3(session_id)
                if layer3:
                    touched.extend(layer3.get("touched_files", []))
                    if not summary:
                        summary = layer3.get("summary")
                    confidence.append(
                        FieldConfidence(
                            field_name="touched_files",
                            level="HIGH",
                            source="ai-code-tracking.db",
                        )
                    )
            except (SQLiteReadError, Exception) as exc:
                warnings.append(f"ai-code-tracking: {exc}")

        if not confidence:
            confidence.append(
                FieldConfidence(field_name="overall", level="LOW", source="no data extracted")
            )

        confidence.append(
            FieldConfidence(
                field_name="project_root",
                level="HIGH" if project_root else "LOW",
                source="state.vscdb folderSelections",
            )
        )

        session = BridgeSession(
            source_ide="cursor",
            session_id=session_id,
            title=title,
            created_at=created_at,
            project_root=project_root,
            mode=mode,
            model=model,
            provider="cursor",
            summary=summary,
            messages=messages,
            context_items=context_items,
            touched_files=touched,
            raw_source_path=str(paths["state_db"]),
            extraction_hash=source_hash,
            provenance=Provenance(
                source_path=str(paths["state_db"]),
                source_hash=source_hash,
            ),
        )

        return ExtractionResult(
            session=session,
            confidence=confidence,
            warnings=warnings,
            errors=errors,
        )

    # ── LAYER 1: state.vscdb ─────────────────────────────────────────────

    def _count_sessions_from_state_db(self) -> int:
        """Count composer sessions in state.vscdb."""
        with SafeSQLiteReader(self.storage_paths["state_db"]) as reader:
            tables = reader.list_tables()
            count = 0

            if "cursorDiskKV" in tables:
                rows = reader.query(
                    "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                )
                count += len(rows)

                # Also check for newer format
                row = reader.query_one(
                    "SELECT value FROM cursorDiskKV WHERE key = 'composer.composerData'"
                )
                if row and row.get("value"):
                    try:
                        data = json.loads(row["value"])
                        if isinstance(data, list):
                            count = max(count, len(data))
                        elif isinstance(data, dict):
                            count = max(count, len(data))
                    except (json.JSONDecodeError, TypeError):
                        pass

            if "ItemTable" in tables:
                # Legacy chat data
                rows = reader.query(
                    "SELECT key FROM ItemTable WHERE key LIKE '%chatdata%' OR key LIKE '%prompts%'"
                )
                count += len(rows)

            return count

    def _sessions_from_state_db(
        self,
        project_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List sessions from state.vscdb."""
        sessions: list[dict[str, Any]] = []

        with SafeSQLiteReader(self.storage_paths["state_db"]) as reader:
            tables = reader.list_tables()

            if "cursorDiskKV" not in tables:
                return sessions

            # Try newer format first: composer.composerData
            row = reader.query_one(
                "SELECT value FROM cursorDiskKV WHERE key = 'composer.composerData'"
            )
            if row and row.get("value"):
                try:
                    data = json.loads(row["value"])
                    sessions.extend(self._parse_composer_list(data, project_filter))
                except (json.JSONDecodeError, TypeError):
                    pass

            # Also check composerData:* keys
            rows = reader.query(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            )
            existing_ids = {s["id"] for s in sessions}
            for r in rows:
                try:
                    data = json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
                    cid = data.get("composerId") or r["key"].split(":", 1)[-1]
                    if cid in existing_ids:
                        continue
                    folder = _extract_folder(data)
                    if project_filter and project_filter.lower() not in (folder or "").lower():
                        continue
                    sessions.append(
                        {
                            "id": cid,
                            "title": data.get("title") or data.get("name") or "Untitled",
                            "date": data.get("createdAt") or data.get("lastUpdated"),
                            "messages": str(len(data.get("messages", data.get("bubbles", [])))),
                            "project": folder or "—",
                        }
                    )
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue

            # Legacy: workbench.panel.aichat.view.aichat.chatdata
            if not sessions:
                for legacy_key in (
                    "workbench.panel.aichat.view.aichat.chatdata",
                    "aiService.prompts",
                    "aiService.generations",
                ):
                    row = reader.query_one(
                        "SELECT value FROM cursorDiskKV WHERE key = ?", (legacy_key,)
                    )
                    if not row:
                        # Try ItemTable
                        if "ItemTable" in tables:
                            row = reader.query_one(
                                "SELECT value FROM ItemTable WHERE key = ?", (legacy_key,)
                            )
                    if row and row.get("value"):
                        try:
                            data = json.loads(row["value"])
                            sessions.extend(
                                self._parse_legacy_chat(data, legacy_key, project_filter)
                            )
                        except (json.JSONDecodeError, TypeError):
                            continue

        return sessions

    def _parse_composer_list(
        self,
        data: Any,
        project_filter: str | None,
    ) -> list[dict[str, Any]]:
        """Parse the composer.composerData blob (list or dict of composers)."""
        sessions: list[dict[str, Any]] = []
        items = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []

        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("composerId") or item.get("id") or str(uuid.uuid4())[:8]
            folder = _extract_folder(item)
            if project_filter and project_filter.lower() not in (folder or "").lower():
                continue

            msg_count = len(item.get("messages", item.get("bubbles", item.get("conversation", []))))
            sessions.append(
                {
                    "id": cid,
                    "title": item.get("title") or item.get("name") or "Untitled",
                    "date": item.get("createdAt") or item.get("lastUpdated") or item.get("updatedAt"),
                    "messages": str(msg_count),
                    "project": folder or "—",
                }
            )

        return sessions

    def _parse_legacy_chat(
        self,
        data: Any,
        key_name: str,
        project_filter: str | None,
    ) -> list[dict[str, Any]]:
        """Parse legacy chat data formats."""
        sessions: list[dict[str, Any]] = []
        if isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, dict):
                    sessions.append(
                        {
                            "id": item.get("id", f"legacy-{i}"),
                            "title": item.get("title", f"Chat {i + 1}"),
                            "date": item.get("timestamp") or item.get("date"),
                            "messages": str(len(item.get("messages", []))),
                            "project": "—",
                        }
                    )
        elif isinstance(data, dict):
            sessions.append(
                {
                    "id": data.get("id", "legacy-0"),
                    "title": data.get("title", key_name),
                    "date": None,
                    "messages": str(len(data.get("messages", data.get("prompts", [])))),
                    "project": "—",
                }
            )
        return sessions

    def _extract_layer1(self, session_id: str) -> dict[str, Any] | None:
        """Extract session data from state.vscdb LAYER 1."""
        with SafeSQLiteReader(self.storage_paths["state_db"]) as reader:
            tables = reader.list_tables()
            source_hash = reader.file_hash

            if "cursorDiskKV" not in tables:
                return None

            # Try direct composerData:<id> key
            row = reader.query_one(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{session_id}",),
            )

            if row and row.get("value"):
                try:
                    data = json.loads(row["value"])
                    return self._parse_composer_data(data, source_hash)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Try the aggregated composer.composerData
            row = reader.query_one(
                "SELECT value FROM cursorDiskKV WHERE key = 'composer.composerData'"
            )
            if row and row.get("value"):
                try:
                    all_data = json.loads(row["value"])
                    items = all_data if isinstance(all_data, list) else list(all_data.values()) if isinstance(all_data, dict) else []
                    for item in items:
                        if isinstance(item, dict):
                            cid = item.get("composerId") or item.get("id")
                            if cid == session_id:
                                return self._parse_composer_data(item, source_hash)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Try bubble messages
            messages = self._extract_bubble_messages(reader, session_id)
            if messages:
                return {
                    "messages": messages,
                    "source_hash": source_hash,
                }

            return None

    def _parse_composer_data(self, data: dict, source_hash: str) -> dict[str, Any]:
        """Parse a single composer data blob into structured fields."""
        messages: list[Message] = []

        # Extract messages from various possible structures
        raw_msgs = data.get("messages") or data.get("bubbles") or data.get("conversation") or []
        for i, msg in enumerate(raw_msgs):
            if isinstance(msg, dict):
                role = msg.get("role") or msg.get("type") or msg.get("sender") or "user"
                content = msg.get("content") or msg.get("text") or msg.get("message") or ""

                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict):
                            parts.append(part.get("text", str(part)))
                        else:
                            parts.append(str(part))
                    content = "\n".join(parts)

                if content:
                    messages.append(
                        Message(
                            id=msg.get("id") or msg.get("bubbleId") or f"cursor-{i}",
                            role=_normalize_role(role),
                            content=str(content),
                            timestamp=_parse_ts(msg.get("timestamp") or msg.get("createdAt")),
                            model=msg.get("model"),
                        )
                    )

        # Extract model config
        model_config = data.get("modelConfig") or data.get("model") or {}
        model_name = None
        if isinstance(model_config, dict):
            model_name = model_config.get("modelName") or model_config.get("model")
        elif isinstance(model_config, str):
            model_name = model_config

        # Extract file selections
        file_selections = []
        for fs in data.get("fileSelections") or data.get("files") or []:
            if isinstance(fs, dict):
                file_selections.append(fs.get("path") or fs.get("uri") or str(fs))
            elif isinstance(fs, str):
                file_selections.append(fs)

        # Extract folder
        folder = _extract_folder(data)

        return {
            "messages": messages,
            "title": data.get("title") or data.get("name"),
            "mode": data.get("mode") or data.get("composerMode"),
            "model": model_name,
            "project_root": folder,
            "created_at": _parse_ts(data.get("createdAt")),
            "file_selections": file_selections,
            "source_hash": source_hash,
        }

    def _extract_bubble_messages(
        self,
        reader: SafeSQLiteReader,
        session_id: str,
    ) -> list[Message]:
        """Extract individual bubble messages for a session."""
        messages: list[Message] = []
        rows = reader.query(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"bubbleId:{session_id}:%",),
        )
        for row in rows:
            try:
                data = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
                if isinstance(data, dict):
                    content = data.get("content") or data.get("text") or ""
                    if content:
                        messages.append(
                            Message(
                                id=data.get("bubbleId") or row["key"],
                                role=_normalize_role(data.get("role", "user")),
                                content=str(content),
                                timestamp=_parse_ts(data.get("timestamp")),
                            )
                        )
            except (json.JSONDecodeError, TypeError):
                continue
        return messages

    # ── LAYER 2: agent-transcripts ───────────────────────────────────────

    def _sessions_from_transcripts(
        self,
        project_filter: str | None,
    ) -> list[dict[str, Any]]:
        """List sessions from agent-transcripts directories."""
        sessions: list[dict[str, Any]] = []
        projects_dir = self.storage_paths["projects"]
        if not projects_dir.exists():
            return sessions

        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            transcripts_dir = project_dir / "agent-transcripts"
            if not transcripts_dir.exists():
                continue

            project_name = project_dir.name
            if project_filter and project_filter.lower() not in project_name.lower():
                continue

            for transcript_dir in transcripts_dir.iterdir():
                if not transcript_dir.is_dir():
                    continue
                sessions.append(
                    {
                        "id": transcript_dir.name,
                        "title": f"Agent transcript ({project_name})",
                        "date": _dir_mtime(transcript_dir),
                        "messages": "?",
                        "project": project_name,
                    }
                )

        return sessions

    def _extract_layer2(self, session_id: str) -> dict[str, Any] | None:
        """Extract transcript data for a session from agent-transcripts."""
        projects_dir = self.storage_paths["projects"]
        if not projects_dir.exists():
            return None

        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            transcript_dir = project_dir / "agent-transcripts" / session_id
            if not transcript_dir.exists():
                continue

            messages: list[Message] = []
            # Parse any JSON/JSONL files in the transcript dir
            for file in sorted(transcript_dir.iterdir()):
                if file.suffix in (".json", ".jsonl"):
                    try:
                        if file.suffix == ".jsonl":
                            with open(file, encoding="utf-8", errors="replace") as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    data = json.loads(line)
                                    msg = _parse_transcript_msg(data, len(messages))
                                    if msg:
                                        messages.append(msg)
                        else:
                            with open(file, encoding="utf-8", errors="replace") as f:
                                data = json.load(f)
                            if isinstance(data, list):
                                for item in data:
                                    msg = _parse_transcript_msg(item, len(messages))
                                    if msg:
                                        messages.append(msg)
                            elif isinstance(data, dict):
                                for item in data.get("messages", data.get("conversation", [data])):
                                    msg = _parse_transcript_msg(item, len(messages))
                                    if msg:
                                        messages.append(msg)
                    except (json.JSONDecodeError, OSError):
                        continue

            if messages:
                return {
                    "messages": messages,
                    "title": f"Agent transcript ({project_dir.name})",
                }

        return None

    # ── LAYER 3: ai-code-tracking.db ─────────────────────────────────────

    def _extract_layer3(self, session_id: str) -> dict[str, Any] | None:
        """Extract touched files and summaries from ai-code-tracking.db."""
        db_path = self.storage_paths["ai_tracking"]
        if not db_path.exists():
            return None

        touched: list[TouchedFile] = []
        summary: str | None = None

        try:
            with SafeSQLiteReader(db_path) as reader:
                tables = reader.list_tables()

                # ai_code_hashes table
                if "ai_code_hashes" in tables:
                    rows = reader.query(
                        "SELECT * FROM ai_code_hashes WHERE conversationId = ?",
                        (session_id,),
                    )
                    if not rows:
                        # Try without conversationId filter — get all
                        rows = reader.query("SELECT * FROM ai_code_hashes LIMIT 100")

                    for row in rows:
                        path = row.get("filePath") or row.get("file_path") or row.get("path")
                        if path:
                            touched.append(
                                TouchedFile(
                                    path=str(path),
                                    status="edited",
                                    conversation_id=row.get("conversationId") or session_id,
                                    hash=row.get("hash") or row.get("codeHash"),
                                )
                            )

                # conversation_summaries table
                if "conversation_summaries" in tables:
                    row = reader.query_one(
                        "SELECT * FROM conversation_summaries WHERE conversationId = ?",
                        (session_id,),
                    )
                    if row:
                        summary = row.get("summary") or row.get("content")

        except SQLiteReadError:
            pass

        if touched or summary:
            return {"touched_files": touched, "summary": summary}
        return None

    # ── Forensics support ────────────────────────────────────────────────

    def forensics_dump(self) -> list[dict[str, Any]]:
        """Deep diagnostic dump of all Cursor storage."""
        results: list[dict[str, Any]] = []
        paths = self.storage_paths

        if paths["state_db"].exists():
            try:
                with SafeSQLiteReader(paths["state_db"]) as reader:
                    tables = reader.list_tables()
                    for table in tables:
                        schema = reader.table_schema(table)
                        count = reader.row_count(table)
                        results.append(
                            {
                                "key": f"[TABLE] {table}",
                                "classification": _classify_table(table),
                                "value_preview": f"{count} rows, {len(schema)} columns",
                                "table": table,
                                "schema": schema,
                            }
                        )

                    # Enumerate interesting keys in cursorDiskKV
                    if "cursorDiskKV" in tables:
                        keys = reader.enumerate_keys(
                            "cursorDiskKV",
                            filters=[
                                "chat", "history", "composer", "conversation",
                                "session", "transcript", "ai", "context",
                                "message", "bubble", "prompt", "generation",
                            ],
                        )
                        for k in keys:
                            results.append(
                                {
                                    "key": k["key"],
                                    "classification": _classify_key(k["key"]),
                                    "value_preview": k["value_preview"],
                                }
                            )
            except SQLiteReadError as exc:
                results.append(
                    {
                        "key": "state.vscdb",
                        "classification": "ERROR",
                        "value_preview": str(exc),
                    }
                )

        if paths["ai_tracking"].exists():
            try:
                with SafeSQLiteReader(paths["ai_tracking"]) as reader:
                    tables = reader.list_tables()
                    for table in tables:
                        schema = reader.table_schema(table)
                        count = reader.row_count(table)
                        results.append(
                            {
                                "key": f"[TRACKING] {table}",
                                "classification": _classify_table(table),
                                "value_preview": f"{count} rows, {len(schema)} columns",
                                "table": table,
                                "schema": schema,
                            }
                        )
            except SQLiteReadError as exc:
                results.append(
                    {
                        "key": "ai-code-tracking.db",
                        "classification": "ERROR",
                        "value_preview": str(exc),
                    }
                )

        return results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_folder(data: dict) -> str | None:
    """Extract folder/project path from composer data."""
    # folderSelections
    folders = data.get("folderSelections") or data.get("folders") or []
    if folders:
        if isinstance(folders[0], dict):
            return folders[0].get("path") or folders[0].get("uri")
        return str(folders[0])
    # Direct folder field
    return data.get("folder") or data.get("cwd") or data.get("projectRoot")


def _normalize_role(role: str) -> str:
    """Normalize role string."""
    role = str(role).lower().strip()
    if role in ("user", "human", "1"):
        return "user"
    if role in ("assistant", "ai", "bot", "model", "2"):
        return "assistant"
    if role in ("system",):
        return "system"
    if role in ("tool", "function"):
        return "tool"
    return "assistant"


def _parse_ts(value: Any) -> datetime | None:
    """Parse a timestamp from various formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            # Handle millisecond timestamps
            if value > 1e12:
                value = value / 1000
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


def _parse_transcript_msg(data: Any, index: int) -> Message | None:
    """Parse a single transcript message."""
    if not isinstance(data, dict):
        return None
    role = data.get("role") or data.get("type") or data.get("sender")
    content = data.get("content") or data.get("text") or data.get("message")
    if not role or not content:
        return None
    return Message(
        id=data.get("id") or f"transcript-{index}",
        role=_normalize_role(str(role)),
        content=str(content),
        timestamp=_parse_ts(data.get("timestamp") or data.get("createdAt")),
    )


def _dir_mtime(path: Path) -> str:
    """Get directory modification time as ISO string."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception:
        return ""


def _classify_table(name: str) -> str:
    """Classify a table name."""
    name_lower = name.lower()
    if any(k in name_lower for k in ("chat", "message", "bubble", "composer", "conversation")):
        return "CHAT"
    if any(k in name_lower for k in ("context", "file", "hash", "tracking")):
        return "CONTEXT"
    if any(k in name_lower for k in ("config", "setting", "state", "preference")):
        return "CONFIG"
    if any(k in name_lower for k in ("cache", "tmp", "temp")):
        return "CACHE"
    return "UNKNOWN"


def _classify_key(key: str) -> str:
    """Classify a key name."""
    key_lower = key.lower()
    if any(k in key_lower for k in ("chat", "message", "bubble", "composer", "conversation", "prompt", "generation")):
        return "CHAT"
    if any(k in key_lower for k in ("context", "file", "selection", "folder")):
        return "CONTEXT"
    if any(k in key_lower for k in ("config", "setting", "state", "preference", "encryption")):
        return "CONFIG"
    if any(k in key_lower for k in ("cache", "tmp", "temp")):
        return "CACHE"
    return "UNKNOWN"
