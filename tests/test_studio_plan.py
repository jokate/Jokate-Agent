"""Studio plan approval (T3 of the discussion campaign): `plan_propose` files a `plan_proposal`; approving it makes
the campaign and its tasks, joins the dependencies and starts it; a revise goes back to the employee's room with a
turn; a plan of more than 12 steps is refused; skip lets it go (§5.2 · §5.3 · §5.5 · §8.4). No AI is called."""
from relay_agent.studio import inbox, tools
from tests.test_studio_inbox import ok, refused, world  # noqa: F401 - `world` is a fixture


def step(title, **over):
    return {"title": title, "reason": "이유", "spec": "명세", "done_criteria": "완료 기준", "type": "feature", **over}


def propose(world, steps=None, **args):
    caller = tools.Caller(employee=world.store.get_employee(world.emp), run_id="run_x", room_id=world.dm,
                          task_id=None, mode="chat")
    body = {"title": "2단 점프 계획", "goal": "2단 점프를 넣는다", "steps": steps if steps is not None else [step("입력"), step("점프")], **args}
    return tools.call("plan_propose", caller, body, machine=world.m, inbox=world.box)


def test_approve_makes_the_campaign_and_tasks_joins_them_and_starts(world):
    other, _ = world.staff("영희")
    out = propose(world, [step("입력"), step("점프", assignee_id=other, depends_on=[0]),
                          step("검증", type="chore", depends_on=[0, 1])], mode="each")
    assert out["ok"] and out["inbox_id"]
    item = world.store.get_inbox_item(out["inbox_id"])
    assert item["kind"] == inbox.PLAN and item["status"] == "pending" and item["task_id"] is None
    assert item["project_id"] == world.pid and item["requester_id"] == world.emp
    assert world.store.list_campaigns(project_id=world.pid) == []  # nothing is made before the decision

    done = ok(world.decide(item, "approve"))
    assert done["task"] is None and done["inbox"]["decision"] == "approve"
    data = done["inbox"]["decision_data"]
    campaign = world.store.get_campaign(data["campaign_id"])
    assert campaign["status"] == "running" and campaign["mode"] == "each" and campaign["title"] == "2단 점프 계획"
    assert campaign["goal"] == "2단 점프를 넣는다"
    tasks = [world.store.get_task(task_id) for task_id in data["task_ids"]]
    assert [t["title"] for t in tasks] == ["입력", "점프", "검증"] and {t["campaign_id"] for t in tasks} == {campaign["id"]}
    assert [t["status"] for t in tasks] == ["backlog"] * 3 and [t["assignee_id"] for t in tasks] == [None, other, None]
    assert tasks[2]["type"] == "chore"
    deps = {t["id"]: sorted(d["depends_on_id"] for d in world.store.list_task_deps(task_id=t["id"])) for t in tasks}
    assert deps == {tasks[0]["id"]: [], tasks[1]["id"]: [tasks[0]["id"]],
                    tasks[2]["id"]: sorted([tasks[0]["id"], tasks[1]["id"]])}
    note = world.said(world.dm)[-1]
    assert campaign["id"] in note["body"] and "3개" in note["body"] and note["meta"]["inbox_id"] == item["id"]
    assert ("chat_turn", world.emp, world.dm) not in world.hk.calls  # an approval needs no turn
    assert ("after", "campaign_started") in world.fx.calls
    refused(world.decide(item, "approve"), 409, "already_decided")  # one campaign only
    assert len(world.store.list_campaigns(project_id=world.pid)) == 1


def test_default_mode_is_milestone_and_a_plan_is_held_while_it_waits(world):
    item = world.store.get_inbox_item(propose(world)["inbox_id"])
    assert item["payload"]["mode"] == "milestone" and item["title"] == "2단 점프 계획"
    done = ok(world.decide(item, "approve"))
    assert world.store.get_campaign(done["inbox"]["decision_data"]["campaign_id"])["mode"] == "milestone"


def test_revise_needs_a_comment_and_goes_back_to_the_employees_room(world):
    item = world.store.get_inbox_item(propose(world)["inbox_id"])
    refused(world.decide(item, "revise"), 422, "invalid_decision")
    assert world.store.get_inbox_item(item["id"])["status"] == "pending"
    done = ok(world.decide(item, "revise", comment="단계를 더 잘게 나눠라"))
    assert done["inbox"]["decision"] == "revise"
    body = world.said(world.dm)[-1]["body"]
    assert "단계를 더 잘게 나눠라" in body and "plan_propose" in body and "0. [feature] 입력" in body
    assert ("chat_turn", world.emp, world.dm) in world.hk.calls
    assert world.store.list_campaigns(project_id=world.pid) == [] and world.store.list_tasks(project_id=world.pid) == []
    again = propose(world)  # the employee files it again, changed
    assert again["ok"] and again["inbox_id"] != item["id"]


def test_skip_lets_the_plan_go_without_a_turn(world):
    item = world.store.get_inbox_item(propose(world)["inbox_id"])
    done = ok(world.decide(item, "skip"))
    assert done["inbox"]["decision"] == "skip" and "넘겼다" in world.said(world.dm)[-1]["body"]
    assert ("chat_turn", world.emp, world.dm) not in world.hk.calls
    assert world.store.list_campaigns(project_id=world.pid) == []


