"""The interview that ends in a hire (H-2), from one end to the other on the mock runner: the owner writes in the
1:1 room of the HR employee a server start makes -> HR's chat turn has its GUIDE, the values of a hire and the
studio MCP -> with the actor token of that turn it drafts and submits -> the `hire` item -> the owner's decision
-> the employee: the row, the folder, the 1:1 room and the first message (§5.5, §8.4, §9.1). No AI is called."""
import pytest
from fastapi.testclient import TestClient

from relay_agent.providers import ProviderRegistry
from relay_agent.runners import MockRunner
from relay_agent.studio import api, chat, employees, sat
from relay_agent.studio.inbox import GREETING
from relay_agent.studio.store import now
from test_studio_chat import Desk, script, studio_on
from test_studio_runs import lab  # noqa: F401 - the fixture `office` stands on
from test_studio_tools import bearer

YAML = "version: 1\ndepartment: program\nchat: {model: sonnet, effort: medium}\n"  # what a draft of these tests writes
PERSONA = "# 하늘\n\n게임 프로젝트의 프로그래머다. 짧게 답한다.\n"
GUIDE = ("# 지침\n\n- 맡은 일: 전투 코드\n\n## 결재 기준\n\n- 묻고 한다: 구조를 바꾸는 선택\n- 알아서 한다: 이름 · 주석\n")
TITLE = f"===== {employees.HIRING_TITLE} ====="


def draft(**over):
    return {"name": "하늘", "department_id": "program", "persona_md": PERSONA, "guide_md": GUIDE,
            "employee_yaml": YAML, **over}


class Acting(MockRunner):
    """A runner that does something in its turn before it answers — what the employee's Claude Code does with
    the tools of that turn. `acts` are taken in order, one a call; what one raises is kept for `Office.turn`."""

    def __init__(self, texts, *acts):
        super().__init__(script(*texts))
        self.acts, self.errors = list(acts), []

    def run(self, call):
        if self.acts:
            try:
                self.acts.pop(0)(call)
            except Exception as e:  # noqa: BLE001 - an assert of the test: the engine must not take it for the run's
                self.errors.append(e)
        return super().run(call)


class Office:
    """Desk with the studio MCP attached and the HR employee a server start makes. A turn waits until `turn()`
    runs it on the test's thread — on the server it runs on a thread of its own, after the request that asked
    for it has returned —, so the tools its runner calls are requests of their own."""

    def __init__(self, lab, monkeypatch):  # noqa: F811
        self.waiting = []
        self.desk = Desk(lab, monkeypatch, self.waiting.append)
        studio_on(self.desk)
        self.lab, self.store, self.pid = lab, lab.store, lab.world.pid
        folder = lab.tmp / "harnessed"  # a program employee is placed only in a project the harness is laid in
        (folder / "Docs").mkdir(parents=True)
        (folder / "CLAUDE.md").write_text("# rules", encoding="utf-8")
        lab.store.update_project(self.pid, path=str(folder))
        made = api.ensure_hr()  # what server.py calls at its start
        assert made is not None and made["id"] == employees.HR_ID
        [room] = self.store.list_rooms(kind="dm", employee_id=employees.HR_ID)
        self.hr, self.room = made["id"], room["id"]
        self.http = TestClient(lab.owner.app)  # this PC, no token of its own: the actor token says who calls
        self.runner = None

    def play(self, texts, *acts):
        self.runner = self.lab.runner = Acting(texts, *acts)

    def say(self, body, room=None):
        return self.desk.say(body, room or self.room)

    def turn(self):
        """Run the one turn that waits; what its act raised is raised here."""
        [work] = self.waiting
        self.waiting.clear()
        work()
        if self.runner.errors:
            raise self.runner.errors.pop(0)

    def tool(self, call, tool, /, **args):
        """A studio tool called from inside that turn: with the actor token its stage was given (§8.2)."""
        token = call.mcp_overrides["studio"]["env"]["STUDIO_ACTOR_TOKEN"]
        r = self.http.post(f"/studio/mcp/{tool}", json=args, headers=bearer(token))
        assert r.status_code == 200, r.text
        return r.json()

    def note(self):
        """The newest message of HR's room."""
        return self.store.list_messages(self.room)[-1]

    def named(self, name):
        return [row for row in self.store.list_employees(include_retired=True) if row["name"] == name]


@pytest.fixture
def office(lab, monkeypatch):  # noqa: F811
    return Office(lab, monkeypatch)


