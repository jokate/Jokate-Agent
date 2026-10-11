"""Studio task runs (S-8, part 3): what follows when a task's run has stopped (§4.7 — the decision texts, the
`stage_skipped` rows, the table) and the recovery at the server's start (§4.8). A restart is a new StudioStore on
the same DB and a new queue with an empty book of slots, on the same engine — what the engine keeps of a run is
on disk. The demo relay on the mock runner, whose stages wait at a gate the test opens: no AI is called."""
import threading
import time

from relay_agent.runners import RunnerError
from relay_agent.studio import TaskMachine, api, queue, work
from relay_agent.studio.inbox import decision_text
from relay_agent.studio.tasks import SYSTEM
from test_studio_inbox import ok
from test_studio_queue import DESIGN, line  # noqa: F401 - the fixture the tests stand on
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on
from test_studio_work import STAGES
from test_studio_ws import wait


def restart(line, monkeypatch):  # noqa: F811
    """The server stops and starts again, as far as studio goes: no thread of a slot is left, the store is
    closed and opened anew on the same DB, and a new queue — its book of slots empty — stands where the old one
    stood (`fx.kick`, `api._queue`). Recovery is the test's to call."""
    wait(lambda: line.q.idle(), 30)
    old = line.store
    api.close_store()
    store = api.get_store()
    assert store is not old
    line.store = line.lab.store = line.lab.world.store = store
    line.m = TaskMachine(store, line.fx)
    line.q = queue.TaskQueue(line.engine, line.fx, api.run_inbox, limit=lambda: line.limit, retry_after=None)
    line.fx.kick = line.q.kick
    monkeypatch.setattr(api, "_queue", line.q)
    assert not line.q.slots()


def recover(line):  # noqa: F811
    return work.recover(line.engine, api.run_inbox, kick=line.q.kick)


def asked(line):  # noqa: F811
    """task id -> the reason of its pending question."""
    return {item["task_id"]: item["payload"].get("reason") for item in line.lab.pending()}


# --- §4.8: the line, the runs that were cut off, the decision texts ----------------------------------------------
def test_tasks_that_stood_in_the_line_go_on_after_a_restart(line, monkeypatch):  # noqa: F811
    line.limit = 0  # no slot: both stay in the line until the server stops
    first, second = line.backlog(title="첫째"), line.backlog(title="둘째")
    for task in (first, second):
        ok(line.ask(task))
    assert not line.q.slots() and line.stages() == []
    restart(line, monkeypatch)
    line.limit = 3
    line.gate.go.set()
    out = recover(line)  # step 3: the line is looked at once
    assert (out["requeued"], out["filed"], out["delivered"], out["broken"]) == ([], [], [], [])
    line.settled(first, second)
    assert line.stages() == [*STAGES, *STAGES] and line.gate.peak == 1  # T3 for each, one after the other
    assert [line.run_of(task).status for task in (first, second)] == ["done", "done"]
    assert [line.row(task)["status"] for task in (first, second)] == ["blocked", "blocked"]
    assert asked(line) == {first["id"]: "no_review_request", second["id"]: "no_review_request"}


