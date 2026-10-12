"""The harness and the registration of a new UE project (M-5): game-harness's two batch files run on the
project's folder, and the folder made a repository of the relay, a git repository and a `projects` row.

Contract: design/studio/ARCHITECTURE.md §6.4 (`POST /studio/projects/create`), §7.4 (`job.step`: … → `harness` →
`register`), §3.3 (a project's `repo` is in the RepoRegistry before the row goes in) and §11.1. The route, the
job and its events are M-8's; here are the two steps, of the same shape as `creator.folder` and `creator.build`:
each is handed the function that runs a process and the one that hears a line of log, and answers with a
`StepResult` instead of raising. Nothing is ever deleted here.

Nothing here imports api, cli or server, and nothing reads or writes the settings of this repository: the
folder of game-harness is an argument (`Config.studio_harness_root`, given by the caller), and so are the
settings file the repository is written to and the function that makes the server read it again. `relay repo
add` and `server._reload_repos` are tied to `ROOT/relay.config.local.yaml`; M-8 hands those in, a test hands in
its own.

What could not be read from here, and is therefore assumed (design/studio/VERIFICATION.md): that the two batch
files take the project's folder as their one argument and may be started from the harness's folder, and what
MNYS has in its `.gitignore` and `.gitattributes` — `defaults/gitignore` and `defaults/gitattributes` are
general UE rules until they are compared.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from ..projctx import INSTRUCTION_FILES
from ..repos import clear_local_repo_field, save_local_repo
from . import importer
from .creator import TAIL_LINES, Log, Run, Spec, StepResult, _entries, _quiet, command_line, run_process
from .machine import _same
from .plugins import DEFAULT_SERVER

BOOTSTRAP, INDEX_BUILD = "bootstrap.bat", "index_build.bat"  # of game-harness, right under its folder
ROOT_SETTING = "studio_harness_root"
WORKSPACE, DOCS = "inplace", "Docs"  # `relay repo add <name> <path> --workspace inplace --docs Docs`
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # the names `POST /repos` takes (server.register_repo)
GIT_INIT = ("git", "init")
DEFAULTS = Path(__file__).with_name("defaults")
# The file a project is given -> the file of this repository it is a copy of. Those have no dot in front, so
# that they are not rules of this repository.
GIT_DEFAULTS = ((".gitignore", DEFAULTS / "gitignore"), (".gitattributes", DEFAULTS / "gitattributes"))


@dataclass
class RegisterResult(StepResult):
    """What `register` did: a `StepResult` with `repo` — the name the repository was saved under, '' until it
    was saved and kept when a later part failed (a cleanup takes it out with `save_local_repo(remove=True)`) —
    and `project`, the row that was put in, None when none was."""
    repo: str = ""
    project: dict | None = None


# --- the harness --------------------------------------------------------------------------------------------
def locate(harness_root: Any) -> Path:
    """The folder of game-harness the setting names, with both batch files in it; `ValueError` — its text says
    which setting or which path — when it is not set, not absolute, not a folder or lacks one of the two."""
    text = str(harness_root or "").strip()
    if not text:
        raise ValueError(f"하네스 저장소 경로가 설정돼 있지 않다: relay.config.local.yaml 의 `{ROOT_SETTING}`")
    home = Path(text)
    if not home.is_absolute():
        raise ValueError(f"`{ROOT_SETTING}` 는 절대 경로여야 한다: {text}")
    if not home.is_dir():
        raise ValueError(f"하네스 저장소 폴더가 없다 (`{ROOT_SETTING}`): {home}")
    for name in (BOOTSTRAP, INDEX_BUILD):
        if not (home / name).is_file():
            raise ValueError(f"하네스 저장소에 스크립트가 없다: {home / name}")
    return home


def commands(spec: Spec, harness_root: Path | str) -> list[list[str]]:
    """`bootstrap.bat <folder>`, then `index_build.bat <folder>`: each batch file started as it is (no
    `cmd /c`), with the project's folder as its one argument."""
    home = Path(harness_root)
    return [[str(home / BOOTSTRAP), str(spec.root)], [str(home / INDEX_BUILD), str(spec.root)]]


def rules(folder: Path) -> list[str]:
    """The instruction files a relay run reads (`projctx.INSTRUCTION_FILES`) that are in `folder`: what
    `importer.rules` says, so that this step and `importer.detect` judge a folder alike."""
    return importer.rules(folder)


