"""Task and campaign state machine (S-2): every cell of the §4.4 table, who may do what, the task_events row
of each transition, and the hooks where runs / gates / apply are injected (fakes here — no AI is called)."""
import re
from pathlib import Path

import pytest

from relay_agent.studio import Actor, Effects, StudioStore, TaskMachine, TaskNotFound, TransitionError
from relay_agent.studio import tasks
from relay_agent.studio.store import now
from relay_agent.studio.tasks import OWNER, SYSTEM

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
S = ("proposed", "backlog", "working", "blocked", "review", "done", "rejected", "cancelled")
GRID = {  # §4.4, copied by hand: row = now, column = next (in the order of S)
    "proposed":  "F0 T1 F1 F1 F1 F1 T2 F10",
    "backlog":   "F6 F0 T3 F2 F2 F2 F5 T10",
    "working":   "F6 F7 F0 T4 T6 F3 F5 F10",
    "blocked":   "F6 F7 T5 F0 F4 F3 F5 T11",
    "review":    "F6 T9 T8 F8 F0 T7 F5 F10",
    "done":      "F9 F9 F9 F9 F9 F0 F9 F9",
    "rejected":  "F9 F9 F9 F9 F9 F9 F0 F9",
    "cancelled": "F9 F9 F9 F9 F9 F9 F9 F0",
}
CELLS = [(frm, to, code) for frm, line in GRID.items() for to, code in zip(S, line.split())]
ALLOWED_CELLS = [c for c in CELLS if c[2].startswith("T")]
FORBIDDEN_CELLS = [c for c in CELLS if c[2].startswith("F")]
PAIR = {code: (frm, to) for frm, to, code in ALLOWED_CELLS}
WHO = {  # the "누가" column of §4.2
    "T1": "owner", "T2": "owner", "T3": "system", "T4": "employee system", "T5": "owner", "T6": "employee",
    "T7": "owner", "T8": "owner", "T9": "owner", "T10": "owner", "T11": "owner",
}
ACTORS = ("owner", "employee", "system")
WRONG = [(rule, kind) for rule, who in WHO.items() for kind in ACTORS if kind not in who.split()]

TEXT = dict(title="2단 점프", reason="조작감", spec="공중에서 한 번 더", done_criteria="테스트 통과", type="feature")
STEP = {  # rule -> (to, the actor of §4.2, arguments of a plain call)
    "T1": ("backlog", "owner", {}),
    "T2": ("rejected", "owner", {"comment": "지금은 안 한다"}),
    "T3": ("working", "system", {}),
    "T4": ("blocked", "employee", {"inbox": {"kind": "question", "title": "점프 높이는?",
                                             "payload": {"question": "점프 높이는?"}}}),
    "T5": ("working", "owner", {"decision": "answer", "comment": "2미터"}),
    "T6": ("review", "employee", {"inbox": {"title": "검수 요청", "payload": {"summary": "끝"}}}),
    "T7": ("done", "owner", {}),
    "T8": ("working", "owner", {"comment": "착지가 어색하다"}),
    "T9": ("backlog", "owner", {}),
    "T10": ("cancelled", "owner", {"reason": "방향이 바뀌었다"}),
    "T11": ("cancelled", "owner", {"reason": "방향이 바뀌었다"}),
}
PATHS = {  # state -> (how the task is created, the transitions that lead there)
    "proposed": ("proposed", []), "rejected": ("proposed", ["T2"]),
    "backlog": ("backlog", []), "cancelled": ("backlog", ["T10"]), "working": ("backlog", ["T3"]),
    "blocked": ("backlog", ["T3", "T4"]), "review": ("backlog", ["T3", "T6"]),
    "done": ("backlog", ["T3", "T6", "T7"]),
}


class FakeEffects(Effects):
    """Stands in for runs, the queue, the harness gate and apply / rollback (S-8 · S-11 · S-12 · M-7)."""

    def __init__(self):
        self.calls = []   # (hook, key) in call order
        self.runs = 0
        self.fail = {}    # (hook, key) -> exception the hook raises
        self.on = {}      # hook -> callable run once inside the hook (another request getting in between)

    def _hit(self, hook, key, *args):
        self.calls.append((hook, key))
        if hook in self.on:
            self.on.pop(hook)(*args)
        if (hook, key) in self.fail:
            raise self.fail[(hook, key)]

    def create_run(self, task):
        self._hit("create_run", task["id"], task)
        self.runs += 1
        return f"run_{self.runs:03d}"

    def cancel_run(self, run_id):
        self._hit("cancel_run", run_id)

    def check(self, rule, task):
        self._hit("check", rule, task)

    def before(self, rule, task):
        self._hit("before", rule, task)

    def after(self, what, row, event_ids):
        self._hit("after", what, row, event_ids)


class World:
    def __init__(self, store):
        self.store = store
        self.fx = FakeEffects()
        self.m = TaskMachine(store, self.fx)
        self.project = store.create_project("게임", "D:/게임", "game")
        self.emp = store.create_employee("민수", "program", project_id=self.project["id"])
        self.mate = store.create_employee("지아", "program", project_id=self.project["id"])
        self.me = Actor.employee(self.emp["id"])
        self.other = Actor.employee(self.mate["id"])

    def actor(self, kind):
        return {"owner": OWNER, "system": SYSTEM, "employee": self.me}[kind]

    def propose(self, **over):
        return self.m.create_task(self.me, "proposed", self.project["id"], **{**TEXT, **over}).task

    def register(self, **over):
        fields = {"assignee_id": self.emp["id"], **TEXT, **over}
        return self.m.create_task(OWNER, "backlog", self.project["id"], **fields).task

    def step(self, task_id, rule, **over):
        """One allowed transition, done by the actor of §4.2 (T3: after the owner's start request)."""
        to, who, kwargs = STEP[rule]
        if rule == "T3" and self.store.get_task(task_id)["queued_at"] is None:
            self.m.request_start(task_id, OWNER)
        return self.m.transition(task_id, to, self.actor(who), **{**kwargs, **over})

    def reach(self, state, **over):
        start, path = PATHS[state]
        task = self.propose(**over) if start == "proposed" else self.register(**over)
        for rule in path:
            self.step(task["id"], rule)
        return self.store.get_task(task["id"])

    def events(self, task_id):
        return self.store.list_task_events(task_id)

    def pending(self, task_id):
        items = self.store.list_inbox(task_id=task_id, status="pending")
        return items[0] if items else None

    def stream(self, after=0):
        return self.store.list_stream_events(after_id=after)

    def mark(self):
        rows = self.stream()
        return rows[-1]["id"] if rows else 0

    def types(self, after):
        return [row["type"] for row in self.stream(after)]

    def snapshot(self, task_id):
        return (self.store.get_task(task_id), len(self.events(task_id)), self.store.list_inbox(task_id=task_id),
                len(self.stream()), len(self.fx.calls), self.store.get_employee(self.emp["id"]))


