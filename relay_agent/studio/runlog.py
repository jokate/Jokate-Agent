"""Per-run record of what the run did with skills and MCP, and what the MCP guard said (S-11 ①).

Contract: design/studio/ARCHITECTURE.md §5.4 (`harness_checks[]`), §10.7 (the guard audit log, 런별 모음),
§11.2 (`ClaudeCliRunner._emit_activity` · `describe_tool` — where the events come from).

Verdict of S-11 ①: whether a headless `claude -p` run leaves the game-harness's own record (hooks, event log,
skill-call record) cannot be told from the code — the harness folder is outside this repository. So the relay
collects what it can see itself, run by run: the events the runner emits (`tool_use` with tool `Skill`,
`mcp_call`, `mcp_result`) and the lines of the guard audit log that carry the run's id. Nothing is read from
the harness folder.

The record is rebuilt in full and overwritten at every `advance()` of a run (`RunSink.idle`), so a run that
is resumed many times ends with the same record. A run the CLI drives never passes the server's
`after_advance`: it has no file (`read` -> None). `after_advance` runs outside the run lock, so two idles of one
run may overlap: writes are serialized in the process, each uses its own temp file name (pid · thread), and a
failed write leaves no temp file behind.

No FastAPI and no `studio_guard` import here: the audit lines are read as plain JSONL.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

VERSION = 1
FOLDER = "runlog"  # runs/studio/runlog/<run_id>.json
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SKILL_KEY = re.compile(r'"skill"\s*:\s*"((?:[^"\\]|\\.)*)')  # also reads a JSON cut off in the middle of the name
COMMAND_CHARS = 200
_WRITE = threading.Lock()  # one write at a time in this process (see `record`)


def skill_name(target: str) -> str:
    """The skill a `Skill` tool_use named, from its `target` (`describe_tool` keeps the input as a JSON text of
    200 characters at most). A text that is not JSON is read as `<name> …` / `/<name> …`. "" when none is found."""
    text = (target or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        m = _SKILL_KEY.search(text)
        if not m:
            return ""
        raw = m.group(1)
        try:
            raw = json.loads('"' + raw + '"')
        except ValueError:
            pass
        return str(raw).strip().lstrip("/")
    return text.split()[0].lstrip("/")


def audit_lines(runs_dir: Path | str, run_id: str) -> list[dict]:
    """The lines of the guard audit log (`<runs_dir>/studio/audit/guard-*.jsonl`) that carry this run's id.
    When the id starts with a date (`YYYYMMDD`), files of days before the previous one are not opened.
    A broken line or a file that cannot be read is skipped."""
    folder = Path(runs_dir) / "studio" / "audit"
    if not run_id or not folder.is_dir():
        return []
    floor = ""
    try:
        floor = (datetime.strptime(run_id[:8], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    except ValueError:
        pass
    lines: list[dict] = []
    for path in sorted(folder.glob("guard-*.jsonl")):
        day = path.stem[len("guard-"):]
        if floor and day.isdigit() and day < floor:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            if run_id not in raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and line.get("run_id") == run_id:
                lines.append(line)
    return lines


def collect(run_id: str, events: list[dict], audit: list[dict]) -> dict:
    """The record of the run (pure). `events` are rows of `HistoryStore.events`, `audit` the lines of the run."""
    skills: list[dict] = []
    mcp: list[dict] = []
    for e in events:
        kind, d = e.get("kind"), e.get("detail") or {}
        if not isinstance(d, dict):
            continue
        if kind == "tool_use" and d.get("tool") == "Skill":
            target = str(d.get("target") or "")
            skills.append({"at": e.get("at"), "stage": e.get("stage"), "name": skill_name(target), "target": target})
        elif kind == "mcp_call":
            mcp.append({"at": e.get("at"), "stage": e.get("stage"), "server": d.get("server"), "tool": d.get("tool"),
                        "target": str(d.get("target") or ""), "chars": None, "is_error": None})
        elif kind == "mcp_result":
            for call in mcp:  # the oldest call of the same tool that has no result yet
                if (call["chars"] is None and call["stage"] == e.get("stage")
                        and call["server"] == d.get("server") and call["tool"] == d.get("tool")):
                    call["chars"], call["is_error"] = d.get("chars"), bool(d.get("is_error"))
                    break
    guards: list[dict] = []
    judged = allowed = 0
    for a in audit:
        if a.get("run_id") != run_id:
            continue
        judged += 1
        if a.get("verdict") == "allow":
            allowed += 1
            continue
        tool = str(a.get("tool") or "")
        server = a.get("server")
        guards.append({"at": a.get("at"), "tool": tool, "mcp": tool.startswith("mcp__"),
                       "rule_id": a.get("rule_id"), "verdict": a.get("verdict"), "outcome": a.get("outcome"),
                       "command": str(a.get("command") or "")[:COMMAND_CHARS],
                       "inbox_id": server.get("inbox_id") if isinstance(server, dict) else None})
    return {"v": VERSION, "run_id": run_id, "collected_at": datetime.now().isoformat(timespec="seconds"),
            "skills": skills, "mcp": mcp, "guards": guards,
            "counts": {"skills": len(skills), "mcp_calls": len(mcp),
                       "mcp_errors": sum(1 for c in mcp if c["is_error"]),
                       "guard_judged": judged, "guard_allowed": allowed, "guard_warnings": len(guards)}}


def snapshot(engine: Any, run_id: str) -> dict:
    """The record as it is now, from the engine's event store and the audit log. Writes nothing."""
    return collect(run_id, engine.history.events(run_id), audit_lines(engine.runs_dir, run_id))


def _path(runs_dir: Path | str, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise ValueError(f"run id not accepted: {run_id!r}")
    return Path(runs_dir) / "studio" / FOLDER / f"{run_id}.json"


def record(engine: Any, run_id: str) -> dict:
    """Write the snapshot to `runs/studio/runlog/<run_id>.json` and return it.

    Compute and write happen under a process lock, one at a time, so the last writer holds the newest snapshot.
    The temp file is named per pid and thread, then `os.replace`d; if writing fails the temp file is removed, the
    old record is left alone and the exception is raised."""
    path = _path(engine.runs_dir, run_id)
    with _WRITE:
        data = snapshot(engine, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    return data


def read(runs_dir: Path | str, run_id: str) -> dict | None:
    """The stored record; None when there is none, it is broken, or it belongs to another run."""
    try:
        data = json.loads(_path(runs_dir, run_id).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) and data.get("run_id") == run_id else None
