"""A new UE project (M-3): the names, folders and engines that are refused, the skeleton with the names put in,
the two command lines of the build, and what a build that fails gives back (§6.4, §7.4). No engine is read and
no UBT is run: the engine is a folder made here, with a Build.bat of a few lines where a process is really run."""
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from relay_agent.runners import pid_alive
from relay_agent.studio import creator, importer, machine

DESIGN = Path(__file__).resolve().parent.parent / "design" / "studio"
FILES = ["Config/DefaultEditor.ini", "Config/DefaultEngine.ini", "Config/DefaultGame.ini", "Docs/README.md",
         "Source/__NAME__.Target.cs", "Source/__NAME__/__NAME__.Build.cs", "Source/__NAME__/__NAME__.cpp",
         "Source/__NAME__/__NAME__.h", "Source/__NAME__Editor.Target.cs", "__NAME__.uproject"]
FAILING_BAT = ("@echo off\r\n"
               "echo fake UBT %*\r\n"
               'if "%~1"=="-projectfiles" (\r\n'
               "  echo made> Made.sln\r\n"
               "  exit /b 0\r\n"
               ")\r\n"
               "echo error C1234: broken\r\n"
               "exit /b 6\r\n")


@pytest.fixture
def parent(tmp_path):
    """The folder the projects go under. A temp folder with a letter `check` refuses in its path (a space or a
    letter outside ASCII in the account's name, say) is itself refused, so there is nothing to make a project in."""
    if not creator.PATH_RE.fullmatch(str(tmp_path)):
        pytest.skip("the temp folder of this account has a letter in its path that `check` refuses")
    made = tmp_path / "Projects"
    made.mkdir()
    return made


def engine(tmp_path, version="5.7", *, bat="@echo off\r\n") -> dict:
    """An engine as `machine.engines()` lists it, its folder holding nothing but Build.bat (none when `bat` is
    None)."""
    home = tmp_path / f"UE_{version}"
    home.mkdir()
    if bat is not None:
        file = creator.build_bat(home)
        file.parent.mkdir(parents=True)
        file.write_bytes(bat.encode("ascii"))
    return {"version": version, "path": str(home), "exists": True}


def refusal(*args, **kwargs) -> creator.Refused:
    with pytest.raises(creator.Refused) as caught:
        creator.check(*args, **kwargs)
    assert caught.value.reason in creator.REASONS and str(caught.value) == caught.value.message
    return caught.value


def spec_of(tmp_path, parent, name="Sample", **kwargs) -> creator.Spec:
    return creator.check(name, "5.7", str(parent), engines=[engine(tmp_path, **kwargs)])


# --- the check ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,reason", [
    ("", "name_invalid"), ("My Game", "name_invalid"), ("게임", "name_invalid"), ("Game게임", "name_invalid"),
    ("1Game", "name_invalid"), ("_Game", "name_invalid"), ("Game-1", "name_invalid"), ("Game.One", "name_invalid"),
    ("../Game", "name_invalid"), ("G" * 21, "name_invalid"), (None, "name_invalid"), (7, "name_invalid"),
    ("Engine", "name_reserved"), ("coreuobject", "name_reserved"), ("CON", "name_reserved"), ("lpt1", "name_reserved"),
])
def test_names_that_are_refused(tmp_path, parent, name, reason):
    refused = refusal(name, "5.7", str(parent), engines=[engine(tmp_path)])
    assert (refused.code, refused.field, refused.reason) == ("precondition_failed", "name", reason)
    assert list(parent.iterdir()) == []


def test_the_letters_a_parent_path_may_have():
    """Without the `parent` fixture, so that this is checked even where the temp folder itself is refused."""
    for allowed in [r"C:\Temp\ue-check", "D:/Work/My_Games-2/v1.0", r"C:\Users\KKKK40~1\AppData\Local\Temp",
                    r"\\server\share\ue", "/home/user/ue"]:
        assert creator.PATH_RE.fullmatch(allowed), allowed
    for letter in [*"&^%|<>()!\"';,=+@#${}[]`", " ", "\t", "한"]:  # what cmd reads as its own, a space, not ASCII
        assert not creator.PATH_RE.fullmatch(rf"C:\A{letter}B"), letter
    assert not creator.PATH_RE.fullmatch(r"C:\a:b")  # a colon anywhere but after the drive letter
    assert not creator.PATH_RE.fullmatch("")


