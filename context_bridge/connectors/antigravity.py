"""Antigravity IDE connector.

Discovery-first approach: enumerates all keys and tables in
Antigravity's storage to find conversation data, since the
internal schema may vary across versions.
"""

from __future__ import annotations

import json
import os
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


# Keys that may contain conversation data
_DISCOVERY_KEYWORDS = [
    "chat", "history", "composer", "conversation", "session",
    "transcript", "ai", "context", "message", "prompt",
    "generation", "brain", "knowledge",
]


class AntigravityConnector(IDEConnector):
    """Connector for Antigravity IDE (discovery-first approach)."""

    @property
    def ide_name(self) -> str:
        return "Antigravity"

    @property
    def storage_paths(self) -> dict[str, Path]:
        appdata = self.appdata()
        home = self.user_profile()
        return {
            "app_dir": appdata / "Antigravity",
            "state_db": appdata / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
            "user_dir": home / ".gemini" / "antigravity",
            "extensions": appdata / "Antigravity" / "User" / "globalStorage",
            "local_storage": appdata / "Antigravity" / "Local Storage" / "leveldb",
            "indexeddb": appdata / "Antigravity" / "IndexedDB",
        }

    def detect(self) -> dict[str, Any]:
        existing = self.existing_paths()
        if not existing:
            return {
                "found": False,
                "status": "not_found",
                "paths": [],
                "sessions_estimate": None,
                "details": "No Antigravity storage found",
            }

        sessions_count = None
        paths = self.storage_paths

        if paths["state_db"].exists():
            try:
                sessions_count = self._count_sessions()
            except Exception:
                pass

        status = "found" if paths["state_db"].exists() else "partial"
        return {
            "found": True,
            "status": status,
            "paths": existing,
            "sessions_estimate": sessions_count,
            "details": f"Found {sessions_count or '?'} sessions (discovery mode)",
        }

    def list_sessions(
        self,
        limit: int = 20,
        project_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        paths = self.storage_paths

        # Primary: scan user dir (Antigravity 'brain' storage)
        if paths["user_dir"].exists():
            try:
                extra = self._sessions_from_user_dir(project_filter)
                existing_ids = {s["id"] for s in sessions}
                for s in extra:
                    if s["id"] not in existing_ids:
                        sessions.append(s)
            except Exception:
                pass

        # Secondary: state.vscdb
        if paths["state_db"].exists():
            try:
                db_sessions = self._sessions_from_state_db(project_filter)
                existing_ids = {s["id"] for s in sessions}
                for s in db_sessions:
                    if s["id"] not in existing_ids:
                        sessions.append(s)
            except (SQLiteReadError, Exception):
                pass

        # Tertiary: scan extensions dir for any db/json files
        if not sessions and paths["extensions"].exists():
            try:
                sessions.extend(self._sessions_from_extensions(project_filter))
            except Exception:
                pass

        sessions.sort(key=lambda s: s.get("date") or "", reverse=True)
        return sessions[:limit]

    def extract_session(self, session_id: str) -> ExtractionResult:
        """Extract a session using discovery-first approach."""
        confidence: list[FieldConfidence] = []
        warnings: list[str] = []
        errors: list[str] = []
        paths = self.storage_paths

        messages: list[Message] = []
        title: str | None = None
        model: str | None = None
        project_root: str | None = None
        created_at: datetime | None = None
        source_hash: str | None = None
        context_items: list[ContextItem] = []
        touched: list[TouchedFile] = []

        # Try state.vscdb
        if paths["state_db"].exists():
            try:
                data = self._extract_from_state_db(session_id)
                if data:
                    messages = data.get("messages", [])
                    title = data.get("title")
                    model = data.get("model")
                    project_root = data.get("project_root")
                    created_at = data.get("created_at")
                    source_hash = data.get("source_hash")

                    confidence.append(
                        FieldConfidence(
                            field_name="messages",
                            level="HIGH" if messages else "LOW",
                            source="state.vscdb",
                        )
                    )
                    confidence.append(
                        FieldConfidence(
                            field_name="title",
                            level="HIGH" if title else "LOW",
                            source="state.vscdb",
                        )
                    )
                else:
                    warnings.append("Session not found in state.vscdb — trying discovery")
            except SQLiteReadError as exc:
                errors.append(f"state.vscdb: {exc}")

        # Try user dir files
        if not messages and paths["user_dir"].exists():
            try:
                data = self._extract_from_user_dir(session_id)
                if data:
                    messages = data.get("messages", [])
                    title = data.get("title")
                    confidence.append(
                        FieldConfidence(
                            field_name="messages",
                            level="MEDIUM",
                            source="user dir files",
                        )
                    )
            except Exception as exc:
                warnings.append(f"User dir scan: {exc}")

        # Try extension storage
        if not messages and paths["extensions"].exists():
            try:
                data = self._extract_from_extensions(session_id)
                if data:
                    messages = data.get("messages", [])
                    title = data.get("title")
                    confidence.append(
                        FieldConfidence(
                            field_name="messages",
                            level="MEDIUM",
                            source="extension storage",
                        )
                    )
            except Exception as exc:
                warnings.append(f"Extension scan: {exc}")

        if not confidence:
            confidence.append(
                FieldConfidence(field_name="overall", level="LOW", source="discovery mode")
            )

        session = BridgeSession(
            source_ide="antigravity",
            session_id=session_id,
            title=title,
            created_at=created_at,
            project_root=project_root,
            model=model,
            provider="antigravity",
            messages=messages,
            context_items=context_items,
            touched_files=touched,
            raw_source_path=str(paths["state_db"]) if paths["state_db"].exists() else None,
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

    # ── State DB methods ─────────────────────────────────────────────────

    def _count_sessions(self) -> int:
        """Count discoverable sessions in state.vscdb."""
        count = 0
        with SafeSQLiteReader(self.storage_paths["state_db"]) as reader:
            tables = reader.list_tables()

            if "cursorDiskKV" in tables:
                rows = reader.query(
                    "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                )
                count += len(rows)

                row = reader.query_one(
                    "SELECT value FROM cursorDiskKV WHERE key = 'composer.composerData'"
                )
                if row and row.get("value"):
                    try:
                        data = json.loads(row["value"])
                        if isinstance(data, (list, dict)):
                            items = data if isinstance(data, list) else list(data.values())
                            count = max(count, len(items))
                    except (json.JSONDecodeError, TypeError):
                        pass

            if "ItemTable" in tables:
                # Look for any conversation-related keys
                for keyword in _DISCOVERY_KEYWORDS:
                    rows = reader.query(
                        f"SELECT key FROM ItemTable WHERE key LIKE '%{keyword}%'"
                    )
                    count += len(rows)

        return count

    def _sessions_from_state_db(
        self,
        project_filter: str | None,
    ) -> list[dict[str, Any]]:
        """Discover sessions from state.vscdb."""
        sessions: list[dict[str, Any]] = []

        with SafeSQLiteReader(self.storage_paths["state_db"]) as reader:
            tables = reader.list_tables()

            # Check cursorDiskKV (Antigravity may use same schema as Cursor)
            if "cursorDiskKV" in tables:
                # Aggregated format
                row = reader.query_one(
                    "SELECT value FROM cursorDiskKV WHERE key = 'composer.composerData'"
                )
                if row and row.get("value"):
                    try:
                        data = json.loads(row["value"])
                        items = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            cid = item.get("composerId") or item.get("id") or ""
                            folder = item.get("folder") or item.get("cwd") or ""
                            if project_filter and project_filter.lower() not in folder.lower():
                                continue
                            sessions.append({
                                "id": cid,
                                "title": item.get("title") or "Untitled",
                                "date": item.get("createdAt") or item.get("updatedAt"),
                                "messages": str(len(item.get("messages", item.get("bubbles", [])))),
                                "project": folder or "—",
                            })
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Individual composerData keys
                rows = reader.query(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                )
                existing_ids = {s["id"] for s in sessions}
                for r in rows:
                    try:
                        data = json.loads(r["value"]) if isinstance(r["value"], str) else {}
                        cid = data.get("composerId") or r["key"].split(":", 1)[-1]
                        if cid in existing_ids:
                            continue
                        sessions.append({
                            "id": cid,
                            "title": data.get("title") or "Untitled",
                            "date": data.get("createdAt"),
                            "messages": str(len(data.get("messages", []))),
                            "project": data.get("folder") or "—",
                        })
                    except (json.JSONDecodeError, TypeError):
                        continue

            # Exclude raw ItemTable dumping as it pollutes the UI with telemetry keys
            # Only return actual parsed composer data
            pass

        return sessions

    def _extract_from_state_db(self, session_id: str) -> dict[str, Any] | None:
        """Extract a session from state.vscdb."""
        with SafeSQLiteReader(self.storage_paths["state_db"]) as reader:
            tables = reader.list_tables()
            source_hash = reader.file_hash

            if "cursorDiskKV" not in tables:
                return None

            # Direct key lookup
            row = reader.query_one(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{session_id}",),
            )
            if row and row.get("value"):
                try:
                    data = json.loads(row["value"])
                    return self._parse_session_data(data, source_hash)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Search in aggregated data
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
                                return self._parse_session_data(item, source_hash)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Search ItemTable
            if "ItemTable" in tables:
                row = reader.query_one(
                    "SELECT value FROM ItemTable WHERE key = ?", (session_id,)
                )
                if row and row.get("value"):
                    try:
                        data = json.loads(row["value"])
                        return self._parse_session_data(data, source_hash)
                    except (json.JSONDecodeError, TypeError):
                        pass

        return None

    def _parse_session_data(self, data: dict, source_hash: str) -> dict[str, Any]:
        """Parse a session data blob."""
        messages: list[Message] = []
        raw_msgs = data.get("messages") or data.get("bubbles") or data.get("conversation") or []

        for i, msg in enumerate(raw_msgs):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or msg.get("type") or "user"
            content = msg.get("content") or msg.get("text") or ""

            if isinstance(content, list):
                parts = [p.get("text", str(p)) if isinstance(p, dict) else str(p) for p in content]
                content = "\n".join(parts)

            if content:
                messages.append(Message(
                    id=msg.get("id") or f"ag-{i}",
                    role=_normalize_role(str(role)),
                    content=str(content),
                    timestamp=_parse_ts(msg.get("timestamp") or msg.get("createdAt")),
                    model=msg.get("model"),
                ))

        model_config = data.get("modelConfig") or data.get("model") or {}
        model_name = model_config.get("modelName") if isinstance(model_config, dict) else str(model_config) if model_config else None

        return {
            "messages": messages,
            "title": data.get("title") or data.get("name"),
            "model": model_name,
            "project_root": data.get("folder") or data.get("cwd"),
            "created_at": _parse_ts(data.get("createdAt")),
            "source_hash": source_hash,
        }

    # ── Extension storage ────────────────────────────────────────────────

    def _sessions_from_extensions(self, project_filter: str | None) -> list[dict[str, Any]]:
        """Scan extension dirs for session-like data."""
        sessions: list[dict[str, Any]] = []
        ext_dir = self.storage_paths["extensions"]
        if not ext_dir.exists():
            return sessions

        for sub in ext_dir.iterdir():
            if not sub.is_dir():
                continue
            for f in sub.rglob("*.json"):
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict) and any(
                        k in data for k in ("messages", "conversation", "chat", "session")
                    ):
                        sessions.append({
                            "id": f.stem,
                            "title": data.get("title") or f.stem,
                            "date": _file_mtime(f),
                            "messages": str(len(data.get("messages", []))),
                            "project": "—",
                        })
                except (json.JSONDecodeError, OSError):
                    continue

            for db_file in sub.rglob("*.db"):
                try:
                    with SafeSQLiteReader(db_file) as reader:
                        tables = reader.list_tables()
                        for table in tables:
                            if any(kw in table.lower() for kw in _DISCOVERY_KEYWORDS):
                                count = reader.row_count(table)
                                sessions.append({
                                    "id": f"{db_file.stem}:{table}",
                                    "title": f"{db_file.name} / {table}",
                                    "date": _file_mtime(db_file),
                                    "messages": str(count),
                                    "project": "—",
                                })
                except (SQLiteReadError, Exception):
                    continue

        return sessions

    def _extract_from_extensions(self, session_id: str) -> dict[str, Any] | None:
        """Try to extract a session from extension storage."""
        ext_dir = self.storage_paths["extensions"]
        if not ext_dir.exists():
            return None

        for f in ext_dir.rglob("*.json"):
            if f.stem == session_id:
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        return self._parse_session_data(data, "")
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    # ── User dir ─────────────────────────────────────────────────────────

    def _sessions_from_user_dir(self, project_filter: str | None) -> list[dict[str, Any]]:
        """Scan ~/.antigravity for conversation data."""
        sessions: list[dict[str, Any]] = []
        user_dir = self.storage_paths["user_dir"]
        if not user_dir.exists():
            return sessions

        # Look for brain/conversation dirs
        brain_dir = user_dir / "brain"
        if brain_dir.exists():
            for conv_dir in brain_dir.iterdir():
                if conv_dir.is_dir():
                    title = f"Conversation {conv_dir.name[:8]}"
                    # Try to extract a human-readable title from markdown artifacts
                    for md_file in ("walkthrough.md", "task.md", "implementation_plan.md"):
                        md_path = conv_dir / md_file
                        if md_path.exists():
                            try:
                                with open(md_path, encoding="utf-8") as f:
                                    for line in f:
                                        if line.startswith("# "):
                                            raw_title = line[2:].strip()
                                            # Strip non-ascii (like emojis) for cmd.exe rendering safety on Windows
                                            title = raw_title.encode("ascii", errors="replace").decode("ascii")
                                            break
                                if title != f"Conversation {conv_dir.name[:8]}":
                                    break
                            except Exception:
                                pass

                    sessions.append({
                        "id": conv_dir.name,
                        "title": title[:50],
                        "date": _dir_mtime(conv_dir),
                        "messages": "?",
                        "project": "—",
                    })

        # Look for any JSON/JSONL files
        for f in user_dir.rglob("*.jsonl"):
            sessions.append({
                "id": f.stem,
                "title": f.name,
                "date": _file_mtime(f),
                "messages": "?",
                "project": "—",
            })

        return sessions

    def _extract_from_user_dir(self, session_id: str) -> dict[str, Any] | None:
        """Try to extract a session from user dir files."""
        user_dir = self.storage_paths["user_dir"]
        if not user_dir.exists():
            return None

        # Check brain dir
        brain_dir = user_dir / "brain" / session_id
        if brain_dir.exists():
            # Look for overview.txt or log files
            overview = brain_dir / ".system_generated" / "logs" / "overview.txt"
            if overview.exists():
                try:
                    content = overview.read_text(encoding="utf-8", errors="replace")
                    messages = self._parse_overview_txt(content)
                    if messages:
                        return {"messages": messages, "title": f"Conversation {session_id[:8]}"}
                except OSError:
                    pass

        return None

    def _parse_overview_txt(self, content: str) -> list[Message]:
        """Parse an Antigravity overview.txt into messages."""
        messages: list[Message] = []
        # Simple heuristic: lines starting with role indicators
        current_role = None
        current_content: list[str] = []
        idx = 0

        for line in content.split("\n"):
            line_stripped = line.strip()
            if line_stripped.startswith("USER:") or line_stripped.startswith("[USER]"):
                if current_role and current_content:
                    messages.append(Message(
                        id=f"overview-{idx}",
                        role=current_role,
                        content="\n".join(current_content).strip(),
                    ))
                    idx += 1
                current_role = "user"
                current_content = [line_stripped.split(":", 1)[-1].strip() if ":" in line_stripped else ""]
            elif line_stripped.startswith("ASSISTANT:") or line_stripped.startswith("[ASSISTANT]") or line_stripped.startswith("MODEL:"):
                if current_role and current_content:
                    messages.append(Message(
                        id=f"overview-{idx}",
                        role=current_role,
                        content="\n".join(current_content).strip(),
                    ))
                    idx += 1
                current_role = "assistant"
                current_content = [line_stripped.split(":", 1)[-1].strip() if ":" in line_stripped else ""]
            else:
                current_content.append(line)

        if current_role and current_content:
            messages.append(Message(
                id=f"overview-{idx}",
                role=current_role,
                content="\n".join(current_content).strip(),
            ))

        return messages

    # ── Forensics ────────────────────────────────────────────────────────

    def forensics_dump(self) -> list[dict[str, Any]]:
        """Deep diagnostic dump of Antigravity storage."""
        results: list[dict[str, Any]] = []
        paths = self.storage_paths

        if paths["state_db"].exists():
            try:
                with SafeSQLiteReader(paths["state_db"]) as reader:
                    tables = reader.list_tables()
                    for table in tables:
                        schema = reader.table_schema(table)
                        count = reader.row_count(table)
                        results.append({
                            "key": f"[TABLE] {table}",
                            "classification": _classify_key(table),
                            "value_preview": f"{count} rows, {len(schema)} columns",
                            "table": table,
                            "schema": schema,
                        })

                    # Enumerate all keys in all tables
                    for table in tables:
                        try:
                            keys = reader.enumerate_keys(table, filters=_DISCOVERY_KEYWORDS)
                            for k in keys:
                                results.append({
                                    "key": f"{table}:{k['key']}",
                                    "classification": _classify_key(k["key"]),
                                    "value_preview": k["value_preview"],
                                })
                        except Exception:
                            continue
            except SQLiteReadError as exc:
                results.append({
                    "key": "state.vscdb",
                    "classification": "ERROR",
                    "value_preview": str(exc),
                })

        # Scan extension dirs
        if paths["extensions"].exists():
            for sub in paths["extensions"].iterdir():
                if sub.is_dir():
                    db_files = list(sub.rglob("*.db")) + list(sub.rglob("*.sqlite"))
                    json_files = list(sub.rglob("*.json"))
                    jsonl_files = list(sub.rglob("*.jsonl"))
                    if db_files or json_files or jsonl_files:
                        results.append({
                            "key": f"[EXT] {sub.name}",
                            "classification": "CONTEXT",
                            "value_preview": f"{len(db_files)} db, {len(json_files)} json, {len(jsonl_files)} jsonl files",
                        })

        return results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_role(role: str) -> str:
    role = role.lower().strip()
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


def _dir_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception:
        return ""


def _classify_key(key: str) -> str:
    key_lower = key.lower()
    if any(k in key_lower for k in ("chat", "message", "bubble", "composer", "conversation", "prompt")):
        return "CHAT"
    if any(k in key_lower for k in ("context", "file", "selection", "folder", "item")):
        return "CONTEXT"
    if any(k in key_lower for k in ("config", "setting", "state", "preference")):
        return "CONFIG"
    if any(k in key_lower for k in ("cache", "tmp", "temp")):
        return "CACHE"
    return "UNKNOWN"
