"""Studio: what the server does after the answer of a T4 (기다림 = 런 종료; studio/cutoff.py, ARCHITECTURE.md
§4.3 T4 · §8.3 · §5.7 · §4.8). The demo relay on the mock runner, whose stages wait at a gate the test opens, and
a clock the test moves: no AI is called and nobody waits 60 seconds."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from relay_agent.studio import api, cutoff
from relay_agent.studio.tasks import Actor
from test_studio_inbox import PAYLOAD, ok
from test_studio_queue import DESIGN, line  # noqa: F401 - the fixture the tests stand on
from test_studio_recovery import recover, restart
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on
from test_studio_tools import ASKS, REVIEW, bearer
from test_studio_work import STAGES
from test_studio_ws import wait

ASK = dict(kind="approval", title="엔진 설정을 바꿔도 되나?", **ASKS["approval"][0])  # files a `pre_approval`


class Clock:
    """A clock the test moves: `timer(delay, fn)` is what Cutoff asks for, `advance(seconds)` runs what has
    come due, in order."""

    def __init__(self):
        self.now, self.due = 0.0, []

    def timer(self, delay, fn):
        handle = SimpleNamespace(at=self.now + delay, fn=fn)
        handle.cancel = lambda: self.due.remove(handle) if handle in self.due else None
        self.due.append(handle)
        return handle

    def advance(self, seconds):
        self.now += seconds
        while True:
            ready = sorted((handle for handle in self.due if handle.at <= self.now), key=lambda handle: handle.at)
            if not ready:
                return
            self.due.remove(ready[0])
            ready[0].fn()


class Bench:
    """`line` with a Cutoff on the clock above where the server's stands (`api._cutoff`), the engine's `cancel`
    recorded, and the MCP tools called over HTTP with the actor token of a run."""

    def __init__(self, line, monkeypatch):  # noqa: F811
        self.line, self.clock, self.cancelled, self.turns = line, Clock(), [], []
        self.cutoff = cutoff.Cutoff(line.engine, api.run_inbox, wait=60, timer=self.clock.timer)
        monkeypatch.setattr(api, "_cutoff", self.cutoff)
        monkeypatch.setattr(api, "_liveness", self.liveness)
        cancel = line.engine.cancel

        def recorded(run_id):
            self.cancelled.append(run_id)
            return cancel(run_id)

        monkeypatch.setattr(line.engine, "cancel", recorded)
        self.http = TestClient(line.owner.app)  # this PC, no token of its own

    def liveness(self, run_id):
        """The engine's, and `running` for the read turns of this test: the engine has no run for those."""
        if run_id in self.turns:
            return {"status": "running", "alive": True}
        return self.line.engine.liveness(run_id)

    def at_work(self):
        """A task whose run is at work — its first stage waits at the gate: (the task, the run's id)."""
        task = self.line.backlog()
        ok(self.line.ask(task))
        wait(lambda: self.line.gate.now == 1)
        return task, self.line.row(task)["run_id"]

    def call(self, tool, token, **args):
        r = self.http.post(f"/studio/mcp/{tool}", json=args, headers=bearer(token))
        assert r.status_code == 200, r.text
        return r.json()

    def ask(self, task, token=None):
        """`ask_owner` from the task's own run, or from the turn `token` is of: the id of the item."""
        out = self.call("ask_owner", token or api.actor_token(self.line.row(task)["run_id"]), **ASK)
        assert out["ok"] and out["end_turn"], out
        return out["inbox_id"]

    def reader(self, task):
        """A read turn of another member of the task's room (§5.7): (the employee, the turn's token)."""
        mate, _ = self.line.lab.world.staff("동료")
        room = self.line.store.list_rooms(kind="task", task_id=task["id"])[0]["id"]
        self.line.store.create_room_member(room, mate)
        run_id = f"cutoff_turn_{len(self.turns) + 1}"
        api.track_run(run_id, mode="read", employee_id=mate, room_id=room, task_id=task["id"])
        self.turns.append(run_id)
        return mate, api.actor_token(run_id)

    def pending(self):
        return [item["id"] for item in self.line.lab.pending()]


@pytest.fixture
def bench(line, monkeypatch):  # noqa: F811
    made = Bench(line, monkeypatch)
    yield made
    for run_id in made.turns:
        api.untrack_run(run_id)


