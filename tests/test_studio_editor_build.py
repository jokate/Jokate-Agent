"""T2 of the 'owner decides less' campaign: the editor watch only tells a studio run (it never cuts it nor asks
`editor_unresponsive`), and the studio MCP tool `editor_build` (studio/editor_build.py). Nothing is built and no
editor is started: the runner, the processes and the port are fakes."""
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay_agent.studio import api, editor, editor_build, editor_watch, notice, tools, work
from test_studio_editor import row as process
from test_studio_editor_check import Ed, Timers, questions, reasons, run_rows, wire  # noqa: F401
from test_studio_inbox import ok
from test_studio_queue import line  # noqa: F401 - the fixture the tests stand on
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on
from test_studio_tools import desk, failed  # noqa: F401
from test_studio_api_tasks import world  # noqa: F401
from test_studio_ws import wait

PORT = 8101


# --- editor_build.build -----------------------------------------------------------------------------------
class Pc:
    """A computer with one project: its editor (a process, a port), its engine and a Build.bat that is a function."""

    def __init__(self, tmp_path, *, running=False, closes=True, builds=True, comes_up=True, output=None):
        self.folder = tmp_path / "Game"
        self.folder.mkdir()
        (self.folder / "Game.uproject").write_text("{}", encoding="utf-8")
        self.engine = tmp_path / "UE_5.7"
        exe = self.engine.joinpath(*editor.EDITOR_EXE)
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        self.project = {"id": "prj_1", "path": str(self.folder), "engine_version": "5.7", "mcp_port": PORT}
        self.running, self.answers = running, running
        self.closes, self.builds, self.comes_up = closes, builds, comes_up
        self.output = output if output is not None else ["Building GameEditor...", "Total execution time: 4 s"]
        self.log, self.ran, self.now, self.paused = [], [], 0.0, []
        self.send_error = None

    def processes(self):
        return [process(77, self.folder / "Game.uproject")] if self.running else []

    def healthy(self, port):
        return port == PORT and self.running and self.answers

    def end(self, pid, force):
        self.log.append(("end", force))
        if self.closes or force:
            self.running = self.answers = False

    def spawn(self, argv, cwd):
        self.log.append(("spawn", argv[1]))
        self.running = True

    def send(self, port, command, params, timeout=None):
        self.log.append(("send", command))
        if self.send_error:
            raise self.send_error
        return {"status": "success"}

    def sleep(self, seconds):
        self.now += seconds
        if self.running and self.comes_up:
            self.answers = True

    def clock(self):
        return self.now

    def run(self, argv, cwd, timeout):
        self.log.append(("build", self.running))
        self.ran.append((list(argv), cwd, timeout))
        self.now += 42
        return (0 if self.builds else 6), list(self.output)

    @contextlib.contextmanager
    def pause(self, project_id):
        self.paused.append(("in", project_id, list(self.log)))
        try:
            yield
        finally:
            self.paused.append(("out", project_id, list(self.log)))

    def deps(self, **over):
        tool = editor.Tools(spawn=self.spawn, end=self.end, send=self.send, sleep=self.sleep, clock=self.clock, start_timeout=30.0)
        base = dict(run=self.run, tools=tool, processes=self.processes, healthy=self.healthy, pause=self.pause,
                    engines=lambda: [{"version": "5.7", "path": str(self.engine), "exists": True}],
                    clock=self.clock, start_timeout=30.0)
        return editor_build.Deps(**{**base, **over})

    def build(self, **over):
        return editor_build.build(self.project, [self.project], self.deps(**over))


def test_a_running_editor_is_closed_then_built_then_started_again(tmp_path):
    pc = Pc(tmp_path, running=True)
    out = pc.build()
    assert out == {"built": True, "seconds": 42.0, "errors": [], "editor_up": True, "editor_note": "",
                   "was_running": True}
    assert pc.log == [("send", editor.SAVE_COMMAND), ("end", False), ("build", False), ("spawn", str(pc.folder / "Game.uproject"))]  # no force
    argv, cwd, _ = pc.ran[0]
    assert argv == [str(pc.engine / "Engine" / "Build" / "BatchFiles" / "Build.bat"), "GameEditor", "Win64",
                    "Development", f"-Project={pc.folder / 'Game.uproject'}", "-WaitMutex"]
    assert cwd == pc.engine and pc.running and pc.answers


