"""Studio REST, part 2 (S-3): campaigns and tasks (§6.4), the owner's cancel (T10 · T11) and the OpenAPI
export. Runs, gates and cleanup are a fake Effects — no AI is called."""
import copy
import json
import re
import sys
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from relay_agent.studio import Actor, Effects, TaskMachine, TransitionError, api, tasks
from relay_agent.studio.store import now
from relay_agent.studio.tasks import OWNER, SYSTEM

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
OPENAPI = DOC.with_name("openapi.json")
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TEXT = dict(title="2단 점프", reason="조작감", spec="공중에서 한 번 더", done_criteria="테스트 통과", type="feature")
QUESTION = {"inbox": {"kind": "question", "title": "점프 높이는?", "payload": {"question": "점프 높이는?"}}}
REVIEW = {"inbox": {"title": "검수 요청", "payload": {"summary": "끝"}}}
PATHS = {  # state -> the steps from a task the owner registered: (to, who, arguments)
    "backlog": [],
    "cancelled": [("cancelled", "owner", {})],
    "working": [("working", "system", {})],
    "blocked": [("working", "system", {}), ("blocked", "employee", QUESTION)],
    "review": [("working", "system", {}), ("review", "employee", REVIEW)],
    "done": [("working", "system", {}), ("review", "employee", REVIEW), ("done", "owner", {})],
}
ADDED = {"room_id", "depends_on", "dependents", "deps_met", "pending_inbox_id", "queue_position", "run"}


class Fx(Effects):
    """Stands in for runs, the "run has stopped" gate and the cleanup of T11 (S-8)."""

    def __init__(self):
        self.calls = []   # (hook, rule) in call order
        self.fail = {}    # (hook, rule) -> exception the hook raises
        self.on = {}      # (hook, rule) -> callable run once inside the hook (another request in between)
        self.runs = 0

    def _hit(self, hook, rule):
        self.calls.append((hook, rule))
        if (hook, rule) in self.on:
            self.on.pop((hook, rule))()
        if (hook, rule) in self.fail:
            raise self.fail[(hook, rule)]

    def create_run(self, task):
        self.runs += 1
        return f"run_{self.runs:03d}"

    def check(self, rule, task):
        self._hit("check", rule)

    def before(self, rule, task):
        self._hit("before", rule)

    def after(self, what, row, event_ids):
        self._hit("after", what)


class World:
    """The server app on a studio DB of its own: two projects, two employees, and a machine to set states up."""

    def __init__(self, server, fx):
        self.owner = TestClient(server.app, headers=AUTH)                    # this PC, with the shared token
        self.remote = TestClient(server.app, client=("192.168.0.7", 50000))  # another device, no token
        self.store = s = api.get_store()
        self.fx = fx
        self.m = TaskMachine(s, fx)
        self.pid = s.create_project("게임", "D:/game", "game")["id"]
        self.elsewhere = s.create_project("도구", "D:/tool", "tool")["id"]
        self.emp = s.create_employee("민수", "program", project_id=self.pid)["id"]
        self.mate = s.create_employee("지아", "program", project_id=self.pid)["id"]
        self.me = Actor.employee(self.emp)
        self.hired = 0

    def hire(self, project_id):
        self.hired += 1
        return self.store.create_employee(f"직원{self.hired}", "program", project_id=project_id)["id"]

    def campaign(self, project_id=None, status="draft"):
        return self.store.create_campaign(project_id or self.pid, "1막", "1막을 끝낸다", mode="each", status=status)

    def task(self, state="backlog", **over):
        """A task taken to `state` by the machine. Each registered task gets an assignee of its own."""
        project_id = over.pop("project_id", self.pid)
        if state in ("proposed", "rejected"):
            row = self.m.create_task(self.me, "proposed", project_id, **{**TEXT, **over}).task
            if state == "rejected":
                self.m.transition(row["id"], "rejected", OWNER, comment="지금은 안 한다")
            return self.store.get_task(row["id"])
        fields = {"assignee_id": self.hire(project_id), **TEXT, **over}
        row = self.m.create_task(OWNER, "backlog", project_id, **fields).task
        for to, who, kwargs in PATHS[state]:
            if to == "working":
                self.m.request_start(row["id"], OWNER)
            actor = {"owner": OWNER, "system": SYSTEM}.get(who) or Actor.employee(row["assignee_id"])
            self.m.transition(row["id"], to, actor, **copy.deepcopy(kwargs))
        return self.store.get_task(row["id"])

    def events(self, task_id):
        return self.store.list_task_events(task_id)

    def marks(self, task_id):
        """What a refused or empty request must leave as it is."""
        return self.store.get_task(task_id), len(self.events(task_id)), len(self.store.list_stream_events())


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    fx = Fx()
    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(api, "_effects", fx)
    api.close_store()
    yield World(server, fx)
    api.close_store()


