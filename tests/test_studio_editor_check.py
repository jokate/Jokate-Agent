"""The editor check of a task's run (M-7; §4.3 T3 · §4.5 · §4.7): before a run that uses the editor goes on, and
while it is at work. The demo relay on the mock runner and an editor that is a list of processes and a port that
answers or does not: no Unreal editor is run and no AI is called."""
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay_agent.repos import RepoRegistry
from relay_agent.studio import api, editor, editor_watch, work
from test_studio_editor import row as process
from test_studio_inbox import ok
from test_studio_queue import line  # noqa: F401 - the fixture the tests stand on
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on
from test_studio_work import STAGES
from test_studio_ws import wait

DESIGN = Path(__file__).resolve().parent.parent / "design" / "studio"
PORT = 8101
RETRY = {"data": {"option_id": "retry"}}


class Ed:
    """An editor that is not there: `running` is whether its process is in the list, `answers` whether its port
    does. Started, it runs; it answers after the first wait unless `comes_up` is False (a modal window, a
    crash). Time passes only when the check sleeps."""

    def __init__(self, uproject):
        self.uproject = uproject
        self.running = self.answers = False
        self.comes_up = True
        self.spawned, self.slept, self.asked, self.now = [], [], 0, 0.0

    def processes(self):
        return [process(77, self.uproject)] if self.running else []

    def healthy(self, port):
        self.asked += 1
        return port == PORT and self.running and self.answers

    def spawn(self, argv, cwd):
        self.spawned.append(list(argv))
        self.running = True

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.running and self.comes_up:
            self.answers = True

    def clock(self):
        return self.now


@pytest.fixture(autouse=True)
def outside_the_studio(monkeypatch):
    """The tests below are the contract of a run outside the studio (question + cut), which is unchanged. The
    runs of `line` do have an employee and a room, so they would count as studio runs: the studio's own
    contract (told, not cut) is tests/test_studio_editor_build.py."""
    monkeypatch.setattr(work, "studio_run", lambda run_id: False)


def uses(line, on):  # noqa: F811
    """The repository of the project names the editor's MCP server, or not."""
    repo = line.store.get_project(line.pid)["repo"]
    line.repos[repo] = {**line.repos[repo], "mcp": [work.EDITOR_MCP] if on else []}
    line.engine.repos = RepoRegistry(line.repos)


