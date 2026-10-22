"""Every Claude Code tool name a studio task run is given (owner's decision of 2026-10-22: an employee run through
the app has the abilities of Claude Code used directly on a PC). One place: `api.guard_stage` hands this list to the
engine, which writes it to both `--tools` and `--allowedTools` of every stage of a task run, whatever `tools` the
relay's stage lists. What the tools may do is still judged by the PreToolUse hook (`guard.py` · `studio_guard.py`)."""
from __future__ import annotations

from pathlib import Path

FULL_TOOLS: list[str] = [
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Bash", "BashOutput", "KillShell",
    "Grep", "Glob",
    "WebSearch", "WebFetch",
    "Task", "TodoWrite", "Skill", "ToolSearch", "ExitPlanMode",
]

# What a chat or read turn is given (T3): the tools that write nothing. No Write · Edit · MultiEdit · NotebookEdit · Bash,
# and no Task either: a sub-agent could inherit a write tool. MCP servers are attached by the engine's own rule (all of
# them, unless the run was narrowed to `chat_mcp`). The hook still judges every call, so a secret file stays unreadable.
READ_TOOLS: list[str] = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "Skill", "ToolSearch", "TodoWrite"]


def read_dirs(home: Path | None = None) -> list[str]:
    """Folders outside the work folder that a chat or read turn may read (`--add-dir`): the user's harness skills and
    plugins, only those that exist. Not all of ~/.claude: its other folders hold the owner's past conversations."""
    base = (home or Path.home()) / ".claude"
    return [(base / name).as_posix() for name in ("skills", "plugins") if (base / name).is_dir()]