# --- the interview -> the draft -> the item -> the approval -> the employee ---------------------------------------
def test_an_interview_in_hrs_room_ends_in_an_employee_placed_in_the_project(office):
    lab, store, pid, seen = office.lab, office.store, office.pid, {}

    def interview(call):
        seen.update(prompt=call.prompt, mcp=list(call.mcp_servers),
                    claims=sat.verify(call.mcp_overrides["studio"]["env"]["STUDIO_ACTOR_TOKEN"]))
        taken = office.tool(call, "employee_draft", **draft(name="민수", project_id=pid))  # 민수 is there already
        seen["taken"] = taken
        seen["refused"] = office.tool(call, "employee_submit", draft_id=taken["draft_id"], reason="프로그래머 충원")
        seen["fixed"] = office.tool(call, "employee_draft", draft_id=taken["draft_id"], name="하늘")
        seen["filed"] = office.tool(call, "employee_submit", draft_id=taken["draft_id"], reason="프로그래머 충원")

    office.play(["하늘 채용 결재를 올렸습니다"], interview)
    office.say("프로그래머 한 명 뽑자. 게임 프로젝트에 배치한다")
    office.turn()

    [run] = office.desk.runs(office.room, office.hr)
    assert run.status == "done"
    assert office.desk.said(office.room)[-1] == ("employee", "하늘 채용 결재를 올렸습니다")
    claims, prompt = seen["claims"], seen["prompt"]
    assert (claims["run"], claims["emp"], claims["room"], claims["mode"]) == (run.id, office.hr, office.room, "chat")
    assert "studio" in seen["mcp"]
    assert "owner: 프로그래머 한 명 뽑자" in prompt
    assert "===== GUIDE.md =====" in prompt and "## 초안과 결재" in prompt and "`employee_submit`" in prompt
    values = prompt.split(TITLE, 1)[1]  # the last section: what HR cannot ask the server for
    assert f"- `{pid}` 게임" in values and "- `program` " in values and "- `design_setup` " in values
    assert "`hr`" not in values

    taken, fixed = seen["taken"], seen["fixed"]
    assert taken["ok"] and [p["code"] for p in taken["problems"]] == ["name_taken"]  # saved with its problem
    assert (seen["refused"]["ok"], seen["refused"]["code"], seen["refused"]["failed"]) == (
        False, "precondition_failed", ["name_taken"])
    assert fixed["ok"] and fixed["draft_id"] == taken["draft_id"] and fixed["problems"] == []
    assert seen["filed"]["ok"]

    [listed] = lab.pending()
    item = store.get_inbox_item(seen["filed"]["inbox_id"])
    assert listed["id"] == item["id"] and (item["kind"], item["requester_id"]) == ("hire", office.hr)
    assert item["project_id"] == pid and item["payload"]["draft_id"] == taken["draft_id"]
    assert (item["payload"]["draft"]["name"], item["payload"]["draft"]["project_id"]) == ("하늘", pid)
    assert office.named("하늘") == []  # nothing is made before the owner decides

    r = lab.decide(item, "approve")
    assert r.status_code == 200, r.text
    [hired] = office.named("하늘")
    assert (hired["department_id"], hired["project_id"], hired["retired_at"]) == ("program", pid, None)
    folder = employees.folder(office.desk.cfg.studio_dir, hired["id"])
    texts = {name: (folder / name).read_text(encoding="utf-8") for name in employees.FILES}
    assert texts == {"PERSONA.md": PERSONA, "GUIDE.md": GUIDE, "MEMORY.md": "", "employee.yaml": YAML}
    [dm] = store.list_rooms(kind="dm", employee_id=hired["id"])
    assert store.get_room_member(dm["id"], hired["id"]) is not None
    [hello] = store.list_messages(dm["id"])
    assert (hello["author_kind"], hello["author_id"], hello["meta"]["event"], hello["body"]) == (
        "employee", hired["id"], "greeting", GREETING.replace("{name}", "하늘"))
    assert lab.pending() == [] and office.waiting == []  # an approval asks no turn of HR