def wire(line, tmp_path, *, port=PORT, mcp=True):  # noqa: F811
    """The project becomes a UE project — a .uproject, an engine that is a file with the right name, a port —
    and the effects read the fake editor instead of this computer."""
    folder = Path(line.store.get_project(line.pid)["path"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Game.uproject").write_text("{}", encoding="utf-8")
    engine = tmp_path / "UE_5.7"
    exe = engine.joinpath(*editor.EDITOR_EXE)
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")  # never run: only where it is matters
    line.store.update_project(line.pid, engine_version="5.7", **({"mcp_port": port} if port else {}))
    uses(line, mcp)
    ed = Ed(folder / "Game.uproject")
    line.fx.editor_engines = lambda: [{"version": "5.7", "path": str(engine), "exists": True}]
    line.fx.editor_processes, line.fx.editor_healthy = ed.processes, ed.healthy
    line.fx.editor_tools = editor.Tools(spawn=ed.spawn, sleep=ed.sleep, clock=ed.clock)
    return ed


def questions(line, task):  # noqa: F811
    return [item for item in line.lab.pending() if item["task_id"] == task["id"]]


def reasons(line, task):  # noqa: F811
    return [item["payload"].get("reason") for item in questions(line, task)]


def run_rows(line, task):  # noqa: F811
    return [event for event in line.store.list_task_events(task["id"]) if event["kind"] == "run"]


# --- before the run goes on (§4.3 T3) --------------------------------------------------------------------------
def test_an_editor_that_is_off_is_started_and_the_run_goes_on(line, tmp_path):  # noqa: F811
    ed = wire(line, tmp_path)
    passed = []
    line.fx.watch = lambda task_id, run_id: passed.append((task_id, run_id))
    line.gate.go.set()
    task = line.backlog()
    ok(line.ask(task))
    line.settled(task)
    assert len(ed.spawned) == 1 and ed.spawned[0][1] == str(ed.uproject)  # the engine's editor, this .uproject
    assert ed.slept == [editor.POLL]  # it was waited for, and answered
    assert line.run_of(task).status == "done" and line.stages() == [*STAGES]
    assert passed ==[(task["id"], line.row(task)["run_id"])]  # the run that passed is handed to the watch
    assert work.EDITOR_EVENT not in reasons(line, task)


def test_an_editor_that_does_not_answer_blocks_the_task_and_retry_checks_again(line, tmp_path, monkeypatch):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.comes_up = False
    monkeypatch.setattr(line.cfg, "studio_editor_wait", 6.0)
    line.gate.go.set()
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.row(task)["status"] == "blocked", 30)
    line.settled(task)
    first, = questions(line, task)
    assert first["kind"] == "question" and first["payload"]["reason"] == "editor_unresponsive"
    assert "6초 안에 응답하지 않았습니다" in first["payload"]["body"] and first["payload"]["error"]
    assert sum(ed.slept) == 6.0 and len(ed.spawned) == 1  # started, then the wait of the settings
    assert line.run_of(task).status == "pending" and line.stages() == []  # the run was not advanced
    run_id = line.row(task)["run_id"]

    ok(line.lab.decide(first, "answer", **RETRY))  # still not answering: the check before the run goes on again
    wait(lambda: line.row(task)["status"] == "blocked", 30)
    line.settled(task)
    second, = questions(line, task)
    assert second["id"] != first["id"] and second["payload"]["reason"] == "editor_unresponsive"
    assert len(ed.spawned) == 1 and line.run_of(task).status == "pending" and line.stages() == []

    ed.answers = True  # the owner closed the window
    asked = ed.asked
    ok(line.lab.decide(second, "answer", **RETRY))
    line.settled(task)
    assert ed.asked > asked and len(ed.spawned) == 1  # looked at again; an editor that runs is not started twice
    assert line.row(task)["run_id"] == run_id and line.run_of(task).status == "done" and line.stages() == [*STAGES]
    assert work.EDITOR_EVENT not in reasons(line, task)


def test_an_editor_that_cannot_be_started_is_asked_about_the_same_way(line, tmp_path):  # noqa: F811
    ed = wire(line, tmp_path)
    line.fx.editor_engines = lambda: []  # the project's engine is not on this computer
    line.gate.go.set()
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.row(task)["status"] == "blocked", 30)
    line.settled(task)
    assert reasons(line, task) == ["editor_unresponsive"] and not ed.spawned and not ed.slept
    assert line.run_of(task).status == "pending"


def test_a_stop_while_the_editor_is_waited_for_ends_the_wait_with_one_question(line, tmp_path, monkeypatch):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.comes_up = False
    monkeypatch.setattr(line.cfg, "studio_editor_wait", 60.0)
    passed = []
    line.fx.watch = lambda task_id, run_id: passed.append((task_id, run_id))
    task = line.backlog()

    def sleep(seconds):  # the owner stops the task during the second wait
        ed.sleep(seconds)
        if len(ed.slept) == 2:
            line.q.stop(task["id"])

    line.fx.editor_tools = dataclasses.replace(line.fx.editor_tools, sleep=sleep)
    line.gate.go.set()
    ok(line.ask(task))
    wait(lambda: line.row(task)["status"] == "blocked", 30)
    line.settled(task)
    assert reasons(line, task) == ["run_cancelled"]  # the one item of a stop: nothing is asked about the editor
    assert len(ed.slept) == 2 and len(ed.spawned) == 1  # the wait ended there, 4 of its 60 seconds in
    assert line.run_of(task).status == "cancelled" and line.stages() == [] and passed == []
    run_id = line.row(task)["run_id"]

    ed.comes_up = True  # `retry`: the check again, and the same run goes on
    item, = questions(line, task)
    ok(line.lab.decide(item, "answer", **RETRY))
    line.settled(task)
    assert line.row(task)["run_id"] == run_id and line.run_of(task).status == "done" and line.stages() == [*STAGES]
    assert passed == [(task["id"], run_id)] and len(ed.spawned) == 1
    assert work.EDITOR_EVENT not in reasons(line, task)


