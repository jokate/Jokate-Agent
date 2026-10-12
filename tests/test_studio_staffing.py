"""Studio staffing (T4): the four triggers that make HR asked about a hire, the three ways a trigger is not raised
twice, the kind `hire_recommendation` (filed, decided `interview` / `skip`, pushed quietly), and a DB that
already holds items going up to version 5 (design/studio/ARCHITECTURE.md §5.3). No model is called."""
import copy
import sqlite3
from datetime import datetime, timedelta

import pytest

from relay_agent.studio import Actor, Effects, Hooks, Inbox, InboxError, StudioStore, TaskMachine, push, stats
from relay_agent.studio import staffing as staffing_mod
from relay_agent.studio.schema import MIGRATIONS
from relay_agent.studio.staffing import Staffing, key_of
from relay_agent.studio.store import now
from relay_agent.studio.tasks import OWNER, SYSTEM

TEXT = dict(title="배경 그리기", reason="필요", spec="한 장", done_criteria="완성", type="feature")
EVIDENCE = {"task_ids": ["task_1"], "numbers": {"waiting": 6, "limit": 5}}


def rec(**over):
    """A payload of a hiring recommendation for the part `art`."""
    return {"reason": "아트 작업이 쌓인다", "role": "배경 아티스트", "model": "sonnet", "effort": "medium",
            "department_id": "art", **EVIDENCE, **over}


class Ask(Hooks):
    def __init__(self):
        self.signals = []

    def staffing_ask(self, signal):
        self.signals.append(signal)


class Fx(Effects):
    """What `create_task` calls after the filing: the staffing look, as the server wires it."""

    def __init__(self):
        self.staff = None

    def staffing(self, task, item):
        self.staff.on_task(task, item)


class World:
    def __init__(self, tmp_path, clock=datetime.now):
        self.store = StudioStore(tmp_path / "studio.sqlite")
        self.hooks, self.fx = Ask(), Fx()
        self.clock = clock
        self.staff = self.fx.staff = Staffing(self.store, ask=self.hooks.staffing_ask, backlog_limit=5,
                                              cooldown_days=3, clock=lambda: self.clock())
        self.m = TaskMachine(self.store, self.fx)
        self.box = Inbox(self.store, self.m, self.hooks, hire_cooldown_days=3)
        self.pid = self.store.create_project("게임", "D:/game", "game")["id"]
        self.other = self.store.create_project("도구", "D:/tool", "tool")["id"]
        self.proposer = self.store.create_employee("제안자", "design_setup", project_id=self.pid)["id"]

    def emp(self, name="민수", department="program", project="game"):
        pid = {"game": self.pid, "tool": self.other, None: None}[project]
        return self.store.create_employee(name, department, project_id=pid)["id"]

    def propose(self, department_id=None, project=None, **extra):
        """An employee registers a task that names a part (the payload key `department_id`)."""
        who = self.proposer
        payload = {"department_id": department_id} if department_id else {}
        return self.m.create_task(Actor.employee(who), "proposed", project or self.pid, **TEXT,
                                  inbox={"title": "제안", "payload": {**payload, **extra}})

    def hr(self):
        return self.emp("인사팀장", "hr", None)

    def inbox(self):
        return self.store.list_inbox(kind="hire_recommendation")

    def close(self):
        self.store.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


# --- the four triggers ------------------------------------------------------------------------------------------
def test_a_task_for_a_part_with_nobody_in_service_is_a_trigger_but_a_part_nobody_names_is_not(world):
    assert world.store.list_employees(department_id="art") == []  # art has no staff by nature
    world.propose()  # no part named: no trigger, however empty art is
    world.staff.sweep()
    assert world.hooks.signals == []

    outcome = world.propose("art")
    [signal] = world.hooks.signals
    assert (signal["trigger"], signal["via"], signal["key"]) == ("no_staff", "task", f"art:{world.pid}")
    assert signal["department_id"] == "art" and signal["project_id"] == world.pid
    assert signal["task_ids"] == [outcome.task["id"]] and signal["numbers"] == {"staff": 0}

    leaver = world.emp("퇴사자", "art")  # a retired employee is nobody in service
    world.store.update_employee(leaver, retired_at=now())
    assert [e for e in world.store.list_employees(department_id="art")] == []


