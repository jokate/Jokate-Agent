"""H-6 (2) end to end: a final review decided with `should_have_asked` -> the system message and the chat turn in
HR's 1:1 room -> HR (mock) drafts and submits the amendment with `source_inbox_id` -> the `hire` approval is
decided -> GUIDE.md changed and committed in the employee's folder -> the employee's next turn, in the
conversation that was open, is sent the new guide. No AI is called (the HR employee's tool calls are made by hand
with the token of its turn)."""
from fastapi.testclient import TestClient

from relay_agent.studio import api, employee_git, employees
from test_studio_chat import desk  # noqa: F401 - the fixture
from test_studio_inbox import ok
from test_studio_runs import lab  # noqa: F401 - the fixture `desk` stands on
from test_studio_tools import bearer

NEW = "모르면 묻는다.\n\n## 결재 기준\n\n- 묻고 한다: 점프 높이처럼 기획서에 없는 수치\n- 알아서 한다: 없음\n"
HR_YAML = "version: 1\ndepartment: hr\nchat: {model: opus, effort: low}\n"
MARKS = [{"index": 0, "mark": "should_have_asked"}, {"index": 1, "mark": "ok"}]


def test_a_should_have_asked_mark_ends_in_a_new_guide_in_the_next_turn(desk, monkeypatch):  # noqa: F811
    world = desk.lab.world
    task, item = world.review()  # a working task of its own employee; the review it filed
    emp = task["assignee_id"]
    emp_dm = desk.store.list_rooms(kind="dm", employee_id=emp)[0]["id"]
    hr, hr_dm = world.hr()
    desk.hire(emp)
    desk.hire(hr, **{"employee.yaml": HR_YAML})
    old_guide = (employees.folder(desk.cfg.studio_dir, emp) / "GUIDE.md").read_text(encoding="utf-8")
    desk.answers("점프 높이를 정했습니다", "확인했습니다. 수정안을 올리겠습니다", "새 기준대로 하겠습니다", "네")

    # (a) the employee talks, so a conversation is open
    desk.say("점프 높이는?", room=emp_dm)
    assert len(desk.calls) == 1 and desk.member(emp_dm, emp)["conversation_id"] == desk.calls[0].session_id

    # (b) the owner decides the review and marks the first judgment
    ok(desk.lab.decide(item, "pass", data={"judgment_marks": MARKS}))

    # (c) HR's 1:1 room: the system message carries what the amendment is written from, and HR's turn went in
    note = desk.store.list_messages(hr_dm)[0]
    assert note["author_kind"] == "system" and note["meta"]["inbox_id"] == item["id"]
    for needed in (item["id"], task["id"], emp, world.pid, "점프 높이를 1.5배로", "기획서에 없다", "source_inbox_id",
                   "employee_draft", "policy_draft"):
        assert needed in note["body"], needed
    assert "입력 버퍼" not in note["body"]  # the `ok` one is not for HR
    assert len(desk.calls) == 2 and item["id"] in desk.calls[1].prompt
    assert desk.member(hr_dm, hr)["fed_message_id"] == note["id"]

    # (d) HR drafts the amendment of that employee's GUIDE.md and submits it, naming the review
    monkeypatch.setattr(api, "_liveness", lambda run_id: {"status": "running", "alive": True})
    monkeypatch.setattr(api, "_run_view", None)
    api.track_run("turn_hr", mode="chat", employee_id=hr, room_id=hr_dm)
    try:
        http, token = TestClient(desk.owner.app), api.actor_token("turn_hr")

        def call(tool, **args):
            r = http.post(f"/studio/mcp/{tool}", json=args, headers=bearer(token))
            assert r.status_code == 200, r.text
            return r.json()

        drafted = call("employee_draft", target_employee_id=emp, file="GUIDE.md", guide_md=NEW)
        assert drafted["ok"] and drafted["problems"] == []
        sent = call("employee_submit", draft_id=drafted["draft_id"], reason="점프 높이를 묻지 않고 정했다",
                    source_inbox_id=item["id"])
        assert sent["ok"], sent
    finally:
        api.untrack_run("turn_hr")

    # (e) a `hire` approval that points at the review; approving it changes the file and commits the folder
    amend = desk.store.get_inbox_item(sent["inbox_id"])
    assert (amend["kind"], amend["status"], amend["requester_id"]) == ("hire", "pending", hr)
    assert amend["payload"]["action"] == "guide_amend" and amend["payload"]["source_inbox_id"] == item["id"]
    assert amend["payload"]["target_employee_id"] == emp and amend["payload"]["new_text"] == NEW
    folder = employees.folder(desk.cfg.studio_dir, emp)
    assert (folder / "GUIDE.md").read_text(encoding="utf-8") == old_guide
    ok(desk.lab.decide(amend, "approve"))
    assert (folder / "GUIDE.md").read_text(encoding="utf-8") == NEW
    [latest, *_] = employee_git.history(folder, "GUIDE.md")
    # the folder had no repository yet: this first commit takes all four files along
    assert latest["message"] == f"guide_amend: GUIDE.md ({amend['id']})" and "GUIDE.md" in latest["files"]
    assert employee_git.show(folder, latest["commit"], "GUIDE.md") == NEW.encode("utf-8")

    # (f) the conversation that was open is told: the next turn resumes it and carries the new guide
    desk.say("다음은 2단 점프야", room=emp_dm)
    last = desk.calls[-1]
    assert last.resume_session == desk.calls[0].session_id
    assert "GUIDE.md)이 대화를 연 뒤 바뀌었다" in last.prompt and "점프 높이처럼 기획서에 없는 수치" in last.prompt
    desk.say("그리고?", room=emp_dm)
    assert "바뀌었다" not in desk.calls[-1].prompt  # sent once, not at every turn


def test_the_first_resumed_turn_after_a_restart_carries_the_guide_once_more(desk):  # noqa: F811
    desk.answers("네", "네", "네")
    guide = (employees.folder(desk.cfg.studio_dir, desk.emp) / "GUIDE.md").read_text(encoding="utf-8").strip()
    desk.say("안녕", room=desk.dm)
    assert "다시 싣는다" not in desk.calls[0].prompt
    desk.say("그리고?", room=desk.dm)
    assert "다시 싣는다" not in desk.calls[-1].prompt and "바뀌었다" not in desk.calls[-1].prompt
    desk.turns._guide_seen.clear()  # what a restart does: nothing is remembered
    desk.say("다시 켰다", room=desk.dm)
    last = desk.calls[-1]
    assert last.resume_session == desk.calls[0].session_id
    assert "다시 싣는다" in last.prompt and guide in last.prompt
    desk.say("계속", room=desk.dm)
    assert "다시 싣는다" not in desk.calls[-1].prompt
