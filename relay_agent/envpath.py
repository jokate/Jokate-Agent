"""Pick up tools installed after this process started.

A Windows process keeps the PATH it was started with. A server started before `claude` (or uv, git, ...)
was installed never finds it — and everything it launches, restarts included, inherits the same stale PATH.
refresh() appends the PATH entries saved in the registry (machine + user) that this process is missing.
Standard library only: the supervisor uses it too.
"""

from __future__ import annotations

import os
from pathlib import Path


def _registry_path() -> list[str]:
    if os.name != "nt":
        return []
    import winreg

    keys = [(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment")]
    entries: list[str] = []
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        entries += [os.path.expandvars(p) for p in str(value).split(os.pathsep) if p.strip()]
    return entries


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.strip().strip('"')))


def refresh(extra: list[str] | None = None) -> list[str]:
    """Append missing registry PATH entries (and `extra`) to os.environ["PATH"]. Returns what was added."""
    current = os.environ.get("PATH", "").split(os.pathsep)
    seen = {_norm(p) for p in current if p}
    added = []
    for entry in _registry_path() + (extra or []):
        if entry and _norm(entry) not in seen and Path(entry).is_dir():
            seen.add(_norm(entry))
            added.append(entry)
    if added:
        os.environ["PATH"] = os.pathsep.join([p for p in current if p] + added)
    return added


def claude_install_dirs() -> list[str]:
    """Where the Claude Code installers put `claude` (in case the installer didn't get it onto PATH)."""
    home = Path.home()
    dirs = [home / ".local" / "bin"]
    if os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / "npm")
    return [str(d) for d in dirs if any((d / n).exists() for n in ("claude.exe", "claude.cmd", "claude"))]
