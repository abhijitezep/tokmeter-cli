"""Read Copilot Chat token usage from VS Code chat session logs.

VS Code persists chat history per workspace under
workspaceStorage/<id>/chatSessions/. Older sessions are a single JSON
document; newer ones are an append-only JSONL journal where each line
mutates the session state:

    {"kind": 0, "v": {...}}          full snapshot, replaces state
    {"kind": 1, "k": [...], "v": x}  set state at path
    {"kind": 2, "k": [...], "v": [x]} append to the array at path

Token counts arrive in later patch lines, so the file must be replayed
in order.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SOURCE = "vscode-chat"

ROOTS_ENV_VAR = "VSCODE_CHAT_PATH"

WORKSPACE_ROOTS = (
    "~/Library/Application Support/Code/User/workspaceStorage",
    "~/Library/Application Support/Code - Insiders/User/workspaceStorage",
    "~/.config/Code/User/workspaceStorage",
    "~/.config/Code - Insiders/User/workspaceStorage",
    "~/AppData/Roaming/Code/User/workspaceStorage",
    "~/AppData/Roaming/Code - Insiders/User/workspaceStorage",
    "~/.vscode-server/data/User/workspaceStorage",
    "~/.vscode-server-insiders/data/User/workspaceStorage",
)

USAGE_DDL = """
CREATE TABLE assistant_usage_events (
  session_id              TEXT,
  turn_index              INTEGER,
  model                   TEXT,
  input_tokens            INTEGER,
  output_tokens           INTEGER,
  cache_read_tokens       INTEGER,
  cache_write_tokens      INTEGER,
  reasoning_tokens        INTEGER,
  total_nano_aiu          INTEGER,
  request_multiplier      REAL,
  duration_ms             INTEGER,
  time_to_first_token_ms  INTEGER,
  token_details_json      TEXT,
  data_status             TEXT,
  created_at              TEXT
)
"""

SESSIONS_DDL = """
CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,
  summary     TEXT,
  repository  TEXT,
  branch      TEXT,
  created_at  TEXT
)
"""

_FORBIDDEN_KEYS = {"__proto__", "constructor", "prototype"}


def _configured_roots() -> tuple[str, ...] | None:
    raw = os.environ.get(ROOTS_ENV_VAR)
    if not raw:
        return None
    return tuple(p for p in raw.split(os.pathsep) if p.strip())


def _safe(keys: list[Any]) -> bool:
    return not any(isinstance(k, str) and k in _FORBIDDEN_KEYS for k in keys)


def _set_at(state: Any, keys: list[Any], value: Any) -> None:
    if not keys or not _safe(keys):
        return
    cur = state
    for key in keys[:-1]:
        if isinstance(key, int) and isinstance(cur, list):
            while len(cur) <= key:
                cur.append(None)
            if not isinstance(cur[key], (dict, list)):
                cur[key] = {}
            cur = cur[key]
        elif isinstance(cur, dict):
            nxt = cur.get(key)
            if not isinstance(nxt, (dict, list)):
                nxt = {}
                cur[key] = nxt
            cur = nxt
        else:
            return
    last = keys[-1]
    if isinstance(cur, list) and isinstance(last, int):
        while len(cur) <= last:
            cur.append(None)
        cur[last] = value
    elif isinstance(cur, dict):
        cur[last] = value


def _append_at(state: Any, keys: list[Any], items: Any) -> None:
    if not _safe(keys) or not isinstance(items, list):
        return
    cur = state
    for key in keys:
        if isinstance(key, int) and isinstance(cur, list):
            if key >= len(cur):
                return
            cur = cur[key]
        elif isinstance(cur, dict):
            nxt = cur.get(key)
            if not isinstance(nxt, list):
                nxt = []
                cur[key] = nxt
            cur = nxt
        else:
            return
    if isinstance(cur, list):
        cur.extend(items)


def reconstruct(path: Path) -> dict[str, Any] | None:
    """Replay a chat session file into its final state."""
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    state: Any = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                kind = entry.get("kind")
                if kind == 0:
                    state = entry.get("v") or {}
                elif kind == 1:
                    _set_at(state, entry.get("k") or [], entry.get("v"))
                elif kind == 2:
                    _append_at(state, entry.get("k") or [], entry.get("v"))
    except OSError:
        return None
    return state if isinstance(state, dict) and state else None


def session_files(roots: tuple[str, ...] | None = None) -> Iterator[Path]:
    effective = roots if roots is not None else _configured_roots() or WORKSPACE_ROOTS
    for root in effective:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for chat_dir in base.glob("*/chatSessions"):
            for path in sorted(chat_dir.iterdir()):
                if path.suffix in (".json", ".jsonl") and path.is_file():
                    yield path


def _iso(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    return (
        datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{int(timestamp % 1000):03d}Z"
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def classify(req: dict[str, Any], prompt: int | None, completion: int | None) -> str:
    """Classify a request's data completeness."""
    if prompt is not None and completion is not None:
        return "complete"
    if completion is not None:
        return "partial"
    if prompt is not None:
        return "prompt-only"
    result = req.get("result")
    if not isinstance(result, dict):
        return "pending"
    if result.get("errorDetails"):
        return "errored"
    return "no-data"