def harness(spec: Spec, harness_root: Any, *, enabled: bool = True, run: Run = run_process,
            log: Log = _quiet) -> StepResult:
    """Step `harness`: install the harness in the project (`bootstrap.bat`), then build its index
    (`index_build.bat`) — the second only when the first ended with 0. Both are run in the harness's folder.
    `enabled` is the `harness` of the request: when it is off nothing is run and the result is `skipped`, also
    when no folder is set. A folder that is not set or lacks a batch file, or a project folder that is not
    there, fails before anything is run. Two commands that ended with 0 and left none of the instruction files
    a relay run reads fail as well: without one the harness is off. `created` is what appeared right under the
    project's folder, also when a command failed."""
    result = StepResult("harness")
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def heard(line: str) -> None:
        tail.append(line)
        log(line)

    if not enabled:
        result.skipped = True
        heard("하네스: 요청에서 꺼져 있다 — 건너뛴다")
        result.tail = list(tail)
        return result
    try:
        home = locate(harness_root)
        if not spec.root.is_dir():
            raise ValueError(f"프로젝트 폴더가 없다(folder 단계가 먼저다): {spec.root}")
    except ValueError as e:
        result.error = str(e)
    else:
        before = _entries(spec.root)
        for label, argv in zip(("하네스 설치", "인덱스 빌드"), commands(spec, home)):
            heard(f"> {command_line(argv)}")
            try:
                code = run(argv, cwd=home, log=heard)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                result.error = f"{label}: 명령을 시작하지 못했다: {e}"
                break
            if code != 0:
                result.error = f"{label}: 명령이 exit {code} 로 끝났다"
                break
        result.created = sorted(str(p) for p in _entries(spec.root) - before)
        if not result.error:
            found = rules(spec.root)
            if found:
                heard(f"규칙 파일: {' · '.join(found)}")
            else:
                result.error = (f"{BOOTSTRAP} 이 규칙 파일을 만들지 않았다 ({' · '.join(INSTRUCTION_FILES)} 중 "
                                f"하나가 있어야 릴레이 런이 읽는다): {spec.root}")
    if result.error:
        heard(result.error)
    result.tail = list(tail)
    return result


# --- the registration ---------------------------------------------------------------------------------------
def register(spec: Spec, store: Any, *, local_file: Path | str, reload: Callable[[], Any],
             mcp: Sequence[str] = (DEFAULT_SERVER,), run: Run = run_process, log: Log = _quiet) -> RegisterResult:
    """Step `register`, in this order: the checks, the repository, git, the row.

    The checks make nothing: the folder has a `.uproject`; no other repository has the name or the folder; no
    project (an archived one too) has the folder, the name or the repository (`importer.conflict`); no project
    that is not archived has the MCP port.
    The repository is what `relay repo add <name> <path> --workspace inplace --docs Docs` writes, in
    `local_file`, with `mcp` (the names of the MCP servers of its runs; `unreal`) added — only when the folder
    names an MCP port (`importer.detect`): without a server of its own the name would reach the `unreal` of
    another project. `reload()` then makes the server read its settings again and gives its RepoRegistry; the
    repository has to be in it with those values (§3.3 — before the row goes in; §11.1 — without a restart).
    git: `git init` unless there is a `.git`, then `.gitignore` and `.gitattributes` from `defaults/` unless
    they are there. No commit is made and LFS is not set up.
    The row: `store.create_project` with the detected port and engine version, and `project.updated`.

    `created` is what was made in the folder (`.git`, the two files); `repo` and `project` say what was made
    outside it. Nothing is taken back when a later part fails. `local_file` and `reload` have no default on
    purpose: this module never names the settings of this repository. `reload` is the one piece of code handed
    in from outside, so whatever it raises — a `TypeError` of a wrong setting too — and an answer that is no
    RepoRegistry (`server._reload_repos` gives None) end the step with an error instead of going up
    (`_reloaded`); a bug of this module is not caught that way."""
    result = RegisterResult("register")
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def heard(line: str) -> None:
        tail.append(line)
        log(line)

    try:
        result.error = _register(spec, store, Path(local_file), reload, mcp, run, heard, result)
    except (OSError, ValueError, KeyError, sqlite3.Error, yaml.YAMLError) as e:
        result.error = f"등록하지 못했다: {e}"
    if result.error:
        heard(result.error)
    result.tail = list(tail)
    return result


def _reloaded(reload: Callable[[], Any]) -> Any:
    """What `reload()` gives, as a RepoRegistry: `ValueError` — which `register` turns into the step's error —
    when it raises anything at all (it reads settings this module does not know) or gives something without
    `repos` and `match`."""
    try:
        registry = reload()
    except Exception as e:  # noqa: BLE001 - code of the caller's: no failure of it may leave the step as a raise
        raise ValueError(f"설정을 다시 읽지 못했다 ({type(e).__name__}): {e}") from e
    if not isinstance(getattr(registry, "repos", None), dict) or not callable(getattr(registry, "match", None)):
        raise ValueError(f"reload 가 RepoRegistry 를 돌려주지 않았다: {type(registry).__name__}")
    return registry


