"""Making a new UE project (M-3): a name, an engine and a parent folder checked, the skeleton of
relay_agent/studio/template/ written with the names put in, and the project built once.

Contract: design/studio/ARCHITECTURE.md §6.4 (`POST /studio/projects/create`) and §7.4 (`job.step`: `folder` →
`build` → …). The route, the job and its events are M-8's; here are the check and the first two steps.

Nothing here imports api, cli or server. `check` reads only — the folders it is told of and the engines of
`machine.engines()` — and raises `Refused` for what cannot be made. `folder` and `build` are the steps: each is
handed the function that runs a process and the one that hears a line of log, and answers with a `StepResult`
instead of raising. What a step made is in `created` when it failed as well: that list is what a cleanup needs.
Nothing is ever deleted here.

The engine's own `Templates/` are not used: whether they can make a C++ project without the editor could not be
confirmed (§6.4), so the skeleton of this repository is what gets written. The two command lines are built on
one file of the engine, `Engine/Build/BatchFiles/Build.bat`; they are provisional until a project made with 5.7
has really been built (design/studio/VERIFICATION.md).
"""

from __future__ import annotations

import locale
import os
import re
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import machine

TEMPLATE_DIR = Path(__file__).with_name("template")
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,19}")  # what the editor's own wizard takes: at most 20 characters
# The letters a parent folder's path may have, as a list of what is allowed: Build.bat runs through cmd, where `&`,
# `^`, `%` and the like cut the -project= argument or run what follows as a command, and UBT trips on a space or a
# letter outside ASCII. `~` stays for 8.3 short names; `:` only after the drive letter at the front.
PATH_RE = re.compile(r"(?:[A-Za-z]:)?[A-Za-z0-9_.~\\/-]+")
RESERVED_NAMES = frozenset(word.lower() for word in (
    "CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10)),  # Windows
    "Engine", "Core", "CoreUObject", "UnrealEd", "Unreal", "UnrealGame", "Launch"))  # a module or target of the engine
TOKEN = re.compile(r"__[A-Z]+__")  # a placeholder of the template, in a file's name or in its text
TAIL_LINES = 40
TIME_LIMIT = 3600.0  # the most seconds one command may run (an estimate: no first build of 5.7 was timed yet)
TIMED_OUT = 124  # the exit code `run_process` gives for a command it ended at the time limit
GRACE = 5.0  # seconds: how long a command's last lines are waited for after it ended
BUILD_BAT =("Engine", "Build", "BatchFiles", "Build.bat")
MIN_ENGINE = (5, 0)
REASONS = ("name_invalid", "name_reserved", "parent_not_absolute", "path_invalid", "parent_missing",
           "folder_exists", "engine_unknown", "engine_missing", "engine_unsupported", "engine_incomplete")

Log = Callable[[str], None]
Run = Callable[..., int]  # run(argv, *, cwd, log) -> exit code


class Refused(Exception):
    """A project that cannot be made as asked. `code` is the error code of §6.1 (`conflict` when the folder is
    already there, else `precondition_failed`), `field` the input at fault (`name` · `parent_dir` ·
    `engine_version`), `reason` one of REASONS, and the text is for the person who asked."""

    def __init__(self, code: str, field: str, reason: str, message: str):
        super().__init__(message)
        self.code, self.field, self.reason, self.message = code, field, reason, message


@dataclass(frozen=True)
class Spec:
    """A project `check` let through."""
    name: str
    engine_version: str
    engine_dir: Path
    parent_dir: Path

    @property
    def root(self) -> Path:
        return self.parent_dir / self.name

    @property
    def uproject(self) -> Path:
        return self.root / f"{self.name}.uproject"


@dataclass
class StepResult:
    """What a step did. `created`: absolute paths of what it made, also when it failed. `error`: '' when it
    went well. `tail`: the last TAIL_LINES lines of the step's log. `skipped`: the step was turned off and did
    nothing — it is `ok` as well (M-5's `harness`; `job.step` has `skipped` for it, §7.4)."""
    step: str
    created: list[str] = field(default_factory=list)
    error: str = ""
    tail: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


def _quiet(line: str) -> None:
    return None


# --- the check ----------------------------------------------------------------------------------------------
def _version(version: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)", version)
    return (int(match[1]), int(match[2])) if match else None


def build_bat(engine_dir: Path | str) -> Path:
    return Path(engine_dir).joinpath(*BUILD_BAT)


