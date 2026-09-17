"""PreToolUse hook: back up a file right before an AI edit tool changes it.

    python backup_hook.py <run_dir> <project_root>

Claude Code runs this before Edit/Write/MultiEdit/NotebookEdit and passes the tool call as JSON on
stdin. The first time a path is touched in a run, its current bytes are copied to
<run_dir>/baseline/<rel> (or it is recorded as not existing yet). Only touched files are ever
copied, so a 14 GB project costs as much as the handful of files the AI edits.

Never blocks the edit: every failure exits 0.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def backup(run_dir: Path, root: Path, file_path: str) -> str | None:
    target = Path(file_path)
    if not target.is_absolute():
        target = root / target
    try:
        rel = target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None  # outside the project: not ours to track
    touched = run_dir / "touched.jsonl"
    known = set()
    if touched.exists():
        known = {json.loads(line)["rel"] for line in touched.read_text(encoding="utf-8").splitlines() if line.strip()}
    if rel in known or (run_dir / "baseline" / rel).exists():
        return rel
    existed = target.is_file()
    if existed:
        dest = run_dir / "baseline" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)
    with touched.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"rel": rel, "existed": existed}, ensure_ascii=False) + "\n")
    return rel


def main() -> None:
    try:
        run_dir, root = Path(sys.argv[1]), Path(sys.argv[2])
        data = json.loads(sys.stdin.read() or "{}")
        tool_input = data.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if path:
            backup(run_dir, root, path)
    except Exception:  # noqa: BLE001 - a backup problem must never stop the AI's work
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