def test_the_check_asks_nothing_about_the_editor_of_a_task_that_was_stopped(line, tmp_path, monkeypatch):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.comes_up = False
    passed, seen = [], []
    line.fx.watch = lambda task_id, run_id: passed.append((task_id, run_id))
    line.limit = 0  # the queue gives no slot: T3 and the check are this test's
    task = line.backlog()
    ok(line.ask(task))
    row, run = line.start(task)
    inbox = api.run_inbox()
    assert (row["status"], run.status) == ("working", "pending")

    assert line.fx.editor_ready(inbox, row, run.id, stopped=lambda: True) is False  # stopped before the look
    assert not ed.spawned and ed.asked == 0 and questions(line, task) == []

    def healthy(port):  # the owner stops while the port is asked
        seen.append(port)
        return ed.healthy(port)

    line.fx.editor_healthy = healthy
    monkeypatch.setattr(line.cfg, "studio_editor_wait", 0)
    assert line.fx.editor_ready(inbox, row, run.id, stopped=lambda: bool(seen)) is False  # no answer, and stopped
    assert len(ed.spawned) == 1 and not ed.slept and questions(line, task) == []
    ed.answers = True
    assert line.fx.editor_ready(inbox, row, run.id, stopped=lambda: bool(seen)) is False  # it answers, and stopped
    assert passed == [] and questions(line, task) == [] and line.row(task)["status"] == "working"

    assert line.fx.editor_ready(inbox, row, run.id, stopped=lambda: False) is True  # not stopped: as with no `stopped`
    assert passed == [(task["id"], run.id)]
    ed.answers = False
    assert line.fx.editor_ready(inbox, row, run.id, stopped=lambda: False) is False
    assert reasons(line, task) == ["editor_unresponsive"] and line.row(task)["status"] == "blocked"
    assert line.run_of(task).status == "pending" and len(ed.spawned) == 1


def test_a_task_that_does_not_use_the_editor_is_not_checked(line, tmp_path):  # noqa: F811
    ed = wire(line, tmp_path, port=None)  # the repository names the editor's server, the project has no port
    passed = []
    line.fx.watch = lambda task_id, run_id: passed.append(task_id)
    line.gate.go.set()
    first = line.backlog(title="포트 없는 프로젝트")
    ok(line.ask(first))
    line.settled(first)
    line.store.update_project(line.pid, mcp_port=PORT)  # a port, and no run that uses the editor's server
    uses(line, False)
    second = line.backlog(title="MCP 없는 작업")
    ok(line.ask(second))
    line.settled(second)
    assert [line.run_of(task).status for task in (first, second)] == ["done", "done"]
    assert ed.asked == 0 and not ed.spawned and not ed.slept and passed == []
    assert line.fx.editor_alive(line.store, line.row(second)) is False  # asked now, once: nothing answers
    assert ed.asked == 1


def test_which_runs_use_the_editor(monkeypatch):
    stages = [SimpleNamespace(mcp=[]), SimpleNamespace(mcp=["unreal"]), SimpleNamespace(mcp=[])]
    monkeypatch.setattr(work.RelaySpec, "load", staticmethod(lambda path: (SimpleNamespace(stages=stages), None)))
    run = SimpleNamespace(mcp=["studio"], relay="demo.yaml", index=0)
    assert work.EDITOR_MCP == "unreal"
    assert work.uses_editor(run) is True  # a stage still to run
    run.index = 2
    assert work.uses_editor(run) is False  # that stage is behind it
    assert work.uses_editor(run, SimpleNamespace(mcp=["unreal"])) is True  # the repository's own
    assert work.uses_editor(SimpleNamespace(mcp=["unreal", "studio"], relay="demo.yaml", index=2)) is True

    def gone(path):
        raise OSError("없다")
    monkeypatch.setattr(work.RelaySpec, "load", staticmethod(gone))
    assert work.uses_editor(SimpleNamespace(mcp=None, relay="demo.yaml", index=0)) is False


def test_bring_up_starts_waits_and_gives_up(line, tmp_path):  # noqa: F811
    ed = wire(line, tmp_path)
    project = line.store.get_project(line.pid)
    how = {"processes": ed.processes, "healthy": ed.healthy, "tools": line.fx.editor_tools}
    engines = line.fx.editor_engines()
    assert editor.bring_up(project, [project], engines, **how) is True and len(ed.spawned) == 1
    assert editor.bring_up(project, [project], engines, **how) is False and len(ed.spawned) == 1  # answers at once
    ed.answers, ed.comes_up = False, False
    with pytest.raises(editor.Refused) as e:
        editor.bring_up(project, [project], engines, timeout=4, **how)
    assert e.value.failed == ["editor_timeout"] and "4초" in e.value.message and len(ed.spawned) == 1
    with pytest.raises(editor.Refused) as e:
        editor.bring_up({**project, "mcp_port": None}, [project], engines, **how)
    assert e.value.failed == ["mcp_port_missing"]


