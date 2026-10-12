"""HR's judging turn, its two tools and the interview taken up (T5; design/studio/ARCHITECTURE.md §5.3 · §5.5 · §8.4 ·
§8.5 · §9.5), on the mock runner: a trigger that passes the gate is a system line in HR's 1:1 room and a chat turn ->
HR raises a recommendation with `hire_recommend` or nothing -> the owner's `interview` puts the recommendation in
that room and a turn goes on with the hiring interview; `skip` leaves the cooldown. No AI is called."""
from types import SimpleNamespace

import pytest

from relay_agent.studio import Actor, api, chat, runs
from relay_agent.studio.store import now
from relay_agent.studio.work import WorkEffects
from test_studio_hr import Office
from test_studio_runs import lab  # noqa: F401 - the fixture `office` stands on

EVENT = chat.STAFFING_SIGNAL
TEXT = dict(title="배경 그리기", reason="필요", spec="한 장", done_criteria="완성", type="feature")


class Judge(Office):
    """Office with the turns connected the way server.py connects them (`EngineHooks(turns=)`): a trigger asks
    HR through them, as `api.staffing_for` does."""

    def __init__(self, lab, monkeypatch):  # noqa: F811
        super().__init__(lab, monkeypatch)
        engine, turns = lab.engine, self.desk.turns
        monkeypatch.setattr(api, "_hooks", runs.EngineHooks(engine, engine.advance, chat=turns.request, turns=turns))
        self.patch = monkeypatch
        self.emp, self.got = lab.world.emp, []  # `got`: what the `recommend` acts were answered
        self.key = f"art:{self.pid}"

    def ask(self, reason="그림 작업이 들어왔다", **more):
        """민수 reports a need for the part `art` in the project; what the entrance answers."""
        return api.staffing_for(self.store).request(self.emp, reason=reason, department_id="art",
                                                    project_id=self.pid, **more)

    def filing(self):
        """Connect the filing of a task to the trigger as server.py does (`WorkEffects.staffing_look`)."""
        look = WorkEffects(None, SimpleNamespace(), lambda: self.store)
        look.staffing_look = api.staffing_on_task
        self.patch.setattr(self.lab.fx, "staffing", look.staffing)

    def propose(self, department_id="art"):
        """An employee proposes a task for a part: the filing is where the trigger comes from."""
        return self.lab.world.m.create_task(Actor.employee(self.emp), "proposed", self.pid, **TEXT,
                                            inbox={"title": "제안", "payload": {"department_id": department_id}})

    def lines(self, event=None):
        return [m for m in self.store.list_messages(self.room)
                if m["author_kind"] == "system" and (event is None or (m["meta"] or {}).get("event") == event)]

    def recommend(self, **over):
        """The act of a turn: HR raises a recommendation for `art`; the result is kept in `got`."""
        args = {"reason": "아트 작업이 쌓인다", "role": "배경 아티스트", "model": "sonnet", "effort": "medium",
                "department_id": "art", "project_id": self.pid, "signal_key": self.key, **over}

        def act(call):
            self.got.append(self.tool(call, "hire_recommend", **args))
        return act


@pytest.fixture
def judge(lab, monkeypatch):  # noqa: F811
    return Judge(lab, monkeypatch)


def test_a_trigger_is_a_line_in_hrs_room_and_a_turn_that_may_leave_nothing(judge):
    seen = {}
    judge.play([""], lambda call: seen.update(prompt=call.prompt, mcp=list(call.mcp_servers)))
    assert judge.ask(role="배경 아티스트") == {"asked": True, "key": judge.key, "why": "asked"}
    [line] = judge.lines(EVENT)
    assert line["meta"]["key"] == judge.key and line["meta"]["trigger"] == "request"
    assert len(judge.waiting) == 1  # the turn is in; it runs on its own thread on the server
    judge.turn()

    prompt = seen["prompt"]
    for part in ("[채용 계기 · request]", judge.key, "그림 작업이 들어왔다", "배경 아티스트", "아트", "민수", "재직 0명",
                 "필요 없으면 아무것도 올리지 마라", f'signal_key="{judge.key}"', "output 은 비우고"):
        assert part in prompt, part
    assert "studio" in seen["mcp"] and "## 채용 권장" in prompt  # HR's GUIDE brings the criteria
    # an empty output is no failure: the room is not told that HR did not answer, and the line counts as read
    assert [m["author_kind"] for m in judge.store.list_messages(judge.room)] == ["system"]
    assert judge.store.list_inbox(kind="hire_recommendation") == []
    assert judge.store.get_room_member(judge.room, judge.hr)["fed_message_id"] == line["id"]


