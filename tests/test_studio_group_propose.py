"""Studio: the plan from a conclusion and the owner's "make this a plan" (T4 of the discussion campaign - ARCHITECTURE.md
section 4.6, 6.6, 9.1). The conclusion turn and the chat and group turns are told to file a plan with `plan_propose`;
`POST /rooms/{id}/messages/{mid}/propose` puts a system message and that employee's turn into the room. The chat relay on
the mock runner as in test_studio_group.py; the light model is a fake - no AI is called."""
from relay_agent.studio import api, employees
from test_studio_group_judge import judged, party  # noqa: F401 - the fixture `party` and the fake light model
from test_studio_plan import step
from test_studio_runs import lab  # noqa: F401 - the fixture `party` stands on


def propose_url(room, message):
    return f"/studio/rooms/{room}/messages/{message}/propose"


def employee_message(party, room, author):
    return [m for m in party.store.list_messages(room) if m["author_id"] == author][-1]


def test_the_conclusion_turn_can_file_a_plan_and_still_writes_the_conclusion(party):
    a, b, c = party.order
    judged(party, {})

    def file_the_plan():
        [(run_id, _)] = [(r, h) for r, h in api.live_runs(party.store).items() if h["mode"] != "work"]
        r = party.owner.post("/studio/mcp/plan_propose", headers={"Authorization": f"Bearer {api.actor_token(run_id)}"},
                             json={"title": "점프 계획", "goal": "점프를 고친다", "steps": [step("입력"), step("점프")]})
        assert r.status_code == 200, r.text
        return "결론: 합의한 것 …"

    party.cast.moves = {a: ["a1", file_the_plan], b: ["b1"], c: [""]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c, a]
    (item,) = party.store.list_inbox(kind="plan_proposal")
    assert item["kind"] == "plan_proposal"
    done = party.messages()[-1]
    assert done["meta"]["kind"] == "conclusion" and done["body"] == "결론: 합의한 것 …"
    last = party.cast.seen[-1]["prompt"]
    assert employees.conclusion_note() in last and "plan_propose" in last and "task_propose" in last
    assert "결론은 output 에 쓴다" in last


def test_a_group_turn_and_a_dm_turn_are_told_to_file_a_plan(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1"], b: ["b1"], c: ["c1"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert all(employees.plan_note().removeprefix("시스템: ") in seen["prompt"] for seen in party.cast.seen[:3])
    party.cast.seen.clear()
    party.cast.moves = {party.desk.emp: ["안녕하세요"]}
    r = party.owner.post(f"/studio/rooms/{party.desk.dm}/messages", json={"body": "안녕"})
    assert r.status_code == 201, r.text
    party.go()
    (seen,) = party.cast.seen
    assert employees.plan_note() in seen["prompt"]


def test_propose_in_a_dm_room_makes_the_system_message_and_the_employees_turn(party):
    emp = party.desk.emp
    party.cast.moves = {emp: ["1. 입력 2. 점프 — 이렇게 하면 됩니다", "알겠습니다"]}
    party.owner.post(f"/studio/rooms/{party.desk.dm}/messages", json={"body": "점프 어떻게 하죠?"})
    party.go()
    said = employee_message(party, party.desk.dm, emp)
    before = len(party.cast.seen)
    r = party.owner.post(propose_url(party.desk.dm, said["id"]))
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["author_kind"] == "system" and row["meta"]["event"] == "propose_request"
    assert row["meta"]["message_id"] == said["id"] and row["meta"]["employee_id"] == emp
    assert "plan_propose" in row["body"] and "task_propose" in row["body"] and "1. 입력 2. 점프" in row["body"]
    party.go()
    assert len(party.cast.seen) == before + 1 and party.cast.seen[-1]["employee_id"] == emp
    assert row["body"] in party.cast.seen[-1]["prompt"]


def test_propose_in_a_group_room_gives_that_employee_alone_a_turn_and_is_not_counted(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1"], b: ["b1"], c: [""]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    said = employee_message(party, party.room, b)
    seen, count = len(party.cast.seen), party.count()
    party.cast.moves = {b: ["계획을 올렸다"]}
    r = party.owner.post(propose_url(party.room, said["id"]))
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["event"] == "propose_request" and r.json() in party.messages()
    party.go()
    assert [s["employee_id"] for s in party.cast.seen[seen:]] == [b] and party.count() == count
    assert "plan_propose" in party.cast.seen[-1]["prompt"]


def test_propose_refuses_what_is_not_an_employees_message_in_a_room_that_takes_it(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1"], b: [""], c: [""]}
    mine = party.say("점프 버그 어떻게 할까요?")
    party.go()
    theirs = employee_message(party, party.room, a)
    notice = party.messages()[0]  # the room's making: a system message
    assert notice["author_kind"] == "system"
    other = party.desk.dm  # another room: the message is not in it
    system_room = next(r["id"] for r in party.store.list_rooms() if r["kind"] == "system")
    seen, total = len(party.cast.seen), len(party.messages())
    cases = [(party.room, mine["id"], 409), (party.room, notice["id"], 409), (party.room, 999999, 404),
             (other, theirs["id"], 404), ("nope", theirs["id"], 404), (system_room, theirs["id"], 409)]
    for room, message, status in cases:
        r = party.owner.post(propose_url(room, message))
        assert r.status_code == status, (room, message, r.text)
    party.go()
    assert len(party.cast.seen) == seen and len(party.messages()) == total


def test_propose_refuses_a_task_room_a_retired_employee_and_one_who_left(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1"], b: ["b1"], c: [""]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    said_a, said_b = employee_message(party, party.room, a), employee_message(party, party.room, b)
    (project,) = party.store.list_projects()
    task = party.store.create_task(project["id"], title="작업", reason="r", spec="s", done_criteria="d", type="feature",
                                   status="backlog", proposer_kind="owner")
    task_room = party.store.create_room("task", title="작업방", task_id=task["id"])["id"]
    in_task = party.store.create_message(task_room, "employee", author_id=a, body="x")
    seen, total, in_room = len(party.cast.seen), len(party.messages()), len(party.store.list_messages(task_room))

    def refused(room, message):
        r = party.owner.post(propose_url(room, message["id"]))
        assert r.status_code == 409, (room, r.text)

    refused(task_room, in_task)
    party.store.update_room_member(party.room, a, left_at=api.now())  # left the room
    refused(party.room, said_a)
    r = party.owner.post(f"/studio/employees/{b}/retire")  # retired
    assert r.status_code == 200, r.text
    refused(party.room, said_b)
    party.go()
    assert len(party.cast.seen) == seen and len(party.messages()) == total
    assert len(party.store.list_messages(task_room)) == in_room


def test_propose_in_a_group_room_restarts_a_discussion_going_on(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1"], b: [""], c: [""]}  # one speaker: no conclusion turn
    party.say("첫 질문")
    party.go()
    said = employee_message(party, party.room, a)

    def file_it():
        r = party.owner.post(propose_url(party.room, said["id"]))
        assert r.status_code == 201, r.text
        return "a2"

    party.cast.moves = {a: [file_it, "계획 올림"], b: ["b2"], c: ["c2"]}
    party.say("두 번째 질문")
    first = len(party.turns())
    party.go()
    after = party.turns()[first:]
    assert b not in after and c not in after, after
    assert after.count(a) >= 1