# --- a revision comes back with the draft's id; a rejection ends it -----------------------------------------------
def test_a_revision_is_reworked_on_the_same_draft_and_a_rejection_asks_no_turn(office):
    lab, store, seen = office.lab, office.store, {}

    def first(call):
        seen["draft"] = office.tool(call, "employee_draft", **draft(project_id=office.pid))["draft_id"]
        seen["one"] = office.tool(call, "employee_submit", draft_id=seen["draft"], reason="프로그래머 충원")["inbox_id"]

    def second(call):
        seen["prompt"] = call.prompt
        seen["fixed"] = office.tool(call, "employee_draft", draft_id=seen["draft"], name="바다")
        seen["two"] = office.tool(call, "employee_submit", draft_id=seen["draft"], reason="이름을 고쳤다")["inbox_id"]

    office.play(["결재를 올렸습니다", "고쳐서 다시 올렸습니다"], first, second)
    office.say("프로그래머 한 명 뽑자")
    office.turn()

    r = lab.decide({"id": seen["one"]}, "revise", comment="이름을 바다로 바꿔라")
    assert r.status_code == 200, r.text
    note = office.note()
    assert note["author_kind"] == "system" and note["meta"] == {"event": "inbox.decided", "inbox_id": seen["one"]}
    assert "코멘트: 이름을 바다로 바꿔라" in note["body"] and note["body"].endswith(f"초안 id: {seen['draft']}")
    office.turn()  # the turn the revision asked for: HR reads the decision as the room's new message
    assert len(office.desk.calls) == 2
    assert "이름을 바다로 바꿔라" in seen["prompt"] and f"초안 id: {seen['draft']}" in seen["prompt"]
    assert seen["fixed"]["problems"] == [] and office.desk.said(office.room)[-1] == ("employee", "고쳐서 다시 올렸습니다")

    [listed] = lab.pending()
    again = store.get_inbox_item(seen["two"])
    assert listed["id"] == again["id"] != seen["one"]
    assert again["payload"]["supersedes_inbox_id"] == seen["one"]  # §5.5: what the revised item is answered with
    assert (again["payload"]["draft_id"], again["payload"]["draft"]["name"]) == (seen["draft"], "바다")

    r = lab.decide(again, "reject", comment="이번에는 뽑지 않는다")
    assert r.status_code == 200, r.text
    note = office.note()
    assert note["author_kind"] == "system" and note["meta"]["inbox_id"] == again["id"]
    assert "이번에는 뽑지 않는다" in note["body"] and note["body"].endswith(f"초안 id: {seen['draft']}")
    assert office.waiting == [] and len(office.desk.calls) == 2  # no turn: HR waits for what the owner says next
    assert lab.pending() == [] and office.named("바다") == [] and office.named("하늘") == []


# --- anyone but HR: neither the values nor the tools --------------------------------------------------------------
def test_an_employee_who_is_not_hr_gets_neither_the_values_nor_hrs_tools(office):
    seen = {}

    def act(call):
        seen["prompt"] = call.prompt
        seen["draft"] = office.tool(call, "employee_draft", **draft())
        seen["submit"] = office.tool(call, "employee_submit", draft_id="drf_00000000", reason="직접 뽑는다")

    office.play(["안녕하세요"], act)
    office.say("안녕?", room=office.desk.dm)  # 민수, of the program part
    office.turn()
    assert TITLE not in seen["prompt"]
    assert [(out["ok"], out["code"]) for out in (seen["draft"], seen["submit"])] == [(False, "actor_not_allowed")] * 2
    assert office.lab.pending() == [] and office.named("하늘") == []
    assert not list((office.desk.cfg.studio_dir / "drafts").glob("*"))  # no draft was saved either


# --- the values themselves ----------------------------------------------------------------------------------------
def test_the_values_of_a_hire_are_the_projects_the_parts_and_the_guarded_models(office):
    store, checks = office.store, employees.context(office.desk.cfg)
    guarded = next(name for name, spec in checks.providers.specs.items() if spec.kind == employees.GUARDED_KIND)
    catalog = [{"provider": guarded, "models": [{"model": "sonnet", "efforts": ["low", "medium"]},
                                                {"model": "haiku", "efforts": []}]},
               {"provider": "no-such-runner", "models": [{"model": "gpt-x", "efforts": ["high"]}]}]
    store.update_department("program", default_models={"chat": {"model": "opus", "effort": "high"}})
    store.update_department("design_doc", default_models={})
    names = {part["id"]: part["name"] for part in store.list_departments()}

    text = employees.hiring_notes(store, checks, catalog)
    lines = text.splitlines()
    assert lines[0] == TITLE and f"- `{office.pid}` 게임" in lines
    assert f"- `program` {names['program']} (chat 기본값: opus · high)" in lines
    assert f"- `design_doc` {names['design_doc']} (chat 기본값: 기본값 없음)" in lines
    assert not any(line.startswith("- `hr`") for line in lines)  # HR hires no HR
    assert "- `sonnet` 노력치 low · medium" in lines
    assert "- `haiku` 노력치 없음 — `effort` 를 적지 않는다" in lines
    assert "gpt-x" not in text  # a runner no guard hook can be put on (§9.4 의 8)

    bare = employees.hiring_notes(store, checks)  # no catalog: the models are left out, the rest stands
    assert "모델" not in bare and f"- `{office.pid}` 게임" in bare
    store.update_project(office.pid, archived_at=now())
    gone = employees.hiring_notes(store, checks, catalog)
    assert office.pid not in gone and "프로젝트: 아직 없다. `project_id` 를 비운다." in gone