def test_the_same_key_is_asked_once_in_the_cooldown(judge):
    judge.play([""])
    assert judge.ask()["why"] == "asked"
    judge.turn()
    again = judge.ask(reason="또 필요하다")
    assert (again["asked"], again["why"]) == (False, "recently")
    assert len(judge.lines(EVENT)) == 1 and judge.waiting == []


def test_nobody_in_hr_means_nothing_is_asked_and_the_record_is_taken_back(judge):
    judge.store.update_employee(judge.hr, retired_at=now())
    assert judge.ask() == {"asked": False, "key": judge.key, "why": "unheard"}
    assert judge.lines(EVENT) == [] and judge.waiting == []
    judge.store.update_employee(judge.hr, retired_at=None)  # HR is back: the key was not held for the cooldown
    assert judge.ask()["why"] == "asked"


def test_the_tools_are_hrs_and_the_employees(judge):
    results = {}

    def act(call):
        results["recommend"] = judge.tool(call, "hire_recommend", reason="x", role="y", model="sonnet",
                                          effort="medium", department_id="art", project_id=judge.pid)
        results["request"] = judge.tool(call, "staff_request", reason="일손이 모자란다", role="아티스트",
                                        department_id="art", project_id=judge.pid)

    judge.play(["확인했다"], act)
    judge.desk.say("안녕", judge.desk.dm)  # 민수's own room: a turn of an employee who is not HR
    judge.turn()
    refused, request = results["recommend"], results["request"]
    assert (refused["ok"], refused["code"]) == (False, "actor_not_allowed")
    assert judge.store.list_inbox(kind="hire_recommendation") == []
    assert request["ok"] and (request["asked"], request["why"]) == (True, "asked")
    assert "보장이 아니다" in request["message"]
    [line] = judge.lines(EVENT)  # the request went to HR's room as a trigger
    assert line["meta"]["key"] == judge.key and "일손이 모자란다" in line["body"] and "아티스트" in line["body"]
    assert len(judge.waiting) == 1  # HR's judging turn waits behind


def test_a_request_without_a_reason_or_a_part_is_refused(judge):
    results = {}

    def act(call):
        results["empty"] = judge.tool(call, "staff_request", reason="")
        results["no_part"] = judge.tool(call, "staff_request", reason="필요하다", department_id="hr")
        results["no_task"] = judge.tool(call, "staff_request", reason="필요하다", task_id="task_none")

    judge.play([""], act)
    judge.desk.say("안녕", judge.desk.dm)
    judge.turn()
    assert (results["empty"]["code"], results["empty"]["fields"]) == ("missing_fields", ["reason"])
    assert (results["no_part"]["code"], results["no_part"]["failed"]) == ("precondition_failed", [])
    assert results["no_task"]["code"] == "not_found"
    assert judge.lines(EVENT) == [] and judge.waiting == []  # nothing reached HR


def test_a_recommendation_then_the_interview_is_taken_up_in_hrs_room(judge):
    lab, store = judge.lab, judge.store
    seen = {}  # one runner for both turns: HR's judging turn, then the interview
    judge.play(["", "이름부터 정하자"], judge.recommend(task_ids=[]), lambda call: seen.update(prompt=call.prompt))
    judge.ask(role="배경 아티스트")
    judge.turn()
    [result] = judge.got
    assert result["ok"], result
    [item] = lab.pending()
    assert (item["kind"], item["requester_id"], item["project_id"]) == ("hire_recommendation", judge.hr, judge.pid)
    payload = item["payload"]
    assert payload["key"] == judge.key == payload["signal_key"] and payload["department_id"] == "art"
    assert payload["numbers"] == {"staff": 0, "placed": 0} and item["title"] == "채용 권장: 아트 · 배경 아티스트"
    [filed] = judge.lines("inbox.created")  # one line in HR's room: what was raised and why
    assert filed["meta"]["inbox_id"] == item["id"] and "아트 작업이 쌓인다" in filed["body"]
    assert [m["author_kind"] for m in store.list_messages(judge.room)] == ["system", "system"]  # no answer line

    r = lab.decide(item, "interview", comment="이번 주에 뽑자")
    assert r.status_code == 200, r.text
    [taken] = judge.lines("inbox.decided")
    for part in ("채용 권장 · 면담 시작", "채용 권장의 내용", "사유: 아트 작업이 쌓인다", "파트: 아트", "역할: 배경 아티스트",
                 "모델: sonnet", "노력치: medium", "다시 묻지 않고", "코멘트: 이번 주에 뽑자"):
        assert part in taken["body"], part
    assert len(judge.waiting) == 1  # HR's chat turn is in
    judge.turn()
    prompt = seen["prompt"]
    for part in ("채용 권장의 내용", "다시 묻지 않고"):  # the GUIDE was in the first turn; this one resumes the conversation
        assert part in prompt, (part, prompt[:300])
    assert f"- `{judge.pid}` 게임" in prompt  # the values of a hire come with a chat turn as before
    assert judge.store.list_messages(judge.room)[-1]["body"] == "이름부터 정하자"  # HR carries on the interview
    assert store.get_inbox_item(item["id"])["decision"] == "interview"