def test_a_run_the_stop_cut_off_and_one_that_never_began_join_the_line_again(line, monkeypatch):  # noqa: F811
    line.limit = 0  # the queue gives no slot: T3 is this test's
    cut, fresh = line.backlog(title="끊긴 런"), line.backlog(line.project(), title="시작 전")
    for task in (cut, fresh):
        ok(line.ask(task))
        line.start(task)  # working, out of the line, its run pending
    run = line.run_of(cut)
    run.status = "running"  # the server stopped while a stage of it was at work
    line.engine.save(run)
    assert line.engine.recover_interrupted() == [run.id]  # the engine's own recovery, which comes first
    assert line.run_of(cut).status == "failed" and line.run_of(cut).error.startswith(work.RESTARTED)
    assert [line.row(task)["queued_at"] for task in (cut, fresh)] == [None, None]

    restart(line, monkeypatch)
    line.limit = 3
    line.gate.go.set()
    line.q.spawn = line.held.append
    out = recover(line)
    assert sorted(out["requeued"]) == sorted([cut["id"], fresh["id"]]) and out["filed"] == []
    assert all(line.row(task)["queued_at"] is not None for task in (cut, fresh))
    assert line.row(cut)["status"] == "working" and line.row(cut)["run_id"] == run.id  # the same run goes on
    last = line.events(cut)[-1]
    assert (last["kind"], last["detail"]) == ("queued", {"resume": True})
    assert set(line.q.slots()) == {cut["id"], fresh["id"]} and line.stages() == []
    while line.held:
        line.held.pop()()
    assert [line.run_of(task).status for task in (cut, fresh)] == ["done", "done"]
    assert line.stages() == [*STAGES, *STAGES]
    assert [event["kind"] for event in line.events(cut)][-3:] == ["queued", "unqueued", "transition"]
    assert asked(line) == {cut["id"]: "no_review_request", fresh["id"]: "no_review_request"}


def test_a_run_that_cannot_be_advanced_is_asked_about_and_not_put_back_in_the_line(line, monkeypatch):  # noqa: F811
    """The thread of a slot comes back with what `advance` raised: the run is as it was — the one the stop cut
    off would still go on by itself. It is not put back in the line, where it would end up the same way for
    ever: the system asks (§4.7 failed · working). A run that never began is not left without anyone either."""
    line.limit = 0
    cut, fresh = line.backlog(title="끊긴 런"), line.backlog(line.project(), title="시작 전")
    for task in (cut, fresh):
        ok(line.ask(task))
        line.start(task)
    run = line.run_of(cut)
    run.status = "running"
    line.engine.save(run)
    assert line.engine.recover_interrupted() == [run.id]

    restart(line, monkeypatch)
    line.limit = 3
    line.q.spawn = line.held.append

    def refuses(run_id, **kwargs):
        raise ValueError("런 폴더를 읽지 못했다")

    monkeypatch.setattr(line.engine, "advance", refuses)
    out = recover(line)
    assert sorted(out["requeued"]) == sorted([cut["id"], fresh["id"]]) and len(line.held) == 2
    for _ in range(6):  # a task put back in the line would be here again, each time
        if line.held:
            line.held.pop()()
    assert not line.held and line.q.idle()
    assert (line.run_of(cut).status, line.run_of(fresh).status) == ("failed", "pending")
    assert work.resumes(line.run_of(cut))  # the run itself still says '서버 재시작'
    for task in (cut, fresh):
        row = line.row(task)
        assert (row["status"], row["queued_at"]) == ("blocked", None)
        resumed = [e for e in line.events(task) if e["kind"] == "queued" and (e["detail"] or {}).get("resume")]
        assert len(resumed) == 1  # the recovery's, and no other
    payloads = {item["task_id"]: item["payload"] for item in line.lab.pending()}
    assert set(payloads) == {cut["id"], fresh["id"]}
    for payload in payloads.values():
        assert (payload["reason"], payload["error"]) == ("run_failed", "런 폴더를 읽지 못했다")
        assert "런 폴더를 읽지 못했다" in payload["body"]
        assert [option["id"] for option in payload["options"]] == ["retry", "wrap_up"]