def test_more_than_twelve_steps_are_refused_and_twelve_are_not(world):
    many = propose(world, [step(f"단계 {i}") for i in range(13)])
    assert many["ok"] is False and many["code"] == "missing_fields" and many["fields"] == ["steps"]
    assert "12" in many["message"] and world.store.list_inbox(kind=inbox.PLAN) == []
    assert propose(world, [step(f"단계 {i}") for i in range(12)])["ok"]
    assert inbox.PLAN_MAX_STEPS == 12
    with_box = world.box.create  # the inbox keeps the limit too, whoever calls it
    import pytest

    with pytest.raises(inbox.InboxError) as e:
        with_box(inbox.PLAN, world.me, title="x", payload={"goal": "g", "mode": "each",
                                                           "steps": [step("a")] * 13}, project_id=world.pid)
    assert e.value.code == "missing_fields" and e.value.as_dict()["fields"] == ["steps"]


def test_a_badly_formed_plan_names_the_step_and_key_and_writes_nothing(world):
    out = propose(world, [step("a"), step("b", type="bogus", depends_on=[1]), {"title": "c"}])
    assert out["ok"] is False and out["code"] == "missing_fields"
    assert out["fields"] == ["steps[1].type", "steps[1].depends_on", "steps[2].reason", "steps[2].spec",
                             "steps[2].done_criteria", "steps[2].type"]
    assert propose(world, [])["code"] == "missing_fields" and propose(world, title="")["fields"] == ["title"]
    assert propose(world, mode="weekly")["fields"] == ["mode"]
    gone = propose(world, [step("a", assignee_id="emp_none")])
    assert gone["ok"] is False and gone["code"] == "precondition_failed"
    assert world.store.list_inbox(kind=inbox.PLAN) == []


def test_an_assignee_who_left_since_the_filing_stops_the_approval_and_the_item_waits(world):
    other, _ = world.staff("영희")
    item = world.store.get_inbox_item(propose(world, [step("a", assignee_id=other)])["inbox_id"])
    world.store.update_employee(other, retired_at="2026-10-17T00:00:00Z")
    refused(world.decide(item, "approve"), 409, "precondition_failed")
    assert world.store.get_inbox_item(item["id"])["status"] == "pending"
    assert world.store.list_campaigns(project_id=world.pid) == []


def test_a_failure_while_making_cancels_what_was_made(world):
    item = world.store.get_inbox_item(propose(world, [step("a"), step("b", depends_on=[0])])["inbox_id"])
    real, calls = world.m.create_task, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("the second task cannot be made")
        return real(*args, **kwargs)

    world.m.create_task = flaky
    try:
        try:
            world.box.decide(item["id"], "approve")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the failure must reach the caller")
    finally:
        world.m.create_task = real
    assert world.store.get_inbox_item(item["id"])["status"] == "pending"
    assert [c["status"] for c in world.store.list_campaigns(project_id=world.pid)] == ["cancelled"]
    assert {t["status"] for t in world.store.list_tasks(project_id=world.pid)} <= {"cancelled"}


def test_a_start_that_fails_after_the_decision_leaves_a_draft_and_says_so(world):
    item = world.store.get_inbox_item(propose(world, [step("a"), step("b", depends_on=[0])])["inbox_id"])
    real = world.m.transition_campaign

    def flaky(*args, **kwargs):
        if args[1] == "running":
            raise RuntimeError("the campaign cannot be started")
        return real(*args, **kwargs)

    world.m.transition_campaign = flaky
    try:
        done = world.box.decide(item["id"], "approve")  # direct: a request builds a machine of its own
    finally:
        world.m.transition_campaign = real
    decided = world.store.get_inbox_item(item["id"])
    assert decided["decision"] == "approve" and decided["status"] != "pending"
    campaign = world.store.list_campaigns(project_id=world.pid)[0]
    assert campaign["status"] == "draft"
    assert {t["status"] for t in world.store.list_tasks(project_id=world.pid)} == {"backlog"}
    last = world.said(world.dm)[-1]
    assert campaign["id"] in last["body"] and "draft" in last["body"]
    assert ("after", "campaign_started") not in world.fx.calls
    assert done["inbox"]["decision"] == "approve"
    world.m.transition_campaign(campaign["id"], "running", inbox.OWNER)  # it can still be started later
    assert world.store.get_campaign(campaign["id"])["status"] == "running"


def test_a_plan_cannot_be_approved_from_a_notification_and_only_an_employee_files_it(world):
    item = world.store.get_inbox_item(propose(world)["inbox_id"])
    assert "plan_proposal" not in inbox.NOTIFY_OK
    refused(world.decide(item, "approve", via="notification"), 422, "invalid_decision")
    assert inbox.DECISIONS[inbox.PLAN] == ("approve", "revise", "skip") and inbox.COMMENT_REQUIRED[inbox.PLAN] == ("revise",)
    import pytest

    with pytest.raises(inbox.InboxError) as e:
        world.box.create(inbox.PLAN, inbox.OWNER, title="x",
                         payload={"goal": "g", "mode": "each", "steps": [step("a")]}, project_id=world.pid)
    assert e.value.code == "actor_not_allowed"
