"""An employee's life by REST (H-3, part 2): rename · assign · leave · retire (§6.4), what a task going on does to
them (§4.9), the `employee.updated` they tell (§7.4), and the look at the running campaigns after them (§4.10).
No AI is called. The campaign test (a leave that ends starts the waiting task) is in test_studio_campaign."""
from relay_agent.studio.tasks import OWNER
from test_studio_inbox import TEXT, ok, refused, world  # noqa: F401 - the fixture these tests stand on


def url(world, what="", emp=None):  # noqa: F811
    return f"/studio/employees/{emp or world.emp}{what}"


def patch(world, name, emp=None):  # noqa: F811
    return world.owner.patch(url(world, emp=emp), json={"name": name})


def assign(world, project_id, emp=None):  # noqa: F811
    return world.owner.post(url(world, "/assign", emp), json={"project_id": project_id})


def leave(world, on, emp=None):  # noqa: F811
    return world.owner.post(url(world, "/leave", emp), json={"on": on})


def retire(world, emp=None):  # noqa: F811
    return world.owner.post(url(world, "/retire", emp))


def told(world, change=None):  # noqa: F811
    return [e for e in world.store.list_stream_events(limit=10_000)
            if e["type"] == "employee.updated" and (change is None or e["data"]["change"] == change)]


def types(world):  # noqa: F811
    return [e["type"] for e in world.store.list_stream_events(limit=10_000)]


def listed(world, **params):  # noqa: F811
    return [e["id"] for e in ok(world.owner.get("/studio/employees", params=params))["items"]]


def other_project(world, archived=False, harness=True):  # noqa: F811
    """A second project. Its folder has the harness (S-11 ②: a program employee is placed only there)."""
    n = len(world.store.list_projects(include_archived=True))
    folder = world.dir / f"other-project{n}"
    (folder / "Docs").mkdir(parents=True, exist_ok=True)
    if harness:
        (folder / "CLAUDE.md").write_text("# rules", encoding="utf-8")
    pid = world.store.create_project(f"다른 게임 {n}", str(folder), f"other{n}")["id"]
    if archived:
        world.store.update_project(pid, archived_at="2026-01-01T00:00:00")
    return pid


# --- assign ---------------------------------------------------------------------------------------------------
def test_assign_moves_the_employee_and_null_takes_it_out(world):  # noqa: F811
    pid = other_project(world)
    row = ok(assign(world, pid))
    assert row["project_id"] == pid and row["dm_room_id"] == world.dm and row["company_wide"] is False
    assert listed(world, project_id=pid) == [world.emp] and listed(world, project_id=world.pid) == []
    [event] = told(world, "assigned")
    assert event["data"]["employee"]["project_id"] == pid and event["project_id"] == pid
    assert ok(assign(world, None))["project_id"] is None
    assert listed(world, project_id=pid) == [] and listed(world) == [world.emp]  # of no project, still in service
    assert len(told(world, "assigned")) == 2


def test_assign_to_the_same_project_says_nothing(world):  # noqa: F811
    before = len(told(world))
    assert ok(assign(world, world.pid))["project_id"] == world.pid
    assert len(told(world)) == before


def test_assign_refuses_hr_a_project_one_that_is_not_there_an_archived_one_and_a_retired_employee(world):  # noqa: F811
    hr, _ = world.hr()
    assert refused(assign(world, world.pid, hr), 409, "precondition_failed")["failed"] == ["company_wide"]
    assert ok(assign(world, None, hr))["project_id"] is None  # null is what an HR employee has
    assert refused(assign(world, "prj_nope"), 409, "precondition_failed")["failed"] == ["project_unknown"]
    old = other_project(world, archived=True)
    assert refused(assign(world, old), 409, "precondition_failed")["failed"] == ["project_archived"]
    assert world.store.get_employee(world.emp)["project_id"] == world.pid
    refused(assign(world, world.pid, "emp_nope"), 404, "not_found")
    ok(retire(world))
    assert refused(assign(world, None), 409, "precondition_failed")["failed"] == ["retired"]
    assert not told(world, "assigned")


def test_assign_needs_the_key(world):  # noqa: F811
    assert world.owner.post(url(world, "/assign"), json={}).status_code == 422


# --- rename ---------------------------------------------------------------------------------------------------
def test_rename_changes_the_name_and_the_room_title(world):  # noqa: F811
    row = ok(patch(world, "  민수2  "))
    assert row["name"] == "민수2" and world.store.get_room(world.dm)["title"] == "민수2"
    [event] = told(world, "renamed")
    assert event["data"]["employee"]["name"] == "민수2" and "room.updated" in types(world)
    before = len(told(world))
    assert ok(patch(world, "민수2"))["name"] == "민수2" and len(told(world)) == before  # the same: no event


def test_rename_keeps_a_room_title_that_was_changed(world):  # noqa: F811
    world.store.update_room(world.dm, title="내 맘대로")
    ok(patch(world, "민수2"))
    assert world.store.get_room(world.dm)["title"] == "내 맘대로"


def test_rename_conflicts_with_others_a_retired_one_too_and_is_not_blank(world):  # noqa: F811
    other, _ = world.staff("지민")
    assert refused(patch(world, "지민"), 409, "conflict")["field"] == "name"
    ok(retire(world, other))
    assert refused(patch(world, "지민"), 409, "conflict")["field"] == "name"  # the name stays taken
    assert refused(patch(world, "   "), 422, "missing_fields")["fields"] == ["name"]
    refused(patch(world, "x", "emp_nope"), 404, "not_found")
    assert world.store.get_employee(world.emp)["name"] == "민수"
    assert ok(patch(world, "지민2", other))["name"] == "지민2"  # a retired one can be renamed: its name is freed
    assert ok(patch(world, "지민"))["name"] == "지민"