def test_a_run_at_work_somewhere_else_is_waited_for_and_gets_its_row_once_it_has_stopped(line):  # noqa: F811
    """The thread of a slot comes back with 'already advancing': someone else took the run up between the
    queue's look and `advance`. Nothing is filed, and the task does not join the line — there the queue would
    go on with the run however it ended. The queue waits, looks again by itself, and does the table when the
    run has stopped: with the run's own error, not the exception's."""
    line.limit = 0
    task = line.backlog()
    ok(line.ask(task))
    _, run = line.start(task)  # working, out of the line, its run pending ...
    line.m.requeue(task["id"], SYSTEM, reason=work.AGAIN)  # ... and in the line again, as after a restart
    line.q.spawn, line.q.retry_after, line.limit = line.held.append, 0.02, 3
    assert line.q.kick() == [task["id"]] and len(line.held) == 1
    lock = line.engine._locks.setdefault(run.id, threading.Lock())
    assert lock.acquire(blocking=False)  # the dashboard, the CLI: the run is theirs now
    run.status = "running"
    line.engine.save(run)
    line.held.pop()()  # `advance` raises: the run is already advancing
    assert line.q.waits() == {task["id"]: run.id} and not line.q.slots()
    time.sleep(0.1)  # the queue looks again, more than once: the run is still at work
    row = line.row(task)
    assert (row["status"], row["queued_at"]) == ("working", None) and line.lab.pending() == []
    assert line.q.waits() == {task["id"]: run.id}

    with line.q._lock:  # no look of the queue while the run is written: it ends failed and is let go
        run.status, run.error = "failed", "[build] 빌드 도구가 죽었다"
        line.engine.save(run)
        lock.release()
    wait(lambda: line.row(task)["status"] == "blocked", 30)
    wait(lambda: line.q.idle(), 30)
    [item] = line.lab.pending()
    payload = item["payload"]
    assert (payload["reason"], payload["error"]) == ("run_failed", "[build] 빌드 도구가 죽었다")
    assert "빌드 도구가 죽었다" in payload["body"] and "already advancing" not in payload["body"]
    assert line.q.waits() == {} and line.row(task)["queued_at"] is None
    resumed = [e for e in line.events(task) if e["kind"] == "queued" and (e["detail"] or {}).get("resume")]
    assert len(resumed) == 1  # this test's, and no other: the task never joined the line by itself


def test_a_run_that_failed_before_advance_raised_is_asked_about_with_its_own_error(line, monkeypatch):  # noqa: F811
    """`advance` raises after the engine has ended the run as failed (`pipeline.py` advance: a run that is
    not `running` any more has its exception raised again). The item tells the run's error, not the
    exception's — and the task is not put back in the line, even with an error the queue would wait out."""
    line.limit = 0
    task = line.backlog()
    ok(line.ask(task))
    line.start(task)
    line.m.requeue(task["id"], SYSTEM, reason=work.AGAIN)
    line.q.spawn, line.limit = line.held.append, 3
    error = f"[-] {work.FOLDER_BUSY}run_cli)이 같은 폴더를 원본에서 수정 중"

    def fails_and_raises(run_id, **kwargs):
        run = line.engine.load(run_id)
        run.status, run.error = "failed", error
        line.engine.history.add_event(run_id, "-", "run_failed", {"error": error})
        line.engine.save(run)
        raise OSError("정지 기록을 쓰지 못했다")

    monkeypatch.setattr(line.engine, "advance", fails_and_raises)
    assert line.q.kick() == [task["id"]]
    for _ in range(3):  # a task put back in the line would be here again
        if line.held:
            line.held.pop()()
    assert not line.held and line.q.idle() and line.q.waits() == {}
    assert work.resumes(line.run_of(task))
    row = line.row(task)
    assert (row["status"], row["queued_at"]) == ("blocked", None)
    [item] = line.lab.pending()
    payload = item["payload"]
    assert (payload["reason"], payload["error"]) == ("run_failed", error)
    assert error in payload["body"] and "정지 기록을 쓰지 못했다" not in payload["body"]
    assert [option["id"] for option in payload["options"]] == ["retry", "wrap_up"]


