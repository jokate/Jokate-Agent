"""Studio task runs (S-8, part 2): the line and its slots (§4.6), what the thread of a slot does when its run
stops (§4.7), and `start` · `stop` (§6.4). The demo relay on the mock runner, whose stages wait at a gate the
test opens: no AI is called."""
import json
import threading

import pytest

from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.studio import api, employees, queue
from relay_agent.studio.tasks import OWNER
from test_studio_inbox import TEXT, ok, refused
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on
from test_studio_work import FILES, STAGES, YAML, Shop
from test_studio_ws import wait

DESIGN = {"summary": "설계했다", "state": "설계 끝", "open_issues": [], "next_steps": [], "output": "설계안",
          "needs_approval": True, "approval_reason": "구조를 바꾼다"}  # what stops a run of approval `ai` at `plan`
START = "/studio/tasks/{id}/start"
STOP = "/studio/tasks/{id}/stop"


class Gate(MockRunner):
    """Every stage waits until the test opens the gate, or its run is cancelled. `now` is how many stages — so
    how many runs — are at work, `peak` the most there were at once."""

    def __init__(self):
        super().__init__()
        self.go = threading.Event()
        self._count = threading.Lock()
        self.now = self.peak = 0

    def run(self, call):
        with self._count:
            self.now += 1
            self.peak = max(self.peak, self.now)
        try:
            for _ in range(3000):
                if self.go.wait(0.01) or (call.cancel_event is not None and call.cancel_event.is_set()):
                    break
            else:
                raise AssertionError("the test never opened the gate")
            return super().run(call)
        finally:
            with self._count:
                self.now -= 1


class Line(Shop):
    """Shop with the queue connected the way server.py connects it (`fx.kick`, `api._queue`), on the runner
    above. The work of a slot runs on a thread, as on the server, unless a test puts `held.append` in
    `q.spawn`: then it is the test's to run, in place."""

    def __init__(self, lab, monkeypatch, tmp_path):  # noqa: F811
        super().__init__(lab, monkeypatch, tmp_path)
        self.tmp, self.limit, self.held = tmp_path, 3, []
        self.gate = lab.runner = Gate()
        self.repos = {"game": {"path": str(tmp_path / "project")}}
        self.q = queue.TaskQueue(lab.engine, self.fx, api.run_inbox, limit=lambda: self.limit, retry_after=None)
        self.fx.kick = self.q.kick
        monkeypatch.setattr(api, "_queue", self.q)

    def project(self):
        """Another project, in a folder and a repo of its own."""
        self.n += 1
        name = f"side{self.n}"
        (self.tmp / name).mkdir()
        self.repos[name] = {"path": str(self.tmp / name)}
        self.engine.repos = RepoRegistry(self.repos)
        return self.store.create_project(name, str(self.tmp / name), name)["id"]

    def backlog(self, pid=None, title="2단 점프", relay="demo", stage_models=None):  # noqa: A002
        """A backlog task that has not asked to start, with an assignee hired into its project."""
        pid = pid or self.pid
        self.n += 1
        emp = self.store.create_employee(f"지수{self.n}", "program", project_id=pid)["id"]
        folder = employees.folder(self.cfg.studio_dir, emp)
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in {**FILES, "employee.yaml": YAML}.items():
            (folder / name).write_bytes(body.encode("utf-8"))
        return self.m.create_task(OWNER, "backlog", pid, assignee_id=emp, relay=relay, stage_models=stage_models,
                                  **{**TEXT, "title": title}).task

    def ask(self, task, **body):
        return self.owner.post(START.format(id=task["id"]), **({"json": body} if body else {}))

    def stop(self, task):
        return self.owner.post(STOP.format(id=task["id"]))

    def row(self, task):
        return self.store.get_task(task["id"])

    def run_of(self, task):
        return self.engine.load(self.row(task)["run_id"])

    def settled(self, *tasks):
        """Wait until no slot is held, no thread of a slot is still in what follows its run (§4.7 의 2–4), and
        each of these tasks has a run and is out of the line."""
        def done():
            rows = [self.row(task) for task in tasks]
            return self.q.idle() and all(row["run_id"] and row["queued_at"] is None for row in rows)
        wait(done, 30)

    def stages(self):
        return [call.stage for call in self.gate.calls]

    def events(self, task):
        return ok(self.owner.get(f"/studio/tasks/{task['id']}/events"))["items"]