def check(name: Any, engine_version: Any, parent_dir: Any, *, engines: list[dict] | None = None) -> Spec:
    """The three inputs as a `Spec`, or `Refused`. In this order: the name, the parent folder, the folder that
    would be made, the engine. `engines` is what `machine.engines()` answers — asked here when not given — and
    only an engine whose folder is there (`exists`) is taken: the registry keeps the key of one that is gone."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Refused("precondition_failed", "name", "name_invalid",
                      f"프로젝트 이름은 영문자로 시작하고 영문자 · 숫자 · 밑줄만 쓴다(공백 · 한글 불가, 최대 20자): {name!r}")
    if name.lower() in RESERVED_NAMES:
        raise Refused("precondition_failed", "name", "name_reserved",
                      f"윈도 장치나 엔진 모듈 · 타깃과 겹치는 이름은 쓸 수 없다: {name}")

    text = str(parent_dir) if isinstance(parent_dir, (str, Path)) else ""
    if not text or not Path(text).is_absolute():
        raise Refused("precondition_failed", "parent_dir", "parent_not_absolute",
                      f"부모 폴더는 절대 경로로 준다: {text!r}")
    if not PATH_RE.fullmatch(text):
        raise Refused("precondition_failed", "parent_dir", "path_invalid",
                      "부모 폴더 경로에는 영문자 · 숫자 · `_ - . ~` 만 쓸 수 있다"
                      f"(공백 · 한글 · `&` `^` `%` 같은 특수 문자 불가): {text}")
    parent = Path(text)
    if not parent.is_dir():
        raise Refused("precondition_failed", "parent_dir", "parent_missing", f"부모 폴더가 없다: {text}")
    if (parent / name).exists():
        raise Refused("conflict", "name", "folder_exists", f"이미 있는 폴더다: {parent / name}")

    found = machine.engines() if engines is None else engines
    engine = next((e for e in found if e.get("version") == engine_version), None)
    if engine is None:
        known = ", ".join(str(e.get("version")) for e in found) or "없음"
        raise Refused("precondition_failed", "engine_version", "engine_unknown",
                      f"이 컴퓨터에 등록되지 않은 엔진이다: {engine_version!r} (등록된 것: {known})")
    problem = engine_problem(engine)
    if problem is not None:
        raise Refused("precondition_failed", "engine_version", *problem)
    return Spec(name=name, engine_version=engine_version, engine_dir=Path(str(engine.get("path"))),
                parent_dir=parent)


def engine_problem(engine: dict) -> tuple[str, str] | None:
    """Why no project can be made with an engine `machine.engines()` lists — `(reason, text)`, in this order:
    its folder is gone, it is older than MIN_ENGINE, it has no Build.bat — or None when one can. `check` refuses
    with it, and the making screen is told with it which engines not to offer (M-8, jobs.py `options`)."""
    label = engine.get("version")
    if not engine.get("exists"):
        return "engine_missing", f"엔진 {label} 의 폴더가 없다: {engine.get('path')}"
    version = _version(label) if isinstance(label, str) else None
    if version is None or version < MIN_ENGINE:
        return "engine_unsupported", f"엔진 {label} 은 지원하지 않는다 ({MIN_ENGINE[0]}.{MIN_ENGINE[1]} 이상)"
    bat = build_bat(Path(str(engine.get("path"))))
    if not bat.is_file():
        return "engine_incomplete", f"엔진 {label} 에 빌드 스크립트가 없다: {bat}"
    return None


# --- the template -------------------------------------------------------------------------------------------
def template_files() -> list[str]:
    """Every file of the template, as paths under TEMPLATE_DIR with `/` between the parts, in order."""
    return sorted(p.relative_to(TEMPLATE_DIR).as_posix() for p in TEMPLATE_DIR.rglob("*") if p.is_file())


def render(text: str, values: dict[str, str]) -> str:
    """`text` with each placeholder replaced by its value, in one pass: a value is never read again, so a name
    that itself looks like a placeholder stays as it is. A placeholder nobody gave a value is a mistake of the
    template (ValueError), not something to leave in a project."""
    def value(match: re.Match) -> str:
        if match.group(0) not in values:
            raise ValueError(f"템플릿에 모르는 자리표시가 있다: {match.group(0)}")
        return values[match.group(0)]

    return TOKEN.sub(value, text)


def values_of(spec: Spec) -> dict[str, str]:
    return {"__NAME__": spec.name, "__ENGINE__": spec.engine_version}


# --- the command lines --------------------------------------------------------------------------------------
def project_files_command(spec: Spec) -> list[str]:
    """Generating the project files (the .sln and what the IDE reads)."""
    return [str(build_bat(spec.engine_dir)), "-projectfiles", f"-project={spec.uproject}", "-game", "-rocket",
            "-progress"]


def build_command(spec: Spec) -> list[str]:
    """Building the editor target — what the editor needs before it can open the project."""
    return [str(build_bat(spec.engine_dir)), f"{spec.name}Editor", "Win64", "Development",
            f"-project={spec.uproject}", "-waitmutex"]


def command_line(argv: Sequence[str]) -> str:
    """A command as one line, for the log."""
    return subprocess.list2cmdline(list(argv))


def _line(raw: bytes) -> str:
    raw = raw.rstrip(b"\r\n")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:  # cmd and the compiler write in the code page of this computer
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _end_tree(process: subprocess.Popen) -> None:
    """End a command and every process it started (Build.bat starts dotnet, UBT and the compilers): the tree
    by `taskkill /T /F` on Windows, the command's own session elsewhere. Never raises."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(process.pid, signal.SIGKILL)  # `start_new_session`: the group is the command's own
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=GRACE)
    except (OSError, subprocess.SubprocessError):
        pass