def test_a_decision_text_that_was_not_delivered_goes_in_before_the_run_goes_on(line, monkeypatch):  # noqa: F811
    line.gate.script["plan"] = [dict(DESIGN)]
    line.gate.go.set()
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.row(task)["status"] == "blocked", 30)
    line.settled(task)
    [item] = line.lab.pending()
    run = line.run_of(task)
    assert item["kind"] == "design_approval" and run.status == "awaiting_approval"

    line.limit = 0  # the decision finds no slot ...
    with monkeypatch.context() as patch:  # ... and the server stops before the text has gone in
        patch.setattr(line.lab.hooks, "interject", lambda run_id, text: False)
        out = ok(line.lab.decide(item, "approve"))
    assert out["inbox"]["delivery"] == "pending" and out["task"]["queued_at"] is not None

    restart(line, monkeypatch)
    line.limit = 3
    line.q.spawn = line.held.append
    told = recover(line)
    assert (told["delivered"], told["requeued"], told["filed"]) == ([item["id"]], [], [])
    row = line.store.get_inbox_item(item["id"])
    assert row["delivery"] == "delivered" and row["delivered_at"]
    notes = [e["detail"]["text"] for e in line.engine.history.events(run.id) if e["kind"] == "user_interject"]
    assert notes == [decision_text(row)]
    assert line.run_of(task).status == "awaiting_approval" and set(line.q.slots()) == {task["id"]}  # then the slot
    line.held.pop()()
    assert line.run_of(task).status == "done" and line.stages() == [*STAGES]
    again = recover(line)  # a second start: nothing is owed, nothing is filed twice
    assert (again["delivered"], again["requeued"], again["filed"], again["broken"]) == ([], [], [], [])


def test_recovery_does_the_table_for_the_runs_that_stopped_unheard(line, monkeypatch):  # noqa: F811
    line.limit = 0
    line.gate.go.set()
    ended, closed = line.backlog(title="끝난 런"), line.backlog(line.project(), title="취소된 런")
    for task in (ended, closed):
        ok(line.ask(task))
        line.start(task)
    assert line.engine.advance(line.run_of(ended).id).status == "done"  # the server stopped before step 3
    line.engine.cancel(line.run_of(closed).id)
    assert line.run_of(closed).status == "cancelled"
    assert [line.row(task)["status"] for task in (ended, closed)] == ["working", "working"]

    restart(line, monkeypatch)
    out = recover(line)
    assert out["requeued"] == [] and len(out["filed"]) == 2
    assert [line.row(task)["status"] for task in (ended, closed)] == ["blocked", "blocked"]
    assert asked(line) == {ended["id"]: "no_review_request", closed["id"]: "run_cancelled"}
    assert sorted(out["filed"]) == sorted(item["id"] for item in line.lab.pending())
    assert recover(line)["filed"] == []  # blocked by now: the table has nothing more to do


def test_recovery_recomputes_the_employees_and_tells_of_a_task_without_its_item(line, monkeypatch):  # noqa: F811
    line.limit = 0
    lost, odd = line.backlog(title="결재를 잃은 작업"), line.backlog(title="줄 밖")
    ok(line.ask(lost))
    line.start(lost)
    item = work.ask(api.run_inbox(), lost["id"], "run_cancelled", work.STOPPED)  # T4: blocked, with its item
    assert line.row(lost)["status"] == "blocked"
    stamp = line.row(lost)["updated_at"]
    with line.store.transaction() as tx:  # what no path of the server does: the item is gone, a status is stale
        tx.exec("UPDATE inbox SET status = 'cancelled', closed_at = ? WHERE id = ?", (stamp, item["id"]))
        tx.exec("UPDATE employees SET status = 'working' WHERE id = ?", (odd["assignee_id"],))

    restart(line, monkeypatch)
    out = recover(line)
    assert out["broken"] == [lost["id"]] and out["employees"] == [odd["assignee_id"]]
    assert line.store.get_employee(odd["assignee_id"])["status"] == "idle"       # §4.9: no task of its own
    assert line.store.get_employee(lost["assignee_id"])["status"] == "blocked"
    assert line.row(lost)["status"] == "blocked" and line.lab.pending() == []    # told of, not mended

    def notes():
        return [m for m in line.store.list_messages("room_system")
                if (m["meta"] or {}).get("event") == "invariant_broken"]

    [note] = notes()
    assert note["meta"] == {"event": "invariant_broken", "task_id": lost["id"]} and lost["id"] in note["body"]
    assert recover(line)["broken"] == [] and len(notes()) == 1  # the next start: as it was — not told twice
    with line.store.transaction() as tx:  # mended and broken again since: the task changed after that line
        tx.exec("UPDATE tasks SET updated_at = ? WHERE id = ?", ("9999-12-31T23:59:59", lost["id"]))
    assert recover(line)["broken"] == [lost["id"]] and len(notes()) == 2
    with monkeypatch.context() as patch:  # no system room: nothing is told, so nothing is counted as told
        patch.setattr(line.store, "get_room", lambda room_id, **kwargs: None)
        assert recover(line)["broken"] == []