def refused(r, status, code):
    assert (r.status_code, r.json().get("code")) == (status, code), r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    return r.json()


def invalid(r):
    """FastAPI's own 422: a list in `detail`, no `code` (§6.3)."""
    assert r.status_code == 422 and isinstance(r.json()["detail"], list) and "code" not in r.json(), r.text


def doc_section(start, end):
    text = DOC.read_text(encoding="utf-8")
    return text[text.index(start):text.index(end)]


def new_task(world, **over):
    return world.owner.post("/studio/tasks", json={"project_id": world.pid, **TEXT, **over})


def patch(world, task_id, **body):
    return world.owner.patch(f"/studio/tasks/{task_id}", json=body)


def assign(world, task_id, **body):
    return world.owner.post(f"/studio/tasks/{task_id}/assign", json=body)


def put_deps(world, task_id, *ids):
    return world.owner.put(f"/studio/tasks/{task_id}/deps", json={"depends_on": list(ids)})


def cancel(world, task_id, **body):
    return world.owner.post(f"/studio/tasks/{task_id}/cancel", json=body)


# --- the contract (§6.4) ----------------------------------------------------------------------------------
def test_routes_are_the_s3_rows_of_the_campaign_and_task_table():
    doc, later = set(), set()
    for line in doc_section("**캠페인 · 작업**", "**결재 · 집계**").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        row = re.fullmatch(r"`(GET|POST|PATCH|PUT|DELETE) (/studio/[^`]+)`", cells[0]) if len(cells) == 5 else None
        if row and "S-3" in cells[4]:
            doc.add((row.group(1), row.group(2)))
        elif row and "S-8" in cells[4]:
            later.add((row.group(1), row.group(2)))
    started = {("POST", "/studio/tasks/{id}/start"), ("POST", "/studio/tasks/{id}/stop")}  # S-8 (studio/queue.py)
    built = {(method, r.path) for r in api.router.routes for method in r.methods
             if r.path.startswith(("/studio/campaigns", "/studio/tasks"))}
    assert len(doc) == 9 and started <= later and built == doc | started  # no campaign creation or action (S-14)


def test_enumerations_are_the_machines():
    assert get_args(api.TaskStatus) == tasks.STATES
    assert get_args(api.TaskType) == tasks.TASK_TYPES
    assert get_args(api.CampaignStatus) == tasks.CAMPAIGN_STATES


def test_campaign_and_task_paths_need_the_owner_token(world):
    t = world.task()["id"]
    calls = [("GET", "/studio/campaigns"), ("GET", "/studio/tasks"), ("POST", "/studio/tasks"),
             ("GET", f"/studio/tasks/{t}"), ("PATCH", f"/studio/tasks/{t}"), ("POST", f"/studio/tasks/{t}/assign"),
             ("PUT", f"/studio/tasks/{t}/deps"), ("POST", f"/studio/tasks/{t}/cancel"),
             ("GET", f"/studio/tasks/{t}/events")]
    for method, path in calls:
        refused(world.remote.request(method, path), 401, "unauthorized")
    assert world.store.get_task(t)["status"] == "backlog"


# --- GET /campaigns ---------------------------------------------------------------------------------------
def test_campaigns_list_filters_and_adds_progress(world):
    a, b = world.campaign(), world.campaign(world.elsewhere, "running")
    world.task(campaign_id=a["id"])
    world.task("cancelled", campaign_id=a["id"])
    world.store.update_task(world.task("done")["id"], campaign_id=a["id"])
    world.task()  # in no campaign
    items = world.owner.get("/studio/campaigns").json()["items"]
    assert [c["id"] for c in items] == [a["id"], b["id"]]
    assert {k: v for k, v in items[0].items() if k != "progress"} == world.store.get_campaign(a["id"])
    progress = items[0]["progress"]
    assert progress["by_status"] == {**dict.fromkeys(tasks.STATES, 0), "backlog": 1, "done": 1, "cancelled": 1}
    assert progress["total"] == 3 and progress["done_ratio"] == pytest.approx(1 / 3)
    assert items[1]["progress"] == {"by_status": dict.fromkeys(tasks.STATES, 0), "total": 0, "done_ratio": 0.0}

    def only(query):
        return [c["id"] for c in world.owner.get(f"/studio/campaigns?{query}").json()["items"]]

    assert only(f"project_id={world.pid}") == [a["id"]] and only("status=running") == [b["id"]]
    assert only(f"project_id={world.pid}&status=running") == []
    invalid(world.owner.get("/studio/campaigns?status=nope"))


