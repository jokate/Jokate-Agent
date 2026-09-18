"""SQLite usage log: every stage call is recorded so leaks are measured, not guessed."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .runners import Usage

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    runner TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL,
    duration_ms INTEGER NOT NULL
)
"""


class UsageStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(SCHEMA)
            self._conn.commit()

    def record(self, run_id: str, stage: str, u: Usage) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (at, run_id, stage, runner, model, input_tokens, cache_creation_input_tokens,"
                " cache_read_input_tokens, output_tokens, cost_usd, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    run_id, stage, u.runner, u.model, u.input_tokens, u.cache_creation_input_tokens,
                    u.cache_read_input_tokens, u.output_tokens, u.cost_usd, u.duration_ms,
                ),
            )
            self._conn.commit()

    def cost_since(self, runner: str, since_iso: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE runner = ? AND at >= ?", (runner, since_iso)
            ).fetchone()
        return float(row[0])

    def by_runner_since(self, since_iso: str) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT runner, COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS cost_usd, "
                "SUM(input_tokens + cache_creation_input_tokens + cache_read_input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens FROM usage WHERE at >= ? GROUP BY runner",
                (since_iso,),
            ).fetchall()
        return {r["runner"]: dict(r) for r in rows}

    def summary(self, run_id: str | None = None) -> list[dict]:
        """Totals grouped by stage and model. cache_hit_ratio = cache_read / total input."""
        where, params = ("WHERE run_id = ?", (run_id,)) if run_id else ("", ())
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT stage, model, COUNT(*) AS calls,
                    SUM(input_tokens) AS input_tokens,
                    SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
                    SUM(cache_read_input_tokens) AS cache_read_input_tokens,
                    SUM(output_tokens) AS output_tokens,
                    ROUND(SUM(COALESCE(cost_usd, 0)), 4) AS cost_usd
                FROM usage {where} GROUP BY stage, model ORDER BY cost_usd DESC""",
                params,
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            total = d["input_tokens"] + d["cache_creation_input_tokens"] + d["cache_read_input_tokens"]
            d["cache_hit_ratio"] = round(d["cache_read_input_tokens"] / total, 3) if total else 0.0
            out.append(d)
        return out