def test_recovery_without_a_store_only_looks_at_the_line():
    kicked = []
    out = work.recover(None, lambda: None, kick=lambda: kicked.append(1))
    assert kicked == [1] and out == {"requeued": [], "filed": [], "cut": [], "delivered": [], "employees": [],
                                     "broken": []}

    def broken():
        raise RuntimeError("boom")

    assert work.recover(None, broken, kick=broken)["requeued"] == []  # the server must start: nothing is raised


# --- §4.7: the table and the skipped stages, on the thread of a slot --------------------------------------------
def test_a_run_that_fails_is_asked_about_and_a_skipped_stage_is_written_once(line):  # noqa: F811
    stage = line.gate.run

    def breaks(call):
        if call.stage == "build":
            raise RunnerError("빌드 도구가 죽었다")
        return stage(call)

    line.gate.run = breaks
    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.gate.now == 1)  # scout is at work
    run_id = line.row(task)["run_id"]
    line.engine.history.add_event(run_id, "scout", "stage_skipped", {"reason": "돌릴 AI 가 없다"})
    [skip] = [e for e in line.engine.history.events(run_id) if e["kind"] == "stage_skipped"]
    line.gate.go.set()
    line.settled(task)

    run = line.run_of(task)
    assert run.status == "failed" and line.stages() == ["scout", "plan"] and line.row(task)["status"] == "blocked"
    [item] = line.lab.pending()
    assert (item["kind"], item["requester_kind"], item["task_id"]) == ("question", "system", task["id"])
    assert item["payload"]["reason"] == "run_failed" and item["payload"]["error"] == run.error
    assert "빌드 도구가 죽었다" in run.error and run.error[:500] in item["payload"]["body"]
    assert [option["id"] for option in item["payload"]["options"]] == ["retry", "wrap_up"]

    rows = [row for row in line.store.list_task_events(task["id"])
            if row["kind"] == "run" and (row["detail"] or {}).get("event") == "stage_skipped"]
    assert [(row["reason"], row["detail"]) for row in rows] == [
        ("돌릴 AI 가 없다", {"run_id": run_id, "stage": "scout", "event": "stage_skipped", "event_id": skip["id"]})]
    assert work.skipped(line.engine, api.run_inbox(), task["id"], run_id) == []  # the same event, not twice


def test_a_run_that_fails_because_its_folder_was_held_joins_the_line_by_itself(line):  # noqa: F811
    line.limit = 0
    task = line.backlog()
    ok(line.ask(task))
    _, run = line.start(task)
    run.status, run.error = "failed", f"[-] {work.FOLDER_BUSY}run_cli)이 같은 폴더를 원본에서 수정 중"
    line.engine.save(run)
    assert work.resumes(line.run_of(task))
    what, row = work.stopped(line.engine, api.run_inbox(), task["id"])  # as the thread of its slot would
    assert what == "requeued" and row["queued_at"] is not None and row["status"] == "working"
    assert line.lab.pending() == [] and work.stopped(line.engine, api.run_inbox(), task["id"]) is None  # in line
