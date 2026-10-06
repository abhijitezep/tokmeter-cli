"""Read-only access to local GitHub Copilot session stores.

Discovers every Copilot SQLite database on the machine, snapshots each
(WAL-safe copy of .db + -wal + -shm), ATTACHes them to an in-memory
connection with UNION ALL views, and joins the published rate table so
every query sees combined history with per-model pricing.
"""

from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

from . import pricing, vscode

DEFAULT_DB_PATH = Path.home() / ".copilot" / "session-store.db"

CANDIDATE_PATTERNS = (
    "~/.copilot/session-store.db",
    "~/Library/Application Support/Code/User/globalStorage/github.copilot-chat/session-store.db",
    "~/Library/Application Support/Code - Insiders/User/globalStorage/github.copilot-chat/session-store.db",
    "~/Library/Application Support/Code*/User/globalStorage/github.copilot*/session-store.db",
    "~/.config/Code*/User/globalStorage/github.copilot*/session-store.db",
    "~/.vscode*/copilot/session-store.db",
)

USAGE_TABLE = "assistant_usage_events"
UNIONED_TABLES = (USAGE_TABLE, "sessions")

REQUIRED_COLUMNS = {
    USAGE_TABLE: ("data_status", "id", "initiator", "finish_reason", "reasoning_effort")
}

CLI_SOURCE = "copilot-cli"
VSCODE_SOURCE = vscode.SOURCE

SOURCES = {
    CLI_SOURCE: {
        "label": "Copilot CLI / desktop",
        "has_cost": True,
        "has_cache_detail": True,
    },
    VSCODE_SOURCE: {
        "label": "VS Code chat",
        "has_cost": False,
        "has_cache_detail": False,
    },
}

USD_PER_AIU = 0.01
NANO_PER_AIU = 1_000_000_000


class SessionStoreError(RuntimeError):
    pass


def _configured_paths() -> list[Path] | None:
    raw = os.environ.get("COPILOT_DB_PATH")
    if not raw:
        return None
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def discover_stores() -> list[Path]:
    """All session stores on this machine, de-duplicated and ordered stably."""
    configured = _configured_paths()
    if configured is not None:
        return [p for p in configured if p.exists()]

    found: list[Path] = []
    seen: set[Path] = set()
    is_windows = sys.platform == "win32"

    for pattern in CANDIDATE_PATTERNS:
        expanded = Path(pattern).expanduser()
        expanded_str = str(expanded)

        if any(ch in expanded_str for ch in "*?["):
            if is_windows:
                matches = sorted(Path(p) for p in glob.glob(expanded_str))
            else:
                matches = sorted(Path(expanded.anchor or "/").glob(expanded_str.lstrip("/")))
        else:
            matches = [expanded]

        for match in matches:
            if not match.exists():
                continue
            key = match.resolve()
            if key in seen:
                continue
            seen.add(key)
            found.append(match)
    return found


