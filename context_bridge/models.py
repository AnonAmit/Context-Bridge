"""Universal data model for Context Bridge sessions.

All IDE-specific data is normalized into these Pydantic v2 models,
providing a single portable schema for cross-IDE context transfer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


class Message(BaseModel):
    """A single message in a conversation thread."""

    id: str
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    timestamp: datetime | None = None
    parent_id: str | None = None
    model: str | None = None

    def preview(self, max_length: int = 80) -> str:
        """Return a truncated preview of the message content."""
        text = self.content.replace("\n", " ").strip()
        if len(text) > max_length:
            return text[: max_length - 1] + "…"
        return text


class ContextItem(BaseModel):
    """An item of context attached to a session (file, URL, terminal output, etc.)."""

    type: Literal["file", "folder", "url", "terminal", "commit", "pr", "doc"]
    value: str
    label: str | None = None


class TouchedFile(BaseModel):
    """A file that was created, edited, deleted, or referenced during a session."""

    path: str
    status: Literal["created", "edited", "deleted", "referenced"]
    conversation_id: str | None = None
    hash: str | None = None


class Provenance(BaseModel):
    """Extraction provenance metadata — included in every exported bridge session."""

    extracted_at: datetime = Field(default_factory=datetime.now)
    source_path: str | None = None
    source_hash: str | None = None
    tool_version: str = "1.0"
    redacted: bool = True


class BridgeSession(BaseModel):
    """The universal session schema that all IDE data is normalized into.

    This is the core portable format written to bridge-session.json files
    and consumed by emitters to generate target-specific artifacts.
    """

    bridge_version: str = "1.0"
    source_ide: str
    session_id: str
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    project_root: str | None = None
    mode: str | None = None  # "chat" | "agent" | "edit"
    model: str | None = None
    provider: str | None = None
    base_instructions: str | None = None
    summary: str | None = None
    messages: list[Message] = Field(default_factory=list)
    context_items: list[ContextItem] = Field(default_factory=list)
    touched_files: list[TouchedFile] = Field(default_factory=list)
    raw_source_path: str | None = None
    extraction_hash: str | None = None
    provenance: Provenance | None = None

    @computed_field
    @property
    def message_count(self) -> int:
        """Total number of messages in this session."""
        return len(self.messages)

    @computed_field
    @property
    def file_count(self) -> int:
        """Total number of touched files."""
        return len(self.touched_files)

    def compute_content_hash(self) -> str:
        """Compute a SHA-256 hash of the session content for deduplication."""
        payload = json.dumps(
            {
                "source_ide": self.source_ide,
                "session_id": self.session_id,
                "messages": [m.model_dump(mode="json") for m in self.messages],
            },
            sort_keys=True,
            default=str,
        )
        return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"

    def to_bridge_json(self, **kwargs: Any) -> str:
        """Serialize to pretty-printed JSON for export."""
        data = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(data, indent=2, default=str, ensure_ascii=False)


class FieldConfidence(BaseModel):
    """Confidence level for a specific extracted field."""

    field_name: str
    level: Literal["HIGH", "MEDIUM", "LOW"]
    source: str | None = None  # where this data came from
    note: str | None = None


class ExtractionResult(BaseModel):
    """Result of extracting a session from an IDE, including confidence metadata."""

    session: BridgeSession
    confidence: list[FieldConfidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def overall_confidence(self) -> str:
        """Return the lowest confidence level across all fields."""
        if not self.confidence:
            return "LOW"
        levels = [c.level for c in self.confidence]
        if "LOW" in levels:
            return "LOW"
        if "MEDIUM" in levels:
            return "MEDIUM"
        return "HIGH"
