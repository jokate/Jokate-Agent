"""`editor_build`, the studio MCP tool that builds the C++ of a project (T2 of the 'owner decides less' campaign).

A Live Coding compile has limits (it only takes a small change inside a .cpp), and a run that could not build
used to stop and ask. This is the way that always works, in this order:

1. an editor that runs is asked to save once (`editor.SAVE_COMMAND`, provisional, a failure is ignored) and closed
   by the path of the editor control (`editor.stop`, not by force: an editor that asks about unsaved work stays,
   and then nothing is built — a forced end loses work);
2. the engine's `Build.bat <Project>Editor Win64 Development -Project=<.uproject> -WaitMutex`, and only that;
3. the editor is started again and waited for until its MCP answers (`editor.bring_up`). After a build that failed
   only an editor that was running is started (the old binary); if it does not answer (it may ask to rebuild the
   module in a modal window) `editor_up` is false and the note says so. An editor that was off stays off.

The answer is `{built, seconds, errors[], editor_up, editor_note, was_running}`. While it runs, and for a while
after, the editor watch of the project's tasks is paused (`Deps.pause`; server.py gives `EditorWatch.paused`):
an editor that is closed on purpose is not one that is lost. The runner and the editor control are in `Deps`,
so a test builds nothing and starts nothing.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager

from . import creator, editor, machine

log = logging.getLogger(__name__)

BUILD_TIMEOUT_S = 3600.0   # seconds Build.bat may take: past it the command is ended
ERROR_LINES = 40           # the most lines of the compiler's complaints that are handed back
LINE_CHARS = 400           # a longer line is cut
ERROR_RE = re.compile(r"\b(error|fatal)\b|\bLNK\d{4}\b", re.IGNORECASE)

Run = Callable[[list[str], Path, float], "tuple[int, list[str]]"]  # (argv, cwd, timeout) -> (exit code, lines)


def run_command(argv: list[str], cwd: Path, timeout: float) -> tuple[int, list[str]]:
    """The default runner: the command to its end (`creator.run_process`), every line it wrote."""
    lines: list[str] = []
    code = creator.run_process(argv, cwd=cwd, log=lines.append, timeout=timeout)
    return code, lines


def _engines() -> list[dict]:
    try:
        return machine.engines()
    except Exception:  # noqa: BLE001 - a registry that cannot be read is a computer without engines
        log.exception("studio: 설치된 엔진 목록을 읽지 못했다")
        return []


@dataclass
class Deps:
    """What `build` does to the outside. A test replaces the parts it needs."""
    run: Run = run_command
    tools: editor.Tools = field(default_factory=editor.Tools)
    processes: Callable[[], list[dict]] = machine.editor_processes
    healthy: Callable[[Any], bool] = machine.health_check
    engines: Callable[[], list[dict]] = _engines
    pause: Callable[[str], ContextManager] = lambda project_id: contextlib.nullcontext()  # the watch of the project
    clock: Callable[[], float] = time.monotonic
    start_timeout: float = editor.START_TIMEOUT
    timeout: float = BUILD_TIMEOUT_S


DEPS = Deps()  # server.py sets `pause`
_busy: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock(project_id: str) -> threading.Lock:
    with _guard:
        return _busy.setdefault(project_id, threading.Lock())


def error_lines(lines: list[str], built: bool) -> list[str]:
    """The lines of the output that complain (`error` · `fatal` · a linker code), at most ERROR_LINES, without
    a line twice. A build that failed with no such line gives its last lines instead; one that built, none."""
    seen: list[str] = []
    for raw in lines:
        text = raw.strip()[:LINE_CHARS]
        if text and ERROR_RE.search(text) and text not in seen:
            seen.append(text)
    if seen:
        return seen[:ERROR_LINES]
    if built:
        return []
    return [raw.strip()[:LINE_CHARS] for raw in lines if raw.strip()][-ERROR_LINES:]


def _answer(*, built: bool, seconds: float, errors: list[str], editor_up: bool, note: str, was_running: bool) -> dict:
    return {"built": built, "seconds": round(seconds, 1), "errors": errors, "editor_up": editor_up,
            "editor_note": note, "was_running": was_running}


def build(project: dict, projects: list[dict], deps: Deps | None = None) -> dict:
    """Build this project's editor target as the module says. `editor.Refused` before anything is done when the
    project cannot be built (no engine · no .uproject, `editor.command_line`). Never starts a second build of
    one project: it answers `built: false` with the reason in `editor_note`."""
    deps = deps or DEPS
    engines = list(deps.engines())
    exe_line, folder = editor.command_line(project, engines)
    uproject = Path(exe_line[1])
    version = str(project.get("engine_version") or "").strip()
    engine = next(e for e in engines if str(e.get("version")) == version)
    lock = _lock(project["id"])
    if not lock.acquire(blocking=False):
        return _answer(built=False, seconds=0, errors=[], editor_up=False, was_running=False,
                       note="이 프로젝트의 빌드가 이미 돌고 있다 — 끝나길 기다린 뒤 다시 부른다")
    try:
        with deps.pause(project["id"]):
            return _build(project, projects, deps, engines, engine, uproject, folder)
    finally:
        lock.release()


def _save(project: dict, deps: Deps) -> bool:
    """Ask the running editor to save its work once, just before it is closed (best effort: the command name is
    provisional, `editor.SAVE_COMMAND`). True when the editor answered without an error."""
    port = project.get("mcp_port")
    if not port:
        return False
    try:
        answer = deps.tools.send(port, editor.SAVE_COMMAND, {}, timeout=editor.SAVE_TIMEOUT)
    except (OSError, ValueError) as e:
        log.info("studio: 닫기 전 저장 명령이 통하지 않았다: %s", e)
        return False
    if not isinstance(answer, dict) or answer.get("status") == "error":
        log.info("studio: 닫기 전 저장 명령이 오류를 돌려줬다: %s", answer)
        return False
    return True


def _build(project: dict, projects: list[dict], deps: Deps, engines: list[dict], engine: dict, uproject: Path,
           folder: Path) -> dict:
    sources = {"processes": deps.processes, "healthy": deps.healthy}
    was_running = editor.status(project, projects, **sources)["running"]
    if was_running:
        saved = _save(project, deps)
        after = editor.stop(project, projects, tools=deps.tools, force=False, **sources)
        if after["running"]:
            return _answer(built=False, seconds=0, errors=[], editor_up=bool(after["healthy"]), was_running=True,
                           note="에디터가 닫히지 않았다 (저장 확인 창이 떠 있을 수 있다) — 강제로 끄지 않았고 빌드도 "
                                "하지 않았다" + ("" if saved else " (저장 명령이 통하지 않았을 수 있다)")
                                + ". 에디터를 닫은 뒤 다시 부른다")
    argv = [str(creator.build_bat(engine["path"])), f"{uproject.stem}Editor", "Win64", "Development",
            f"-Project={uproject}", "-WaitMutex"]
    began = deps.clock()
    try:
        code, lines = deps.run(argv, Path(engine["path"]), deps.timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        code, lines = -1, [f"빌드 명령을 실행하지 못했다: {e}"]
    seconds = deps.clock() - began
    built = code == 0
    errors = error_lines(list(lines), built)
    log.info("studio: %s 빌드 %s (%.0f초, 종료 코드 %s)", uproject.stem, "성공" if built else "실패", seconds, code)
    if not built:
        if not was_running:  # an editor that was off is not started by a build that failed
            return _answer(built=False, seconds=seconds, errors=errors, editor_up=False, was_running=False,
                           note=f"빌드가 실패했다 (종료 코드 {code}) — 에디터는 켜지 않았다. 오류를 고친 뒤 다시 부른다")
        try:
            editor.bring_up(project, projects, engines, tools=deps.tools, timeout=deps.start_timeout, **sources)
        except editor.Refused as e:
            return _answer(built=False, seconds=seconds, errors=errors, editor_up=False, was_running=True,
                           note=f"빌드가 실패했다 (종료 코드 {code}) — 에디터를 다시 켜려 했지만 응답하지 않았다 "
                                f"(모듈 재빌드 창이 떠 있을 수 있다): {e.message}")
        return _answer(built=False, seconds=seconds, errors=errors, editor_up=True, was_running=True,
                       note=f"빌드가 실패했다 (종료 코드 {code}) — 에디터는 다시 켰다(예전 바이너리). "
                            "오류를 고친 뒤 다시 부른다")
    try:
        editor.bring_up(project, projects, engines, tools=deps.tools, timeout=deps.start_timeout, **sources)
    except editor.Refused as e:
        return _answer(built=True, seconds=seconds, errors=errors, editor_up=False, was_running=was_running,
                       note=f"빌드는 됐지만 에디터가 다시 뜨지 않았다: {e.message}")
    return _answer(built=True, seconds=seconds, errors=errors, editor_up=True, was_running=was_running, note="")