def test_skip_leaves_only_the_cooldown_and_a_second_recommendation_is_refused(judge):
    lab = judge.lab
    judge.play(["", ""], judge.recommend(), judge.recommend())  # the second turn tries again
    judge.ask()
    judge.turn()
    [item] = lab.pending()
    before = len(judge.store.list_messages(judge.room))
    assert lab.decide(item, "skip").status_code == 200
    assert len(judge.store.list_messages(judge.room)) == before and judge.waiting == []  # no line, no turn

    again = judge.ask(reason="그래도 필요하다")
    assert (again["asked"], again["why"]) == (False, "skipped")
    judge.desk.say("다시 올려 봐", judge.room)
    judge.turn()
    refused = judge.got[-1]
    assert (refused["ok"], refused["code"]) == (False, "precondition_failed")
    assert lab.pending() == [] and len(judge.store.list_inbox(kind="hire_recommendation")) == 1


def test_a_recommendation_of_a_new_part_carries_the_signal_key(judge):
    judge.play([""], judge.recommend(department_id="", part_name="사운드", part_job="효과음을 만든다"))
    judge.ask()
    judge.turn()
    [result] = judge.got
    assert result["ok"], result
    [item] = judge.lab.pending()
    assert item["payload"]["key"] == f"new:사운드:{judge.pid}" and item["payload"]["signal_key"] == judge.key
    assert item["payload"]["numbers"] == {"staff": 0, "placed": 0} and "사운드" in item["title"]
    assert judge.ask(reason="또")["why"] == "open"  # the trigger's key waits through the signal_key


def _seen(judge):
    seen = {}
    judge.play([""], lambda call: seen.update(prompt=call.prompt))
    return seen


def test_a_proposal_for_a_part_without_staff_reaches_hrs_judging_turn(judge):
    judge.filing()
    seen = _seen(judge)
    outcome = judge.propose()
    assert outcome.task["status"] == "proposed" and outcome.inbox["kind"] == "task_proposal"
    [line] = judge.lines(EVENT)
    assert line["meta"]["trigger"] == "no_staff" and line["meta"]["key"] == judge.key
    assert len(judge.waiting) == 1
    judge.turn()
    assert "[채용 계기 · no_staff]" in seen["prompt"] and judge.key in seen["prompt"]
    assert judge.store.list_inbox(kind="hire_recommendation") == []
    judge.propose()  # the same part again: filed, but the key is in its cooldown
    assert len(judge.lines(EVENT)) == 1 and judge.waiting == []


def test_a_proposal_for_a_part_with_staff_nobody_placed_reaches_hrs_judging_turn(judge):
    judge.store.create_employee("지아", "art", project_id=None)
    judge.filing()
    seen = _seen(judge)
    judge.propose()
    [line] = judge.lines(EVENT)
    assert line["meta"]["trigger"] == "no_placement" and line["meta"]["key"] == judge.key
    judge.turn()
    assert "[채용 계기 · no_placement]" in seen["prompt"]


def test_a_filing_nobody_in_hr_can_hear_is_kept_and_the_record_is_taken_back(judge):
    judge.filing()
    judge.store.update_employee(judge.hr, retired_at=now())
    outcome = judge.propose()
    assert outcome.task["status"] == "proposed" and outcome.inbox["kind"] == "task_proposal"
    assert judge.lines(EVENT) == [] and judge.waiting == []
    judge.store.update_employee(judge.hr, retired_at=None)
    judge.propose()
    assert len(judge.lines(EVENT)) == 1