def repo_clash(registry: Any, name: str, root: Path | str) -> tuple[str, str] | None:
    """Would a repository `name` of the folder `root` clash with one of `registry` (a RepoRegistry)?
    `(field, text)` of the first that does — `repo`: the name is registered for another folder (an entry of
    another folder is not written over); `path`: the folder is registered under another name — or None. An
    entry of the same name and folder is no clash: `register` writes it again. Asked by `register` and, before
    a job is started, by M-8 (jobs.py `admit`)."""
    for repo in registry.repos.values():
        here = _same(repo.resolved) == _same(root)
        if repo.name == name and not here:
            return "repo", f"저장소 이름 '{name}' 은(는) 이미 다른 경로로 등록돼 있다: {repo.path or '(경로 없음)'}"
        if repo.name != name and here:
            return "path", f"이 폴더는 이미 저장소 '{repo.name}' (으)로 등록돼 있다: {root}"
    return None


def _register(spec: Spec, store: Any, local_file: Path, reload: Callable[[], Any], mcp: Sequence[str],
              run: Run, heard: Log, result: RegisterResult) -> str:
    """The parts of `register`; '' or the error that ended it."""
    name, root = spec.name, spec.root.resolve()
    found = importer.detect(root, [], top=root)
    port = found["mcp"]["port"]

    # --- the checks: nothing is made before all of them pass
    if found["uproject"] is None:
        return f"프로젝트 폴더에 .uproject 파일이 없다: {root}"
    if not REPO_NAME.fullmatch(name):
        return f"저장소 이름으로 쓸 수 없다 (영문 · 숫자 · -_. 40자 이내): {name}"
    known = _reloaded(reload)
    taken = repo_clash(known, name, root)
    if taken is not None:
        return taken[1]
    again = any(repo.name == name for repo in known.repos.values())  # no clash: the entry of this folder
    others = store.list_projects(include_archived=True)
    clash = importer.conflict(others, name=name, folder=root, repo=name)
    if clash is not None:
        return clash[1]
    holder = next((p for p in others
                   if port is not None and p["mcp_port"] == port and p["archived_at"] is None), None)
    if holder is not None:  # a project brought in is registered without the port; one made here is not made
        return f"MCP 포트 {port} 은(는) 프로젝트 '{holder['name']}' 이(가) 쓰고 있다"

    # --- the repository, and the server reading it
    servers = [server for server in mcp if server] if port is not None else []
    save_local_repo(local_file, name, {"path": root.as_posix(), "workspace": WORKSPACE, "docs": DOCS,
                                       "mcp": servers})
    if not servers:
        clear_local_repo_field(local_file, name, "mcp")  # save skips empty values: one left by an earlier try
    result.repo = name
    heard(f"저장소 등록{' (있던 항목)' if again else ''}: {name} -> {root.as_posix()} "
          f"(workspace {WORKSPACE}, docs {DOCS}, mcp {' · '.join(servers) if servers else '없음'})")
    if not servers:
        heard("MCP: 폴더에서 포트를 찾지 못했다 — mcp 를 넣지 않는다")
    registry = _reloaded(reload)
    seen, home = registry.repos.get(name), registry.match(root)
    if seen is None or _same(seen.resolved) != _same(root) or home is None or home.name != name:
        return f"등록한 저장소 '{name}' 이(가) RepoRegistry 에 보이지 않는다 (reload 가 읽는 설정: {local_file})"
    if (seen.workspace, seen.docs, list(seen.mcp)) != (WORKSPACE, DOCS, servers):
        return (f"저장소 '{name}' 의 값이 쓴 것과 다르다: workspace {seen.workspace}, docs {seen.docs}, "
                f"mcp {list(seen.mcp)}")

    # --- git
    dot_git = root / ".git"
    if dot_git.exists():
        heard("git: 이미 저장소다 — git init 을 하지 않는다")
    else:
        argv = list(GIT_INIT)
        heard(f"> {command_line(argv)}")
        try:
            code = run(argv, cwd=root, log=heard)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return f"git init: 명령을 시작하지 못했다: {e}"
        if dot_git.exists():
            result.created.append(str(dot_git))
        if code != 0:
            return f"git init: 명령이 exit {code} 로 끝났다"
        if not dot_git.exists():
            return f"git init 이 0 으로 끝났는데 .git 이 없다: {root}"
    for file_name, default in GIT_DEFAULTS:
        target = root / file_name
        if target.exists():
            heard(f"{file_name}: 있던 것 그대로")
            continue
        target.write_bytes(default.read_bytes())
        result.created.append(str(target))
        heard(f"파일: {file_name}")

    # --- the row
    version = importer.engine_version_of(found["engine"]["association"]) or spec.engine_version
    with store.transaction() as tx:
        clash = importer.conflict(store.list_projects(include_archived=True, tx=tx), name=name, folder=root,
                                  repo=name)
        if clash is not None:  # one that came between the check and here
            return clash[1]
        try:
            row = store.create_project(name, str(root), name, tx=tx, mcp_port=port, engine_version=version)
        except sqlite3.IntegrityError as e:
            return f"이름 · 저장소 · 포트가 다른 프로젝트와 겹친다: {e}"
        store.emit(tx, "project.updated", {"project": row}, project_id=row["id"])
    result.project = row
    heard(f"프로젝트 등록: {row['id']} {name} (MCP 포트 {port if port is not None else '없음'})")
    return ""