# --- GET /tasks -------------------------------------------------------------------------------------------
def test_tasks_list_filters_by_each_query(world):
    campaign = world.campaign()
    a = world.task(campaign_id=campaign["id"])
    b = world.task("working")
    c = world.task(parent_id=a["id"], assignee_id=world.mate)
    d = world.task(project_id=world.elsewhere)

    def ids(query=""):
        return [t["id"] for t in world.owner.get(f"/studio/tasks{query}").json()["items"]]

    assert ids() == [a["id"], b["id"], c["id"], d["id"]]
    assert ids(f"?project_id={world.elsewhere}") == [d["id"]]
    assert ids(f"?campaign_id={campaign['id']}") == [a["id"]]
    assert ids("?status=working") == [b["id"]]
    assert ids(f"?assignee_id={world.mate}") == [c["id"]]
    assert ids(f"?parent_id={a['id']}") == [c["id"]]
    assert ids(f"?project_id={world.pid}&status=backlog") == [a["id"], c["id"]]
    assert world.owner.get("/studio/tasks?status=working").json()["items"] == [world.store.get_task(b["id"])]
    world.store.update_task(c["id"], sort_order=-1)
    assert ids(f"?project_id={world.pid}") == [c["id"], a["id"], b["id"]]
    invalid(world.owner.get("/studio/tasks?status=backlog,working"))  # one value only


# --- POST /tasks (C2) -------------------------------------------------------------------------------------
def test_post_task_registers_into_backlog_with_its_room(world):
    before, campaign = world.task(), world.campaign()
    r = new_task(world, campaign_id=campaign["id"], parent_id=before["id"], assignee_id=world.mate,
                 is_milestone=True, relay="studio-task", depends_on=[before["id"], before["id"]])
    assert r.status_code == 201, r.text
    row = r.json()
    assert row == world.store.get_task(row["id"])
    assert (row["status"], row["proposer_kind"], row["assignee_id"], row["is_milestone"], row["relay"]) == (
        "backlog", "owner", world.mate, True, "studio-task")
    assert (row["campaign_id"], row["parent_id"], row["queued_at"]) == (campaign["id"], before["id"], None)
    assert [d["depends_on_id"] for d in world.store.list_task_deps(task_id=row["id"])] == [before["id"]]
    event, = world.events(row["id"])
    assert (event["kind"], event["rule"], event["actor_kind"], event["to_status"]) == (
        "created", "C2", "owner", "backlog")
    assert event["detail"] == {"depends_on": [before["id"]]}
    assert len(world.store.list_rooms(kind="task", task_id=row["id"])) == 1
    assert [e["type"] for e in world.store.list_stream_events() if e["task_id"] == row["id"]] == [
        "task.created", "room.updated"]
    assert world.fx.calls[-1] == ("after", "C2")
    assert new_task(world).json()["assignee_id"] is None  # the six alone are enough


def test_post_task_names_every_empty_required_field(world):
    count = len(world.store.list_tasks())
    body = refused(world.owner.post("/studio/tasks", json={}), 422, "missing_fields")
    assert body["fields"] == ["project_id", "title", "reason", "spec", "done_criteria", "type"]
    assert refused(new_task(world, title="  ", spec=None), 422, "missing_fields")["fields"] == ["title", "spec"]
    invalid(new_task(world, type="bug"))
    invalid(new_task(world, status="working"))  # a key the request does not have
    invalid(new_task(world, depends_on="task_1"))
    assert len(world.store.list_tasks()) == count


def test_post_task_refuses_what_c2_cannot_have(world):
    far = world.task(project_id=world.elsewhere)
    far_campaign = world.campaign(world.elsewhere)
    world.store.update_employee(world.mate, retired_at=now())
    count = len(world.store.list_tasks())
    for over in ({"project_id": "prj_none"}, {"parent_id": "task_none"}, {"campaign_id": "cmp_none"},
                 {"campaign_id": far_campaign["id"]}, {"assignee_id": "emp_none"}, {"assignee_id": world.mate},
                 {"depends_on": ["task_none"]}, {"depends_on": [far["id"]]}):
        assert refused(new_task(world, **over), 409, "precondition_failed")["failed"] == [], over
    assert len(world.store.list_tasks()) == count and world.store.list_task_deps() == []