def test_parent_folders_that_are_refused(tmp_path, parent):
    engines = [engine(tmp_path)]
    spaced, korean, file = parent / "My Games", parent / "게임", parent / "file.txt"
    special = [parent / "A&B", parent / "A^B", parent / "A%B"]  # folders Windows lets one make and cmd misreads
    for folder in [spaced, korean, *special]:
        folder.mkdir()
    file.write_text("x", encoding="utf-8")
    for given, reason in [("Projects", "parent_not_absolute"), ("", "parent_not_absolute"), (None, "parent_not_absolute"),
                          (str(spaced), "path_invalid"), (str(korean), "path_invalid"),
                          *((str(folder), "path_invalid") for folder in special),
                          (str(parent / "no&where"), "path_invalid"),  # the letters are looked at before the folder
                          (str(parent / "nowhere"), "parent_missing"), (str(file), "parent_missing")]:
        refused = refusal("Sample", "5.7", given, engines=engines)
        assert (refused.code, refused.field, refused.reason) == ("precondition_failed", "parent_dir", reason), given
    assert sorted(p.name for p in parent.iterdir()) == sorted(
        ["My Games", "게임", "A&B", "A^B", "A%B", "file.txt"])  # nothing was made


def test_a_folder_that_is_already_there_is_a_conflict(tmp_path, parent):
    engines = [engine(tmp_path)]
    (parent / "Sample").mkdir()
    (parent / "Other").write_text("a file of that name", encoding="utf-8")
    for name in ("Sample", "Other"):
        refused = refusal(name, "5.7", str(parent), engines=engines)
        assert (refused.code, refused.field, refused.reason) == ("conflict", "name", "folder_exists")
    assert list((parent / "Sample").iterdir()) == []


def test_engines_that_are_refused(tmp_path, parent):
    """Only an engine whose folder is there is taken: the registry of this PC still names 4.27, whose folder is
    gone. One older than 5.0, and one without Build.bat, cannot build the skeleton either."""
    old = engine(tmp_path, "4.27")
    bare = engine(tmp_path, "5.6", bat=None)
    good = engine(tmp_path, "5.7")
    found = machine.engines(lambda: {"5.7": good["path"], "4.27": str(tmp_path / "gone" / "UE_4.27"),
                                     "5.1": str(tmp_path / "gone" / "UE_5.1"), "5.6": bare["path"]})
    assert [(e["version"], e["exists"]) for e in found] == [("4.27", False), ("5.1", False), ("5.6", True),
                                                            ("5.7", True)]
    for version, engines, reason in [("4.27", found, "engine_missing"), ("5.1", found, "engine_missing"),
                                     ("5.9", found, "engine_unknown"), ("", found, "engine_unknown"),
                                     (None, found, "engine_unknown"), ("5.7", [], "engine_unknown"),
                                     ("4.27", [old], "engine_unsupported"), ("5.6", found, "engine_incomplete")]:
        refused = refusal("Sample", version, str(parent), engines=engines)
        assert (refused.code, refused.field, refused.reason) == ("precondition_failed", "engine_version", reason)
    assert creator.check("Sample", "5.7", str(parent), engines=found).engine_dir == Path(good["path"])
    assert list(parent.iterdir()) == []  # the check makes nothing


def test_the_engines_of_this_computer_are_asked_when_none_are_given(tmp_path, parent, monkeypatch):
    good = engine(tmp_path)
    monkeypatch.setattr(machine, "engines", lambda: [good])
    assert creator.check("Sample", "5.7", str(parent)).engine_dir == Path(good["path"])
    monkeypatch.setattr(machine, "engines", lambda: [])
    assert refusal("Sample", "5.7", str(parent)).reason == "engine_unknown"


def test_what_the_check_lets_through(tmp_path, parent):
    spec = spec_of(tmp_path, parent, "Sample_2")
    assert spec == creator.Spec("Sample_2", "5.7", tmp_path / "UE_5.7", parent)
    assert spec.root == parent / "Sample_2" and spec.uproject == parent / "Sample_2" / "Sample_2.uproject"
    assert creator.check("G" * 20, "5.7", parent, engines=[{"version": "5.7", "path": str(tmp_path / "UE_5.7"),
                                                             "exists": True}]).name == "G" * 20  # a Path as well