# --- (1) the wait is over and the run is still at work ----------------------------------------------------------
def test_a_run_still_at_work_when_the_wait_is_over_is_cut_and_goes_on_in_its_conversation_after_t5(line, bench):  # noqa: F811
    task, run_id = bench.at_work()
    item_id = bench.ask(task)  # T4 from the task's own run: it was told to end its turn — the wait begins
    assert line.row(task)["status"] == "blocked" and bench.cutoff.waiting() == [task["id"]]
    bench.clock.advance(59)
    assert bench.cancelled == [] and line.run_of(task).status == "running" and line.gate.now == 1
    bench.clock.advance(1)
    assert bench.cancelled == [run_id] and bench.cutoff.waiting() == []
    wait(lambda: line.q.idle(), 30)  # the thread of the slot is back: the run has been written
    assert line.run_of(task).status == "cancelled"
    assert line.row(task)["status"] == "blocked" and bench.pending() == [item_id]  # no `run_cancelled` question
    session = line.run_of(task).stage_sessions[STAGES[0]]  # the conversation of the stage that was cut

    out = ok(line.lab.decide({"id": item_id}, "approve"))  # T5: the line, a slot, and the same run goes on
    assert out["task"]["status"] == "working"
    wait(lambda: line.gate.now == 1)
    bench.clock.advance(3600)  # no wait is left over
    assert bench.cancelled == [run_id]
    line.gate.go.set()
    line.settled(task)
    assert line.row(task)["run_id"] == run_id and line.run_of(task).status == "done"
    assert line.stages() == [STAGES[0], *STAGES]  # the stage that was cut, again, and then the rest
    again = line.gate.calls[1]
    assert session and session in (again.resume_session, again.session_id)  # in the conversation it had


# --- (2) the run stopped first · the decision came first ---------------------------------------------------------
def test_a_run_that_stopped_by_itself_before_the_wait_is_over_is_left_as_it_stopped(line, bench):  # noqa: F811
    line.gate.script["plan"] = [dict(DESIGN)]  # the run will stop at its design gate
    task, run_id = bench.at_work()
    item_id = bench.ask(task)
    line.gate.go.set()
    wait(lambda: line.q.idle(), 30)
    assert line.run_of(task).status == "awaiting_approval"  # a state `cancel` would take, were it asked
    bench.clock.advance(60)
    assert bench.cancelled == [] and line.run_of(task).status == "awaiting_approval"
    assert line.row(task)["status"] == "blocked" and bench.pending() == [item_id] and bench.cutoff.waiting() == []


def test_a_decision_that_comes_before_the_wait_is_over_keeps_the_run(line, bench):  # noqa: F811
    task, run_id = bench.at_work()
    item_id = bench.ask(task)
    bench.clock.advance(30)
    out = ok(line.lab.decide({"id": item_id}, "approve"))  # T5 while the run is still at work
    assert out["task"]["status"] == "working"
    bench.clock.advance(30)  # the wait is over: the task is not blocked any more and its item is decided
    assert bench.cancelled == [] and line.run_of(task).status == "running" and line.gate.now == 1
    assert bench.cutoff.waiting() == []
    line.gate.go.set()
    wait(lambda: line.q.idle(), 30)  # the run ended with the decision text still owed: it is opened again for the
    # text, and its task — in the line since the decision — is taken up once more (§4.7 '런을 이을 때' 의 `done` 행)
    [back] = [e["detail"]["to"] for e in line.engine.history.events(run_id) if e["kind"] == "run_reopened"]
    assert line.run_of(task).status == "done" and line.stages() == [*STAGES, *STAGES[STAGES.index(back):]]
    assert bench.cancelled == []  # every stage ran to its end: never cut