# --- GET /tasks/{id} --------------------------------------------------------------------------------------
def test_task_detail_adds_room_deps_inbox_queue_and_run(world, monkeypatch):
    first = world.task()
    task = world.task(depends_on=[first["id"]])

    def get(task_id):
        return world.owner.get(f"/studio/tasks/{task_id}").json()

    view, row = get(task["id"]), world.store.get_task(task["id"])
    assert {k: view[k] for k in row} == row and set(view) - set(row) == ADDED
    room, = world.store.list_rooms(kind="task", task_id=task["id"])
    assert (view["room_id"], view["depends_on"], view["dependents"]) == (room["id"], [first["id"]], [])
    assert (view["deps_met"], view["pending_inbox_id"], view["queue_position"], view["run"]) == (
        False, None, None, None)
    assert (get(first["id"])["dependents"], get(first["id"])["deps_met"]) == ([task["id"]], True)

    second = world.task()
    world.store.update_task(second["id"], queued_at="2026-01-01T00:00:01")  # asked first, though made later
    world.store.update_task(first["id"], queued_at="2026-01-01T00:00:02")
    assert [get(t["id"])["queue_position"] for t in (second, first, task)] == [1, 2, None]

    blocked = world.task("blocked")
    item, = world.store.list_inbox(task_id=blocked["id"], status="pending")
    view = get(blocked["id"])
    assert view["pending_inbox_id"] == item["id"] and view["run"] is None  # until S-8 wires the engine's view
    monkeypatch.setattr(api, "_run_view", lambda run_id: {"id": run_id, "status": "paused", "changes_status": None})
    assert get(blocked["id"])["run"] == {"id": blocked["run_id"], "status": "paused", "changes_status": None}
    assert get(task["id"])["run"] is None  # no run id: nothing to look up
    refused(world.owner.get("/studio/tasks/task_none"), 404, "not_found")


# --- PATCH /tasks/{id} ------------------------------------------------------------------------------------
def test_patch_changes_what_is_sent_and_leaves_an_edited_row(world):
    task, campaign = world.task(), world.campaign()
    r = patch(world, task["id"], title="3단 점프", type="fix", relay=" studio-task ", campaign_id=campaign["id"],
              is_milestone=True, sort_order=3, spec=task["spec"])
    assert r.status_code == 200, r.text
    row = r.json()
    assert row == world.store.get_task(task["id"])
    assert (row["title"], row["type"], row["relay"], row["campaign_id"], row["is_milestone"], row["sort_order"]) == (
        "3단 점프", "fix", "studio-task", campaign["id"], True, 3)
    assert (row["reason"], row["spec"], row["status"]) == (task["reason"], task["spec"], "backlog")
    event = world.events(task["id"])[-1]
    assert (event["kind"], event["actor_kind"], event["rule"]) == ("edited", "owner", None)
    assert event["detail"] == {"fields": {
        "title": {"from": "2단 점프", "to": "3단 점프"}, "type": {"from": "feature", "to": "fix"},
        "relay": {"from": None, "to": "studio-task"}, "campaign_id": {"from": None, "to": campaign["id"]},
        "is_milestone": {"from": False, "to": True}, "sort_order": {"from": 0, "to": 3}}}
    last = world.store.list_stream_events()[-1]
    assert (last["type"], last["task_id"], last["data"]["change"]) == ("task.updated", task["id"], "edited")

    marks = world.marks(task["id"])
    for body in ({}, {"title": "3단 점프", "sort_order": 3, "is_milestone": True}):  # nothing differs
        r = patch(world, task["id"], **body)
        assert r.status_code == 200 and r.json() == row
    assert world.marks(task["id"]) == marks
    cleared = patch(world, task["id"], relay=None, campaign_id=None).json()
    assert (cleared["relay"], cleared["campaign_id"]) == (None, None)