def test_an_editor_that_is_off_is_built_at_once_and_started(tmp_path):
    pc = Pc(tmp_path, running=False)
    out = pc.build()
    assert out["built"] and out["editor_up"] and out["was_running"] is False
    assert pc.log == [("build", False), ("spawn", str(pc.folder / "Game.uproject"))]


def test_a_failed_build_gives_the_error_lines_and_restarts_the_editor_that_was_running(tmp_path):
    lines = [f"info {n}" for n in range(100)] + ["Game.cpp(12): error C2065: 'X': undeclared identifier"] * 2 + [
        f"fatal error number {n}" for n in range(60)]
    pc = Pc(tmp_path, running=True, builds=False, output=lines)
    out = pc.build()
    assert out["built"] is False and out["editor_up"] is True and out["seconds"] == 42.0
    assert out["errors"][0] == "Game.cpp(12): error C2065: 'X': undeclared identifier"
    assert len(out["errors"]) == editor_build.ERROR_LINES and out["errors"].count(out["errors"][0]) == 1  # deduplicated
    assert "종료 코드 6" in out["editor_note"] and "다시 켰다" in out["editor_note"]
    assert ("spawn", str(pc.folder / "Game.uproject")) in pc.log and out["editor_up"] is True and pc.running


def test_an_editor_that_was_off_is_not_started_by_a_failed_build(tmp_path):
    pc = Pc(tmp_path, running=False, builds=False)
    out = pc.build()
    assert out["built"] is False and out["editor_up"] is False and out["was_running"] is False
    assert not any(kind in ("spawn", "send") for kind, _ in pc.log) and not pc.running


def test_a_failed_build_and_an_editor_that_does_not_answer_again_is_told(tmp_path):
    pc = Pc(tmp_path, running=True, builds=False, comes_up=False)
    out = pc.build()
    assert out["built"] is False and out["editor_up"] is False
    assert "종료 코드 6" in out["editor_note"] and "응답하지 않았다" in out["editor_note"]


def test_a_save_command_that_fails_does_not_stop_the_build(tmp_path):
    pc = Pc(tmp_path, running=True)
    pc.send_error = OSError("연결 거부")
    out = pc.build()
    assert out["built"] is True and ("build", False) in pc.log


def test_the_client_tool_limit_is_longer_than_the_build_limit_of_the_mcp_server():
    import re
    from relay_agent import runners
    text = (Path(__file__).resolve().parent.parent / "mcp_servers" / "studio.py").read_text(encoding="utf-8")
    seconds = int(re.search(r"BUILD_TIMEOUT_S = (\d+)", text).group(1))
    assert runners.MCP_TOOL_TIMEOUT_MS >= 1000 * seconds


def test_a_failed_build_without_an_error_line_gives_the_last_lines(tmp_path):
    pc = Pc(tmp_path, builds=False, output=[f"line {n}" for n in range(100)])
    out = pc.build()
    assert out["errors"] == [f"line {n}" for n in range(60, 100)]


def test_an_editor_that_does_not_come_up_again_is_told(tmp_path):
    pc = Pc(tmp_path, running=True, comes_up=False)
    out = pc.build()
    assert out["built"] is True and out["editor_up"] is False and "다시 뜨지 않았다" in out["editor_note"]
    assert "초 안에 응답하지 않았습니다" in out["editor_note"]


def test_an_editor_that_does_not_close_is_not_ended_and_nothing_is_built(tmp_path):
    pc = Pc(tmp_path, running=True, closes=False)
    out = pc.build()
    assert out["built"] is False and out["was_running"] is True and "닫히지 않았다" in out["editor_note"]
    assert not pc.ran and ("end", True) not in pc.log and pc.running


def test_a_runner_that_cannot_start_is_a_failed_build(tmp_path):
    def broken(argv, cwd, timeout):
        raise OSError("Build.bat 없음")

    out = Pc(tmp_path).build(run=broken)
    assert out["built"] is False and out["errors"] == ["빌드 명령을 실행하지 못했다: Build.bat 없음"]


def test_the_watch_is_paused_for_the_whole_build_and_one_build_at_a_time(tmp_path):
    pc = Pc(tmp_path, running=True)
    seen = []

    def run(argv, cwd, timeout):
        seen.append(pc.build())  # a second build of the project while this one is at work
        return pc.run(argv, cwd, timeout)

    out = pc.build(run=run)
    assert out["built"] and seen[0]["built"] is False and "이미 돌고 있다" in seen[0]["editor_note"]
    assert [(kind, pid) for kind, pid, _ in pc.paused] == [("in", "prj_1"), ("out", "prj_1")]
    assert pc.paused[0][2] == [] and ("spawn", str(pc.folder / "Game.uproject")) in pc.paused[1][2]  # around all of it