def test_a_part_with_staff_none_of_whom_is_in_the_tasks_project_is_a_trigger(world):
    world.emp("민수", "program", "tool")  # in service, placed in the other project
    world.propose("program")
    [signal] = world.hooks.signals
    assert (signal["trigger"], signal["key"], signal["numbers"]) == (
        "no_placement", f"program:{world.pid}", {"staff": 1, "placed": 0})
    world.emp("지아", "program", "game")  # someone placed there: another task of the part is no trigger
    world.hooks.signals.clear()
    world.propose("program")
    assert world.hooks.signals == []


def test_the_part_is_read_from_the_assignee_or_the_suggested_employee_when_the_payload_names_none(world):
    gone = world.emp("퇴사자", "design_doc", "game")
    world.store.update_employee(gone, retired_at=now())
    world.propose(suggested_assignee_id=gone)  # the part of the one suggested, retired ones count
    [signal] = world.hooks.signals
    assert (signal["trigger"], signal["department_id"]) == ("no_staff", "design_doc")


def test_more_waiting_tasks_than_the_limit_in_front_of_one_employee_is_a_trigger(world):
    who = world.emp("민수", "program", "game")
    for n in range(5):
        world.m.create_task(OWNER, "backlog", world.pid, **TEXT, assignee_id=who)
    assert world.hooks.signals == []  # five do not pass the limit of five
    world.m.create_task(OWNER, "backlog", world.pid, **TEXT, assignee_id=who)
    [signal] = world.hooks.signals
    assert (signal["trigger"], signal["key"], signal["employee_id"]) == ("backlog", f"program:{world.pid}", who)
    assert signal["numbers"] == {"waiting": 6, "limit": 5} and len(signal["task_ids"]) == 6
    world.staff.backlog_limit = 0  # 0 or less = the queue is not looked at
    world.hooks.signals.clear()
    world.m.create_task(OWNER, "backlog", world.pid, **TEXT, assignee_id=who)
    assert world.hooks.signals == []


def test_an_employees_request_is_a_trigger_through_the_entrance_function(world):
    who = world.emp("민수", "program", "game")
    answer = world.staff.request(who, reason="리뷰가 밀린다")
    assert answer == {"asked": True, "key": f"program:{world.pid}", "why": "asked"}
    [signal] = world.hooks.signals
    assert (signal["trigger"], signal["via"], signal["employee_id"], signal["reason"]) == (
        "request", "request", who, "리뷰가 밀린다")
    art = world.staff.request(who, reason="배경이 필요", department_id="art", project_id=world.other)
    assert art["key"] == f"art:{world.other}" and art["asked"] is True
    # nobody in service, nothing said, no such part: not heard
    world.store.update_employee(who, retired_at=now())
    assert world.staff.request(who, reason="x") == {"asked": False, "key": None, "why": "invalid"}
    other = world.emp("지아", "program", "game")
    assert world.staff.request(other, reason="  ")["why"] == "invalid"
    assert world.staff.request(other, reason="x", department_id="없는파트")["why"] == "invalid"
    assert world.staff.request(other, reason="x", department_id="hr")["why"] == "invalid"