@pytest.fixture
def line(lab, monkeypatch, tmp_path):  # noqa: F811
    out = Line(lab, monkeypatch, tmp_path)
    yield out
    out.gate.go.set()  # whatever is still at work ends before the store is closed
    while out.held:
        out.held.pop()()
    wait(lambda: out.q.idle(), 30)  # the queue of now: a test may have put a new one there (a restart)


# --- the two limits (§4.6) --------------------------------------------------------------------------------------
def test_two_tasks_of_one_project_run_one_after_the_other(line):
    first, second = line.backlog(title="첫째"), line.backlog(title="둘째")
    for task in (first, second):
        row = ok(line.ask(task))  # the answer is the task in the line: T3 is the queue's
        assert (row["status"], row["run_id"]) == ("backlog", None) and row["queued_at"] is not None
    wait(lambda: line.gate.now == 1)
    assert set(line.q.slots()) == {first["id"]} and line.row(first)["status"] == "working"
    waiting = ok(line.owner.get(f"/studio/tasks/{second['id']}"))
    assert (waiting["status"], waiting["queue_position"], waiting["run_id"]) == ("backlog", 1, None)
    line.gate.go.set()
    line.settled(first, second)
    assert line.gate.peak == 1 and line.stages() == [*STAGES, *STAGES]  # the second began when the first had ended
    assert [line.run_of(task).status for task in (first, second)] == ["done", "done"]
    assert line.run_of(first).id != line.run_of(second).id


def test_other_projects_run_together_and_the_fourth_waits_for_a_slot(line):
    tasks = [line.backlog(line.project(), title=f"작업 {n}") for n in range(4)]
    for task in tasks:
        ok(line.ask(task))
    wait(lambda: line.gate.now == 3)
    assert set(line.q.slots()) == {task["id"] for task in tasks[:3]}
    assert [line.row(task)["status"] for task in tasks] == ["working", "working", "working", "backlog"]
    assert line.row(tasks[3])["queued_at"] is not None and not line.q.holds(tasks[3]["id"])
    line.gate.go.set()
    line.settled(*tasks)
    assert line.gate.peak == 3 and all(line.run_of(task).status == "done" for task in tasks)


def test_a_folder_a_run_from_outside_holds_makes_the_line_wait(line, monkeypatch):
    line.gate.go.set()
    line.q.retry_after = 0.02
    holder, asked = ["run_cli"], []

    def folder_holder(folder, exclude=None):
        asked.append(folder)
        return holder[0]

    monkeypatch.setattr(line.engine, "folder_holder", folder_holder)
    task = line.backlog()
    ok(line.ask(task))
    row = line.row(task)
    assert (row["status"], row["run_id"]) == ("backlog", None) and row["queued_at"] is not None
    assert not line.q.slots() and asked and asked[0].endswith("project")
    holder[0] = None  # the run from outside let the folder go: the queue looks again by itself
    line.settled(task)
    assert line.run_of(task).status == "done"


def test_a_start_that_makes_no_run_gives_its_slot_to_the_next_in_line(line):
    line.gate.go.set()
    line.q.spawn = line.held.append
    bad, good = line.backlog(title="없는 릴레이", relay="nope"), line.backlog(title="다음")
    ok(line.ask(bad))
    ok(line.ask(good))
    assert set(line.q.slots()) == {bad["id"]}
    line.held.pop()()  # T3 makes no run: backlog, out of the line (§4.3), and the line is looked at again
    row = line.row(bad)
    assert (row["status"], row["queued_at"], row["run_id"]) == ("backlog", None, None)
    assert [event["detail"]["phase"] for event in line.events(bad) if event["kind"] == "run"] == ["create"]
    assert set(line.q.slots()) == {good["id"]}
    line.held.pop()()
    assert line.run_of(good).status == "done" and not line.q.slots() and not line.held