def test_a_project_without_an_engine_is_refused_before_anything_is_done(tmp_path):
    pc = Pc(tmp_path, running=True)
    pc.project["engine_version"] = "9.9"
    with pytest.raises(editor.Refused) as e:
        pc.build()
    assert e.value.failed == ["engine_unknown"] and pc.log == [] and not pc.paused


# --- the tool ---------------------------------------------------------------------------------------------
def test_the_tool_builds_the_project_of_the_callers_task_run(desk, monkeypatch):  # noqa: F811
    task = desk.working()
    asked = []

    def build(project, projects, deps=None):
        asked.append(project["id"])
        return {"built": True, "seconds": 12.5, "errors": [], "editor_up": True, "editor_note": "", "was_running": True}

    monkeypatch.setattr(tools.editor_build_mod, "build", build)
    out = desk.call("editor_build", desk.token(task))
    assert out["ok"] and out["built"] and out["editor_up"] and out["seconds"] == 12.5 and not out["end_turn"]
    assert asked == [task["project_id"]]
    failed(desk.call("editor_build", desk.turn(desk.w.mate, task=task)), "actor_not_allowed")  # not the task's run
    failed(desk.call("editor_build", desk.turn(desk.w.emp)), "no_task_context")
    assert desk.post("editor_build", desk.token(task), project_id="x").status_code == 422  # no argument at all


def test_the_tool_passes_a_refusal_on_and_a_failed_build_is_a_result(desk, monkeypatch):  # noqa: F811
    token = desk.token(desk.working())
    out = failed(desk.call("editor_build", token), "precondition_failed")  # the world's project has no engine
    assert out["failed"] == ["engine_unknown"]

    monkeypatch.setattr(tools.editor_build_mod, "build", lambda *a, **k: {
        "built": False, "seconds": 3.0, "errors": ["error C1"], "editor_up": False, "editor_note": "빌드가 실패했다",
        "was_running": False})
    out = desk.call("editor_build", token)
    assert out["ok"] and out["built"] is False and out["errors"] == ["error C1"] and "빌드하지 못했다" in out["message"]


# --- the watch ---------------------------------------------------------------------------------------------
class Handle:
    def cancel(self):
        pass


class Fakes:
    """A watch on a task whose editor answers or not, with its own clock and timers."""

    def __init__(self, monkeypatch, *, studio):
        self.task = {"id": "tsk_1", "run_id": "run_1", "status": "working", "project_id": "prj_1"}
        self.alive, self.now, self.told, self.lost = True, 0.0, [], []
        monkeypatch.setattr(work, "studio_run", lambda run_id: studio)
        monkeypatch.setattr(notice, "mode", lambda run_id: "work" if studio else None)
        monkeypatch.setattr(notice, "tell", lambda run_id, kind, detail: self.told.append((run_id, kind, detail)))
        monkeypatch.setattr(work, "editor_lost", lambda *a: self.lost.append(a) or True)
        store = SimpleNamespace(get_task=lambda task_id: self.task)
        effects = SimpleNamespace(editor_alive=lambda store, task: self.alive)
        self.timers = Timers()
        self.watch = editor_watch.EditorWatch(None, effects, lambda: SimpleNamespace(store=store), lambda task_id: True,
                                              timer=self.timers, clock=lambda: self.now, quiet_after=120)
        self.watch.begin("tsk_1", "run_1")

    def looks(self, n):
        for _ in range(n):
            self.timers.fire()


def test_the_watch_only_tells_a_studio_run_once_and_goes_on(monkeypatch):
    f = Fakes(monkeypatch, studio=True)
    f.alive = False
    f.looks(2)
    assert not f.told
    f.looks(1)  # the third in a row
    assert len(f.told) == 1 and f.told[0][1:] == ("editor", {"why": "3번 연속 응답하지 않았다"})
    f.looks(4)  # still silent: told once, the watch goes on, the run is not cut and nothing is asked
    assert len(f.told) == 1 and not f.lost and f.watch.watching() == ["tsk_1"] and len(f.timers.due) == 1
    f.alive = True
    f.looks(1)
    f.alive = False
    f.looks(3)  # it answered in between: a new silence is told anew
    assert len(f.told) == 2 and not f.lost