def test_the_daily_reports_sweep_looks_at_every_open_task_once(world):
    held = world
    held.fx.staff = type("Mute", (), {"on_task": lambda self, task, item: None})()  # the filing sees nothing
    held.propose("art")
    held.propose("art")
    held.m.create_task(OWNER, "backlog", held.pid, **TEXT)
    assert held.hooks.signals == []
    moment = datetime(2026, 3, 2, 18, 30)
    reporter = stats.Reporter(lambda: held.store, at=lambda: "18:00", clock=lambda: moment,
                              writer=lambda model: None, sweep=lambda store: held.staff.sweep())
    assert reporter.look() is not None  # the report is written, then the sweep
    assert [s["via"] for s in held.hooks.signals] == ["report"] and held.hooks.signals[0]["key"] == f"art:{held.pid}"
    assert len(held.hooks.signals[0]["task_ids"]) == 1  # the task it was found by
    assert reporter.look() is None and len(held.hooks.signals) == 1  # a day that has its report is not swept again

    def broken(store):
        raise RuntimeError("sweep")

    again = stats.Reporter(lambda: held.store, at=lambda: "18:00", clock=lambda: moment + timedelta(days=1),
                           writer=lambda model: None, sweep=broken)
    assert again.look() is not None  # a sweep that fails does not take the report with it


# --- not raised twice ---------------------------------------------------------------------------------------------
def test_a_key_with_a_recommendation_waiting_is_not_raised_or_asked_again(world):
    hr = world.hr()
    first = world.box.create("hire_recommendation", Actor.employee(hr), title="아트 파트 채용", payload=rec(),
                             project_id=world.pid)
    assert first["payload"]["key"] == f"art:{world.pid}" and first["status"] == "pending"
    with pytest.raises(InboxError) as caught:  # the same key again
        world.box.create("hire_recommendation", Actor.employee(hr), title="또", payload=rec(), project_id=world.pid)
    assert caught.value.code == "conflict" and caught.value.extra["field"] == "key"
    assert caught.value.extra["inbox"]["id"] == first["id"]
    world.propose("art")  # a trigger of that key: HR is not asked
    assert world.hooks.signals == []
    assert [i["id"] for i in world.inbox()] == [first["id"]]
    world.box.create("hire_recommendation", Actor.employee(hr), title="다른 프로젝트", payload=rec(),
                     project_id=world.other)  # another project is another key
    assert len(world.inbox()) == 2


