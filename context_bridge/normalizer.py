"""Normalizer: converts raw IDE-specific data into a BridgeSession.

The normalizer orchestrates the extraction process:
1. Selects the correct connector based on IDE name
2. Extracts session data
3. Applies redaction rules
4. Computes content hashes
5. Returns a clean ExtractionResult
"""

from __future__ import annotations

import re
from typing import Any

from context_bridge.connectors.antigravity import AntigravityConnector
from context_bridge.connectors.base import IDEConnector
from context_bridge.connectors.claude_code import ClaudeCodeConnector
from context_bridge.connectors.codex import CodexConnector
from context_bridge.connectors.cursor import CursorConnector
from context_bridge.models import BridgeSession, ExtractionResult, Provenance


# ── Connector registry ───────────────────────────────────────────────────────

_CONNECTORS: dict[str, type[IDEConnector]] = {
    "codex": CodexConnector,
    "cursor": CursorConnector,
    "antigravity": AntigravityConnector,
    "claude": ClaudeCodeConnector,
    "claude_code": ClaudeCodeConnector,
}


def get_connector(ide_name: str) -> IDEConnector:
    """Get a connector instance by IDE name."""
    key = ide_name.lower().replace(" ", "_").replace("-", "_")
    cls = _CONNECTORS.get(key)
    if cls is None:
        available = ", ".join(sorted(_CONNECTORS.keys()))
        raise ValueError(f"Unknown IDE: {ide_name!r} — available: {available}")
    return cls()


def get_all_connectors() -> list[IDEConnector]:
    """Get instances of all known connectors."""
    seen: set[str] = set()
    connectors: list[IDEConnector] = []
    for key, cls in _CONNECTORS.items():
        cls_name = cls.__name__
        if cls_name not in seen:
            seen.add(cls_name)
            connectors.append(cls())
    return connectors


# ── Redaction patterns ───────────────────────────────────────────────────────

_REDACTION_PATTERNS = [
    # API keys
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    # Bearer tokens
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer [REDACTED_TOKEN]"),
    # Generic tokens/keys in key=value format
    (re.compile(r'(?i)(api[_-]?key|token|secret|password|auth)\s*[=:]\s*["\']?[A-Za-z0-9._\-]{8,}["\']?'),
     r"\1=[REDACTED]"),
    # Cursor encryption keys
    (re.compile(r"(?i)(blobEncryptionKey|speculativeSummarizationEncryptionKey)\s*[=:]\s*[^\s,}]+"),
     r"\1=[REDACTED]"),
    # Cookie values
    (re.compile(r'(?i)(cookie|session_id|auth_token)\s*[=:]\s*[^\s;,}]+'),
     r"\1=[REDACTED]"),
]


def redact_content(text: str) -> str:
    """Apply redaction patterns to a text string."""
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_session(session: BridgeSession) -> BridgeSession:
    """Apply redaction to all message content and instructions in a session."""
    for msg in session.messages:
        msg.content = redact_content(msg.content)

    if session.base_instructions:
        session.base_instructions = redact_content(session.base_instructions)

    if session.summary:
        session.summary = redact_content(session.summary)

    return session


# ── Normalization pipeline ───────────────────────────────────────────────────

def normalize_session(
    ide_name: str,
    session_id: str,
    redact: bool = True,
) -> ExtractionResult:
    """Full normalization pipeline: extract → redact → hash.

    Args:
        ide_name: Name of the source IDE.
        session_id: Session ID to extract.
        redact: Whether to apply redaction (default True).

    Returns:
        ExtractionResult with the normalized BridgeSession.
    """
    connector = get_connector(ide_name)
    result = connector.extract_session(session_id)

    # Apply redaction
    if redact:
        result.session = redact_session(result.session)
        if result.session.provenance:
            result.session.provenance.redacted = True

    # Compute content hash
    result.session.extraction_hash = result.session.compute_content_hash()

    return result


def detect_all_ides() -> list[dict[str, Any]]:
    """Detect all known IDEs on the local machine."""
    results: list[dict[str, Any]] = []
    for connector in get_all_connectors():
        try:
            results.append(connector.detection_row())
        except Exception as exc:
            results.append({
                "ide": connector.ide_name,
                "status": "error",
                "paths": [],
                "sessions": f"Error: {exc}",
            })
    return results


def list_sessions(
    ide_name: str,
    limit: int = 20,
    project_filter: str | None = None,
) -> list[dict[str, Any]]:
    """List sessions for a specific IDE."""
    connector = get_connector(ide_name)
    return connector.list_sessions(limit=limit, project_filter=project_filter)
