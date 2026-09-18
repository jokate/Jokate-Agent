"""Project context: the instructions, skills, MCP servers and commands bound to a folder.

A headless stage runs with a trimmed tool list, a replaced system prompt and often a copy of the
folder, so without this it loses what a normal Claude Code session in the same folder would get:
the project's CLAUDE.md (up the tree), its .claude/skills, its MCP servers and the commands those
instructions tell the AI to run (e.g. `python Tools/mnys_q.py`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MARKERS = ("CLAUDE.md", ".claude", ".mcp.json")
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")
# `python Tools/x.py ...` in instructions -> pre-approve "python Tools/x.py" (headless runs can't ask)
COMMAND = re.compile(r"`((?:python3?|py|uv run|node|npx|pwsh|powershell|dotnet|bash|sh)\s+[^\s`]+)[^`]*`")
MAX_SCAN = 200_000  # chars per instruction/skill file


@dataclass
class ProjectContext:
    root: Path
    instructions: list[Path] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp: dict[str, dict] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)  # permissions.allow of the project's own settings

    def allowed_bash(self) -> list[str]:
        rules = [r for c in self.commands for r in (f"Bash({c})", f"Bash({c}:*)")]
        return rules + [a for a in self.allow if a.startswith("Bash")]

    def allowed_other(self) -> list[str]:
        return [a for a in self.allow if not a.startswith("Bash")] + (["Skill"] if self.skills else [])

    def contains(self, path: Path) -> bool:
        path = Path(path).resolve()
        return path == self.root or self.root in path.parents

    def prompt_lines(self, cwd: Path) -> list[str]:
        parts = []
        if self.instructions:
            parts.append("지침 " + ", ".join(f"`{p.relative_to(self.root).as_posix()}`" for p in self.instructions))
        if self.skills:
            parts.append("스킬 " + ", ".join(self.skills))
        if self.mcp:
            parts.append("MCP " + ", ".join(self.mcp))
        lines = [f"- 프로젝트 루트: `{self.root.as_posix()}`" + (f" ({' · '.join(parts)} 적용됨 — 지침을 따르고 "
                                                            f"맞는 스킬이 있으면 Skill 로 쓴다)" if parts else "")]
        if Path(cwd).resolve() != self.root and self.commands:
            lines.append(f"- 지침·스킬의 상대 경로 명령(예: `{self.commands[0]}`)은 루트에서 실행한다: "
                         f"`cd {self.root.as_posix()} && <명령>`")
        if not self.contains(cwd):
            lines.append("- 작업 디렉터리는 복사본이다. 루트는 조회·도구 실행용이고, 파일 수정은 작업 디렉터리에서만 한다.")
        return lines


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_SCAN]
    except OSError:
        return ""


def discover(path: Path, home: Path | None = None) -> ProjectContext | None:
    """Walk up from `path` like Claude Code does for CLAUDE.md (not stopping at a nested git repo such as
    MNYS/Source, but never into the home folder: ~/.claude is user-level) and take the outermost folder
    with CLAUDE.md, .claude/ or .mcp.json as the project root."""
    path = Path(path).resolve()
    home = (home or Path.home()).resolve()
    chain: list[Path] = []
    for d in [path, *path.parents]:
        if d == home or d in home.parents:
            break
        chain.append(d)
    marked = [d for d in chain if any((d / m).exists() for m in MARKERS)]
    if not marked:
        return None
    root = marked[-1]
    down = [d for d in reversed(chain) if d == root or root in d.parents]  # root .. path

    ctx = ProjectContext(root=root)
    texts = []
    for d in down:
        for name in INSTRUCTION_FILES:
            f = d / name
            if f.is_file():
                ctx.instructions.append(f)
                texts.append(_read(f))
    for d in down:
        for skill in sorted((d / ".claude" / "skills").glob("*/SKILL.md")):
            if skill.parent.name not in ctx.skills:
                ctx.skills.append(skill.parent.name)
                texts.append(_read(skill))
    for text in texts:
        for m in COMMAND.finditer(text):
            cmd = " ".join(m.group(1).split())
            script = cmd.split()[-1]
            if ("/" in script or "." in script) and "<" not in script and cmd not in ctx.commands:
                ctx.commands.append(cmd)
    for name in ("settings.json", "settings.local.json"):
        allow = (_json(root / ".claude" / name).get("permissions") or {}).get("allow") or []
        ctx.allow += [a for a in allow if isinstance(a, str) and a not in ctx.allow]
    ctx.mcp.update(_json(root / ".mcp.json").get("mcpServers") or {})
    projects = _json(home / ".claude.json").get("projects") or {}
    for key in (root.as_posix(), str(root)):
        ctx.mcp.update((projects.get(key) or {}).get("mcpServers") or {})
    return ctx