def test_a_skipped_recommendation_is_not_raised_for_the_cooldown_days(world):
    first = world.box.create("hire_recommendation", SYSTEM, title="아트 파트 채용", payload=rec(), project_id=world.pid)
    world.box.decide(first["id"], "skip")
    with pytest.raises(InboxError) as caught:
        world.box.create("hire_recommendation", SYSTEM, title="다시", payload=rec(), project_id=world.pid)
    assert caught.value.code == "precondition_failed"
    world.propose("art")
    assert world.hooks.signals == []  # nor is HR asked while the skip is fresh

    old = (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")
    with world.store.transaction() as tx:
        tx.exec("UPDATE inbox SET closed_at = ? WHERE id = ?", (old, first["id"]))
    again = world.box.create("hire_recommendation", SYSTEM, title="다시", payload=rec(), project_id=world.pid)
    assert again["status"] == "pending" and again["id"] != first["id"]
    world.box.decide(again["id"], "interview")  # an interview is no skip: that key may be raised anew
    assert world.box.create("hire_recommendation", SYSTEM, title="셋째", payload=rec(), project_id=world.pid)


def test_hr_is_asked_about_one_key_once_in_the_cooldown(world):
    moments = [datetime(2026, 3, 2, 9, 0)]
    world.clock = lambda: moments[0]
    world.propose("art")
    world.propose("art")  # the same key, an hour later or not: once
    assert len(world.hooks.signals) == 1
    assert [r["key"] for r in world.store._rows("SELECT key FROM hire_signals")] == [f"art:{world.pid}"]
    moments[0] = datetime(2026, 3, 4, 20, 0)  # 2 days 11 hours: still inside the three days
    world.propose("art")
    assert len(world.hooks.signals) == 1
    moments[0] = datetime(2026, 3, 5, 9, 1)  # past them
    world.propose("art")
    assert len(world.hooks.signals) == 2
    world.propose("art", project=world.other)  # another project: another key
    assert len(world.hooks.signals) == 3
    world.staff.cooldown_days = 0  # 0 or less = no wait
    world.propose("art")
    assert len(world.hooks.signals) == 4


# --- the kind -----------------------------------------------------------------------------------------------------
def test_a_recommendation_is_filed_by_the_system_or_hr_and_checked(world):
    item = world.box.create("hire_recommendation", SYSTEM, title="  아트 파트 채용\n둘째 줄", payload=rec(),
                            project_id=world.pid)
    assert (item["kind"], item["title"], item["task_id"], item["run_id"], item["project_id"]) == (
        "hire_recommendation", "아트 파트 채용", None, None, world.pid)
    assert item["requester_kind"] == "system" and item["payload"]["project_id"] == world.pid
    assert world.box.view(item["id"])["allowed_decisions"] == ["interview", "skip"]
    created = [e for e in world.store.list_stream_events(limit=50) if e["type"] == "inbox.created"]
    assert created and created[-1]["data"]["inbox"]["id"] == item["id"]

    with pytest.raises(InboxError) as caught:  # the owner, a non-HR employee: not allowed to file it
        world.box.create("hire_recommendation", OWNER, title="x", payload=rec(), project_id=world.pid)
    assert caught.value.code == "actor_not_allowed"
    with pytest.raises(InboxError) as caught:
        world.box.create("hire_recommendation", Actor.employee(world.emp()), title="x", payload=rec(),
                         project_id=world.pid)
    assert caught.value.code == "actor_not_allowed"

    def fields(payload, project=None, title="제목"):
        with pytest.raises(InboxError) as e:
            world.box.create("hire_recommendation", SYSTEM, title=title, payload=payload,
                             project_id=world.other if project is None else project)
        return e.value

    assert fields(rec(task_ids=[], numbers={})).extra["fields"] == ["task_ids"]  # nothing to show for it
    assert fields(rec(numbers={"waiting": "6"})).extra["fields"] == ["numbers"]
    assert fields({**rec(), "part_name": "사운드", "part_job": "음향"}).extra["fields"] == ["department_id"]
    no_part = rec()
    del no_part["department_id"]
    assert fields(no_part).extra["fields"] == ["department_id"]
    assert fields({**no_part, "part_name": "사운드"}).extra["fields"] == ["part_job"]
    assert fields(rec(role=" ")).extra["fields"] == ["role"]
    assert fields(rec(), title=" ").extra["fields"] == ["title"]
    assert fields(rec(), project="").extra["fields"] == ["project_id"]
    assert fields(rec(department_id="없는파트")).code == "precondition_failed"
    assert fields(rec(), project="prj_none").code == "precondition_failed"
    new_part = {**no_part, "part_name": "사운드", "part_job": "음향", "part_relay": "editor-task"}
    sound = world.box.create("hire_recommendation", SYSTEM, title="새 파트", payload=new_part, project_id=world.pid)
    assert sound["payload"]["key"] == f"new:사운드:{world.pid}"


def test_the_two_decisions_write_only_the_decision(world):
    one = world.box.create("hire_recommendation", SYSTEM, title="하나", payload=rec(), project_id=world.pid)
    two = world.box.create("hire_recommendation", SYSTEM, title="둘", payload=rec(), project_id=world.other)
    for bad in ("approve", "reject", "revise"):
        with pytest.raises(InboxError) as caught:
            world.box.decide(one["id"], bad)
        assert caught.value.code == "invalid_decision" and caught.value.extra["allowed"] == ["interview", "skip"]
    with pytest.raises(InboxError) as caught:  # nothing can be carried; the notification decides approvals only
        world.box.decide(one["id"], "interview", data={"x": 1})
    assert caught.value.code == "invalid_decision"
    with pytest.raises(InboxError) as caught:
        world.box.decide(one["id"], "interview", via="notification")
    assert caught.value.code == "invalid_decision"

    done = world.box.decide(one["id"], "interview", comment="이번 주에 면담한다")
    assert done["task"] is None and done["inbox"]["status"] == "decided" and done["inbox"]["decision"] == "interview"
    assert done["inbox"]["decision_comment"] == "이번 주에 면담한다" and done["inbox"]["delivery"] == "none"
    skipped = world.box.decide(two["id"], "skip")  # no comment needed
    assert skipped["inbox"]["decision"] == "skip" and skipped["inbox"]["decision_data"] == {"via": "app"}
    with pytest.raises(InboxError) as caught:
        world.box.decide(one["id"], "skip")
    assert caught.value.code == "already_decided"
    events = [e["type"] for e in world.store.list_stream_events(limit=50)]
    assert events.count("inbox.decided") == 2
    assert inbox_text(done["inbox"]).startswith(f"[결재 {one['id']} · 채용 권장 · 면담 시작]")
    assert world.hooks.signals == []  # the interview itself is T5's: nothing was asked of HR


def inbox_text(item):
    from relay_agent.studio.inbox import decision_text
    return decision_text(item)


def test_a_recommendation_is_pushed_quietly(world):
    item = world.box.create("hire_recommendation", SYSTEM, title="아트 파트 채용", payload=rec(), project_id=world.pid)
    event = [e for e in world.store.list_stream_events(limit=50) if e["type"] == "inbox.created"][-1]
    grade, title, line, link = push.classify(event, world.store)
    assert (grade, title, line, link) == (push.QUIET, "아트 파트 채용", "채용 권장", {"type": "inbox", "id": item["id"]})


# --- a DB that already holds items goes up to version 5 -------------------------------------------------------
def test_version_5_keeps_every_item_and_index_of_a_running_db(tmp_path):
    path = tmp_path / "studio.sqlite"
    old = StudioStore(path, migrations=MIGRATIONS[:4])
    project = old.create_project("게임", "D:/game", "game")
    emp = old.create_employee("민수", "program", project_id=project["id"])
    task = old.create_task(project["id"], status="backlog", proposer_kind="owner", **{k: v for k, v in TEXT.items()})
    pending = old.create_inbox_item("task_proposal", "제안", "system", project_id=project["id"], payload={"a": 1})
    asked = old.create_inbox_item("question", "질문", "employee", requester_id=emp["id"], task_id=task["id"],
                                  payload={"reason": "asked"})
    decided = old.create_inbox_item("hire", "채용", "system", payload={"action": "hire"})
    old.update_inbox_item(decided["id"], status="decided", decision="reject", decision_comment="안 한다",
                          decision_data={"via": "app"}, closed_at=now())
    before = [old.get_inbox_item(i["id"]) for i in (pending, asked, decided)]
    order = [i["id"] for i in old.list_inbox()]
    old.close()

    store = StudioStore(path)
    try:
        assert store.migrated == [5]
        assert [store.get_inbox_item(i["id"]) for i in (pending, asked, decided)] == before  # every column
        assert [i["id"] for i in store.list_inbox()] == order
        assert (tmp_path / "studio.sqlite.bak-v4").exists()
        names = {r["name"] for r in store._rows("SELECT name FROM sqlite_master WHERE tbl_name = 'inbox'")}
        assert {"inbox_status", "inbox_task", "inbox_run", "inbox_one_pending_per_task", "inbox_undelivered"} <= names
        again = store.create_inbox_item("hire_recommendation", "권장", "system", payload={"key": "art:-"})
        assert again["kind"] == "hire_recommendation"
        with pytest.raises(sqlite3.IntegrityError):  # the partial unique index is still there
            store.create_inbox_item("question", "또", "system", task_id=task["id"], payload={})
        with pytest.raises(sqlite3.IntegrityError):  # and the CHECK of kind still holds
            store.create_inbox_item("nothing", "x", "system")
        assert store._rows("SELECT COUNT(*) AS n FROM hire_signals")[0]["n"] == 0
    finally:
        store.close()


def test_the_key_names_a_part_or_a_part_to_make():
    assert key_of("art", "prj_1") == "art:prj_1" and key_of("art") == "art:-"
    assert key_of(None, "prj_1", part_name=" 사운드 ") == "new:사운드:prj_1"
    assert staffing_mod.keys_of({"key": "a:b", "signal_key": "c:d"}) == {"a:b", "c:d"}


def test_a_look_that_fails_does_not_undo_the_filing_and_the_server_effects_hand_the_task_over(world):
    class Broken(Effects):
        def staffing(self, task, item):
            raise RuntimeError("staffing")

    outcome = TaskMachine(world.store, Broken()).create_task(OWNER, "backlog", world.pid, **TEXT)
    assert world.store.get_task(outcome.task["id"]) is not None

    from types import SimpleNamespace

    from relay_agent.studio.work import WorkEffects

    effects, seen = WorkEffects(None, SimpleNamespace(), lambda: world.store), []
    effects.staffing({"id": "t1"}, None)  # nothing wired: nothing happens
    effects.staffing_look = lambda task, item: seen.append((task["id"], item))
    effects.staffing({"id": "t2"}, {"id": "i2"})
    assert seen == [("t2", {"id": "i2"})]


# --- T4.1: an approved proposal still names its part, a question nobody heard is not kept, a payload is not written ---
def _mute(world):
    world.fx.staff = type("Mute", (), {"on_task": lambda self, task, item: None})()  # the filing sees nothing


def test_an_approved_task_still_brings_its_part_to_the_sweep_and_the_request(world):
    _mute(world)
    outcome = world.propose("art")
    world.box.decide(outcome.inbox["id"], "approve")
    task = world.store.get_task(outcome.task["id"])
    assert task["status"] == "backlog"
    [signal] = world.staff.sweep()
    assert (signal["via"], signal["key"], signal["task_ids"]) == ("report", f"art:{world.pid}", [task["id"]])
    helper = world.emp("민수", "program", "game")
    with world.store.transaction() as tx:
        tx.exec("DELETE FROM hire_signals")
    result = world.staff.request(helper, reason="배경이 필요하다", task_id=task["id"])
    assert result["key"] == f"art:{world.pid}"


def test_create_does_not_write_into_the_payload_it_was_given(world):
    payload = rec()
    before = copy.deepcopy(payload)
    item = world.box.create("hire_recommendation", SYSTEM, title="아트 파트 채용", payload=payload, project_id=world.pid)
    assert payload == before and "key" not in payload
    assert item["payload"]["key"] == f"art:{world.pid}"


def test_a_question_nobody_heard_is_not_recorded(world):
    deaf = Staffing(world.store, ask=lambda s: False, clock=lambda: world.clock())
    world.fx.staff = deaf
    world.propose("art")
    with world.store.transaction() as tx:
        assert tx.row("SELECT COUNT(*) AS n FROM hire_signals")["n"] == 0
    world.fx.staff = world.staff
    world.propose("art")  # the same key is asked now
    assert len(world.hooks.signals) == 1

    old = "2020-01-01T00:00:00"  # an older row stays as it was
    with world.store.transaction() as tx:
        tx.exec("UPDATE hire_signals SET asked_at = ?", (old,))
    assert deaf.gate({"key": f"art:{world.pid}", "trigger": "no_staff"}) == (False, "unheard")
    with world.store.transaction() as tx:
        assert tx.row("SELECT asked_at FROM hire_signals")["asked_at"] == old

    def boom(signal):
        raise RuntimeError("ask")

    with pytest.raises(RuntimeError):
        Staffing(world.store, ask=boom).gate({"key": "x:-", "trigger": "request"})
    with world.store.transaction() as tx:
        assert tx.row("SELECT 1 AS n FROM hire_signals WHERE key = 'x:-'") is None


def test_effort_is_a_closed_set_and_model_is_open(world):
    with pytest.raises(InboxError) as caught:
        world.box.create("hire_recommendation", SYSTEM, title="x", payload=rec(effort="extreme"), project_id=world.other)
    assert caught.value.extra["fields"] == ["effort"]
    assert world.box.create("hire_recommendation", SYSTEM, title="x", payload=rec(model="아무모델"),
                            project_id=world.other)