def test_text_is_stored_as_sent_and_only_the_optional_names_are_trimmed(world):
    sent = {**TEXT, "title": "  2단 점프 ", "spec": "  공중에서 한 번 더\n착지하면 초기화\n", "relay": "  studio-task "}
    r = world.owner.post("/studio/tasks", json={"project_id": world.pid, **sent})
    assert r.status_code == 201, r.text
    row = world.store.get_task(r.json()["id"])
    assert (r.json()["title"], r.json()["spec"]) == (sent["title"], sent["spec"])
    assert (row["title"], row["spec"], row["relay"]) == (sent["title"], sent["spec"], "studio-task")

    count = len(world.events(row["id"]))
    r = patch(world, row["id"], title=" 2단 점프")  # differs by its spaces only: still another value
    assert r.status_code == 200, r.text
    assert r.json()["title"] == world.store.get_task(row["id"])["title"] == " 2단 점프"
    events = world.events(row["id"])
    assert len(events) == count + 1
    assert (events[-1]["kind"], events[-1]["detail"]) == (
        "edited", {"fields": {"title": {"from": "  2단 점프 ", "to": " 2단 점프"}}})


def test_is_milestone_in_an_edited_row_is_a_boolean_whoever_wrote_it(world):
    patched, proposed = world.task(), world.task("proposed")
    assert patch(world, patched["id"], is_milestone=True).status_code == 200
    world.m.transition(proposed["id"], "backlog", OWNER, edits={"is_milestone": True})  # T1 with a correction
    assert world.store.get_task(proposed["id"])["is_milestone"] is True
    for task in (patched, proposed):
        rows = [event for event in world.events(task["id"]) if event["kind"] == "edited"]
        change = rows[-1]["detail"]["fields"]["is_milestone"]
        assert change["from"] is False and change["to"] is True  # 1 == True: only `is` tells the two apart


def test_patch_refuses_by_state_and_by_empty_values(world):
    task, far_campaign = world.task(), world.campaign(world.elsewhere)
    marks = world.marks(task["id"])
    body = refused(patch(world, task["id"], title=" ", reason=None, sort_order=None), 422, "missing_fields")
    assert body["fields"] == ["title", "reason", "sort_order"]
    invalid(patch(world, task["id"], assignee_id=world.mate))  # the assignee has a path of its own
    invalid(patch(world, task["id"], type="bug"))
    invalid(patch(world, task["id"], is_milestone="maybe"))
    for campaign_id in (far_campaign["id"], "cmp_none"):
        assert refused(patch(world, task["id"], campaign_id=campaign_id), 409, "precondition_failed")["failed"] == []
    assert world.marks(task["id"]) == marks

    world.m.request_start(task["id"], OWNER)  # in the line: what the start request checked stays as it was
    refused(patch(world, task["id"], title="줄 선 뒤"), 409, "precondition_failed")
    assert patch(world, task["id"], is_milestone=True).json()["is_milestone"] is True

    working = world.task("working")
    refused(patch(world, working["id"], spec="다르게"), 409, "precondition_failed")
    assert patch(world, working["id"], sort_order=7).json()["sort_order"] == 7  # until the task is closed
    refused(patch(world, world.task("proposed")["id"], title="제안 고치기"), 409, "precondition_failed")
    done = world.task("done")
    refused(patch(world, done["id"], is_milestone=True), 409, "precondition_failed")
    assert world.store.get_task(done["id"]) == done
    refused(patch(world, "task_none", title="없는 작업"), 404, "not_found")


# --- POST /tasks/{id}/assign ------------------------------------------------------------------------------
def test_assign_sets_and_clears_the_assignee_in_backlog(world):
    task = world.task()
    r = assign(world, task["id"], assignee_id=world.mate)
    assert r.status_code == 200, r.text
    assert r.json() == world.store.get_task(task["id"]) and r.json()["assignee_id"] == world.mate
    event = world.events(task["id"])[-1]
    assert (event["kind"], event["detail"]) == ("assigned", {"from": task["assignee_id"], "to": world.mate})
    assert assign(world, task["id"], assignee_id=None).json()["assignee_id"] is None
    invalid(assign(world, task["id"]))  # the key is required: an empty body must not clear the assignee
    invalid(assign(world, task["id"], assignee_id=world.mate, reason="겸사겸사"))
    refused(assign(world, task["id"], assignee_id="emp_none"), 409, "precondition_failed")
    refused(assign(world, world.task("working")["id"], assignee_id=world.mate), 409, "precondition_failed")
    refused(assign(world, "task_none", assignee_id=world.mate), 404, "not_found")