# --- while the run is at work (§4.5) ---------------------------------------------------------------------------
class Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Timers:
    """A clock the test turns: `fire()` is the oldest timer going off."""

    def __init__(self):
        self.due, self.handles = [], []

    def __call__(self, delay, fn):
        self.due.append((delay, fn))
        self.handles.append(Handle())
        return self.handles[-1]

    def fire(self):
        self.due.pop(0)[1]()


def test_an_editor_lost_while_the_run_is_at_work_cuts_the_run(line, tmp_path):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.running = ed.answers = True
    timers = Timers()
    watch = editor_watch.EditorWatch(line.engine, line.fx, api.run_inbox, line.q.holds, timer=timers)
    line.fx.watch = watch.begin
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.gate.now == 1)
    run_id = line.row(task)["run_id"]
    assert not ed.spawned and watch.watching() == [task["id"]]
    assert [delay for delay, _ in timers.due] == [editor_watch.EVERY] and (watch.every, watch.misses) == (30, 3)

    timers.fire()  # it answers: the watch goes on
    ed.answers = False
    timers.fire()
    timers.fire()
    ed.answers = True  # two looks without an answer, then one with: a long command, not a lost editor
    timers.fire()
    ed.answers = False
    timers.fire()
    timers.fire()
    assert line.row(task)["status"] == "working" and line.run_of(task).status == "running" and len(timers.due) == 1
    timers.fire()  # the third in a row
    assert line.row(task)["status"] == "blocked"
    line.settled(task)
    assert line.run_of(task).status == "cancelled" and watch.watching() == [] and not timers.due
    item, = questions(line, task)  # one question: no `run_cancelled` besides
    assert item["payload"]["reason"] == "editor_unresponsive" and item["payload"]["body"] == work.EDITOR_LOST
    last = run_rows(line, task)[-1]
    assert last["reason"] == work.EDITOR_CUT and last["detail"] == {"run_id": run_id, "event": "editor_unresponsive"}

    ed.answers = True  # the editor is back: `retry` checks it and the same run goes on
    line.gate.go.set()
    ok(line.lab.decide(item, "answer", **RETRY))
    line.settled(task)
    assert line.row(task)["run_id"] == run_id and line.run_of(task).status == "done"
    assert watch.watching() == [task["id"]]  # watched anew, until the next look finds the run stopped
    timers.fire()
    assert watch.watching() == [] and not timers.due


def test_a_task_that_waits_already_gets_no_second_question_when_its_editor_is_lost(line, tmp_path):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.running = ed.answers = True
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.gate.now == 1)
    inbox, run_id = api.run_inbox(), line.row(task)["run_id"]
    assert work.editor_lost(line.engine, inbox, task["id"], "run_other") is False  # not the task's run
    work.ask(inbox, task["id"], "run_failed", "다른 일로 묻는다", error="다른 일")  # blocked, its run still at work
    assert work.editor_lost(line.engine, inbox, task["id"], run_id) is True
    line.settled(task)
    assert reasons(line, task) == ["run_failed"] and line.run_of(task).status == "cancelled"
    assert [row["detail"]["event"] for row in run_rows(line, task)] == ["editor_unresponsive"]
    assert work.editor_lost(line.engine, inbox, task["id"], run_id) is False  # the run is not at work any more
    assert len(run_rows(line, task)) == 1


def test_a_run_that_ended_before_the_cut_keeps_its_question_and_gets_a_row(line, tmp_path, monkeypatch):  # noqa: F811
    ed = wire(line, tmp_path)
    ed.running = ed.answers = True
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.gate.now == 1)
    inbox, run_id = api.run_inbox(), line.row(task)["run_id"]

    def ended(run_id):  # the run ended between the look at it and the cut
        raise ValueError(f"run {run_id} is already done")

    with monkeypatch.context() as patch:  # not `undo()`: that would take the patches of `line` with it
        patch.setattr(line.engine, "cancel", ended)
        assert work.editor_lost(line.engine, inbox, task["id"], run_id) is False  # not cut
    assert line.row(task)["status"] == "blocked" and reasons(line, task) == ["editor_unresponsive"]
    last = run_rows(line, task)[-1]  # the question stays, and the row says what became of the run
    assert last["reason"] == work.EDITOR_ENDED != work.EDITOR_CUT
    assert last["detail"] == {"run_id": run_id, "event": "editor_unresponsive"}

    line.gate.go.set()  # the run ends by itself: the task waits already, nothing is asked besides
    line.settled(task)
    assert line.run_of(task).status == "done" and reasons(line, task) == ["editor_unresponsive"]
    assert len(run_rows(line, task)) == 1

    item, = questions(line, task)  # `retry`: the editor is checked and the same run is taken up again
    asked = ed.asked
    ok(line.lab.decide(item, "answer", **RETRY))
    line.settled(task)
    assert ed.asked > asked and line.row(task)["run_id"] == run_id and line.run_of(task).status == "done"
    assert work.EDITOR_EVENT not in reasons(line, task)


