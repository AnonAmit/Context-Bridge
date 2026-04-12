"""Read-only SQLite helper for safely accessing IDE databases.

All connections use URI mode with immutable=1 to guarantee
read-only access. If the file is locked, a temporary copy
is made before opening.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any


class SQLiteReadError(Exception):
    """Raised when a SQLite database cannot be read."""


def _safe_connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite database in read-only, immutable mode.

    If the file is locked (PermissionError), copies to a temp file first.
    NEVER opens IDE databases in default write mode.
    """
    if not db_path.exists():
        raise SQLiteReadError(f"Database not found: {db_path}")

    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        # Test that the connection actually works
        conn.execute("SELECT 1")
        return conn
    except (sqlite3.OperationalError, PermissionError):
        # File may be locked by the IDE — copy to temp and retry
        return _copy_and_connect(db_path)


def _copy_and_connect(db_path: Path) -> sqlite3.Connection:
    """Copy a locked database to a temp file and open it read-only."""
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="cb_sqlite_"))
        tmp_path = tmp_dir / db_path.name
        shutil.copy2(db_path, tmp_path)

        uri = f"file:{tmp_path.as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1")
        return conn
    except Exception as exc:
        raise SQLiteReadError(
            f"Cannot open database even after copy: {db_path} — {exc}"
        ) from exc


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return f"sha256:{sha.hexdigest()}"


class SafeSQLiteReader:
    """Read-only SQLite reader with safety guarantees.

    Usage:
        reader = SafeSQLiteReader(Path("state.vscdb"))
        tables = reader.list_tables()
        rows = reader.query("SELECT * FROM ItemTable LIMIT 10")
        reader.close()

    Or as a context manager:
        with SafeSQLiteReader(path) as reader:
            ...
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.file_hash = compute_file_hash(db_path)
        self._conn = _safe_connect(db_path)

    def __enter__(self) -> SafeSQLiteReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def list_tables(self) -> list[str]:
        """List all table names in the database."""
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [row["name"] for row in rows]

    def table_schema(self, table_name: str) -> list[dict]:
        """Return column info for a table.

        Returns list of {name, type, notnull, pk} dicts.
        """
        rows = self._conn.execute(f"PRAGMA table_info([{table_name}])").fetchall()
        return [
            {
                "name": row["name"],
                "type": row["type"],
                "notnull": bool(row["notnull"]),
                "pk": bool(row["pk"]),
            }
            for row in rows
        ]

    def row_count(self, table_name: str) -> int:
        """Return the number of rows in a table."""
        result = self._conn.execute(f"SELECT COUNT(*) as cnt FROM [{table_name}]").fetchone()
        return result["cnt"] if result else 0

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a read-only query and return rows as dicts."""
        try:
            cursor = self._conn.execute(sql, params)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except sqlite3.OperationalError as exc:
            raise SQLiteReadError(f"Query failed: {sql} — {exc}") from exc

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        """Execute a query and return the first row or None."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def enumerate_keys(
        self,
        table: str,
        key_column: str = "key",
        filters: list[str] | None = None,
    ) -> list[dict]:
        """Enumerate all keys in a table, optionally filtering by substring.

        Returns list of {key, value_preview} dicts.
        """
        rows = self.query(f"SELECT * FROM [{table}]")
        results = []
        for row in rows:
            key_val = str(row.get(key_column, ""))
            if filters:
                if not any(f.lower() in key_val.lower() for f in filters):
                    continue
            # Build a preview of the value
            value_cols = [c for c in row if c != key_column]
            if value_cols:
                raw_val = str(row[value_cols[0]])
                preview = raw_val[:200] if len(raw_val) > 200 else raw_val
            else:
                preview = "—"
            results.append({"key": key_val, "value_preview": preview})
        return results