# --- PUT /tasks/{id}/deps ---------------------------------------------------------------------------------
def test_put_deps_replaces_the_set(world):
    a, b, task = world.task(), world.task(), world.task()
    r = put_deps(world, task["id"], a["id"], b["id"], a["id"])
    assert r.status_code == 200, r.text
    assert r.json() == {**world.store.get_task(task["id"]), "depends_on": [a["id"], b["id"]]}
    event = world.events(task["id"])[-1]
    assert (event["kind"], event["actor_kind"], event["rule"]) == ("deps", "owner", None)
    assert event["detail"] == {"from": [], "to": [a["id"], b["id"]]}
    last = world.store.list_stream_events()[-1]
    assert (last["type"], last["data"]["change"]) == ("task.updated", "deps")

    marks = world.marks(task["id"])
    assert put_deps(world, task["id"], b["id"], a["id"]).json()["depends_on"] == [a["id"], b["id"]]  # the same set
    assert world.marks(task["id"]) == marks
    assert put_deps(world, task["id"], b["id"]).json()["depends_on"] == [b["id"]]
    assert world.events(task["id"])[-1]["detail"] == {"from": [a["id"], b["id"]], "to": [b["id"]]}
    assert put_deps(world, task["id"]).json()["depends_on"] == []
    assert world.store.list_task_deps(task_id=task["id"]) == []
    assert put_deps(world, world.task("proposed")["id"], a["id"]).json()["depends_on"] == [a["id"]]


def test_put_deps_refuses_cycles_other_projects_and_later_states(world):
    a, b, c = world.task(), world.task(), world.task()
    far = world.task(project_id=world.elsewhere)
    assert put_deps(world, b["id"], a["id"]).status_code == 200  # b waits for a
    assert put_deps(world, c["id"], b["id"]).status_code == 200  # c waits for b
    marks = world.marks(a["id"])
    for ids in ([a["id"]], [b["id"]], [c["id"]], ["task_none"], [far["id"]]):  # itself, a cycle of 2 and of 3
        assert refused(put_deps(world, a["id"], *ids), 409, "precondition_failed")["failed"] == [], ids
    assert world.marks(a["id"]) == marks and world.store.list_task_deps(task_id=a["id"]) == []

    queued = world.task()
    world.m.request_start(queued["id"], OWNER)
    refused(put_deps(world, queued["id"], a["id"]), 409, "precondition_failed")
    refused(put_deps(world, world.task("working")["id"], a["id"]), 409, "precondition_failed")
    assert put_deps(world, world.task("done")["id"]).status_code == 200  # nothing to change: the row as it is
    invalid(world.owner.put(f"/studio/tasks/{a['id']}/deps", json={}))
    refused(put_deps(world, "task_none", a["id"]), 404, "not_found")


# --- POST /tasks/{id}/cancel (T10 · T11) ------------------------------------------------------------------
def test_cancel_closes_a_backlog_task_as_t10(world):
    task = world.task()
    r = cancel(world, task["id"], reason="방향이 바뀌었다")
    assert r.status_code == 200, r.text
    row = r.json()
    assert row == world.store.get_task(task["id"]) and row["status"] == "cancelled" and row["finished_at"]
    event, = [e for e in world.events(task["id"]) if e["kind"] == "transition"]
    assert (event["rule"], event["from_status"], event["to_status"], event["actor_kind"], event["reason"]) == (
        "T10", "backlog", "cancelled", "owner", "방향이 바뀌었다")
    for send in ({}, {"json": {}}, {"json": {"reason": None}}):  # the body and the reason may be left out
        other = world.task()
        r = world.owner.post(f"/studio/tasks/{other['id']}/cancel", **send)
        assert r.status_code == 200 and r.json()["status"] == "cancelled", r.text
        event, = [e for e in world.events(other["id"]) if e["kind"] == "transition"]
        assert event["reason"] == tasks.DEFAULT_REASON["T10"]


def test_cancel_closes_a_blocked_task_as_t11_with_its_pending_item(world):
    task = world.task("blocked")
    item, = world.store.list_inbox(task_id=task["id"], status="pending")
    r = cancel(world, task["id"], reason="그만둔다")
    assert r.status_code == 200 and r.json()["status"] == "cancelled", r.text
    assert world.store.get_inbox_item(item["id"])["status"] == "cancelled"
    assert [c for c in world.fx.calls if c[1] == "T11"] == [("check", "T11"), ("before", "T11"), ("after", "T11")]
    event, = [e for e in world.events(task["id"]) if e["rule"] == "T11"]
    assert (event["from_status"], event["to_status"], event["reason"]) == ("blocked", "cancelled", "그만둔다")


