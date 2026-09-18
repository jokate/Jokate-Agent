"""Sessions, questions (turns), and the activity event log.

- session: a line of work that spans many requests (e.g. "HON 캐릭터 작업")
- turn:    one question/request the user made in a session, linked to its relay run
- event:   what actually happened inside a run (stage start/end, each tool call, gates)

A new run in a session starts with a short summary of the earlier turns, not their transcripts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workdir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    at TEXT NOT NULL,
    question TEXT NOT NULL,
    relay TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    stage TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_state (
    name TEXT PRIMARY KEY,
    exhausted_until TEXT,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS provider_limits (
    name TEXT NOT NULL,
    window TEXT NOT NULL,
    utilization REAL,
    resets_at REAL,
    status TEXT,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (name, window)
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id);
"""

CONTEXT_TURNS = 8
QUESTION_CHARS = 160
RESULT_CHARS = 200


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HistoryStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
            if "repo" not in columns:  # migration for databases created before repositories existed
                self._conn.execute("ALTER TABLE sessions ADD COLUMN repo TEXT")
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # --- sessions ------------------------------------------------------------
    def create_session(self, title: str, workdir: str, repo: str | None = None) -> dict:
        sid = datetime.now().strftime("s%Y%m%d-") + uuid.uuid4().hex[:6]
        now = _now()
        self._exec("INSERT INTO sessions (id, title, workdir, created_at, updated_at, repo) VALUES (?,?,?,?,?,?)",
                   (sid, title, workdir, now, now, repo))
        return self.get_session(sid)

    def get_session(self, session_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self) -> list[dict]:
        return self._rows(
            """SELECT s.*, COUNT(t.id) AS turns,
                      (SELECT question FROM turns WHERE session_id = s.id ORDER BY id DESC LIMIT 1) AS last_question
               FROM sessions s LEFT JOIN turns t ON t.session_id = s.id
               GROUP BY s.id ORDER BY s.updated_at DESC"""
        )

    # --- turns ---------------------------------------------------------------
    def add_turn(self, session_id: str, question: str, relay: str, run_id: str, at: str | None = None) -> None:
        now = _now()
        self._exec(
            "INSERT INTO turns (session_id, at, question, relay, run_id, status) VALUES (?,?,?,?,?,?)",
            (session_id, at or now, question, relay, run_id, "pending"),
        )
        self._exec("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))

    def has_turn(self, run_id: str) -> bool:
        return bool(self._rows("SELECT 1 FROM turns WHERE run_id = ?", (run_id,)))

    def find_session_by_run_prefix(self, prefix: str) -> str | None:
        rows = self._rows("SELECT session_id FROM turns WHERE run_id LIKE ? LIMIT 1", (prefix + "%",))
        return rows[0]["session_id"] if rows else None

    def update_turn(self, run_id: str, status: str, result: str | None = None) -> None:
        if result is None:
            self._exec("UPDATE turns SET status = ? WHERE run_id = ?", (status, run_id))
        else:
            self._exec("UPDATE turns SET status = ?, result = ? WHERE run_id = ?", (status, result, run_id))

    def delete_session(self, session_id: str) -> list[str]:
        """Remove a session with its turns and activity log. Returns the run ids it held."""
        runs = [r["run_id"] for r in self.turns(session_id)]
        for run_id in runs:
            self._exec("DELETE FROM events WHERE run_id = ?", (run_id,))
        self._exec("DELETE FROM turns WHERE session_id = ?", (session_id,))
        self._exec("DELETE FROM sessions WHERE id = ?", (session_id,))
        return runs

    def turns(self, session_id: str) -> list[dict]:
        return self._rows("SELECT * FROM turns WHERE session_id = ? ORDER BY id", (session_id,))

    def search_turns(self, query: str, limit: int = 30) -> list[dict]:
        like = f"%{query}%"
        return self._rows(
            """SELECT t.*, s.title AS session_title FROM turns t JOIN sessions s ON s.id = t.session_id
               WHERE t.question LIKE ? OR t.result LIKE ? ORDER BY t.id DESC LIMIT ?""",
            (like, like, limit),
        )

    def session_context(self, session_id: str, exclude_run: str | None = None) -> list[str]:
        """One line per earlier turn, newest last. This is all a new run inherits from the session."""
        rows = self._rows(
            "SELECT * FROM turns WHERE session_id = ? AND run_id != ? ORDER BY id DESC LIMIT ?",
            (session_id, exclude_run or "", CONTEXT_TURNS),
        )
        lines = []
        for t in reversed(rows):
            # Every stage of every later run re-sends these lines, so keep each one short.
            question = " ".join(t["question"].split())
            question = question[:QUESTION_CHARS] + ("…" if len(question) > QUESTION_CHARS else "")
            result = " ".join(t["result"].split())[:RESULT_CHARS]
            lines.append(f"[{t['at'][:16]}] Q: {question} → {t['status']}: {result} (run {t['run_id']})")
        return lines

    # --- events --------------------------------------------------------------
    def add_event(self, run_id: str, stage: str, kind: str, detail: dict) -> None:
        self._exec(
            "INSERT INTO events (run_id, at, stage, kind, detail) VALUES (?,?,?,?,?)",
            (run_id, _now(), stage, kind, json.dumps(detail, ensure_ascii=False)),
        )

    # --- providers -------------------------------------------------------------
    def set_provider_exhausted(self, name: str, until: str | None, reason: str = "") -> None:
        self._exec(
            "INSERT INTO provider_state (name, exhausted_until, reason) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET exhausted_until = excluded.exhausted_until, reason = excluded.reason",
            (name, until, reason),
        )

    def provider_exhausted_until(self, name: str) -> str | None:
        rows = self._rows("SELECT exhausted_until FROM provider_state WHERE name = ?", (name,))
        until = rows[0]["exhausted_until"] if rows else None
        if until and until > datetime.now(timezone.utc).isoformat(timespec="seconds"):
            return until
        return None

    def record_limits(self, name: str, status: str | None, windows: list[dict]) -> None:
        now = _now()
        for w in windows:
            self._exec(
                "INSERT INTO provider_limits VALUES (?,?,?,?,?,?) ON CONFLICT(name, window) DO UPDATE SET "
                "utilization = excluded.utilization, resets_at = excluded.resets_at, status = excluded.status, "
                "captured_at = excluded.captured_at",
                (name, w["window"], w.get("utilization"), w.get("resets_at"), status, now),
            )

    def limits(self, name: str) -> list[dict]:
        return self._rows("SELECT * FROM provider_limits WHERE name = ? ORDER BY window", (name,))

    def events_of_kinds(self, kinds: list[str], after_id: int = 0, limit: int = 50) -> list[dict]:
        """Across all runs, e.g. finished/failed/waiting — the dashboard's alert feed."""
        marks = ",".join("?" * len(kinds))
        rows = self._rows(
            f"""SELECT e.*, s.title AS session_title, t.session_id AS session_id, t.question AS question
                FROM events e LEFT JOIN turns t ON t.run_id = e.run_id LEFT JOIN sessions s ON s.id = t.session_id
                WHERE e.kind IN ({marks}) AND e.id > ? ORDER BY e.id DESC LIMIT ?""",
            (*kinds, after_id, limit))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows[::-1]

    def last_event_id(self) -> int:
        rows = self._rows("SELECT COALESCE(MAX(id), 0) AS m FROM events")
        return rows[0]["m"]

    def events(self, run_id: str, after_id: int = 0) -> list[dict]:
        rows = self._rows("SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after_id))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows
