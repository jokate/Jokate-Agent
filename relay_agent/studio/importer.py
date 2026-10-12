"""Bringing in a project that is already there (M-6): what a UE project folder holds, found by reading it.

Contract: design/studio/ARCHITECTURE.md §6.4 (`POST /studio/projects`) and §3.3. `detect` is asked by
studio/api.py `import_project`, which registers the folder as a `projects` row.

Nothing here changes the folder: files are opened for reading only, nothing is created, and no process is
started — not git either, whose `status` may refresh `.git/index`. What is missing (the MCP port, the harness)
is reported; filling it in is M-4 · M-5's.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..projctx import INSTRUCTION_FILES  # the rules files a relay run reads; projctx imports nothing of studio
from .machine import _same  # paths as two of them are compared: slashes, case, the slash at the end

INI = ("Config", "DefaultEngine.ini")
INI_SECTION = "[unrealmcp]"  # compared in lower case
INI_KEY = "port"
MCP_JSON = ".mcp.json"
PORT_ENV = "UNREAL_MCP_PORT"
HARNESS_FOLDER = "Docs"


# --- reading ----------------------------------------------------------------------------------------------
def _text(file: Path) -> str | None:
    """A file of the project as text: UTF-16 when it starts with that mark (the editor saves some ini files
    so), else UTF-8 with or without one. None when it cannot be read."""
    try:
        raw = file.read_bytes()
    except OSError:
        return None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _json(file: Path) -> Any:
    """The JSON of a file; None when it is not there or is not JSON."""
    text = _text(file)
    try:
        return json.loads(text) if text is not None else None
    except (ValueError, RecursionError):
        return None


def _port(value: Any) -> int | None:
    """A TCP port written as a number or as digits; None for anything else (true, 0, 70000, '55557.5')."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().strip('"').strip()
        value = int(text) if re.fullmatch(r"[0-9]{1,5}", text) else None
    return value if isinstance(value, int) and 1 <= value <= 65535 else None


# --- the .uproject and its engine -------------------------------------------------------------------------
def find_uproject(folder: Path) -> Path | None:
    """The `.uproject` file right under `folder` — the first by name when there are several; None when there
    is none or the folder cannot be listed."""
    try:
        found = [p for p in Path(folder).iterdir() if p.suffix.lower() == ".uproject" and p.is_file()]
    except OSError:
        return None
    return min(found, key=lambda p: (p.name.lower(), p.name)) if found else None


def engine_association(uproject: Path) -> str:
    """`EngineAssociation` of a .uproject as it is written: '5.7' for an engine of the launcher, a GUID for one
    built from source. '' when the file cannot be read or does not say."""
    data = _json(Path(uproject))
    value = data.get("EngineAssociation") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) else ""


def engine_version_of(association: str) -> str:
    """What `projects.engine_version` takes: the association when it is a version ('5.7'), '' when it is a
    GUID or empty — a GUID is not a version, and nothing here can tell which engine it names."""
    return association if re.fullmatch(r"[0-9]+\.[0-9]+", association or "") else ""


# --- the MCP port -----------------------------------------------------------------------------------------
def ini_port(folder: Path) -> int | None:
    """`Port` of the `[UnrealMCP]` section of Config/DefaultEngine.ini — the port the editor's plugin listens
    on. Read line by line: a UE ini repeats keys (`+Key=`), which configparser refuses. The last `Port=` of the
    section counts, as the engine reads it; None when there is none or it is not a port."""
    text = _text(Path(folder).joinpath(*INI))
    found, inside = None, False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("["):
            inside = line.lower() == INI_SECTION
        elif inside and not line.startswith((";", "#")):
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == INI_KEY:
                found = _port(value)
    return found


def mcp_json_port(folder: Path) -> int | None:
    """`UNREAL_MCP_PORT` in the `env` of a server of `.mcp.json` — the first server, in the file's order, that
    names one. None when no server does, the value is not a port or the file cannot be read."""
    data = _json(Path(folder) / MCP_JSON)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    for server in (servers.values() if isinstance(servers, dict) else ()):
        env = server.get("env") if isinstance(server, dict) else None
        if isinstance(env, dict) and PORT_ENV in env:
            return _port(env[PORT_ENV])
    return None


