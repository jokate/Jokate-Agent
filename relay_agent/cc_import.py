"""Import Claude Code conversations into Agent 카태 sessions.

Claude Code keeps each conversation as JSONL under ~/.claude/projects/<cwd-slug>/<session>.jsonl.
Only what a relay needs is kept: each typed prompt and a short excerpt of the final answer to it.
Tool calls, tool output, thinking and system reminders are dropped, so an imported session costs
a few lines in the baton instead of the whole transcript.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .history import HistoryStore

PROJECTS_DIR = Path.home() / ".claude" / "projects"
ANSWER_CHARS = 400
NOISE_PREFIXES = ("<command-", "<local-command", "Caveat:", "<system-reminder>", "<bash-", "[Request interrupted")


def cwd_slug(cwd: str | Path) -> str:
    """Claude Code's project folder name: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _project_dir(project: str) -> Path:
    d = (PROJECTS_DIR / project).resolve()
    if d.parent != PROJECTS_DIR.resolve():
        raise FileNotFoundError(project)  # no path traversal outside ~/.claude/projects
    return d


def _lines(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _prompt_text(entry: dict) -> str | None:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):  # tool results come back as lists; typed prompts may too (images)
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if not texts or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(texts)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return None
    return text


def list_projects() -> list[dict]:
    out = []
    if not PROJECTS_DIR.exists():
        return out
    for d in sorted(PROJECTS_DIR.iterdir()):
        files = list(d.glob("*.jsonl")) if d.is_dir() else []
        if not files:
            continue
        cwd = None
        for entry in _lines(max(files, key=lambda p: p.stat().st_mtime)):
            if entry.get("cwd"):
                cwd = entry["cwd"]
                break
        out.append({"project": d.name, "cwd": cwd, "sessions": len(files),
                    "updated": datetime.fromtimestamp(max(p.stat().st_mtime for p in files)).isoformat(timespec="seconds")})
    return sorted(out, key=lambda p: p["updated"], reverse=True)


def list_sessions(project: str, limit: int = 30) -> list[dict]:
    d = _project_dir(project)
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        title, first, prompts, cwd = None, None, 0, None
        for entry in _lines(f):
            if entry.get("type") == "custom-title":
                title = entry.get("customTitle") or entry.get("title") or title
            cwd = cwd or entry.get("cwd")
            text = _prompt_text(entry)
            if text:
                prompts += 1
                first = first or text
        if prompts:
            out.append({"session_id": f.stem, "title": title or (first or "")[:60], "first_prompt": (first or "")[:160],
                        "prompts": prompts, "cwd": cwd, "size_kb": f.stat().st_size // 1024,
                        "updated": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")})
    return out


def extract_turns(project: str, session_id: str) -> tuple[list[dict], dict]:
    """[{at, question, answer}] plus meta {title, cwd}."""
    path = _project_dir(project) / f"{session_id}.jsonl"
    if path.parent != _project_dir(project) or not path.exists():
        raise FileNotFoundError(path)
    turns: list[dict] = []
    meta = {"title": None, "cwd": None}
    for entry in _lines(path):
        if entry.get("type") == "custom-title":
            meta["title"] = entry.get("customTitle") or entry.get("title") or meta["title"]
        meta["cwd"] = meta["cwd"] or entry.get("cwd")
        text = _prompt_text(entry)
        if text:
            turns.append({"at": (entry.get("timestamp") or "")[:19], "question": text, "answer": ""})
            continue
        if turns and entry.get("type") == "assistant" and not entry.get("isSidechain"):
            blocks = entry.get("message", {}).get("content") or []
            said = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
            if said:
                turns[-1]["answer"] = said  # keep the latest text reply for this prompt
    for t in turns:
        answer = " ".join(t["answer"].split())
        t["answer"] = answer[:ANSWER_CHARS] + ("…" if len(answer) > ANSWER_CHARS else "")
        if len(t["question"]) > 1500:
            t["question"] = t["question"][:1500] + "…"
    return turns, meta


def import_session(history: HistoryStore, project: str, session_id: str, title: str | None = None,
                   workdir: str | None = None, target_session: str | None = None) -> dict:
    """Create (or update) a 카태 session from a Claude Code conversation. Re-importing adds only new prompts."""
    turns, meta = extract_turns(project, session_id)
    return import_turns(history, session_id, turns, meta, title, workdir, target_session)


def import_turns(history: HistoryStore, source_id: str, turns: list[dict], meta: dict, title: str | None = None,
                 workdir: str | None = None, target_session: str | None = None) -> dict:
    """Store already-extracted turns. Lets a client on another machine send its compact summary
    instead of the server needing access to that machine's ~/.claude folder."""
    session_id = source_id
    prefix = f"cc-{session_id[:8]}-"
    existing = history.find_session_by_run_prefix(prefix)
    sid = target_session or existing
    if sid is None:
        sid = history.create_session(
            title or f"[Claude Code] {meta.get('title') or (turns[0]['question'][:50] if turns else session_id[:8])}",
            workdir or meta.get("cwd") or str(Path.home()),
        )["id"]
    added = 0
    for i, t in enumerate(turns, start=1):
        run_id = f"{prefix}{i:03d}"
        if history.has_turn(run_id):
            continue
        history.add_turn(sid, t["question"], "claude-code", run_id, at=t["at"] or None)
        history.update_turn(run_id, "imported", t["answer"])
        added += 1
    return {"session_id": sid, "imported": added, "total_prompts": len(turns)}


def latest_session_for_cwd(cwd: str | Path) -> tuple[str, str] | None:
    """(project, session_id) of the most recently active Claude Code conversation in this folder."""
    d = PROJECTS_DIR / cwd_slug(cwd)
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
    if files:
        return d.name, files[0].stem
    # Fallback: match the cwd recorded inside transcripts (covers slug rule differences).
    target = os.path.normcase(os.path.abspath(str(cwd)))
    newest = None
    for f in PROJECTS_DIR.glob("*/*.jsonl") if PROJECTS_DIR.exists() else []:
        for entry in _lines(f):
            if entry.get("cwd"):
                if os.path.normcase(os.path.abspath(entry["cwd"])) == target and (
                        newest is None or f.stat().st_mtime > newest.stat().st_mtime):
                    newest = f
                break
    return (newest.parent.name, newest.stem) if newest else None