def run_process(argv: Sequence[str], *, cwd: Path | str, log: Log, timeout: float | None = None) -> int:
    """Run a command to its end and give its exit code; every line it writes (stderr with stdout) goes to
    `log`. No window, no input. OSError when it cannot be started.

    A command that runs longer than `timeout` seconds (`TIME_LIMIT` when None) is ended with every process it
    started (`_end_tree`); the last line is then "제한 시간 …" and the answer is `TIMED_OUT` instead of an
    exception — a step takes it as any other exit code that is not 0. The lines are read on a thread of their
    own, so a process that was left holding the command's output cannot keep this from returning: after the
    command ended its lines are waited for `GRACE` seconds, and what comes later is dropped. What `log`
    raised is raised here, once the command is over. A server that stops leaves the command running."""
    limit = TIME_LIMIT if timeout is None else float(timeout)
    process = subprocess.Popen(list(argv), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, start_new_session=os.name != "nt",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    gate, state = threading.Lock(), {"over": False, "error": None}

    def read() -> None:
        try:
            for raw in process.stdout:
                with gate:
                    if state["over"]:
                        return
                    if state["error"] is None:  # after an error the lines are still read: the command goes on
                        try:
                            log(_line(raw))
                        except Exception as e:  # noqa: BLE001 - the caller's, raised again on its thread
                            state["error"] = e
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdout.close()  # here, never on the other thread: it may still be read
            except OSError:
                pass

    reader = threading.Thread(target=read, daemon=True, name="katae-studio-command")
    reader.start()
    try:
        code: int | None = process.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        _end_tree(process)
        code = None
    reader.join(GRACE)
    with gate:
        state["over"] = True
    if state["error"] is not None:
        raise state["error"]
    if code is None:
        log(f"제한 시간 {limit:g}초를 넘겨 명령을 끝냈다")
        return TIMED_OUT
    return code


# --- the steps ----------------------------------------------------------------------------------------------
def folder(spec: Spec, *, run: Run = run_process, log: Log = _quiet) -> StepResult:
    """Step `folder`: make the project's folder and write the template into it. `created` is the folder, then
    each file. The folder must not be there: one that is — also one a failed run left half made — fails with
    nothing made and nothing touched. `run` is not used; it is taken so that every step is called alike."""
    result, values = StepResult("folder"), values_of(spec)
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def heard(line: str) -> None:
        tail.append(line)
        log(line)

    try:
        spec.root.mkdir()
        result.created.append(str(spec.root))
        heard(f"폴더: {spec.root}")
        for relative in template_files():
            target = spec.root.joinpath(*render(relative, values).split("/"))
            text = (TEMPLATE_DIR / relative).read_bytes().decode("utf-8")  # bytes: the line ends stay as they are
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(render(text, values).encode("utf-8"))
            result.created.append(str(target))
            heard(f"파일: {target.relative_to(spec.root).as_posix()}")
    except FileExistsError:
        result.error = f"이미 있는 폴더다: {spec.root}"
    except (OSError, ValueError) as e:
        result.error = f"프로젝트 폴더를 만들지 못했다: {e}"
    if result.error:
        heard(result.error)
    result.tail = list(tail)
    return result


def _entries(root: Path) -> set[Path]:
    try:
        return set(root.iterdir())
    except OSError:
        return set()


def build(spec: Spec, *, run: Run = run_process, log: Log = _quiet) -> StepResult:
    """Step `build`: generate the project files, then build the editor target — the second only when the first
    ended with 0. `created` is what appeared right under the project's folder (the .sln, `Intermediate`,
    `Binaries` …), also when a command failed. On a failure `error` names the command and its exit code and
    `tail` holds the last lines both commands wrote."""
    result = StepResult("build")
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def heard(line: str) -> None:
        tail.append(line)
        log(line)

    bat = build_bat(spec.engine_dir)
    if not spec.uproject.is_file():
        result.error = f"프로젝트 파일이 없다(folder 단계가 먼저다): {spec.uproject}"
    elif not bat.is_file():
        result.error = f"엔진에 빌드 스크립트가 없다: {bat}"
    else:
        before = _entries(spec.root)
        for label, argv in (("프로젝트 파일 생성", project_files_command(spec)),
                            ("에디터 타깃 빌드", build_command(spec))):
            heard(f"> {command_line(argv)}")
            try:
                code = run(argv, cwd=spec.root, log=heard)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                result.error = f"{label}: 명령을 시작하지 못했다: {e}"
                break
            if code != 0:
                result.error = f"{label}: 명령이 exit {code} 로 끝났다"
                break
        result.created = sorted(str(p) for p in _entries(spec.root) - before)
    if result.error:
        heard(result.error)
    result.tail = list(tail)
    return result