def _first_prompt(requests: list[Any]) -> str | None:
    for req in requests:
        if not isinstance(req, dict):
            continue
        message = req.get("message")
        text = message.get("text") if isinstance(message, dict) else None
        text = " ".join(str(text or "").split())
        if text:
            return text[:77] + "…" if len(text) > 78 else text
    return None


def extract(state: dict[str, Any], path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pull a session row and its usage rows out of a replayed session."""
    session_id = str(state.get("sessionId") or path.stem)
    requests = state.get("requests")
    rows: list[dict[str, Any]] = []
    if not isinstance(requests, list):
        return {}, rows

    for index, req in enumerate(requests):
        if not isinstance(req, dict):
            continue
        result = req.get("result") if isinstance(req.get("result"), dict) else {}
        meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}

        prompt = _int(meta.get("promptTokens"))
        completion = _int(req.get("completionTokens")) or _int(meta.get("outputTokens"))
        status = classify(req, prompt, completion)

        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        model = str(req.get("modelId") or "").strip()
        if model.startswith("copilot/"):
            model = model[len("copilot/"):]

        rows.append(
            {
                "session_id": session_id,
                "turn_index": index,
                "model": model or "unknown",
                "input_tokens": prompt,
                "output_tokens": completion,
                "created_at": _iso(req.get("timestamp")),
                "duration_ms": _int(timings.get("totalElapsed")),
                "time_to_first_token_ms": _int(timings.get("firstProgress")),
                "data_status": status,
            }
        )

    if not rows:
        return {}, rows

    session = {
        "id": session_id,
        "summary": str(state.get("customTitle") or "").strip() or _first_prompt(requests),
        "created_at": next((r["created_at"] for r in rows if r["created_at"]), None),
    }
    return session, rows


def build(target: Path, roots: tuple[str, ...] | None = None) -> int:
    """Materialise VS Code chat usage into a SQLite file. Returns row count."""
    if target.exists():
        target.unlink()

    conn = sqlite3.connect(target)
    try:
        conn.execute(USAGE_DDL)
        conn.execute(SESSIONS_DDL)
        usage: list[dict[str, Any]] = []
        sessions: dict[str, dict[str, Any]] = {}

        for path in session_files(roots):
            state = reconstruct(path)
            if not state:
                continue
            session, rows = extract(state, path)
            if not rows:
                continue
            usage.extend(rows)
            if session:
                sessions.setdefault(session["id"], session)

        if usage:
            columns = [
                "session_id", "turn_index", "model", "input_tokens", "output_tokens",
                "duration_ms", "time_to_first_token_ms", "created_at", "data_status",
            ]
            conn.executemany(
                f"INSERT INTO assistant_usage_events ({','.join(columns)}) "
                f"VALUES ({','.join('?' * len(columns))})",
                [tuple(r.get(c) for c in columns) for r in usage],
            )
            conn.executemany(
                "INSERT INTO sessions (id, summary, created_at) VALUES (?,?,?)",
                [(s["id"], s["summary"], s["created_at"]) for s in sessions.values()],
            )
            conn.commit()
        return len(usage)
    finally:
        conn.close()


def signature(roots: tuple[str, ...] | None = None) -> tuple[Any, ...]:
    """Cheap change-detection over every chat session file."""
    parts: list[Any] = []
    for path in session_files(roots):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(parts)
