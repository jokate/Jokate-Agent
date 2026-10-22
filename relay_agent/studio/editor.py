"""A project's Unreal editor, driven from outside (M-7; design/studio/ARCHITECTURE.md §6.4 · §6.6): start it, stop
it, say how it is, take a picture of its viewport.

Nothing here imports api or server. Which processes are editors, which project one belongs to and whether one
answers come from studio/machine.py: `status` is `{running, pid, healthy, checked_at}`, where `healthy` is the
MCP `health_check` of the project's port. Starting is the engine's own `UnrealEditor.exe <.uproject>`. Stopping
touches only the processes whose command line names a .uproject under this project's folder — an editor of
another project, or of none, is never ended.

A picture is asked of the editor's MCP with a path the server chose, under the attachments folder
(`shot_<hex12>/viewport-<time>.png`), so nothing is written in the project's folder. With the editor off it is
started first and waited for. An editor that only just came up may answer its port before it has a viewport: its
refusal is asked again for `ready_timeout`, while one that was already answering is asked once. The file may be
written a frame after the answer and is waited for (`file_wait`). A `shot_` folder is a place to fetch the picture
from, not to keep it: the ones older than a day go when the next picture is taken (`prune_shots`).

What the outside does — starting a process, ending one, the MCP exchange, waiting — is in `Tools`, each part
replaced in a test. A refusal is `Refused`: `precondition_failed` with one reason in `failed`.

Provisional, like `machine.mcp_send`: the command `take_screenshot` and its argument `filepath` are those of the
public unreal-mcp; the plugin's source lies outside this repository and was not read.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from . import machine
from .files import MAX_ATTACHMENT_MB
from .importer import find_uproject
from .store import now

log = logging.getLogger(__name__)

EDITOR_EXE = ("Engine", "Binaries", "Win64", "UnrealEditor.exe")  # under an engine's folder
SCREENSHOT_COMMAND = "take_screenshot"  # provisional: the MCP command that saves the viewport
SCREENSHOT_PARAM = "filepath"           # provisional: its argument, the file to write
SAVE_COMMAND = "save_all"               # provisional: the MCP command that saves the open assets (VERIFICATION M-7)
SAVE_TIMEOUT = 30.0                     # seconds the editor has to answer the save command
SHOT_FOLDER = "shot_"                   # + 12 hex digits: the folder of one picture under the attachments
SHOT_TIMEOUT = 30.0     # seconds the editor has to answer the screenshot command
START_TIMEOUT = 300.0   # seconds an editor that was just started has to answer (an estimate: none was timed)
POLL = 2.0              # seconds between two looks while waiting
STOP_GRACE = 15.0       # seconds an editor has to close by itself before it is ended by force
# The four below are estimates too: no editor was timed.
READY_TIMEOUT = 30.0    # seconds, from the first screenshot command, an editor that just came up is asked again
FILE_WAIT = 5.0         # seconds the file has to appear after the editor said it took the picture
FILE_POLL = 0.2         # seconds between two looks at the file
SHOT_KEEP = 24 * 3600.0  # seconds a `shot_` folder is kept before the next screenshot removes it
SHOT_NAME = re.compile(rf"{SHOT_FOLDER}[0-9a-f]{{12}}")  # the whole name of such a folder (api.ATTACHED_ID)
REASONS = ("engine_unknown", "engine_missing", "editor_missing", "uproject_missing", "editor_start_failed",
           "mcp_port_missing", "editor_timeout", "screenshot_failed", "screenshot_missing")


class Refused(Exception):
    """What was asked cannot be done. `code` is the code of §6.3 (`precondition_failed`), `failed` the reasons
    — one of REASONS."""

    def __init__(self, code: str, message: str, failed: Sequence[str] = ()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.failed = list(failed)


def _refuse(reason: str, message: str) -> Refused:
    return Refused("precondition_failed", message, [reason])


# --- the outside ------------------------------------------------------------------------------------------
def launch(argv: Sequence[str], cwd: Path | str) -> Any:
    """Start a process that outlives this one and shares nothing with it: no console, no pipes, a process group
    of its own (so a Ctrl+C in the server's console does not reach the editor)."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(list(argv), cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=os.name != "nt")


def end_process(pid: int, force: bool) -> None:
    """Ask a process to close (`taskkill /PID`: the window is told to close, so the editor may still ask about
    unsaved work) or, with `force`, end it and what it started (`/T /F`). Never raises."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), *(["/T", "/F"] if force else [])],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


@dataclass(frozen=True)
class Tools:
    """What this module does to the outside, and how long it waits. A test replaces the parts it needs."""
    spawn: Callable[[Sequence[str], Path], Any] = launch        # (argv, cwd): start the editor
    end: Callable[[int, bool], None] = end_process              # (pid, force): close it, or end it
    send: Callable[..., dict] = machine.mcp_send                # (port, command, params, timeout=) -> answer
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    shot_timeout: float = SHOT_TIMEOUT
    start_timeout: float = START_TIMEOUT
    poll: float = POLL
    stop_grace: float = STOP_GRACE
    ready_timeout: float = READY_TIMEOUT
    file_wait: float = FILE_WAIT
    file_poll: float = FILE_POLL


Processes = Callable[[], list[dict]]   # -> [{pid, name, cmdline}] (machine.editor_processes)
Healthy = Callable[[Any], bool]        # mcp_port -> does the editor answer (machine.health_check)

_locks: dict[str, threading.Lock] = {}  # one per project: two requests must not start two editors of one project
_locks_guard = threading.Lock()


def _lock(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


# --- the command line -------------------------------------------------------------------------------------
def command_line(project: dict, engines: list[dict]) -> tuple[list[str], Path]:
    """`([<engine>/Engine/Binaries/Win64/UnrealEditor.exe, <.uproject>], the project's folder)`: what starts
    this project's editor, and where. `engines` is `[{version, path, exists}]` (machine.engines). Refused with
    `engine_unknown` (the project's `engine_version` is not an installed one), `engine_missing` (its folder is
    gone), `editor_missing` (no UnrealEditor.exe in it), `uproject_missing` (no .uproject right under the
    project's folder)."""
    version = str(project.get("engine_version") or "").strip()
    engine = next((e for e in engines if str(e.get("version")) == version), None) if version else None
    if engine is None:
        raise _refuse("engine_unknown", f"이 컴퓨터에 설치된 엔진이 아닙니다: {version or '(engine_version 없음)'}")
    if not engine.get("exists"):
        raise _refuse("engine_missing", f"엔진 {version} 의 폴더가 없습니다: {engine.get('path')}")
    exe = Path(str(engine["path"])).joinpath(*EDITOR_EXE)
    if not exe.is_file():
        raise _refuse("editor_missing", f"에디터 실행 파일이 없습니다: {exe}")
    folder = str(project.get("path") or "").strip()
    uproject = find_uproject(Path(folder)) if folder else None
    if uproject is None:
        raise _refuse("uproject_missing", f"프로젝트 폴더에 .uproject 파일이 없습니다: {folder or '(path 없음)'}")
    return [str(exe), str(uproject)], Path(folder)


# --- status -----------------------------------------------------------------------------------------------
def _pids(project: dict, projects: list[dict], processes: Processes) -> list[int]:
    """The pids of this project's editors, smallest first: the processes whose command line names a .uproject
    under this project's folder (machine.editors — a folder nested in another project's goes to the deeper one)."""
    known = projects if any(p.get("id") == project["id"] for p in projects) else [*projects, project]
    rows = machine.editors(list(processes()), known, lambda _port: False)
    return sorted(row["pid"] for row in rows if row["project_id"] == project["id"])


def _answers(healthy: Healthy, port: Any) -> bool:
    try:
        return bool(healthy(port))
    except Exception:  # noqa: BLE001 — a check that fails is an editor that did not answer
        log.exception("studio: 에디터의 health_check 를 하지 못했다 (포트 %s)", port)
        return False


def status(project: dict, projects: list[dict], *, processes: Processes = machine.editor_processes,
           healthy: Healthy = machine.health_check) -> dict:
    """`{running, pid, healthy, checked_at}` of this project's editor. `pid` is the smallest one when several
    run, None when none does. `healthy` is asked once, and only of a running editor whose project has an
    `mcp_port`: an editor that is off, or one without a port, is not healthy."""
    pids = _pids(project, projects, processes)
    port = project.get("mcp_port")
    return {"running": bool(pids), "pid": pids[0] if pids else None,
            "healthy": bool(pids) and bool(port) and _answers(healthy, port), "checked_at": now()}


# --- start · stop -----------------------------------------------------------------------------------------
def _start_if_off(project: dict, projects: list[dict], engines: list[dict], processes: Processes,
                  tools: Tools) -> bool:
    """Start the editor unless one of this project runs; True when it was started here. One at a time for a
    project."""
    with _lock(project["id"]):
        if _pids(project, projects, processes):
            return False
        argv, cwd = command_line(project, engines)
        try:
            tools.spawn(argv, cwd)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            raise _refuse("editor_start_failed", f"에디터를 실행하지 못했습니다: {e}") from e
        log.info("studio: 에디터를 실행했다 — %s", " ".join(argv))
        return True


def start(project: dict, projects: list[dict], engines: list[dict], *,
          processes: Processes = machine.editor_processes, healthy: Healthy = machine.health_check,
          tools: Tools = Tools()) -> dict:
    """Start this project's editor and answer its status at once — it takes minutes to come up, so whoever asked
    reads the status again. An editor that already runs is left alone and its status is the answer."""
    if _start_if_off(project, projects, engines, processes, tools):
        return status(project, projects, processes=processes, healthy=lambda _port: False)  # cannot answer yet
    return status(project, projects, processes=processes, healthy=healthy)


def stop(project: dict, projects: list[dict], *, processes: Processes = machine.editor_processes,
         healthy: Healthy = machine.health_check, tools: Tools = Tools(), force: bool = True) -> dict:
    """Close this project's editors — only them — and answer the status. Each is asked to close; the ones still
    there after `stop_grace` are ended by force, so unsaved work in the editor is lost then. With `force` false
    none is ended: the wait is the same and an editor that did not close (it asks about unsaved work) is still
    `running` in the answer. No editor running: the status at once."""
    with _lock(project["id"]):
        targets = _pids(project, projects, processes)
        if targets:
            for pid in targets:
                tools.end(pid, False)
            deadline = tools.clock() + tools.stop_grace
            while True:
                left = [pid for pid in _pids(project, projects, processes) if pid in targets]
                if not left or tools.clock() >= deadline:
                    break
                tools.sleep(tools.poll)
            if force:
                for pid in left:  # never a pid that was not this project's when the stop began
                    tools.end(pid, True)
    return status(project, projects, processes=processes, healthy=healthy)


# --- screenshot -------------------------------------------------------------------------------------------
def _problem(answer: Any) -> str | None:
    """What the editor said went wrong with a command; None when it said nothing of the kind."""
    if not isinstance(answer, dict):
        return "응답이 JSON 객체가 아닙니다"
    result = answer.get("result") if isinstance(answer.get("result"), dict) else {}
    if answer.get("status") == "error" or answer.get("success") is False or result.get("success") is False:
        return str(answer.get("error") or answer.get("message") or result.get("error") or result.get("message")
                   or "에디터가 오류라고만 답했습니다")
    return None


def _ask(tools: Tools, port: Any, target: Path) -> Refused | None:
    """Ask the editor once for the picture: None when it said it took it, else the refusal (`screenshot_failed`)."""
    try:
        answer = tools.send(port, SCREENSHOT_COMMAND, {SCREENSHOT_PARAM: str(target)}, timeout=tools.shot_timeout)
    except Exception as e:  # noqa: BLE001 — no connection, no answer in time, not JSON
        refusal = _refuse("screenshot_failed", f"에디터가 스크린샷 요청에 답하지 않았습니다: {e}")
        refusal.__cause__ = e
        return refusal
    problem = _problem(answer)
    return None if problem is None else _refuse("screenshot_failed", f"에디터가 스크린샷을 찍지 못했습니다: {problem}")


def _size(path: Path) -> int:
    """The bytes of a file; 0 when it is not there (yet)."""
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def prune_shots(root: Path | str, keep: float = SHOT_KEEP) -> list[str]:
    """Remove the `shot_<hex12>` folders right under `root` last written more than `keep` seconds ago and return
    their names. Nothing else goes: a message's folder, an approval's (`inb_…`), a file or a link named like a
    shot, a folder whose name only begins like one. Never raises — a root that is not there has nothing to
    remove, and a folder that cannot be read is left for the next time."""
    removed: list[str] = []
    before = time.time() - keep
    try:
        entries = sorted(Path(root).iterdir())
    except FileNotFoundError:
        return removed
    except OSError:
        log.exception("studio: 오래된 스크린샷 폴더를 찾지 못했다 — %s", root)
        return removed
    for entry in entries:
        try:
            if not SHOT_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            if entry.stat().st_mtime >= before:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry.name)
    if removed:
        log.info("studio: %s초가 지난 스크린샷 폴더 %d개를 지웠다 — %s", f"{keep:g}", len(removed), ", ".join(removed))
    return removed


def bring_up(project: dict, projects: list[dict], engines: list[dict], *,
             processes: Processes = machine.editor_processes, healthy: Healthy = machine.health_check,
             tools: Tools = Tools(), timeout: float | None = None) -> bool:
    """Have this project's editor answer: one that is off is started, and `health_check` is asked every `poll`
    until it answers. True when it was started here or had to be waited for — it is still coming up —, False
    when it answered at once.

    `Refused`: the project has no `mcp_port` (`mcp_port_missing`, before anything is started), the editor cannot
    be started (the reasons of `command_line`, `editor_start_failed`), or it did not answer within `timeout`
    seconds — `tools.start_timeout` when None — (`editor_timeout`; the editor is left as it is). What a
    screenshot and the editor check of a task's run (work.py) share."""
    port = project.get("mcp_port")
    if not port:
        raise _refuse("mcp_port_missing", "이 프로젝트에는 mcp_port 가 없어 에디터의 응답을 확인할 수 없습니다")
    limit = tools.start_timeout if timeout is None else timeout
    fresh = _start_if_off(project, projects, engines, processes, tools)
    deadline = tools.clock() + limit
    while not _answers(healthy, port):
        if tools.clock() >= deadline:
            raise _refuse("editor_timeout", f"에디터가 {limit:g}초 안에 응답하지 않았습니다 "
                          f"(MCP 포트 {port}). 에디터는 그대로 두었습니다")
        tools.sleep(tools.poll)
        fresh = True  # it did not answer a moment ago: it is still coming up, whoever started it
    return fresh


def screenshot(project: dict, projects: list[dict], engines: list[dict], root: Path | str, *,
               processes: Processes = machine.editor_processes, healthy: Healthy = machine.health_check,
               tools: Tools = Tools()) -> Path:
    """Save a picture of the editor's viewport and return the file: `<root>/shot_<hex12>/viewport-<time>.png`.

    An editor that is off is started, and `health_check` is waited for up to `start_timeout` — past it the
    refusal is `editor_timeout` and the editor is left as it is. Refused before anything is started when the
    project has no `mcp_port` (`mcp_port_missing`). `screenshot_failed`: the command got no answer or an error
    for one. `screenshot_missing`: it answered but the file is not there, is empty or is larger than an
    attachment may be. A refusal leaves no folder behind.

    The command is sent again — every `poll`, for `ready_timeout` from the first time, with the same file — only
    to an editor this call started or had to wait for: its port may answer before it has a viewport. An editor
    that was already answering is asked once, so what it says is wrong is told at once. After a good answer a
    file that is not there or is empty is looked at again every `file_poll` for `file_wait`. `shot_` folders
    older than SHOT_KEEP are removed here, once the editor answers (`prune_shots`)."""
    port = project.get("mcp_port")
    if not port:
        raise _refuse("mcp_port_missing", "이 프로젝트에는 mcp_port 가 없어 에디터에 스크린샷을 요청할 수 없습니다")
    fresh = bring_up(project, projects, engines, processes=processes, healthy=healthy, tools=tools)
    prune_shots(root)
    folder = Path(root) / f"{SHOT_FOLDER}{uuid.uuid4().hex[:12]}"
    target = folder / f"viewport-{datetime.now():%Y%m%d-%H%M%S}.png"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        ready = tools.clock() + tools.ready_timeout
        while True:
            refusal = _ask(tools, port, target)
            if refusal is None:
                break
            if not fresh or tools.clock() >= ready:
                raise refusal
            tools.sleep(tools.poll)
        size, patience = _size(target), tools.clock() + tools.file_wait
        while size <= 0 and tools.clock() < patience:  # the plugin may write the file a frame after it answers
            tools.sleep(tools.file_poll)
            size = _size(target)
        if size <= 0:
            raise _refuse("screenshot_missing", f"에디터는 답했지만 스크린샷 파일이 없거나 비었습니다: {target}")
        if size > MAX_ATTACHMENT_MB * 1024 * 1024:
            raise _refuse("screenshot_missing", f"스크린샷이 첨부 한도 {MAX_ATTACHMENT_MB}MB 를 넘습니다: {size} 바이트")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return target