# --- start (§6.4 · §9.5) ----------------------------------------------------------------------------------------
def test_start_keeps_the_override_and_refuses_one_that_cannot_run(line):
    line.gate.go.set()
    line.q.spawn = line.held.append
    task = line.backlog()
    over = refused(line.ask(task, stage_models={"build": {"model": "fable", "effort": "max"}}),
                   409, "precondition_failed")  # the file's cap: opus · high
    assert over["failed"] == ["model_over_cap", "effort_over_cap"]
    assert line.row(task)["queued_at"] is None and not line.row(task)["stage_models"] and not line.held
    assert line.owner.post(START.format(id=task["id"]), json={"relay": "demo"}).status_code == 422
    refused(line.owner.post(START.format(id="tsk_none")), 404, "not_found")

    slot = {"build": {"model": "opus", "effort": "high"}}
    row = ok(line.ask(task, stage_models=slot))
    assert (row["status"], row["stage_models"]) == ("backlog", slot) and row["queued_at"] is not None
    assert line.q.holds(task["id"]) and line.q.slots()[task["id"]]["run_id"] is None  # the slot comes before T3
    refused(line.ask(task), 409, "precondition_failed")  # in the line already
    line.held.pop()()
    run = line.run_of(task)
    assert run.status == "done" and run.stage_models["build"] == {"provider": "claude", **slot["build"]}
    assert line.row(task)["status"] == "blocked" and not line.q.slots()  # done, no review request: T4 (§4.7)

    kept = line.backlog(line.project(), stage_models={"plan": {"model": "sonnet"}})
    cleared = line.backlog(line.project(), stage_models={"plan": {"model": "sonnet"}})
    assert ok(line.ask(kept))["stage_models"] == {"plan": {"model": "sonnet"}}  # no body: what the task keeps
    assert ok(line.ask(cleared, stage_models={}))["stage_models"] == {}        # {}: the override is gone
    while line.held:
        line.held.pop()()
    assert line.run_of(kept).stage_models["plan"]["model"] == "sonnet"
    assert line.run_of(cleared).stage_models["plan"]["model"] == "opus"  # the file's slot, lowered to its cap


# --- stop (§6.4) and the run that goes on (§4.7) ----------------------------------------------------------------
def test_stop_takes_a_task_out_of_the_line_and_a_run_at_work_to_blocked(line):
    first, second = line.backlog(title="첫째"), line.backlog(title="둘째")
    ok(line.ask(first))
    ok(line.ask(second))
    wait(lambda: line.gate.now == 1)
    row = ok(line.stop(second))  # backlog, in the line: it only leaves the line
    assert (row["status"], row["queued_at"], row["run_id"]) == ("backlog", None, None)
    assert line.events(second)[-1]["kind"] == "unqueued"
    refused(line.stop(second), 409, "precondition_failed")  # nothing to stop
    refused(line.owner.post(STOP.format(id="tsk_none")), 404, "not_found")

    ok(line.stop(first))  # its run is at work: cancelled, and the thread that advanced it files the T4
    wait(lambda: line.row(first)["status"] == "blocked", 30)
    line.settled(first)
    assert line.run_of(first).status == "cancelled" and line.stages() == ["scout"]
    [item] = line.lab.pending()
    assert (item["kind"], item["task_id"], item["requester_kind"]) == ("question", first["id"], "system")
    assert item["payload"]["reason"] == "run_cancelled"
    assert [option["id"] for option in item["payload"]["options"]] == ["retry", "wrap_up"]
    assert line.row(second)["run_id"] is None  # the slot is free, and the task that left the line is not started
    refused(line.stop(first), 409, "precondition_failed")  # blocked: nothing to stop


def decided(line):
    """(first, second): the first task's run stopped at its design gate and the owner approved it (T5) while
    the second task's run holds the project — so the first is working and stands in the line again."""
    line.gate.script["plan"] = [dict(DESIGN)]
    line.gate.go.set()
    first = line.backlog(title="첫째")
    ok(line.ask(first))
    wait(lambda: line.row(first)["status"] == "blocked", 30)
    line.settled(first)
    [item] = line.lab.pending()
    assert item["kind"] == "design_approval" and line.run_of(first).status == "awaiting_approval"
    line.gate.go.clear()
    second = line.backlog(title="둘째")
    ok(line.ask(second))
    wait(lambda: line.gate.now == 1)
    out = ok(line.lab.decide(item, "approve"))
    assert out["task"]["status"] == "working" and out["task"]["queued_at"] is not None
    assert set(line.q.slots()) == {second["id"]} and line.run_of(first).status == "awaiting_approval"
    assert ok(line.owner.get(f"/studio/tasks/{first['id']}"))["queue_position"] == 1
    return first, second


def test_a_run_that_goes_on_after_a_decision_waits_for_a_slot_too(line):
    first, second = decided(line)
    line.gate.go.set()
    line.settled(first, second)
    assert [line.run_of(task).status for task in (first, second)] == ["done", "done"]
    assert line.stages() == ["scout", "plan", *STAGES, "build", "review"] and line.gate.peak == 1
    assert [event["kind"] for event in line.events(first)][-2:] == ["unqueued", "transition"]
    assert line.row(first)["status"] == "blocked"  # the run ended without a review request: the system asks (§4.7)
    assert [item["payload"]["reason"] for item in line.lab.pending()] == ["no_review_request"] * 2