def test_the_queue_takes_up_again_a_run_cut_just_after_its_decision(line, bench, monkeypatch):  # noqa: F811
    """The gap of `cut` (§8.3 알려진 틈 (나)): T5 comes after its last look and before its `cancel`."""
    task, run_id = bench.at_work()
    item_id = bench.ask(task)
    cancel, seen, answers = line.engine.cancel, [], []  # the bench's `cancel`: it records, then asks the engine

    def decided_first(cut_run):
        if not seen:  # `cut` has just seen the task blocked, the item pending and the run at work
            seen.append(cut_run)
            answers.append(ok(line.lab.decide({"id": item_id}, "approve")))  # T5: `working`, and in the line
        return cancel(cut_run)

    monkeypatch.setattr(line.engine, "cancel", decided_first)
    bench.clock.advance(60)
    assert seen == [run_id] and [out["task"]["status"] for out in answers] == ["working"]
    assert bench.cancelled == [run_id] and bench.cutoff.waiting() == []  # the run was cut all the same
    # The slot is let go and the queue goes on with the run of a working task in the line: `cancelled` is one it
    # takes up (queue.NOT_TAKEN_UP). Out of the line and a stage at the gate — both are true only after that.
    wait(lambda: line.row(task)["queued_at"] is None and line.gate.now == 1, 30)
    assert line.row(task)["status"] == "working" and line.row(task)["run_id"] == run_id
    assert bench.pending() == []  # no `run_cancelled` question (§4.7 의 표, the last row)
    bench.clock.advance(3600)  # no wait is left over
    assert bench.cancelled == [run_id]
    line.gate.go.set()
    line.settled(task)
    assert line.row(task)["run_id"] == run_id and line.run_of(task).status == "done"
    assert line.stages() == [STAGES[0], *STAGES]  # the stage that was cut, again, and then the rest


# --- §5.7: a T4 from a turn that is not the task's run ------------------------------------------------------------
def test_a_t4_from_another_turn_cuts_the_task_run_at_once_and_starts_no_wait(line, bench):  # noqa: F811
    task, run_id = bench.at_work()
    mate, token = bench.reader(task)
    item_id = bench.ask(task, token)  # the task's run was not told to end its turn: no waiting for it
    assert bench.cancelled == [run_id] and bench.cutoff.waiting() == [] and bench.clock.due == []
    wait(lambda: line.q.idle(), 30)
    assert line.run_of(task).status == "cancelled"
    item = line.store.get_inbox_item(item_id)
    assert (item["requester_id"], item["run_id"], item["status"]) == (mate, run_id, "pending")
    assert line.row(task)["status"] == "blocked" and bench.pending() == [item_id]


# --- (4) T6: the run does its remaining stages ---------------------------------------------------------------------
def test_the_run_is_not_cut_after_a_review_request(line, bench):  # noqa: F811
    assert cutoff.TIMED == ("T4",)  # the one list: T6 starts no wait
    task, run_id = bench.at_work()
    out = bench.call("task_request_review", api.actor_token(run_id), **REVIEW)  # T6, through the same way in
    assert out["ok"] and out["end_turn"], out
    assert line.row(task)["status"] == "review" and bench.cutoff.waiting() == [] and bench.clock.due == []
    for caller in (run_id, "another_turn"):  # neither the wait nor the cut at once
        assert bench.cutoff.after("T6", task["id"], caller_run_id=caller, inbox_id=out["inbox_id"]) == cutoff.NONE
    bench.clock.advance(3600)
    assert bench.cancelled == [] and line.run_of(task).status == "running" and line.gate.now == 1
    line.gate.go.set()
    line.settled(task)
    assert line.run_of(task).status == "done" and line.stages() == [*STAGES]
    assert line.row(task)["status"] == "review" and bench.cancelled == []


# --- (3) the server started again inside the wait ------------------------------------------------------------------
def test_recovery_cuts_the_run_of_a_blocked_task_that_is_still_at_work(line, bench, monkeypatch):  # noqa: F811
    line.limit = 0  # the queue gives no slot: T3 is this test's, and no thread holds the runs
    asked, reviewed = line.backlog(title="묻고 기다린다"), line.backlog(line.project(), title="검수를 기다린다")
    for task in (asked, reviewed):
        ok(line.ask(task))
        line.start(task)
    box = api.run_inbox()
    item = box.create("pre_approval", Actor.employee(asked["assignee_id"]), title="바꿔도 되나?",
                      payload=PAYLOAD["pre_approval"], task_id=asked["id"])  # T4
    box.create("final_review", Actor.employee(reviewed["assignee_id"]), title="검수",
               payload=PAYLOAD["final_review"], task_id=reviewed["id"])  # T6
    for task in (asked, reviewed):  # the timers went with the server; the runs are at work in a process that lives
        run = line.run_of(task)
        run.status = "running"
        line.engine.save(run)
    assert bench.cutoff.waiting() == [] and bench.clock.due == []

    restart(line, monkeypatch)
    out = recover(line)
    assert out["cut"] == [asked["id"]] and (out["requeued"], out["filed"]) == ([], [])
    assert bench.cancelled == [line.row(asked)["run_id"]]
    assert (line.run_of(asked).status, line.run_of(reviewed).status) == ("cancelled", "running")  # T6: left alone
    assert (line.row(asked)["status"], line.row(reviewed)["status"]) == ("blocked", "review")
    kinds = {entry["task_id"]: entry["kind"] for entry in line.lab.pending()}
    assert kinds == {asked["id"]: "pre_approval", reviewed["id"]: "final_review"}  # nothing new was filed
    assert line.store.get_inbox_item(item["id"])["status"] == "pending"
    assert recover(line)["cut"] == []  # the next start: nothing is at work any more