# --- the template -------------------------------------------------------------------------------------------
def test_the_template_is_the_skeleton_and_is_package_data():
    """An installed server finds the template beside the module: pyproject.toml lists every file of it."""
    assert creator.template_files() == FILES
    root = Path(creator.__file__).resolve().parents[2]
    tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    assert "relay_agent.studio" in tool["packages"]
    package = creator.TEMPLATE_DIR.parent
    listed = {p for pattern in tool["package-data"]["relay_agent.studio"] for p in package.glob(pattern)}
    files = {p for p in creator.TEMPLATE_DIR.rglob("*") if p.is_file()}
    assert len(files) == len(FILES) and files <= listed
    for file in files:
        raw = file.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf") and not file.name.startswith("."), file
        assert set(creator.TOKEN.findall(raw.decode("utf-8"))) <= {"__NAME__", "__ENGINE__"}, file
        if file.suffix != ".md":
            assert raw.isascii(), file  # what the engine's tools read


def test_render_replaces_in_one_pass_and_refuses_what_it_does_not_know():
    values = {"__NAME__": "__ENGINE__", "__ENGINE__": "5.7"}
    assert creator.render("__NAME__Editor uses __ENGINE__, __name__ and _NAME_", values) == \
        "__ENGINE__Editor uses 5.7, __name__ and _NAME_"
    with pytest.raises(ValueError, match="__PORT__"):
        creator.render("Port=__PORT__", values)


def test_folder_writes_the_skeleton_with_the_names_put_in(tmp_path, parent):
    spec, lines = spec_of(tmp_path, parent), []

    def never(*args, **kwargs):
        raise AssertionError("the folder step runs nothing")

    result = creator.folder(spec, run=never, log=lines.append)
    assert result.ok and result.step == "folder" and result.error == ""
    expected = [f.replace("__NAME__", "Sample") for f in FILES]
    on_disk = sorted(p.relative_to(spec.root).as_posix() for p in spec.root.rglob("*") if p.is_file())
    assert on_disk == sorted(expected)
    assert result.created == [str(spec.root)] + [str(spec.root.joinpath(*f.split("/"))) for f in expected]
    assert len(lines) == len(result.created) and result.tail == lines
    for file in spec.root.rglob("*"):  # no placeholder is left, in a name or in a text
        assert not creator.TOKEN.search(file.name), file
        if file.is_file():
            assert not creator.TOKEN.search(file.read_text(encoding="utf-8")), file

    assert importer.find_uproject(spec.root) == spec.uproject
    assert importer.engine_association(spec.uproject) == "5.7"
    project = json.loads(spec.uproject.read_text(encoding="utf-8"))
    assert project["Modules"] == [{"Name": "Sample", "Type": "Runtime", "LoadingPhase": "Default"}]
    assert project["Plugins"] == []  # M-4 fills it
    source = spec.root / "Source"
    game, editor = (source / "Sample.Target.cs").read_text("utf-8"), (source / "SampleEditor.Target.cs").read_text("utf-8")
    assert "class SampleTarget : TargetRules" in game and "TargetType.Game" in game
    assert "class SampleEditorTarget : TargetRules" in editor and "TargetType.Editor" in editor
    for target in (game, editor):
        assert 'ExtraModuleNames.Add("Sample")' in target and "BuildSettingsVersion.Latest" in target
    assert "class Sample : ModuleRules" in (source / "Sample" / "Sample.Build.cs").read_text("utf-8")
    assert 'IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, Sample, "Sample")' in \
        (source / "Sample" / "Sample.cpp").read_text("utf-8")
    assert "ProjectName=Sample" in (spec.root / "Config" / "DefaultGame.ini").read_text("utf-8")
    assert "[unrealmcp]" not in (spec.root / "Config" / "DefaultEngine.ini").read_text("utf-8").lower()  # M-4's
    assert "Unreal Engine 5.7" in (spec.root / "Docs" / "README.md").read_text("utf-8")


def test_folder_leaves_a_folder_that_is_there_alone(tmp_path, parent):
    spec = spec_of(tmp_path, parent)
    assert creator.folder(spec).ok
    spec.uproject.write_text("edited", encoding="utf-8")
    again = creator.folder(spec)
    assert not again.ok and again.created == [] and str(spec.root) in again.error and again.tail == [again.error]
    assert spec.uproject.read_text(encoding="utf-8") == "edited"


def test_folder_gives_what_it_made_when_it_fails(tmp_path, parent, monkeypatch):
    spec = spec_of(tmp_path, parent)
    monkeypatch.setattr(creator, "template_files", lambda: ["Config/DefaultGame.ini", "Config/NoSuch.ini"])
    result = creator.folder(spec)
    assert not result.ok and "NoSuch.ini" in result.error
    assert result.created == [str(spec.root), str(spec.root / "Config" / "DefaultGame.ini")]
    assert (spec.root / "Config" / "DefaultGame.ini").is_file()  # nothing is deleted here: the cleanup is M-8's