@pytest.mark.parametrize("state, cell", [("proposed", "F10"), ("working", "F10"), ("review", "F10"),
                                         ("done", "F9"), ("rejected", "F9"), ("cancelled", "F0")])
def test_cancel_from_any_other_state_is_409_illegal_transition(world, state, cell):
    task = world.task(state)
    marks, calls = world.marks(task["id"]), len(world.fx.calls)
    body = refused(cancel(world, task["id"], reason="그만"), 409, "illegal_transition")
    assert (body["from"], body["to"]) == (state, "cancelled") and cell in body["detail"]
    assert world.marks(task["id"]) == marks and len(world.fx.calls) == calls


def test_cancel_reports_the_gate_the_engine_and_a_lost_race(world):
    task = world.task("blocked")
    world.fx.fail[("check", "T11")] = TransitionError("precondition_failed", "런이 아직 돈다", failed=["run_active"])
    assert refused(cancel(world, task["id"]), 409, "precondition_failed")["failed"] == ["run_active"]
    del world.fx.fail[("check", "T11")]
    world.fx.fail[("before", "T11")] = ValueError("변경을 되돌릴 수 없다")
    assert "되돌릴" in refused(cancel(world, task["id"]), 409, "engine_error")["detail"]
    assert world.store.get_task(task["id"])["status"] == "blocked"
    assert len(world.store.list_inbox(task_id=task["id"], status="pending")) == 1

    waiting = world.task()  # another request cancels it between this one's check and its write
    world.fx.on[("check", "T10")] = lambda: TaskMachine(world.store).transition(waiting["id"], "cancelled", OWNER)
    refused(cancel(world, waiting["id"]), 409, "stale_state")
    invalid(cancel(world, world.task()["id"], why="키가 다르다"))
    refused(cancel(world, "task_none"), 404, "not_found")


@pytest.mark.parametrize("state, rule", [("blocked", "T11"), ("backlog", "T10")])
def test_cancel_that_fails_after_the_commit_is_a_server_error_and_stays_cancelled(world, state, rule):
    task = world.task(state)
    broken = TestClient(world.owner.app, headers=AUTH, raise_server_exceptions=False)
    world.fx.fail[("after", rule)] = ValueError("후속 처리가 실패했다")
    r = broken.post(f"/studio/tasks/{task['id']}/cancel")
    assert r.status_code == 500 and "engine_error" not in r.text  # not a refusal: the cancel is written
    assert world.store.get_task(task["id"])["status"] == "cancelled"
    assert world.store.list_inbox(task_id=task["id"], status="pending") == []
    del world.fx.fail[("after", rule)]
    body = refused(cancel(world, task["id"]), 409, "illegal_transition")
    assert (body["from"], body["to"]) == ("cancelled", "cancelled")


# --- GET /tasks/{id}/events -------------------------------------------------------------------------------
def test_task_events_page_forward_from_after_id(world):
    task = world.task()
    world.task()  # its rows are not this task's
    for n in range(4):
        world.m.record(task["id"], SYSTEM, "progress", f"{n + 1}단계")
    events = world.events(task["id"])
    assert len(events) == 5

    def page(query=""):
        return world.owner.get(f"/studio/tasks/{task['id']}/events{query}").json()

    assert page() == {"items": events, "has_more": False}
    assert page("?limit=2") == {"items": events[:2], "has_more": True}
    assert page(f"?limit=2&after_id={events[1]['id']}") == {"items": events[2:4], "has_more": True}
    assert page(f"?limit=3&after_id={events[1]['id']}") == {"items": events[2:], "has_more": False}
    assert page(f"?after_id={events[-1]['id']}") == {"items": [], "has_more": False}
    invalid(world.owner.get(f"/studio/tasks/{task['id']}/events?limit=201"))
    invalid(world.owner.get(f"/studio/tasks/{task['id']}/events?limit=0"))
    refused(world.owner.get("/studio/tasks/task_none/events"), 404, "not_found")