# --- the way in, on its own · the server's own ---------------------------------------------------------------------
class Runs:
    """An engine that knows the status of its runs and what it was asked to cancel."""

    def __init__(self, **runs):
        self.runs, self.cancelled = runs, []

    def load(self, run_id):
        if run_id not in self.runs:
            raise FileNotFoundError(run_id)
        return SimpleNamespace(id=run_id, status=self.runs[run_id])

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        self.runs[run_id] = "cancelled"


def test_the_way_in_keeps_one_wait_per_task_and_starts_none_for_nothing():
    engine, clock, seconds = Runs(r1="running"), Clock(), [60]
    tasks = {"t1": {"id": "t1", "status": "blocked", "run_id": "r1"},
             "t2": {"id": "t2", "status": "blocked", "run_id": "ghost"}}
    items = {"a": {"id": "a", "status": "pending"}, "b": {"id": "b", "status": "pending"}}
    store = SimpleNamespace(get_task=tasks.get, get_inbox_item=items.get)
    made = cutoff.Cutoff(engine, lambda: SimpleNamespace(store=store), wait=lambda: seconds[0], timer=clock.timer)

    assert made.after("T4", "t1", caller_run_id="r1", inbox_id="a") == cutoff.WAITS and made.waiting() == ["t1"]
    clock.advance(30)
    items["a"]["status"] = "decided"  # T5, and the run that is still at work asks again: T4 with a new item
    assert made.after("T4", "t1", caller_run_id="r1", inbox_id="b") == cutoff.WAITS and len(clock.due) == 1
    clock.advance(30)  # the first wait would be over now: it was dropped
    assert engine.cancelled == [] and made.waiting() == ["t1"]
    clock.advance(30)
    assert engine.cancelled == ["r1"] and made.waiting() == [] and clock.due == []

    engine.runs["r1"], seconds[0] = "running", 0  # no wait at all: at once
    assert made.after("T4", "t1", caller_run_id="r1", inbox_id="b") == cutoff.CUT and engine.cancelled == ["r1", "r1"]
    seconds[0] = 60
    for task_id, run_id in (("t1", "r1"), ("t2", "ghost"), ("t3", "r1")):  # stopped · no run of this engine · no task
        assert made.after("T4", task_id, caller_run_id=run_id, inbox_id="b") == cutoff.NONE
    assert clock.due == [] and engine.cancelled == ["r1", "r1"]

    engine.runs["r1"] = "running"
    assert made.after("T4", "t1", caller_run_id="r1", inbox_id="b") == cutoff.WAITS
    tasks["t1"]["run_id"] = "r2"  # a new run took the task's place: the wait was not for it
    engine.runs["r2"] = "running"
    clock.advance(60)
    assert engine.cancelled == ["r1", "r1"]
    broken = cutoff.Cutoff(engine, lambda: 1 / 0, timer=clock.timer)  # the answer goes out whatever happens here
    assert broken.after("T4", "t1", caller_run_id="r2", inbox_id="b") == cutoff.NONE


def test_the_server_connects_its_cutoff_and_the_wait_is_the_configs(monkeypatch):
    import relay_agent.server as server

    assert server.cfg.studio_cut_after == 60 and server.task_cutoff.wait() == 60
    monkeypatch.setattr(server.cfg, "studio_cut_after", 5)
    assert server.task_cutoff.wait() == 5  # asked each time a wait begins
    assert api._cutoff is server.task_cutoff and server.task_cutoff.engine is server.engine