# --- git --------------------------------------------------------------------------------------------------
def in_git(folder: Path, top: Path | None = None) -> bool:
    """Is there a `.git` — a folder, or the file a worktree has — at `folder` or above it, as far up as `top`
    (the root of the registered repository)? Without `top` only `folder` itself is looked at. git is not run."""
    at = Path(folder)
    while True:
        if (at / ".git").exists():
            return True
        if top is None or at == top or top not in at.parents:
            return False
        at = at.parent


# --- the projects that are already there ------------------------------------------------------------------
def conflict(others: Iterable[dict], *, name: str, folder: Path | str, repo: str) -> tuple[str, str] | None:
    """Would a project of this folder, name and repository clash with one of `others` (the `projects` rows, the
    archived ones too — they keep their name and repository)? `(field, detail)` of the first that does, in the
    order `path` · `name` · `repo` (§6.4); None when none does. The list of projects does not show an archived
    one, so the detail says when all that hold it are archived. Asked by `POST /studio/projects` and by M-5's
    `register` — one rule for a project brought in and one made."""
    rows = list(others)
    for field, taken, detail in (
            ("path", lambda p: _same(p["path"]) == _same(folder), f"이 폴더는 이미 프로젝트로 등록돼 있습니다: {folder}"),
            ("name", lambda p: p["name"] == name, f"이름 '{name}' 은(는) 다른 프로젝트가 쓰고 있습니다"),
            ("repo", lambda p: p["repo"] == repo, f"저장소 '{repo}' 은(는) 다른 프로젝트가 쓰고 있습니다")):
        held = [p for p in rows if taken(p)]
        if held:
            note = " (보관된 프로젝트입니다)" if all(p["archived_at"] is not None for p in held) else ""
            return field, detail + note
    return None


# --- the whole answer -------------------------------------------------------------------------------------
def rules(folder: Path | str) -> list[str]:
    """The instruction files a relay run reads (`projctx.INSTRUCTION_FILES`) that are in `folder`, in that
    order. The one judgement of "the harness's rules are here": the `harness` step of a new project (M-5) and
    `detect` both ask this, so that a project the step passed is not reported as lacking them when brought in."""
    return [name for name in INSTRUCTION_FILES if (Path(folder) / name).is_file()]


def detect(folder: Path, engines: list[dict], top: Path | None = None) -> dict:
    """What `POST /studio/projects` answers as `detected` (§6.4): `{uproject, engine, mcp, harness, git,
    missing}`. `engines` is `machine.engines()` — `[{version, path, exists}]`; `top` the root of the repository
    the folder lies in. `mcp.port` is the ini's, and `.mcp.json`'s only when the ini names none: the ini is
    what the plugin in the editor reads. `mcp.in_use_by` is left None for the caller, who knows the other
    projects. `harness.claude_md` keeps its name (the key is the contract) and means: one of the rules files a
    relay run reads is there (`rules` — `CLAUDE.md`, `CLAUDE.local.md` or `.claude/CLAUDE.md`); `harness.docs`
    is the `Docs` folder. `missing` names what M-4 (`mcp`) and M-5 (`harness`) would fill in."""
    folder = Path(folder)
    uproject = find_uproject(folder)
    association = engine_association(uproject) if uproject is not None else ""
    installed = bool(association) and any(
        e.get("version") == association and bool(e.get("exists")) for e in engines)
    ini, listed = ini_port(folder), mcp_json_port(folder)
    port = ini if ini is not None else listed
    harness = {"claude_md": bool(rules(folder)), "docs": (folder / HARNESS_FOLDER).is_dir()}
    missing = [name for name, gone in (("mcp", port is None), ("harness", not all(harness.values()))) if gone]
    return {"uproject": uproject.name if uproject is not None else None,
            "engine": {"association": association, "installed": installed},
            "mcp": {"port": port, "ini": ini, "mcp_json": listed, "in_use_by": None},
            "harness": harness, "git": in_git(folder, top), "missing": missing}