def test_a_project_made_in_the_middle_of_the_interview_is_in_the_values_of_the_next_turn(office):
    store = office.store
    office.play(["어느 프로젝트에 둘까요?", "새 게임에 두겠습니다"])
    office.say("프로그래머 한 명 뽑자")
    office.turn()
    made = store.create_project("새 게임", "D:/new-game", "")  # after the conversation began
    office.say("방금 만든 프로젝트에 배치하자")
    office.turn()

    one, two = office.runner.calls
    assert one.resume_session is None and one.session_id
    assert (two.resume_session, two.session_id) == (one.session_id, None)  # the turn resumes: no goal is sent
    assert TITLE in one.prompt and made["id"] not in one.prompt
    said, values = two.prompt.split(TITLE, 1)  # the new message, then the section as it is now
    assert "owner: 방금 만든 프로젝트에 배치하자" in said and TITLE not in values
    assert f"- `{made['id']}` 새 게임" in values and f"- `{office.pid}` 게임" in values
    assert "- `program` " in values and "`hr`" not in values
    assert "===== GUIDE.md =====" not in two.prompt and "===== PERSONA.md =====" not in two.prompt  # that only
    assert office.desk.said(office.room)[-1] == ("employee", "새 게임에 두겠습니다")


def test_the_values_are_of_hrs_chat_turn_and_not_of_a_read_turn(office):
    turns, store = office.desk.turns, office.store
    hr, other = store.get_employee(office.hr), store.get_employee(office.desk.emp)
    assert turns._hiring(store, hr, chat.Where("chat", None)).startswith(TITLE)
    task = {"id": "tsk_00000000", "title": "점프"}  # a task room: HR reads there, and drafts no hire (§8.4)
    assert turns._hiring(store, hr, chat.Where("read", office.pid, task)) == ""
    assert turns._hiring(store, other, chat.Where("chat", office.pid)) == ""


def test_a_catalog_that_cannot_be_read_does_not_stop_hrs_turn(office, monkeypatch):
    def broken(self):
        raise RuntimeError("no catalog")

    monkeypatch.setattr(ProviderRegistry, "catalog", broken)
    seen = {}
    office.play(["무엇을 맡길까요?"], lambda call: seen.update(prompt=call.prompt))
    office.say("프로그래머 한 명 뽑자")
    office.turn()
    values = seen["prompt"].split(TITLE, 1)[1]
    assert f"- `{office.pid}` 게임" in values and "- `program` " in values and "모델 —" not in values
    assert office.desk.said(office.room)[-1] == ("employee", "무엇을 맡길까요?")


# --- hr/GUIDE.md says what the server does ------------------------------------------------------------------------
def test_the_hr_guide_names_the_values_and_what_each_decision_asks():
    guide = employees.hr_texts()["GUIDE.md"]
    assert f"'{employees.HIRING_TITLE}' 절" in guide  # where the ids and the models are
    assert "수정 요청이면 코멘트대로 고쳐, 글에 적힌 초안 id 를 `draft_id` 로 다시 올린다" in guide
    assert "반려면 다시 올리지 않고 owner 의 다음 말을 기다린다" in guide  # §5.5: a rejection asks no turn
    assert "절은 턴마다 새로 온다 — 가장 최근 것을 본다" in guide  # `_make`: a turn that resumes gets it again


def test_a_start_names_an_hr_guide_made_before_the_section_and_leaves_it(office, monkeypatch):
    warned = []
    monkeypatch.setattr(api.log, "warning", lambda text, *args: warned.append(text % args))
    assert api.ensure_hr() is None and warned == []  # the guide a start made names the section
    guide = employees.folder(office.desk.cfg.studio_dir, office.hr) / "GUIDE.md"
    old = "# 지침\n\n- 결정은 이 방으로 온다. 반려되면 고쳐서 다시 올린다.\n".encode("utf-8")
    guide.write_bytes(old)
    assert api.ensure_hr() is None  # the server starts again
    [line] = warned
    assert office.hr in line and "GUIDE.md" in line and employees.HIRING_TITLE in line
    # said in the log and not written over (§3.15); H-6 only adds the '프로젝트 정책' section after the owner's text
    assert guide.read_bytes().startswith(old) and b"policy_draft" in guide.read_bytes()