# --- the command lines and the build ------------------------------------------------------------------------
def test_the_two_command_lines(tmp_path, parent):
    spec = spec_of(tmp_path, parent)
    bat = str(tmp_path / "UE_5.7" / "Engine" / "Build" / "BatchFiles" / "Build.bat")
    uproject = str(parent / "Sample" / "Sample.uproject")
    assert creator.project_files_command(spec) == [bat, "-projectfiles", f"-project={uproject}", "-game", "-rocket",
                                                   "-progress"]
    assert creator.build_command(spec) == [bat, "SampleEditor", "Win64", "Development", f"-project={uproject}",
                                           "-waitmutex"]
    assert creator.command_line(creator.build_command(spec)) == \
        f"{bat} SampleEditor Win64 Development -project={uproject} -waitmutex"
    assert creator.command_line(["C:\\Epic Games\\Build.bat", "-game"]) == '"C:\\Epic Games\\Build.bat" -game'


def test_build_runs_the_two_commands_in_order(tmp_path, parent):
    spec, calls, lines = spec_of(tmp_path, parent), [], []
    creator.folder(spec)

    def run(argv, *, cwd, log):
        calls.append((list(argv), cwd))
        log(f"ran {len(calls)}")
        (spec.root / ("Sample.sln" if len(calls) == 1 else "Binaries")).mkdir()
        return 0

    result = creator.build(spec, run=run, log=lines.append)
    assert result.ok and result.step == "build"
    assert calls == [(creator.project_files_command(spec), spec.root), (creator.build_command(spec), spec.root)]
    assert result.created == [str(spec.root / "Binaries"), str(spec.root / "Sample.sln")]
    assert lines == ["> " + creator.command_line(creator.project_files_command(spec)), "ran 1",
                     "> " + creator.command_line(creator.build_command(spec)), "ran 2"]
    assert result.tail == lines


def test_a_build_that_fails_gives_the_error_and_the_tail_of_the_log(tmp_path, parent):
    spec, calls, lines = spec_of(tmp_path, parent), [], []
    creator.folder(spec)

    def run(argv, *, cwd, log):
        calls.append(argv[1])
        if len(calls) == 1:
            (spec.root / "Sample.sln").write_text("", encoding="utf-8")
            return 0
        for n in range(100):
            log(f"line {n}")
        log("error C1234: broken")
        return 6

    result = creator.build(spec, run=run, log=lines.append)
    assert not result.ok and "에디터 타깃 빌드" in result.error and "exit 6" in result.error
    assert len(result.tail) == creator.TAIL_LINES == 40 and result.tail == lines[-40:]
    assert result.tail[-2:] == ["error C1234: broken", result.error]
    assert result.created == [str(spec.root / "Sample.sln")]  # what was made before it failed
    assert len(lines) == 2 + 101 + 1  # the callback heard every line, not the tail only

    calls.clear()
    first = creator.build(spec, run=lambda argv, *, cwd, log: calls.append(argv[1]) or 3)
    assert calls == ["-projectfiles"]  # the target is not built when the project files could not be made
    assert "프로젝트 파일 생성" in first.error and "exit 3" in first.error and first.created == []


def test_a_build_that_cannot_start(tmp_path, parent):
    spec, calls = spec_of(tmp_path, parent), []

    def run(argv, *, cwd, log):
        calls.append(argv)
        raise FileNotFoundError(2, "no such file", argv[0])

    before = creator.build(spec, run=run)  # the folder step did not run
    assert not before.ok and str(spec.uproject) in before.error and calls == [] and before.created == []
    creator.folder(spec)
    result = creator.build(spec, run=run)
    assert not result.ok and "시작하지 못했다" in result.error and len(calls) == 1
    assert result.tail == ["> " + creator.command_line(creator.project_files_command(spec)), result.error]
    creator.build_bat(spec.engine_dir).unlink()  # the engine lost its script after the check
    gone = creator.build(spec, run=run)
    assert not gone.ok and "Build.bat" in gone.error and len(calls) == 1