@pytest.fixture
def world(tmp_path):
    store = StudioStore(tmp_path / "studio" / "studio.sqlite")
    yield World(store)
    store.close()


def refused(code, call, *args, **kwargs):
    with pytest.raises(TransitionError) as err:
        call(*args, **kwargs)
    assert err.value.code == code, err.value.detail
    assert err.value.detail.strip()
    return err.value


# --- the table itself ---------------------------------------------------------------------------------
def test_grid_is_the_documents_table():
    section = DOC.read_text(encoding="utf-8").split("### 4.4 64칸 분류")[1].split("### 4.5")[0]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    assert tuple(c.strip() for c in lines[0].strip("|").split("|"))[1:] == S
    doc = {}
    for line in lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        doc[cells[0].strip("*")] = " ".join(cells[1:])
    assert doc == GRID
    assert (len(CELLS), len(ALLOWED_CELLS), len(FORBIDDEN_CELLS)) == (64, 11, 53)


def test_module_tables_are_the_grid():
    assert tasks.STATES == S
    for frm, to, code in CELLS:
        assert tasks.classify(frm, to) == code
    assert (len(tasks.ALLOWED), len(tasks.FORBIDDEN)) == (11, 53)
    assert {r: set(who) for r, who in tasks.RULE_ACTORS.items() if r.startswith("T")} == \
        {r: set(who.split()) for r, who in WHO.items()}
    assert tasks.RULE_ACTORS["C1"] == ("employee",) and tasks.RULE_ACTORS["C2"] == ("owner",)
    assert len(tasks.EVENT_KINDS) == 12 and len(tasks.BLOCKING_KINDS) == 6
    with pytest.raises(ValueError):
        tasks.classify("backlog", "paused")


