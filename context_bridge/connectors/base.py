"""Abstract base class for all IDE connectors.

Every connector implements the same interface, enabling the CLI
to work uniformly across Codex, Cursor, Antigravity, and Claude Code.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from context_bridge.models import BridgeSession, ExtractionResult


class IDEConnector(ABC):
    """Abstract base class for IDE session extraction.

    Subclasses must implement:
      - ide_name: human-readable name
      - detect(): check if this IDE's storage exists on the machine
      - list_sessions(): enumerate available sessions
      - extract_session(session_id): full session extraction
    """

    @property
    @abstractmethod
    def ide_name(self) -> str:
        """Human-readable name for this IDE (e.g., 'Codex CLI')."""
        ...

    @property
    @abstractmethod
    def storage_paths(self) -> dict[str, Path]:
        """Map of named storage paths for this IDE.

        Example: {"index": Path("~/.codex/session_index.jsonl")}
        """
        ...

    @abstractmethod
    def detect(self) -> dict[str, Any]:
        """Detect whether this IDE's storage exists on the local machine.

        Returns:
            {
                "found": bool,
                "status": "found"|"partial"|"not_found",
                "paths": [list of existing paths as strings],
                "sessions_estimate": int or None,
                "details": str,
            }
        """
        ...

    @abstractmethod
    def list_sessions(
        self,
        limit: int = 20,
        project_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available sessions.

        Returns list of dicts with at minimum:
            {id, title, date, messages, project}
        """
        ...

    @abstractmethod
    def extract_session(self, session_id: str) -> ExtractionResult:
        """Extract a full session by ID.

        Returns an ExtractionResult with the BridgeSession and confidence metadata.
        """
        ...

    # ── Shared helpers ────────────────────────────────────────────────────

    @staticmethod
    def resolve_env_path(template: str) -> Path:
        """Resolve a path template with environment variables.

        Supports %USERPROFILE%, %APPDATA%, %LOCALAPPDATA%, ~, etc.
        """
        # Expand ~ first
        expanded = os.path.expanduser(template)
        # Then expand environment variables (Windows-style and Unix-style)
        expanded = os.path.expandvars(expanded)
        return Path(expanded)

    @staticmethod
    def user_profile() -> Path:
        """Return the user's home directory."""
        return Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))

    @staticmethod
    def appdata() -> Path:
        """Return %APPDATA% (Roaming)."""
        return Path(
            os.environ.get("APPDATA", Path(os.path.expanduser("~")) / "AppData" / "Roaming")
        )

    @staticmethod
    def local_appdata() -> Path:
        """Return %LOCALAPPDATA%."""
        return Path(
            os.environ.get(
                "LOCALAPPDATA",
                Path(os.path.expanduser("~")) / "AppData" / "Local",
            )
        )

    def existing_paths(self) -> list[str]:
        """Return list of storage paths that actually exist on disk."""
        return [
            str(p) for p in self.storage_paths.values() if p.exists()
        ]

    def detection_row(self) -> dict[str, Any]:
        """Build a row dict suitable for ui.detection_table()."""
        result = self.detect()
        return {
            "ide": self.ide_name,
            "status": result.get("status", "not_found"),
            "paths": result.get("paths", []),
            "sessions": result.get("sessions_estimate", "—"),
        }