def test_stop_of_a_working_task_in_the_line_takes_it_out_and_blocks_it(line):
    first, second = decided(line)
    row = ok(line.stop(first))  # its run has stopped: out of the line, and T4 at once
    assert (row["status"], row["queued_at"]) == ("blocked", None)
    [item] = [item for item in line.lab.pending() if item["task_id"] == first["id"]]
    assert (item["kind"], item["requester_kind"], item["payload"]["reason"]) == ("question", "system", "run_cancelled")
    assert [event["kind"] for event in line.events(first)][-2:] == ["unqueued", "transition"]
    line.gate.go.set()
    line.settled(second)
    assert line.run_of(first).status == "awaiting_approval" and line.stages() == ["scout", "plan", *STAGES]


def test_stop_between_the_last_look_of_a_slots_thread_and_advance_runs_no_stage(line, monkeypatch):
    """The slot's thread has seen that it was not stopped and `engine.advance` is not in yet: a `stop` that comes
    there sets the slot's cancel event, which `advance` is given — the run is not revived (resume=True)."""
    first, second = decided(line)
    line.q.spawn = line.held.append  # the slot the first task gets is this test's to run
    line.gate.go.set()
    wait(lambda: line.held, 30)
    work = line.held.pop()
    status, seen = line.q._status, []

    def stop_first(run_id):  # `_go` asks for the run's status right after that look: the owner stops just then
        seen.append(run_id)
        if len(seen) == 1:
            line.q.stop(first["id"])
        return status(run_id)

    monkeypatch.setattr(line.q, "_status", stop_first)  # not before the slot was given: `_pick` asks under the lock
    work()
    assert seen[0] == line.row(first)["run_id"] and not line.q.slots()
    assert line.run_of(first).status == "cancelled" and line.run_of(second).status == "done"
    assert line.stages() == ["scout", "plan", *STAGES]  # before the event went to `advance`: + build · review, done
    assert line.row(first)["status"] == "blocked"
    [item] = [item for item in line.lab.pending() if item["task_id"] == first["id"]]
    assert (item["kind"], item["requester_kind"], item["payload"]["reason"]) == ("question", "system", "run_cancelled")


def test_advance_with_a_cancel_event_that_is_set_already_runs_no_stage(line):
    """The engine's side of it: `advance(cancel=)` goes on the caller's event, and one that was set before the call
    was in ends the run cancelled — no stage is run, and a cancelled run is not revived."""
    line.limit = 0  # the queue gives no slot: T3 and `advance` are this test's
    task = line.backlog()
    ok(line.ask(task))
    row, run = line.start(task)
    assert (row["status"], run.status) == ("working", "pending") and not line.q.slots()
    cancel = threading.Event()
    cancel.set()
    assert line.engine.advance(run.id, cancel=cancel).status == "cancelled"
    assert line.engine.advance(run.id, resume=True, cancel=cancel).status == "cancelled"
    assert line.engine.load(run.id).status == "cancelled" and line.stages() == []


# --- the server's own queue · the saved contract ----------------------------------------------------------------
def test_the_server_connects_its_queue_and_the_limit_is_the_configs(monkeypatch):
    import relay_agent.server as server

    assert server.cfg.studio_max_runs == 3 and server.task_queue.limit() == 3
    monkeypatch.setattr(server.cfg, "studio_max_runs", 1)
    assert server.task_queue.limit() == 1  # asked each time the line is looked at
    assert server.task_runs.kick == server.task_queue.kick and api._queue is server.task_queue
    assert server.task_queue.engine is server.engine and server.task_queue.effects is server.task_runs


def test_openapi_json_holds_start_and_stop():
    saved = json.loads(api.OPENAPI_PATH.read_text(encoding="utf-8"))
    schema = api.openapi_schema()
    for path in (START, STOP):
        assert saved["paths"][path] == schema["paths"][path], path
    assert saved["components"]["schemas"]["Start"] == schema["components"]["schemas"]["Start"]
    assert not schema["paths"][START]["post"]["requestBody"].get("required")  # `start` takes no body too
    assert "requestBody" not in schema["paths"][STOP]["post"]