def test_tasks_module_imports_nothing_of_the_engine():
    source = Path(tasks.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^(?:from|import)\s+(\S+)", source, re.M)
    assert [m for m in imports if m.startswith((".", "relay_agent"))] == [".store"]


# --- all 64 cells -------------------------------------------------------------------------------------
@pytest.mark.parametrize("frm,to,rule", ALLOWED_CELLS, ids=[f"{c[2]}-{c[0]}-{c[1]}" for c in ALLOWED_CELLS])
def test_allowed_cell_moves_and_records_who_when_why(world, frm, to, rule):
    task = world.reach(frm)
    seen = len(world.events(task["id"]))
    out = world.step(task["id"], rule)
    assert out.rule == rule and out.task["status"] == to == world.store.get_task(task["id"])["status"]
    rows = [e for e in world.events(task["id"])[seen:] if e["kind"] == "transition"]
    assert rows == [out.event]
    who = world.actor(STEP[rule][1])
    assert (out.event["rule"], out.event["from_status"], out.event["to_status"]) == (rule, frm, to)
    assert (out.event["actor_kind"], out.event["actor_id"]) == (who.kind, who.id)
    assert STAMP.fullmatch(out.event["at"]) and out.event["reason"].strip()
    moved = [r for r in world.stream() if r["type"] == "task.transitioned"][-1]["data"]
    assert (moved["rule"], moved["from"], moved["to"], moved["event_id"]) == (rule, frm, to, out.event["id"])
    assert world.fx.calls[-1] == ("after", rule)


@pytest.mark.parametrize("frm,to,code", FORBIDDEN_CELLS, ids=[f"{c[2]}-{c[0]}-{c[1]}" for c in FORBIDDEN_CELLS])
def test_forbidden_cell_is_refused_for_every_actor(world, frm, to, code):
    task = world.reach(frm)
    before = world.snapshot(task["id"])
    for kind in ACTORS:
        err = refused("illegal_transition", world.m.transition, task["id"], to, world.actor(kind))
        assert (err.from_status, err.to_status, err.rule) == (frm, to, code)
        assert err.as_dict() == {"detail": err.detail, "code": "illegal_transition", "from": frm, "to": to}
    assert world.snapshot(task["id"]) == before


@pytest.mark.parametrize("rule,kind", WRONG, ids=[f"{r}-{k}" for r, k in WRONG])
def test_actor_outside_the_who_column_is_refused(world, rule, kind):
    frm, to = PAIR[rule]
    task = world.reach(frm)
    before = world.snapshot(task["id"])
    err = refused("actor_not_allowed", world.m.transition, task["id"], to, world.actor(kind), **STEP[rule][2])
    assert (err.from_status, err.to_status, err.rule) == (frm, to, rule)
    assert err.as_dict() == {"detail": err.detail, "code": "actor_not_allowed"}
    assert world.snapshot(task["id"]) == before


def test_employee_cannot_complete_or_cancel(world):
    review = world.reach("review")
    for actor in (world.me, world.other):  # A1: even the assignee who asked for the review
        refused("actor_not_allowed", world.m.transition, review["id"], "done", actor)
    working = world.reach("working")
    assert refused("illegal_transition", world.m.transition, working["id"], "done", world.me).rule == "F3"
    backlog, blocked = world.reach("backlog"), world.reach("blocked")
    refused("actor_not_allowed", world.m.transition, backlog["id"], "cancelled", world.me)
    refused("actor_not_allowed", world.m.transition, blocked["id"], "cancelled", world.me)
    states = [world.store.get_task(t["id"])["status"] for t in (review, working, backlog, blocked)]
    assert states == ["review", "working", "backlog", "blocked"]


# --- C1 · C2 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("to", ["proposed", "backlog"])
@pytest.mark.parametrize("blank", [("reason",), ("spec",), ("done_criteria",), ("title",),
                                   ("reason", "spec", "done_criteria")], ids="+".join)
@pytest.mark.parametrize("value", ["", "  \n", None], ids=["empty", "space", "none"])
def test_creation_with_an_empty_required_field_is_refused(world, to, blank, value):
    actor = world.me if to == "proposed" else OWNER
    fields = {**TEXT, **{name: value for name in blank}}
    err = refused("missing_fields", world.m.create_task, actor, to, world.project["id"], **fields)
    assert sorted(err.fields) == sorted(blank)
    assert err.as_dict() == {"detail": err.detail, "code": "missing_fields", "fields": err.fields}
    assert world.store.list_tasks() == [] and world.store.list_inbox() == [] and world.stream() == []


def test_c1_files_a_proposal_and_c2_opens_the_room(world):
    extra = {"payload": {"suggested_assignee_id": world.mate["id"]}}
    out = world.m.create_task(world.me, "proposed", world.project["id"], **TEXT, inbox=extra)
    task = out.task
    assert (task["status"], task["proposer_kind"], task["proposer_id"]) == ("proposed", "employee", world.emp["id"])
    [event] = world.events(task["id"])
    assert event == out.event and STAMP.fullmatch(event["at"])
    assert (event["kind"], event["rule"], event["from_status"], event["to_status"]) == ("created", "C1", None, "proposed")
    assert (event["actor_kind"], event["actor_id"], event["reason"]) == ("employee", world.emp["id"], TEXT["reason"])
    item = world.pending(task["id"])
    assert item == out.inbox and event["detail"] == {"inbox_id": item["id"]}
    assert (item["kind"], item["requester_kind"], item["requester_id"]) == ("task_proposal", "employee", world.emp["id"])
    assert (item["title"], item["run_id"], item["project_id"]) == (TEXT["title"], None, world.project["id"])
    assert item["payload"] == {**TEXT, "suggested_assignee_id": world.mate["id"]}
    assert out.room is None and world.store.list_rooms(kind="task") == []
    assert world.types(0) == ["task.created", "inbox.created"] and len(out.stream_ids) == 2

    mark = world.mark()
    out = world.m.create_task(OWNER, "backlog", world.project["id"], **TEXT, assignee_id=world.emp["id"])
    assert (out.task["status"], out.task["proposer_kind"], out.task["proposer_id"]) == ("backlog", "owner", None)
    assert out.task["assignee_id"] == world.emp["id"]
    [event] = world.events(out.task["id"])
    assert (event["kind"], event["rule"], event["from_status"], event["to_status"], event["actor_kind"]) == \
        ("created", "C2", None, "backlog", "owner")
    assert out.inbox is None and world.pending(out.task["id"]) is None
    assert out.room["kind"] == "task" and out.room["task_id"] == out.task["id"]
    assert world.types(mark) == ["task.created", "room.updated"]
    assert [c for c in world.fx.calls if c[0] == "after"] == [("after", "C1"), ("after", "C2")]


def test_c1_takes_a_suggested_assignee_without_checking_employment(world):
    pid = world.project["id"]
    gone = world.store.create_employee("현우", "program", project_id=pid)
    world.store.update_employee(gone["id"], retired_at=now())
    out = world.m.create_task(world.me, "proposed", pid, **TEXT, assignee_id=gone["id"])  # §4.3 C1: only the proposer
    tid = out.task["id"]
    assert out.task["assignee_id"] is None and out.inbox["payload"]["suggested_assignee_id"] == gone["id"]

    extra = {"payload": {"suggested_assignee_id": world.mate["id"]}}  # a payload that names one wins
    both = world.m.create_task(world.me, "proposed", pid, **TEXT, assignee_id=world.emp["id"], inbox=extra)
    assert both.task["assignee_id"] is None and both.inbox["payload"]["suggested_assignee_id"] == world.mate["id"]

    # the check comes where the suggestion would become the assignee: C2 and the edits of T1
    refused("precondition_failed", world.m.create_task, OWNER, "backlog", pid, **TEXT, assignee_id=gone["id"])
    refused("precondition_failed", world.m.transition, tid, "backlog", OWNER, edits={"assignee_id": gone["id"]})
    assert world.store.get_task(tid) == out.task and world.pending(tid) == out.inbox


def test_creation_follows_who_and_where(world):
    pid = world.project["id"]
    for actor, to in ((OWNER, "proposed"), (SYSTEM, "proposed"), (world.me, "backlog"), (SYSTEM, "backlog")):  # A4
        assert refused("actor_not_allowed", world.m.create_task, actor, to, pid, **TEXT).to_status == to
    err = refused("illegal_transition", world.m.create_task, OWNER, "working", pid, **TEXT)
    assert err.as_dict()["from"] is None and err.as_dict()["to"] == "working"
    with pytest.raises(ValueError):
        world.m.create_task(OWNER, "backlog", pid, **{**TEXT, "type": "epic"})

    other = world.store.create_project("툴", "D:/툴", "tool")
    campaign = world.store.create_campaign(other["id"], "1막", "첫 판")
    for over in ({"parent_id": "task_none"}, {"campaign_id": campaign["id"]}, {"assignee_id": "emp_none"}):
        assert refused("precondition_failed", world.m.create_task, OWNER, "backlog", pid, **TEXT, **over).failed == []
    refused("precondition_failed", world.m.create_task, OWNER, "backlog", "prj_none", **TEXT)
    world.store.update_employee(world.emp["id"], retired_at=now())
    refused("precondition_failed", world.m.create_task, world.me, "proposed", pid, **TEXT)
    assert world.store.list_tasks() == []


# --- one life, row by row -----------------------------------------------------------------------------
def test_every_step_appends_its_task_events_rows(world):
    tid = world.propose()["id"]
    expected = [("created", "C1", None, "proposed", "employee")]
    life = [
        ("T1", [("transition", "T1", "proposed", "backlog", "owner"), ("assigned", None, None, None, "owner")]),
        ("start", [("queued", None, None, None, "owner")]),
        ("T3", [("transition", "T3", "backlog", "working", "system")]),
        ("T4", [("transition", "T4", "working", "blocked", "employee")]),
        ("T5", [("transition", "T5", "blocked", "working", "owner")]),
        ("resume", [("unqueued", None, None, None, "system")]),
        ("T6", [("transition", "T6", "working", "review", "employee")]),
        ("T8", [("transition", "T8", "review", "working", "owner")]),
        ("resume", [("unqueued", None, None, None, "system")]),
        ("T6", [("transition", "T6", "working", "review", "employee")]),
        ("T9", [("transition", "T9", "review", "backlog", "owner")]),
        ("start", [("queued", None, None, None, "owner")]),
        ("T3", [("transition", "T3", "backlog", "working", "system")]),
        ("T6", [("transition", "T6", "working", "review", "employee")]),
        ("T7", [("transition", "T7", "review", "done", "owner")]),
    ]
    for what, rows in life:
        if what == "start":
            world.m.request_start(tid, OWNER)
        elif what == "resume":
            world.m.unqueue(tid, SYSTEM, reason="슬롯을 얻어 런을 잇는다")
        elif what == "T1":
            world.step(tid, "T1", edits={"assignee_id": world.emp["id"]})
        else:
            world.step(tid, what)
        expected += rows
        log = world.events(tid)
        assert [(e["kind"], e["rule"], e["from_status"], e["to_status"], e["actor_kind"]) for e in log] == expected
    assert all(STAMP.fullmatch(e["at"]) and e["reason"].strip() for e in log)
    assert all((e["actor_kind"] == "employee") == (e["actor_id"] == world.emp["id"]) for e in log)
    assert [e["id"] for e in log] == sorted(e["id"] for e in log)
    task = world.store.get_task(tid)
    assert task["status"] == "done" and task["run_id"] == "run_002" and task["finished_at"]


# --- rule by rule -------------------------------------------------------------------------------------
def test_t1_applies_the_edits_and_opens_the_room(world):
    task = world.propose()
    item = world.pending(task["id"])
    refused("missing_fields", world.m.transition, task["id"], "backlog", OWNER, edits={"spec": "  "})
    refused("precondition_failed", world.m.transition, task["id"], "backlog", OWNER, edits={"assignee_id": "emp_none"})
    refused("precondition_failed", world.m.transition, task["id"], "backlog", OWNER, inbox_id="inb_other")
    with pytest.raises(ValueError):
        world.m.transition(task["id"], "backlog", OWNER, edits={"depends_on": []})
    assert world.store.get_task(task["id"]) == task and world.pending(task["id"]) == item

    mark = world.mark()
    edits = {"title": "3단 점프", "assignee_id": world.emp["id"], "spec": TEXT["spec"]}
    out = world.m.transition(task["id"], "backlog", OWNER, inbox_id=item["id"], comment="담당만 정했다", edits=edits)
    assert (out.task["title"], out.task["assignee_id"], out.task["spec"]) == ("3단 점프", world.emp["id"], TEXT["spec"])
    log = world.events(task["id"])
    assert [(e["kind"], e["rule"]) for e in log] == \
        [("created", "C1"), ("transition", "T1"), ("edited", None), ("assigned", None)]
    assert log[2]["detail"] == {"fields": {"title": {"from": "2단 점프", "to": "3단 점프"}}}
    assert log[3]["detail"] == {"from": None, "to": world.emp["id"]}
    assert out.event["detail"] == {"inbox_id": item["id"], "decision": "approve"}
    assert (out.inbox["status"], out.inbox["decision"], out.inbox["decision_comment"], out.inbox["delivery"]) == \
        ("decided", "approve", "담당만 정했다", "none")
    assert out.inbox["closed_at"] and out.inbox["id"] == item["id"]
    assert out.room == world.store.list_rooms(kind="task", task_id=task["id"])[0]
    assert world.types(mark) == ["inbox.decided", "task.transitioned", "task.updated", "task.updated", "room.updated"]


def test_t2_closes_the_proposal_with_the_comment_as_the_reason(world):
    task = world.propose()
    with pytest.raises(ValueError):
        world.m.transition(task["id"], "rejected", OWNER, decision="approve")
    out = world.step(task["id"], "T2")
    assert (out.task["status"], out.task["run_id"]) == ("rejected", None) and out.task["finished_at"]
    assert out.event["reason"] == "지금은 안 한다"
    assert (out.inbox["status"], out.inbox["decision"], out.inbox["decision_comment"], out.inbox["delivery"]) == \
        ("decided", "reject", "지금은 안 한다", "none")
    assert world.store.list_rooms(kind="task", task_id=task["id"]) == []


def test_t3_makes_the_run_first_and_seats_the_assignee(world):
    task = world.reach("backlog")
    refused("precondition_failed", world.m.transition, task["id"], "working", SYSTEM)  # nobody asked to start
    assert ("create_run", task["id"]) not in world.fx.calls
    world.m.request_start(task["id"], OWNER)
    mark, calls = world.mark(), len(world.fx.calls)
    out = world.m.transition(task["id"], "working", SYSTEM)
    assert world.fx.calls[calls:] == [("check", "T3"), ("create_run", task["id"]), ("after", "T3")]
    assert (out.task["run_id"], out.task["queued_at"]) == ("run_001", None) and out.task["started_at"]
    assert out.event["detail"] == {"run_id": "run_001"}
    assert world.store.get_room_member(out.room["id"], world.emp["id"])["left_at"] is None
    assert world.store.get_employee(world.emp["id"])["status"] == "working"
    rows = world.stream(mark)
    assert [r["type"] for r in rows] == ["task.transitioned", "employee.status", "room.updated"]
    assert set(rows[0]["data"]) == {"task", "rule", "from", "to", "actor", "reason", "event_id", "inbox_id"}
    assert rows[0]["data"]["actor"] == {"kind": "system", "id": None} and rows[0]["data"]["inbox_id"] is None
    assert rows[0]["data"]["task"] == out.task and list(out.stream_ids) == [r["id"] for r in rows]
    assert rows[1]["data"] == {"employee_id": world.emp["id"], "status": "working", "prev": "idle"}
    assert [m["employee_id"] for m in rows[2]["data"]["members"]] == [world.emp["id"]]


def test_t3_stays_in_backlog_and_records_when_the_run_cannot_be_made(world):
    task = world.reach("backlog")
    world.m.request_start(task["id"], OWNER)
    world.fx.fail[("create_run", task["id"])] = ValueError("릴레이 없음: studio-task")
    mark = world.mark()
    err = refused("precondition_failed", world.m.transition, task["id"], "working", SYSTEM)
    assert err.failed == [] and "릴레이 없음" in err.detail
    row = world.store.get_task(task["id"])
    assert (row["status"], row["queued_at"], row["run_id"], row["started_at"]) == ("backlog", None, None, None)
    last = world.events(task["id"])[-1]
    assert (last["kind"], last["rule"], last["to_status"], last["actor_kind"]) == ("run", None, None, "system")
    assert last["detail"] == {"error": "릴레이 없음: studio-task", "phase": "create"} and "릴레이 없음" in last["reason"]
    [event] = world.stream(mark)
    assert event["type"] == "task.updated" and event["data"]["change"] == "run"
    assert world.fx.calls[-1] == ("after", "run")

    plain = TaskMachine(world.store)  # nothing wired: the default Effects cannot make a run either
    world.m.request_start(task["id"], OWNER)
    refused("precondition_failed", plain.transition, task["id"], "working", SYSTEM)
    assert world.store.get_task(task["id"])["status"] == "backlog"


def test_t3_closes_the_new_run_when_the_commit_fails(world):
    task = world.reach("backlog")
    world.m.request_start(task["id"], OWNER)
    world.fx.on["create_run"] = lambda row: world.m.unqueue(task["id"], OWNER)  # the owner takes the request back
    refused("stale_state", world.m.transition, task["id"], "working", SYSTEM)
    assert world.fx.calls[-1] == ("cancel_run", "run_001")
    row = world.store.get_task(task["id"])
    assert (row["status"], row["run_id"], row["queued_at"]) == ("backlog", None, None)


def test_a_raising_after_hook_leaves_the_change_committed(world):
    boom = RuntimeError("push 실패")  # §4.5: `after` runs once the commit is done, and the engine does not swallow it
    world.fx.fail[("after", "C2")] = boom
    with pytest.raises(RuntimeError) as err:
        world.register()
    assert err.value is boom
    [task] = world.store.list_tasks()
    assert task["status"] == "backlog" and [e["rule"] for e in world.events(task["id"])] == ["C2"]
    assert world.types(0) == ["task.created", "room.updated"]

    world.m.request_start(task["id"], OWNER)
    mark = world.mark()
    world.fx.fail[("after", "T3")] = boom
    with pytest.raises(RuntimeError) as err:
        world.step(task["id"], "T3")
    assert err.value is boom
    row = world.store.get_task(task["id"])
    assert (row["status"], row["run_id"], row["queued_at"]) == ("working", "run_001", None)
    assert not [c for c in world.fx.calls if c[0] == "cancel_run"]  # the run stays: cancel_run is for a failed commit
    assert world.events(task["id"])[-1]["rule"] == "T3" and "task.transitioned" in world.types(mark)
    assert refused("illegal_transition", world.m.transition, task["id"], "working", SYSTEM).rule == "F0"

    campaign = world.store.create_campaign(world.project["id"], "1막", "첫 판")
    world.fx.fail[("after", "campaign")] = boom
    with pytest.raises(RuntimeError) as err:
        world.m.transition_campaign(campaign["id"], "running", OWNER)
    assert err.value is boom
    fresh = world.store.get_campaign(campaign["id"])
    assert fresh["status"] == "running" and fresh["started_at"]
    assert world.stream()[-1]["type"] == "campaign.updated"


def test_t4_files_a_blocking_item_and_leaves_the_line(world):
    task = world.reach("blocked")
    decided = world.step(task["id"], "T5").inbox  # working again, waiting for a slot
    assert world.store.get_task(task["id"])["queued_at"]
    seen, mark = len(world.events(task["id"])), world.mark()
    ask = {"kind": "budget", "title": "예산 한도\n둘째 줄", "payload": {"limit_usd": 5}}
    out = world.m.transition(task["id"], "blocked", SYSTEM, inbox=ask)
    assert [(e["kind"], e["rule"]) for e in world.events(task["id"])[seen:]] == [("unqueued", None), ("transition", "T4")]
    assert out.task["queued_at"] is None and out.event["reason"] == "예산 한도\n둘째 줄"
    item = world.pending(task["id"])
    assert item == out.inbox and item["title"] == "예산 한도"
    assert (item["kind"], item["requester_kind"], item["requester_id"]) == ("budget", "system", None)
    assert (item["run_id"], item["task_id"], item["payload"]) == (task["run_id"], task["id"], {"limit_usd": 5})
    assert out.event["detail"] == {"run_id": task["run_id"], "inbox_id": item["id"]}
    assert world.store.get_employee(world.emp["id"])["status"] == "blocked"
    assert world.types(mark) == ["inbox.created", "task.transitioned", "employee.status", "task.updated"]
    assert world.store.get_inbox_item(decided["id"])["delivery"] == "pending"  # the earlier decision still waits


def test_t4_checks_the_kind_and_the_room(world):
    task = world.reach("working")
    ask = lambda kind: {"inbox": {"kind": kind, "title": "?"}}
    refused("actor_not_allowed", world.m.transition, task["id"], "blocked", world.other, **ask("question"))
    refused("actor_not_allowed", world.m.transition, task["id"], "blocked", world.me, **ask("budget"))
    refused("actor_not_allowed", world.m.transition, task["id"], "blocked", SYSTEM, **ask("pre_approval"))
    for bad in ({}, ask("final_review")):
        with pytest.raises(ValueError):
            world.m.transition(task["id"], "blocked", SYSTEM, **bad)
    assert world.store.get_task(task["id"]) == task and world.pending(task["id"]) is None
    room = world.store.list_rooms(kind="task", task_id=task["id"])[0]
    world.store.create_room_member(room["id"], world.mate["id"])  # the owner put a second employee in
    out = world.m.transition(task["id"], "blocked", world.other, **ask("structure_choice"))
    assert (out.inbox["requester_id"], out.inbox["run_id"]) == (world.mate["id"], task["run_id"])
    assert refused("illegal_transition", world.m.transition, task["id"], "blocked", world.me, **ask("question")).rule == "F0"


def test_t5_decides_queues_and_resets_the_exchange_count_of_a_limit_item(world):
    task = world.reach("working")
    world.store.update_task(task["id"], exchange_count=20)
    limit = {"kind": "question", "title": "왕복 한도", "payload": {"reason": "exchange_limit"}}
    world.m.transition(task["id"], "blocked", SYSTEM, inbox=limit)
    for bad in ({}, {"decision": "approve"}):  # no default: a forgotten value must not become an approval
        with pytest.raises(ValueError):
            world.m.transition(task["id"], "working", OWNER, **bad)
    assert world.store.get_task(task["id"])["status"] == "blocked"
    mark = world.mark()
    data = {"option_id": "continue", "via": "app"}
    out = world.m.transition(task["id"], "working", OWNER, decision="answer", data=data)
    assert out.task["exchange_count"] == 0 and out.task["queued_at"] and out.task["run_id"] == task["run_id"]
    assert (out.inbox["status"], out.inbox["decision"], out.inbox["delivery"]) == ("decided", "answer", "pending")
    assert out.inbox["decision_data"] == data and out.inbox["delivered_at"] is None
    assert out.event["detail"]["decision"] == "answer"
    assert world.types(mark) == ["inbox.decided", "task.transitioned", "employee.status"]

    other = world.reach("working")
    world.store.update_task(other["id"], exchange_count=7)
    world.step(other["id"], "T4")
    assert world.step(other["id"], "T5").task["exchange_count"] == 7
    world.step(other["id"], "T4", inbox={"kind": "question", "title": "또"})
    assert world.step(other["id"], "T5", reset_exchange=True).task["exchange_count"] == 0


def test_t6_needs_a_running_run_and_passes_the_gate_codes_through(world):
    task = world.reach("blocked")
    world.step(task["id"], "T5")  # working, but waiting for a slot
    assert refused("precondition_failed", world.m.transition, task["id"], "review", world.me).failed == []
    world.m.unqueue(task["id"], SYSTEM)
    world.fx.fail[("check", "T6")] = TransitionError("precondition_failed", "검증 표기가 없다",
                                                     failed=["verification_missing"])
    err = refused("precondition_failed", world.m.transition, task["id"], "review", world.me)
    assert err.failed == ["verification_missing"] and err.as_dict()["failed"] == ["verification_missing"]
    assert world.store.get_task(task["id"])["status"] == "working" and world.pending(task["id"]) is None
    refused("actor_not_allowed", world.m.transition, task["id"], "review", world.other)  # A2: not the assignee
    world.fx.fail.clear()
    mark = world.mark()
    out = world.step(task["id"], "T6")
    assert (out.inbox["kind"], out.inbox["requester_id"], out.inbox["run_id"]) == \
        ("final_review", world.emp["id"], task["run_id"])
    assert out.inbox["payload"] == {"summary": "끝"} and out.inbox["status"] == "pending"
    assert world.types(mark) == ["inbox.created", "task.transitioned", "employee.status"]
    assert world.store.get_employee(world.emp["id"])["status"] == "idle"


def test_inbox_created_leaves_the_package_out_of_the_stream(world):
    task = world.reach("working")
    out = world.step(task["id"], "T6", inbox={"payload": {"summary": "끝", "package": {"diff": "x" * 50}}})
    assert out.inbox["payload"]["package"] == {"diff": "x" * 50} and out.inbox["title"] == TEXT["title"]
    created = [r for r in world.stream() if r["type"] == "inbox.created"][-1]["data"]["inbox"]
    assert created["payload"] == {"summary": "끝"} and created["id"] == out.inbox["id"]


def test_t7_runs_its_side_effects_before_the_commit_and_drops_undelivered_decisions(world):
    task = world.reach("blocked")
    first = world.step(task["id"], "T5").inbox  # delivery pending
    world.m.unqueue(task["id"], SYSTEM)
    world.step(task["id"], "T6")
    review = world.pending(task["id"])
    world.fx.fail[("before", "T7")] = TransitionError("precondition_failed", "커밋 실패", failed=["commit_failed"])
    assert refused("precondition_failed", world.m.transition, task["id"], "done", OWNER).failed == ["commit_failed"]
    assert world.store.get_task(task["id"])["status"] == "review" and world.pending(task["id"]) == review
    world.fx.fail.clear()
    mark, calls = world.mark(), len(world.fx.calls)
    out = world.step(task["id"], "T7")
    assert world.fx.calls[calls:] == [("check", "T7"), ("before", "T7"), ("after", "T7")]
    assert out.task["finished_at"] and out.task["run_id"] == task["run_id"]  # done keeps its run
    assert (out.inbox["id"], out.inbox["decision"], out.inbox["delivery"]) == (review["id"], "pass", "none")
    assert world.store.get_inbox_item(first["id"])["delivery"] == "none"
    assert world.types(mark) == ["inbox.decided", "task.transitioned"]


def test_t8_queues_the_rework_and_t9_gives_the_run_back(world):
    task = world.reach("review")
    run = task["run_id"]
    out = world.step(task["id"], "T8")
    rework = out.inbox
    assert out.task["queued_at"] and out.task["run_id"] == run and out.event["reason"] == "착지가 어색하다"
    assert (rework["decision"], rework["decision_comment"], rework["delivery"]) == ("rework", "착지가 어색하다", "pending")
    assert world.store.get_employee(world.emp["id"])["status"] == "working"
    world.m.unqueue(task["id"], SYSTEM)
    world.store.update_task(task["id"], exchange_count=5)
    world.step(task["id"], "T6")
    calls = len(world.fx.calls)
    out = world.step(task["id"], "T9")
    assert world.fx.calls[calls:] == [("check", "T9"), ("before", "T9"), ("after", "T9")]
    assert (out.task["run_id"], out.task["queued_at"], out.task["exchange_count"]) == (None, None, 0)
    assert out.task["assignee_id"] == world.emp["id"] and out.task["finished_at"] is None
    assert out.event["detail"] == {"run_id": run, "inbox_id": out.inbox["id"], "decision": "revert"}
    assert (out.inbox["decision"], out.inbox["delivery"]) == ("revert", "none")
    assert world.store.get_inbox_item(rework["id"])["delivery"] == "none"
    assert len(world.store.list_rooms(kind="task", task_id=task["id"])) == 1  # back in backlog: no second room


def test_t10_cancels_a_queued_backlog_task_without_an_unqueued_row(world):
    task = world.reach("backlog")
    world.m.request_start(task["id"], OWNER)
    seen, mark = len(world.events(task["id"])), world.mark()
    out = world.step(task["id"], "T10")
    assert (out.task["status"], out.task["queued_at"], out.task["run_id"]) == ("cancelled", None, None)
    assert out.task["finished_at"] and out.task["assignee_id"] == world.emp["id"]
    assert [(e["kind"], e["rule"], e["reason"]) for e in world.events(task["id"])[seen:]] == \
        [("transition", "T10", "방향이 바뀌었다")]
    [row] = world.stream(mark)
    assert row["type"] == "task.transitioned" and row["data"]["inbox_id"] is None and out.inbox is None
    assert ("before", "T10") not in world.fx.calls


def test_t11_closes_the_blocking_item_and_drops_the_run(world):
    task = world.reach("blocked")
    item = world.pending(task["id"])
    mark, calls = world.mark(), len(world.fx.calls)
    out = world.step(task["id"], "T11")
    assert world.fx.calls[calls:] == [("check", "T11"), ("before", "T11"), ("after", "T11")]
    assert (out.task["status"], out.task["run_id"]) == ("cancelled", None) and out.task["finished_at"]
    assert out.event["detail"] == {"run_id": task["run_id"], "inbox_id": item["id"]}
    assert out.event["reason"] == "방향이 바뀌었다"
    closed = world.store.get_inbox_item(item["id"])
    assert closed == out.inbox and closed["closed_at"]
    assert (closed["status"], closed["decision"], closed["delivery"]) == ("cancelled", None, "none")
    assert world.pending(task["id"]) is None
    rows = world.stream(mark)
    assert [r["type"] for r in rows] == ["inbox.cancelled", "task.transitioned", "employee.status"]
    assert rows[1]["data"]["inbox_id"] == item["id"]
    assert rows[2]["data"] == {"employee_id": world.emp["id"], "status": "idle", "prev": "blocked"}


def test_a_state_changed_after_the_checks_is_stale(world):
    task = world.reach("backlog")
    world.fx.on["check"] = lambda row: world.step(task["id"], "T3")  # the queue wins between check and UPDATE
    err = refused("stale_state", world.m.transition, task["id"], "cancelled", OWNER, reason="그만")
    assert err.as_dict() == {"detail": err.detail, "code": "stale_state"}
    row = world.store.get_task(task["id"])
    assert (row["status"], row["finished_at"]) == ("working", None)
    assert [e["rule"] for e in world.events(task["id"]) if e["kind"] == "transition"] == ["T3"]

    blocked = world.reach("blocked")
    item = world.pending(blocked["id"])
    world.fx.on["before"] = lambda row: world.step(blocked["id"], "T5")  # decided while the rollback ran
    refused("stale_state", world.m.transition, blocked["id"], "cancelled", OWNER)
    assert world.store.get_task(blocked["id"])["status"] == "working"
    assert world.store.get_inbox_item(item["id"])["status"] == "decided"


# --- what is not a transition -------------------------------------------------------------------------
def test_start_request_checks_the_assignee_and_joins_the_line(world):
    m, s = world.m, world.store
    loose = world.register(assignee_id=None)
    err = refused("precondition_failed", m.request_start, loose["id"], OWNER)
    assert err.failed == [] and "담당이 없다" in err.detail

    task = world.register()
    for change, word in (({"status": "on_leave"}, "휴직"), ({"project_id": None}, "배치"), ({"retired_at": now()}, "재직")):
        s.update_employee(world.emp["id"], **change)
        assert word in refused("precondition_failed", m.request_start, task["id"], OWNER).detail
        s.update_employee(world.emp["id"], status="idle", project_id=world.project["id"], retired_at=None)
    refused("actor_not_allowed", m.request_start, task["id"], world.me)  # A1
    assert s.get_task(task["id"])["queued_at"] is None

    mark = world.mark()
    out = m.request_start(task["id"], SYSTEM, reason="캠페인 자동 진행")
    assert out.task["status"] == "backlog" and out.task["queued_at"] and out.rule is None
    event = world.events(task["id"])[-1]
    assert event == out.event
    assert (event["kind"], event["rule"], event["to_status"], event["actor_kind"], event["reason"]) == \
        ("queued", None, None, "system", "캠페인 자동 진행")
    [row] = world.stream(mark)
    assert row["type"] == "task.updated" and row["task_id"] == task["id"]
    assert row["data"] == {"task": out.task, "change": "queued", "event_id": event["id"]}
    refused("precondition_failed", m.request_start, task["id"], OWNER)  # already in the line
    assert refused("illegal_transition", m.request_start, world.propose()["id"], OWNER).rule == "F1"
    refused("precondition_failed", m.request_start, world.reach("review")["id"], OWNER)
    assert refused("illegal_transition", m.request_start, world.reach("done")["id"], OWNER).rule == "F9"


@pytest.mark.parametrize("mode,milestone,dep_state,ok", [
    (None, False, "review", False), (None, False, "done", True),
    ("each", False, "review", False), ("each", False, "done", True),
    ("nonstop", False, "review", True), ("nonstop", False, "working", False), ("nonstop", False, "cancelled", False),
    ("milestone", False, "review", True), ("milestone", True, "review", False), ("milestone", True, "done", True),
])
def test_start_waits_for_dependencies_by_campaign_mode(world, mode, milestone, dep_state, ok):
    campaign = world.store.create_campaign(world.project["id"], "1막", "첫 판", mode=mode) if mode else None
    cid = campaign["id"] if campaign else None
    first = world.reach(dep_state, campaign_id=cid, is_milestone=milestone)
    second = world.register(campaign_id=cid)
    world.store.create_task_dep(second["id"], first["id"])
    if ok:
        assert world.m.request_start(second["id"], OWNER).task["queued_at"]
    else:
        assert "선행" in refused("precondition_failed", world.m.request_start, second["id"], OWNER).detail
    never = TaskMachine(world.store, world.fx, deps_ready=lambda task: False)  # S-14 may inject its own
    refused("precondition_failed", never.request_start, world.register()["id"], OWNER)


def test_unqueue_assign_and_record_leave_their_rows(world):
    m = world.m
    task = world.register()
    refused("precondition_failed", m.unqueue, task["id"], OWNER)  # not in the line
    m.request_start(task["id"], OWNER)
    refused("precondition_failed", m.assign, task["id"], OWNER, world.mate["id"])  # in the line
    refused("actor_not_allowed", m.unqueue, task["id"], world.me)
    assert m.unqueue(task["id"], OWNER, reason="내일 한다").task["queued_at"] is None
    for actor in (world.me, SYSTEM):
        refused("actor_not_allowed", m.assign, task["id"], actor, world.mate["id"])
    refused("precondition_failed", m.assign, task["id"], OWNER, "emp_none")
    refused("precondition_failed", m.assign, task["id"], OWNER, world.emp["id"])  # no change
    out = m.assign(task["id"], OWNER, world.mate["id"])
    assert out.task["assignee_id"] == world.mate["id"]
    assert out.event["detail"] == {"from": world.emp["id"], "to": world.mate["id"]}
    assert m.assign(task["id"], OWNER, None).task["assignee_id"] is None
    assert [e["kind"] for e in world.events(task["id"])] == ["created", "queued", "unqueued", "assigned", "assigned"]
    assert all(e["reason"].strip() and e["rule"] is None for e in world.events(task["id"])[1:])

    working = world.reach("working")
    refused("precondition_failed", m.assign, working["id"], OWNER, world.mate["id"])  # backlog only
    out = m.record(working["id"], SYSTEM, "guard", "rm -rf 를 거부했다", {"run_id": working["run_id"], "command": "rm -rf ."})
    assert (out.event["kind"], out.event["rule"], out.event["to_status"], out.event["actor_kind"]) == \
        ("guard", None, None, "system")
    assert out.event["detail"]["command"] == "rm -rf ." and out.task == working
    assert m.record(working["id"], world.me, "progress").event["reason"].strip()
    with pytest.raises(ValueError):
        m.record(working["id"], SYSTEM, "transition", "x")
    for call, args in ((m.transition, ("done", OWNER)), (m.request_start, (OWNER,)), (m.unqueue, (OWNER,)),
                       (m.assign, (OWNER, None)), (m.record, (SYSTEM, "run"))):
        with pytest.raises(TaskNotFound):
            call("task_none", *args)


def test_employee_status_follows_the_assigned_tasks(world):
    status = lambda: world.store.get_employee(world.emp["id"])["status"]
    a = world.reach("working")
    assert status() == "working"
    b = world.reach("blocked")
    assert status() == "working"  # rule 2 comes before rule 3
    world.step(a["id"], "T4")
    assert status() == "blocked"
    world.step(a["id"], "T11")
    assert status() == "blocked"
    world.step(b["id"], "T5")
    assert status() == "working"
    world.store.update_employee(world.emp["id"], status="on_leave")  # rule 1: the owner's leave is not overwritten
    mark = world.mark()
    world.step(b["id"], "T4", inbox={"kind": "question", "title": "또"})
    assert status() == "on_leave" and "employee.status" not in world.types(mark)
    assert world.store.get_employee(world.mate["id"])["status"] == "idle"


def test_actor_and_error_shapes():
    for bad in (("employee",), ("owner", "emp_1"), ("robot",)):
        with pytest.raises(ValueError):
            Actor(*bad)
    assert Actor.employee("emp_1").as_dict() == {"kind": "employee", "id": "emp_1"} and OWNER.as_dict()["id"] is None
    with pytest.raises(ValueError):
        TransitionError("not_found", "없다")
    err = TransitionError("precondition_failed", "담당이 없다")
    assert str(err) == "담당이 없다" and err.as_dict() == {"detail": "담당이 없다", "code": "precondition_failed", "failed": []}
    assert set(tasks.CODES) == {"illegal_transition", "actor_not_allowed", "precondition_failed", "missing_fields",
                                "stale_state"}


# --- campaigns (§4.10) --------------------------------------------------------------------------------
C_STATES = ("draft", "running", "paused", "done", "cancelled")
C_ALLOWED = {
    ("draft", "running"): "owner", ("running", "paused"): "owner", ("paused", "running"): "owner",
    ("running", "done"): "system",
    ("draft", "cancelled"): "owner", ("running", "cancelled"): "owner", ("paused", "cancelled"): "owner",
}
C_PATH = {"draft": [], "running": ["running"], "paused": ["running", "paused"], "done": ["running", "done"],
          "cancelled": ["cancelled"]}


def campaign_in(world, state):
    campaign = world.store.create_campaign(world.project["id"], "1막", "첫 판")
    for to in C_PATH[state]:
        campaign = world.m.transition_campaign(campaign["id"], to, SYSTEM if to == "done" else OWNER)
    return campaign


@pytest.mark.parametrize("frm,to", [(f, t) for f in C_STATES for t in C_STATES],
                         ids=[f"{f}-{t}" for f in C_STATES for t in C_STATES])
def test_campaign_cells(world, frm, to):
    campaign = campaign_in(world, frm)
    who = C_ALLOWED.get((frm, to))
    move = world.m.transition_campaign
    if who is None:
        for actor in (OWNER, SYSTEM, world.me):
            err = refused("illegal_transition", move, campaign["id"], to, actor)
            assert (err.as_dict()["from"], err.as_dict()["to"]) == (frm, to)
        assert world.store.get_campaign(campaign["id"]) == campaign
        return
    for actor in (SYSTEM if who == "owner" else OWNER, world.me):
        refused("actor_not_allowed", move, campaign["id"], to, actor)
    assert world.store.get_campaign(campaign["id"]) == campaign
    mark = world.mark()
    out = move(campaign["id"], to, world.actor(who))
    assert out["status"] == to and out == world.store.get_campaign(campaign["id"])
    assert (out["finished_at"] is not None) == (to in ("done", "cancelled"))
    assert (out["started_at"] is not None) == (to == "running" or frm != "draft")
    [row] = world.stream(mark)
    assert row["type"] == "campaign.updated" and row["data"] == {"campaign": out}
    assert world.fx.calls[-1] == ("after", "campaign")


def test_campaign_is_done_only_when_every_task_is_closed(world):
    campaign = campaign_in(world, "running")
    started = campaign["started_at"]
    task = world.register(campaign_id=campaign["id"])
    err = refused("precondition_failed", world.m.transition_campaign, campaign["id"], "done", SYSTEM)
    assert err.failed == [] and world.store.get_campaign(campaign["id"])["status"] == "running"
    world.step(task["id"], "T10")
    world.m.transition_campaign(campaign["id"], "paused", OWNER)
    assert world.m.transition_campaign(campaign["id"], "running", OWNER)["started_at"] == started
    assert world.m.transition_campaign(campaign["id"], "done", SYSTEM)["status"] == "done"
    with pytest.raises(ValueError):
        world.m.transition_campaign(campaign["id"], "review", OWNER)
    with pytest.raises(tasks.CampaignNotFound):
        world.m.transition_campaign("cmp_none", "running", OWNER)