# --- the machine's side of PATCH · deps (studio/tasks.py) -------------------------------------------------
def test_edit_and_set_deps_are_the_owners_and_refuse_a_callers_bug(world):
    task, dep = world.task(), world.task()
    for call in (lambda: world.m.edit(task["id"], world.me, {"title": "직원이 고친다"}),
                 lambda: world.m.set_deps(task["id"], SYSTEM, [dep["id"]])):
        with pytest.raises(TransitionError) as e:
            call()
        assert e.value.code == "actor_not_allowed"
    for fields in ({"assignee_id": world.mate}, {"type": "bug"}, {"sort_order": None}, {"sort_order": True},
                   {"is_milestone": None}):
        with pytest.raises(ValueError):
            world.m.edit(task["id"], OWNER, fields)
    assert world.m.edit(task["id"], OWNER, {"title": task["title"]}) is None
    assert world.m.set_deps(task["id"], OWNER, []) is None
    assert world.store.get_task(task["id"]) == task and len(world.events(task["id"])) == 1


def test_a_proposal_carries_its_predecessors_too(world):
    dep = world.task()
    out = world.m.create_task(world.me, "proposed", world.pid, depends_on=[dep["id"]], **TEXT)
    assert [d["depends_on_id"] for d in world.store.list_task_deps(task_id=out.task["id"])] == [dep["id"]]
    assert out.event["detail"] == {"inbox_id": out.inbox["id"], "depends_on": [dep["id"]]}
    count = len(world.store.list_tasks())
    with pytest.raises(TransitionError) as e:
        world.m.create_task(OWNER, "backlog", world.pid, depends_on=["task_none"], **TEXT)
    assert (e.value.code, e.value.failed, e.value.rule) == ("precondition_failed", [], "C2")
    assert len(world.store.list_tasks()) == count


# --- OpenAPI (§6.1) ---------------------------------------------------------------------------------------
def test_openapi_holds_every_studio_path_and_nothing_else():
    schema = api.openapi_schema()
    built = {(method, r.path) for r in api.router.routes for method in r.methods}
    listed = {(method.upper(), path) for path, item in schema["paths"].items() for method in item}
    assert listed == built and all(path.startswith("/studio/") for path in schema["paths"])
    assert set(schema["components"]["schemas"]["NewTask"]["properties"]) == {
        "project_id", "title", "reason", "spec", "done_criteria", "type", "campaign_id", "parent_id",
        "assignee_id", "is_milestone", "relay", "depends_on"}
    assert schema["components"]["schemas"]["Assign"]["required"] == ["assignee_id"]
    assert not schema["paths"]["/studio/tasks/{id}/cancel"]["post"]["requestBody"].get("required")
    text = api.openapi_text()
    assert text == api.openapi_text() and text.endswith("}\n") and "\r" not in text
    assert json.loads(text) == json.loads(json.dumps(schema)) and "카태" in text  # not \u-escaped


def test_openapi_json_in_the_repository_is_current():
    """S-3: the saved contract is the one this code serves. `uv run relay studio openapi` writes it again."""
    assert api.OPENAPI_PATH == OPENAPI
    if not OPENAPI.is_file() or not OPENAPI.read_text(encoding="utf-8").strip():
        api.export_openapi(OPENAPI)  # only when there is nothing to compare with; a stale file is not replaced
        pytest.fail("design/studio/openapi.json 이 없어서 새로 썼다. 다시 돌려 통과하면 그 파일을 저장소에 넣는다")
    assert json.loads(OPENAPI.read_text(encoding="utf-8")) == json.loads(api.openapi_text()), (
        "design/studio/openapi.json 이 지금 코드와 다르다. uv run relay studio openapi 로 다시 내보낸다")
    # the text too, line ends aside (read_text reads CRLF as LF): the order of the keys, the indent, the last newline
    assert OPENAPI.read_text(encoding="utf-8") == api.openapi_text(), (
        "design/studio/openapi.json 의 내용은 같지만 내보낸 글과 다르다 (키 순서 · 들여쓰기 · 끝 줄바꿈). "
        "uv run relay studio openapi 로 다시 내보낸다")
    assert api.openapi_is_current()


def test_relay_studio_openapi_writes_and_checks_a_file(tmp_path, monkeypatch, capsys):
    from relay_agent import cli

    out = tmp_path / "nested" / "openapi.json"

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["relay", "studio", "openapi", "--out", str(out), *args])
        with pytest.raises(SystemExit) as done:
            cli.main()
        return done.value.code

    assert run("--check") == 1 and not out.exists()
    assert run() == 0
    assert out.read_bytes() == api.openapi_text().encode("utf-8") and b"\r" not in out.read_bytes()
    assert run("--check") == 0
    out.write_text("{}", encoding="utf-8")
    assert run("--check") == 1 and out.read_text(encoding="utf-8") == "{}"  # --check writes nothing
    assert str(out) in capsys.readouterr().out