def test_run_process_gives_the_lines_and_the_exit_code(tmp_path):
    lines = []
    script = ("import sys; sys.stdout.buffer.write('하나\\r\\n'.encode('utf-8')); sys.stdout.flush(); "
              "sys.stderr.buffer.write(b'two\\n'); sys.stderr.flush(); "
              "sys.stdout.buffer.write(b'\\xff three\\n'); sys.exit(3)")
    assert creator.run_process([sys.executable, "-c", script], cwd=tmp_path, log=lines.append) == 3
    assert lines[:2] == ["하나", "two"] and lines[2].endswith(" three") and len(lines) == 3
    with pytest.raises(OSError):
        creator.run_process([str(tmp_path / "no-such.exe")], cwd=tmp_path, log=lines.append)


# A command that starts a child which holds the command's output for a minute, and says the child's pid
WITH_A_CHILD = ("import os, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                "stdout=sys.stdout, stderr=sys.stdout); print(child.pid, flush=True); ")


def end(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def gone(pid: int, seconds: float = 10.0) -> bool:
    end_at = time.monotonic() + seconds
    while pid_alive(pid) and time.monotonic() < end_at:
        time.sleep(0.1)
    return not pid_alive(pid)


def test_run_process_ends_a_command_and_what_it_started_at_the_time_limit(tmp_path):
    lines, start = [], time.monotonic()
    code = creator.run_process([sys.executable, "-c", WITH_A_CHILD + "time.sleep(60)"], cwd=tmp_path,
                               log=lines.append, timeout=4.0)
    try:
        assert code == creator.TIMED_OUT == 124 and time.monotonic() - start < 30
        assert lines[0].isdigit() and lines[-1] == "제한 시간 4초를 넘겨 명령을 끝냈다"
        assert gone(int(lines[0]))  # the child too: the tree was ended, not only the command
    finally:
        if lines and lines[0].isdigit():
            end(int(lines[0]))
    assert creator.TIME_LIMIT == 3600.0
    architecture = (DESIGN / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for word in ("`creator.TIME_LIMIT`", "`creator.TIMED_OUT`", "`creator.GRACE`", "`taskkill /T /F`"):
        assert word in architecture, word
    assert "제한 시간과 프로세스 트리 종료가 없다" not in architecture


def test_run_process_does_not_wait_for_a_child_that_holds_the_output(tmp_path, monkeypatch):
    monkeypatch.setattr(creator, "GRACE", 0.5)
    lines, start = [], time.monotonic()
    code = creator.run_process([sys.executable, "-c", WITH_A_CHILD + "os._exit(0)"], cwd=tmp_path,
                               log=lines.append)
    try:
        assert code == 0 and time.monotonic() - start < 30  # the child sleeps for 60
        assert len(lines) == 1 and lines[0].isdigit() and pid_alive(int(lines[0]))
    finally:
        if lines and lines[0].isdigit():
            end(int(lines[0]))
    assert gone(int(lines[0])) and len(lines) == 1  # what comes after the wait is dropped


def test_what_the_log_raises_is_raised_once_the_command_is_over(tmp_path):
    def log(line):
        raise RuntimeError(f"못 적는다: {line}")

    with pytest.raises(RuntimeError, match="못 적는다: one"):
        creator.run_process([sys.executable, "-c", "print('one'); print('two')"], cwd=tmp_path, log=log)


@pytest.mark.skipif(os.name != "nt", reason="Build.bat is a Windows batch file")
def test_a_fake_build_bat_that_fails_is_really_run(tmp_path, parent):
    """The default runner starts the engine's Build.bat as it is — here one that makes the project files and
    then fails the build with 6."""
    spec, lines = spec_of(tmp_path, parent, bat=FAILING_BAT), []
    creator.folder(spec)
    result = creator.build(spec, log=lines.append)
    assert not result.ok and "에디터 타깃 빌드" in result.error and "exit 6" in result.error
    assert result.created == [str(spec.root / "Made.sln")]
    assert result.tail == lines and "error C1234: broken" in lines
    heard = [line for line in lines if line.startswith("fake UBT ")]
    assert len(heard) == 2 and "-projectfiles" in heard[0] and "SampleEditor Win64 Development" in heard[1]
    assert str(spec.uproject) in heard[0] and str(spec.uproject) in heard[1]


# --- the documents ------------------------------------------------------------------------------------------
def test_the_documents_say_what_the_code_does():
    architecture = (DESIGN / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for reason in creator.REASONS:
        assert f"`{reason}`" in architecture, reason
    assert "`relay_agent/studio/creator.py`" in architecture and "`relay_agent/studio/template/`" in architecture
    assert "/".join(creator.BUILD_BAT) in architecture
    assert "M-3 5.7 로 실제 빌드" in (DESIGN / "VERIFICATION.md").read_text(encoding="utf-8")