# --- tasks going on (§4.9) ------------------------------------------------------------------------------------
def test_a_task_going_on_stops_assign_leave_and_retire(world):  # noqa: F811
    task = world.task("working")
    who = task["assignee_id"]
    pid = other_project(world)
    for answer in (assign(world, pid, who), leave(world, True, who), retire(world, who)):
        assert refused(answer, 409, "precondition_failed")["failed"] == ["active_tasks"]
    row = world.store.get_employee(who)
    assert (row["project_id"], row["status"], row["retired_at"]) == (world.pid, "working", None)
    assert not told(world, "assigned") and not told(world, "leave") and not told(world, "retired")
    assert ok(patch(world, "새이름", who))["name"] == "새이름"  # a name is no matter of a run


def test_a_backlog_task_that_is_not_in_the_line_keeps_its_assignee(world):  # noqa: F811
    task = world.m.create_task(OWNER, "backlog", world.pid, assignee_id=world.emp, **TEXT).task
    assert ok(retire(world))["retired_at"] is not None
    assert world.store.get_task(task["id"])["assignee_id"] == world.emp


def test_a_task_in_the_line_stops_the_move(world):  # noqa: F811
    task = world.m.create_task(OWNER, "backlog", world.pid, assignee_id=world.emp, **TEXT).task
    world.store.update_task(task["id"], queued_at="2026-01-01T00:00:00")
    assert refused(retire(world), 409, "precondition_failed")["failed"] == ["active_tasks"]


# --- leave ----------------------------------------------------------------------------------------------------
def test_leave_on_and_off(world):  # noqa: F811
    row = ok(leave(world, True))
    assert row["status"] == "on_leave"
    assert types(world)[-2:] == ["employee.status", "employee.updated"]
    status = [e for e in world.store.list_stream_events(limit=10_000) if e["type"] == "employee.status"][-1]
    assert (status["data"]["status"], status["data"]["prev"]) == ("on_leave", "idle")
    assert told(world, "leave")[-1]["data"]["employee"]["status"] == "on_leave"
    before = len(told(world))
    assert ok(leave(world, True))["status"] == "on_leave" and len(told(world)) == before  # already
    assert ok(leave(world, False))["status"] == "idle"
    assert types(world)[-2:] == ["employee.status", "employee.updated"] and len(told(world, "leave")) == 2
    before = len(told(world))
    assert ok(leave(world, False))["status"] == "idle" and len(told(world)) == before


def test_a_leave_that_ends_works_the_status_out_from_the_tasks(world):  # noqa: F811
    ok(leave(world, True))
    task = world.task("working")  # a task that began while the assignee was away, as an old DB has it
    who = task["assignee_id"]
    world.store.update_employee(who, status="on_leave")
    assert ok(leave(world, False, who))["status"] == "working"


def test_a_retired_employee_takes_no_leave(world):  # noqa: F811
    ok(retire(world))
    assert refused(leave(world, True), 409, "precondition_failed")["failed"] == ["retired"]
    refused(leave(world, True, "emp_nope"), 404, "not_found")


# --- retire ---------------------------------------------------------------------------------------------------
def test_retire_keeps_the_row_the_folder_and_the_room(world):  # noqa: F811
    from relay_agent.studio import employees
    folder = employees.folder(world.dir, world.emp)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "PERSONA.md").write_text("나는 민수다.\n", encoding="utf-8")
    said = world.store.create_message(world.dm, "employee", author_id=world.emp, body="안녕하세요")
    row = ok(retire(world))
    assert row["retired_at"] and row["project_id"] == world.pid and row["dm_room_id"] == world.dm
    assert world.emp not in listed(world) and world.emp not in listed(world, project_id=world.pid)
    assert world.emp in listed(world, retired=1) and world.emp in listed(world, retired=1, project_id=world.pid)
    assert (folder / "PERSONA.md").is_file() and world.store.get_room(world.dm)["archived_at"] is None
    items = ok(world.owner.get(f"/studio/rooms/{world.dm}/messages"))["items"]
    assert [m["id"] for m in items] == [said["id"]]
    [event] = told(world, "retired")
    assert event["data"]["employee"]["retired_at"] == row["retired_at"]
    again = ok(retire(world))
    assert again == row and len(told(world, "retired")) == 1  # the same row, no second event
    refused(retire(world, "emp_nope"), 404, "not_found")


def test_an_hr_employee_can_retire(world):  # noqa: F811
    hr, _ = world.hr()
    assert ok(retire(world, hr))["retired_at"]


# --- the look at the campaigns (§4.10) ------------------------------------------------------------------------
class Look:
    def __init__(self):
        self.seen = []

    def __call__(self, *args, **kw):
        self.seen.append((args, kw))


def test_every_change_looks_at_the_running_campaigns_once(world):  # noqa: F811
    look = Look()
    world.fx.campaign = look
    pid = other_project(world)
    ok(patch(world, "민수2"))
    ok(assign(world, pid))
    ok(leave(world, True))
    ok(leave(world, False))
    ok(retire(world))
    assert look.seen == [((None,), {})] * 5
    ok(patch(world, "민수2"))
    ok(retire(world))
    assert len(look.seen) == 5  # nothing changed: no look
    refused(assign(world, pid), 409, "precondition_failed")
    assert len(look.seen) == 5


def test_a_look_that_fails_does_not_fail_the_answer(world):  # noqa: F811
    def boom(*args):
        raise RuntimeError("훑기가 터졌다")
    world.fx.campaign = boom
    assert ok(patch(world, "민수2"))["name"] == "민수2"
    assert world.store.get_employee(world.emp)["name"] == "민수2"