def test_a_watch_ends_when_there_is_nothing_left_to_watch():
    task = {"id": "tsk_1", "run_id": "run_1", "status": "working", "project_id": "prj_1"}
    state = {"task": task, "holds": True, "alive": True, "store": True}
    inbox = SimpleNamespace(store=SimpleNamespace(get_task=lambda task_id: state["task"]))
    fx = SimpleNamespace(editor_alive=lambda store, row: state["alive"])

    def began(**over):
        state.update({"task": task, "holds": True, "alive": True, "store": True, **over})
        timers = Timers()
        watch = editor_watch.EditorWatch(None, fx, lambda: inbox if state["store"] else None,
                                         lambda task_id: state["holds"], timer=timers)
        assert watch.begin("tsk_1", "run_1") is True and watch.watching() == ["tsk_1"]
        return watch, timers

    for over in ({"holds": False}, {"alive": None}, {"store": False}, {"task": None},
                 {"task": {**task, "run_id": "run_2"}}, {"task": {**task, "status": "done"}}):
        watch, timers = began(**over)
        timers.fire()
        assert watch.watching() == [] and not timers.due, over

    watch, timers = began()
    timers.fire()
    assert watch.watching() == ["tsk_1"] and len(timers.due) == 1  # something to watch: again

    def broken(store, row):
        raise RuntimeError("포트를 읽지 못했다")
    fx.editor_alive = broken
    timers.fire()  # a look that fails ends the watch and raises nothing
    assert watch.watching() == [] and not timers.due
    fx.editor_alive = lambda store, row: state["alive"]

    watch, timers = began()
    old = timers.due[0][1]
    assert watch.begin("tsk_1", "run_1") is True and timers.handles[0].cancelled  # the older watch is dropped
    old()  # and its timer, had it fired all the same, looks at nothing
    assert len(timers.due) == 2 and watch.watching() == ["tsk_1"]

    off = editor_watch.EditorWatch(None, fx, lambda: inbox, lambda task_id: True, every=0, timer=timers)
    assert off.begin("tsk_1", "run_1") is False and off.watching() == []


# --- the server and the documents --------------------------------------------------------------------------------
def test_the_server_connects_the_check_and_the_watch(lab):  # noqa: F811
    import relay_agent.server as server

    assert server.cfg.studio_editor_wait == 300
    assert server.task_runs.watch == server.task_editor_watch.begin
    assert server.task_editor_watch.holds == server.task_queue.holds
    assert server.task_editor_watch.watching() == []  # no timer until a run that uses the editor has passed
    assert server.task_queue.effects is server.task_runs and callable(server.task_runs.editor_ready)


def test_the_documents_say_what_was_settled():
    text = (DESIGN / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "(잠정, M-7)" not in text
    assert "| `relay_agent/studio/editor_watch.py` |" in text and "`Config.studio_editor_wait`(기본 300초)" in text
    assert "`pipeline.py` 1107–1137, 호출 1677–1679" in text and "`uses_editor`" in text
    assert "`editor` 는 `null` 로 확정한다" in text
    assert "기다리는 동안 owner 가 `stop` 하면" in text and "결재는 `run_cancelled` 하나다" in text
    assert "런이 이미 끝나 있으면 끊지 않는다" in text and "| M-7 | 확정 (MCP 전송 형식은 잠정) |" in text
    assert "| `RelayEngine._advance` | `pipeline.py` 1620–1679 |" in text and "1510–1565" not in text
    manual = (DESIGN / "VERIFICATION.md").read_text(encoding="utf-8")
    assert "M-7 작업 시작 전 에디터 검사와 무응답 결재" in manual and "M-7 꺼진 상태에서 스크린샷 요청 → 이미지" in manual
