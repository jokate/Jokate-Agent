"""Repository registry: name each working repo once, then run relays by name.

Shared definition (relay.config.yaml, in git) + this machine's path (relay.config.local.yaml):

    repos:
      mnys:
        workspace: inplace          # default workspace mode for this repo
        relay: quick                # default relay
        verify: ["uv run pytest -q"]  # verification commands: pre-approved and handed to the AI
        docs: Docs                  # docs-read MCP root (relative to the repo)
        excludes: [Plugins/Big]     # extra snapshot excludes
        notes: "UE5 C++/GAS. ..."   # short standing context (capped)
        url: https://...            # optional, for `relay repo clone`

Why it saves tokens: the verify commands and notes replace discovery turns (finding how to test,
re-reading READMEs), and pre-approved commands avoid the denied-command retry loop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

NOTES_CHARS = 400


class RepoSpec(BaseModel):
    name: str
    path: str = ""
    url: str = ""
    workspace: Literal["none", "copy", "inplace"] | None = None
    relay: str | None = None
    verify: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    docs: str | None = None
    excludes: list[str] = Field(default_factory=list)
    include_ignored: bool = False  # also snapshot files the repo's .gitignore hides
    auto_apply: bool | None = None  # copy mode: apply results automatically (None = relay default)
    hook_only: list[str] | None = None  # top-level folders not scanned (default ["Engine"]); only hook backups there
    max_snapshot_mb: float | None = None  # override the global snapshot size limit
    notes: str = ""
    # apply the folder's own CLAUDE.md (up the tree), .claude/skills, MCP servers and the commands they name
    project_context: bool = True

    @property
    def resolved(self) -> Path | None:
        return Path(self.path).expanduser().resolve() if self.path else None

    @property
    def exists(self) -> bool:
        return bool(self.resolved and self.resolved.is_dir())

    @property
    def docs_root(self) -> Path | None:
        if not self.docs or not self.resolved:
            return None
        p = Path(self.docs)
        return p if p.is_absolute() else self.resolved / p

    def verify_tools(self) -> list[str]:
        """Exact command plus the same command with extra arguments."""
        return [t for c in self.verify for t in (f"Bash({c})", f"Bash({c}:*)")]

    def prompt_lines(self, branch: str | None = None) -> list[str]:
        lines = [f"- 저장소: `{self.name}`" + (f" (브랜치 {branch})" if branch else "")]
        if self.verify:
            lines.append("- 검증 명령(미리 허용됨, 이것만 사용): " + ", ".join(f"`{c}`" for c in self.verify))
        if self.notes:
            notes = " ".join(self.notes.split())
            lines.append("- 저장소 메모: " + notes[:NOTES_CHARS] + ("…" if len(notes) > NOTES_CHARS else ""))
        return lines

    def git_info(self) -> dict:
        if not self.exists:
            return {}
        return git_info(self.resolved)


def git_info(path: Path) -> dict:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    if git("rev-parse", "--is-inside-work-tree") != "true":
        return {"git": False}
    status = git("status", "--porcelain") or ""
    return {"git": True, "branch": git("branch", "--show-current") or "(detached)",
            "head": (git("rev-parse", "--short", "HEAD") or "")[:12], "dirty": len(status.splitlines())}


def save_local_repo(local_file: Path, name: str, fields: dict | None, remove: bool = False) -> None:
    """Add/update/remove a repo entry in relay.config.local.yaml (this machine only)."""
    import yaml

    data = (yaml.safe_load(local_file.read_text(encoding="utf-8")) if local_file.exists() else None) or {}
    repos = data.setdefault("repos", {})
    if remove:
        repos.pop(name, None)
    else:
        entry = repos.setdefault(name, {})
        entry.update({k: v for k, v in (fields or {}).items() if v not in (None, "", [])})
    local_file.write_text("# This machine only (git-ignored).\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                          encoding="utf-8")


TIER = {"haiku": "경량", "sonnet": "표준", "opus": "고급", "fable": "최상급"}


class RepoRegistry:
    def __init__(self, data: dict[str, dict] | None = None):
        self.repos = {name: RepoSpec(name=name, **(spec or {})) for name, spec in (data or {}).items()}

    def get(self, name: str) -> RepoSpec:
        if name not in self.repos:
            raise KeyError(f"등록되지 않은 저장소: {name} (등록: relay repo add {name} <경로>)")
        return self.repos[name]

    def match(self, path: str | Path) -> RepoSpec | None:
        """The registered repo containing this path (deepest match)."""
        target = Path(path).expanduser().resolve()
        best = None
        for repo in self.repos.values():
            root = repo.resolved
            if root and (target == root or root in target.parents):
                if best is None or len(str(root)) > len(str(best.resolved)):
                    best = repo
        return best

    def status(self) -> list[dict]:
        out = []
        for repo in self.repos.values():
            info = repo.git_info() if repo.exists else {}
            out.append({
                "name": repo.name, "path": str(repo.resolved or ""), "exists": repo.exists, "url": repo.url,
                "workspace": repo.workspace, "relay": repo.relay, "verify": repo.verify,
                "docs": str(repo.docs_root) if repo.docs_root else None, "notes": repo.notes, **info,
            })
        return out