def _table_columns(conn: sqlite3.Connection, alias: str, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA {alias}.table_info("{table}")').fetchall()
    return [r[1] for r in rows]


class SessionStore:
    """Queries from snapshots of every discovered store, merged via UNION ALL."""

    def __init__(self, skip_vscode: bool = False) -> None:
        self._lock = threading.Lock()
        self._tmpdir = Path(tempfile.mkdtemp(prefix="tokmeter-cli-"))
        self._signature: tuple[Any, ...] | None = None
        self._snapshots: list[tuple[Path, Path]] = []
        self._skipped: list[Path] = []
        self._skip_vscode = skip_vscode
        self._vscode_db: Path | None = None
        self._vscode_sig: tuple[Any, ...] | None = None
        self._vscode_rows: int = 0

    @property
    def sources(self) -> list[Path]:
        return [src for src, _ in self._snapshots]

    @property
    def skipped(self) -> list[Path]:
        return list(self._skipped)

    def _file_signature(self, source: Path) -> tuple[Any, ...]:
        parts: list[Any] = [str(source)]
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(source) + suffix)
            try:
                stat = candidate.stat()
                parts.append((suffix, stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                parts.append((suffix, None, None))
        return tuple(parts)

    def _copy(self, source: Path, target: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(source) + suffix)
            dst = Path(str(target) + suffix)
            if src.exists():
                shutil.copyfile(src, dst)
            elif dst.exists():
                dst.unlink()

    def _has_usage_table(self, snapshot: Path) -> bool:
        conn = sqlite3.connect(snapshot)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (USAGE_TABLE,),
            ).fetchone()
        except sqlite3.DatabaseError:
            return False
        finally:
            conn.close()
        return row is not None

    def _refresh(self) -> None:
        self._refresh_vscode()

        sources = discover_stores()
        if not sources and not self._vscode_rows:
            raise SessionStoreError(
                f"No Copilot session store found. Looked in {DEFAULT_DB_PATH} and "
                "the VS Code global storage directories. Set COPILOT_DB_PATH to "
                "point at one (or several, separated by os.pathsep)."
            )

        signature = tuple(self._file_signature(s) for s in sources)
        if signature == self._signature:
            return

        snapshots: list[tuple[Path, Path]] = []
        skipped: list[Path] = []
        for index, source in enumerate(sources):
            target = self._tmpdir / f"store{index}.db"
            self._copy(source, target)
            if self._has_usage_table(target):
                snapshots.append((source, target))
            else:
                skipped.append(source)

        if not snapshots and not self._vscode_rows:
            listed = ", ".join(str(s) for s in sources)
            raise SessionStoreError(
                f"Found {len(sources)} Copilot session store(s) ({listed}) but none "
                f"contain an '{USAGE_TABLE}' table, and no VS Code chat usage was "
                "found either."
            )

        self._snapshots = snapshots
        self._skipped = skipped
        self._signature = signature

    def _refresh_vscode(self) -> None:
        if self._skip_vscode or _configured_paths() is not None:
            self._vscode_db = None
            self._vscode_rows = 0
            return
        sig = vscode.signature()
        if sig == self._vscode_sig and self._vscode_db is not None:
            return
        target = self._tmpdir / "vscode-chat.db"
        try:
            rows = vscode.build(target)
        except (OSError, sqlite3.DatabaseError):
            self._vscode_db, self._vscode_rows = None, 0
            return
        self._vscode_db = target if rows else None
        self._vscode_rows = rows
        self._vscode_sig = sig

    def _attachments(self) -> list[tuple[str, Path]]:
        items = [(CLI_SOURCE, snapshot) for _, snapshot in self._snapshots]
        if self._vscode_db is not None:
            items.append((VSCODE_SOURCE, self._vscode_db))
        return items

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        attachments: list[tuple[str, str]] = []
        for index, (label, snapshot) in enumerate(self._attachments()):
            alias = f"s{index}"
            conn.execute("ATTACH DATABASE ? AS " + alias, (str(snapshot),))
            attachments.append((alias, label))

        for table in UNIONED_TABLES:
            present = [(a, lbl) for a, lbl in attachments if _table_columns(conn, a, table)]
            if not present:
                continue
            columns: list[str] = []
            for alias, _ in present:
                for column in _table_columns(conn, alias, table):
                    if column not in columns:
                        columns.append(column)
            columns = [c for c in columns if c != "source"]
            for required in REQUIRED_COLUMNS.get(table, ()):
                if required not in columns:
                    columns.append(required)
            selects = []
            for alias, label in present:
                available = set(_table_columns(conn, alias, table))
                projection = ", ".join(
                    (f'"{c}"' if c in available else f'NULL AS "{c}"') for c in columns
                )
                selects.append(
                    f'SELECT {projection}, \'{label}\' AS "source" FROM {alias}."{table}"'
                )
            body = " UNION ALL ".join(selects)
            if table == "sessions" and len(selects) > 1:
                body = f'SELECT * FROM ({body}) GROUP BY "id"'
            name = "_usage_raw" if table == USAGE_TABLE else table
            conn.execute(f'CREATE TEMP VIEW "{name}" AS {body}')
        self._attach_rates(conn)
        return conn

    @staticmethod
    def _attach_rates(conn: sqlite3.Connection) -> None:
        if not conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name='_usage_raw'"
        ).fetchone():
            return
        conn.execute(
            "CREATE TEMP TABLE model_rates ("
            " model TEXT PRIMARY KEY, input_usd REAL, cached_usd REAL,"
            " output_usd REAL, ceiling_input_usd REAL, cache_write_usd REAL, rate_inferred INTEGER)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO model_rates VALUES (?,?,?,?,?,?,?)", pricing.rate_rows()
        )
        conn.execute(
            f'CREATE TEMP VIEW "{USAGE_TABLE}" AS'
            " SELECT r.*, m.input_usd, m.cached_usd, m.output_usd,"
            " m.ceiling_input_usd, m.cache_write_usd, m.rate_inferred"
            " FROM _usage_raw r LEFT JOIN model_rates m ON m.model = r.model"
        )

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql, tuple(params)).fetchall()
            finally:
                conn.close()
        return [dict(row) for row in rows]