def test_the_watch_of_a_run_outside_the_studio_cuts_it_as_before(monkeypatch):
    f = Fakes(monkeypatch, studio=False)
    f.alive = False
    f.looks(3)
    assert len(f.lost) == 1 and f.lost[0][2:] == ("tsk_1", "run_1")
    assert not f.told and f.watch.watching() == [] and not f.timers.due


def test_the_watch_does_not_count_while_an_editor_build_runs_and_a_while_after(monkeypatch):
    f = Fakes(monkeypatch, studio=True)
    f.alive = False
    f.looks(2)
    with f.watch.paused("prj_1"):
        f.looks(5)  # the editor is closed on purpose
        with f.watch.paused("prj_1"):  # a second build at once: counted
            pass
        f.looks(2)
    f.now = 60.0
    f.looks(5)  # the quiet after the build
    assert not f.told and not f.lost
    f.now = 121.0
    f.looks(2)
    assert not f.told  # the count started over when the pause ended: 2 looks are not 3
    f.looks(1)
    assert len(f.told) == 1 and not f.lost
    other = Fakes(monkeypatch, studio=True)
    other.alive = False
    with other.watch.paused("prj_other"):  # another project's build does not pause this one
        other.looks(3)
    assert len(other.told) == 1


# --- the check before a run goes on, and the start of the server -------------------------------------------
def test_a_studio_run_whose_editor_does_not_come_up_goes_on_with_a_notice(line, tmp_path, monkeypatch):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.comes_up = False
    monkeypatch.setattr(line.cfg, "studio_editor_wait", 6.0)
    monkeypatch.setattr(work, "studio_run", lambda run_id: True)
    told, passed = [], []
    monkeypatch.setattr(notice, "tell", lambda run_id, kind, detail: told.append((run_id, kind, detail)))
    line.fx.watch = lambda task_id, run_id: passed.append(run_id)
    line.gate.go.set()
    task = line.backlog()
    ok(line.ask(task))
    line.settled(task)
    assert line.run_of(task).status == "done" and work.EDITOR_EVENT not in reasons(line, task)  # the run went on
    assert [(kind, "응답하지 않았습니다" in detail["why"]) for _, kind, detail in told] == [("editor", True)]
    assert passed == []  # nothing to watch: the editor never came up


def test_a_studio_run_is_not_cut_by_a_lost_editor(line, tmp_path, monkeypatch):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.running = ed.answers = True
    monkeypatch.setattr(work, "studio_run", lambda run_id: True)
    told = []
    monkeypatch.setattr(notice, "tell", lambda run_id, kind, detail: told.append(kind))
    timers = Timers()
    watch = editor_watch.EditorWatch(line.engine, line.fx, api.run_inbox, line.q.holds, timer=timers)
    line.fx.watch = watch.begin
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.gate.now == 1)
    ed.answers = False
    for _ in range(5):
        timers.fire()
    assert told == ["editor"] and line.row(task)["status"] == "working" and line.run_of(task).status == "running"
    assert reasons(line, task) == [] and not run_rows(line, task)
    line.gate.go.set()
    line.settled(task)
    assert line.run_of(task).status == "done"


def test_recovery_closes_an_open_editor_question_of_a_studio_run(line, tmp_path, monkeypatch):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.running = ed.answers = True
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.gate.now == 1)
    inbox = api.run_inbox()
    work.ask(inbox, task["id"], work.EDITOR_EVENT, work.EDITOR_LOST)  # what an older server filed
    assert reasons(line, task) == ["editor_unresponsive"] and line.row(task)["status"] == "blocked"

    out = {}
    with monkeypatch.context() as patch:  # a run that is not studio's: left as it is
        patch.setattr(work, "_saved_mode", lambda engine, run_id: None)
        patch.setattr(notice, "mode", lambda run_id: None)
        work._release_editor(line.engine, inbox, out)
    assert out["editor_released"] == [] and reasons(line, task) == ["editor_unresponsive"]

    monkeypatch.setattr(notice, "mode", lambda run_id: "work")
    work._release_editor(line.engine, inbox, out)
    assert len(out["editor_released"]) == 1 and reasons(line, task) == []
    assert line.row(task)["status"] != "blocked"
