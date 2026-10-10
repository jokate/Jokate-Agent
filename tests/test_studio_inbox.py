"""Studio inbox (S-5, part 1): each kind is filed, decided and followed up (§5) — the task moves, the run is
fed and resumed, the employee is made — and a second decision of the same item is refused. The engine's part
is a fake Effects (runs, apply, rollback) and a fake Hooks (interject, commit, chat turn): no AI is called."""
import hashlib
import re
from pathlib import Path
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from relay_agent.studio import Actor, Effects, Hooks, Inbox, InboxError, TaskMachine, TransitionError, api, inbox
from relay_agent.studio.store import now
from relay_agent.studio.tasks import BLOCKING_KINDS, OWNER, SYSTEM

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TEXT = dict(title="2단 점프", reason="조작감", spec="공중에서 한 번 더", done_criteria="테스트 통과", type="feature")
OPTIONS = [{"id": "a", "label": "컴포넌트", "detail": "ActorComponent 로 뺀다"},
           {"id": "b", "label": "상속", "detail": "Character 를 상속한다"}]
PAYLOAD = {
    "design_approval": {"stage": "design", "stop_reason": "설계 게이트"},
    "structure_choice": {"question": "점프를 어디에 둘까?", "options": OPTIONS, "recommended": "a"},
    "question": {"reason": "asked", "body": "점프 높이는?",
                 "options": [{"id": "hi", "label": "높게"}, {"id": "lo", "label": "낮게"}]},
    "pre_approval": {"what": "입력 매핑 변경", "why": "2단 점프 키", "if_not": "기존 키를 쓴다"},
    "budget": {"scope": "run", "stage": "implement", "spent_usd": 5.2, "limit_usd": 5.0, "stop_reason": "런 예산"},
    "risky_command": {"command": "git  push\n origin main", "tool": "Bash", "rule_id": "git_push",
                      "reason": "원격에 올린다"},
    "final_review": {"summary": "2단 점프를 넣었다",
                     "checklist": [{"criterion": "테스트 통과", "status": "verified_auto", "note": ""}],
                     "judgment_calls": [{"text": "점프 높이를 1.5배로", "why": "기획서에 없다"},
                                        {"text": "입력 버퍼 0.1초", "why": "관례"}]},
}
BY_SYSTEM = ("design_approval", "budget", "risky_command")  # §4.3 T4: the server files these
DRAFT = {"name": "서연", "department_id": "program", "persona_md": "# 서연\n", "guide_md": "# 지침\n",
         "employee_yaml": "department: program\n"}


class Fx(Effects):
    """Stands in for the engine behind a transition: runs, the gates, apply (T7) and rollback (T9 · T11)."""

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


class Hk(Hooks):
    """Stands in for engine.interject, the git commit of H-3 and the chat turn of S-7."""

    def __init__(self):
        self.calls = []
        self.stopped = True  # the run has stopped, so a decision text goes in at once
        self.fail = None

    def interject(self, run_id, text):
        self.calls.append(("interject", run_id, text))
        if self.fail:
            raise self.fail
        return self.stopped

    def commit(self, folder, message):
        self.calls.append(("commit", Path(folder), message))

    def chat_turn(self, employee_id, room_id):
        self.calls.append(("chat_turn", employee_id, room_id))

    def fed(self):
        return [call for call in self.calls if call[0] == "interject"]


class World:
    """The server app on a studio DB of its own: a project, an employee with a 1:1 room, and an inbox."""

    def __init__(self, server, fx, hk):
        self.owner = TestClient(server.app, headers=AUTH)
        self.store = s = api.get_store()
        self.fx, self.hk, self.dir = fx, hk, server.cfg.studio_dir
        self.m = TaskMachine(s, fx)
        self.box = Inbox(s, self.m, hk, studio_dir=self.dir)
        self.pid = s.create_project("게임", "D:/game", "game")["id"]
        self.emp, self.dm = self.staff("민수")
        self.me = Actor.employee(self.emp)
        self.n = 0
        self._hr = None

    def staff(self, name, department="program", placed=True):
        emp = self.store.create_employee(name, department, project_id=self.pid if placed else None)["id"]
        dm = self.store.create_room("dm", title=name, employee_id=emp)["id"]
        self.store.create_room_member(dm, emp)
        return emp, dm

    def hr(self):
        """The HR employee (made on first use): (id, 1:1 room id)."""
        if self._hr is None:
            self._hr = self.staff("인사팀장", "hr", placed=False)
        return self._hr

    def task(self, state="working"):
        """`proposed`: 민수 registered it. Otherwise the owner did, with an assignee of its own; `working` has
        been started and holds a run."""
        if state == "proposed":
            return self.m.create_task(self.me, "proposed", self.pid, **TEXT).task
        self.n += 1
        who, _ = self.staff(f"직원{self.n}")
        row = self.m.create_task(OWNER, "backlog", self.pid, assignee_id=who, **TEXT).task
        if state == "working":
            self.m.request_start(row["id"], OWNER)
            row = self.m.transition(row["id"], "working", SYSTEM).task
        return row

    def resume(self, task):
        """What S-8 does when it gets a slot after T5: the task leaves the line and its run goes on."""
        self.store.update_task(task["id"], queued_at=None)

    def file(self, kind, task=None, payload=None):
        """An item of `kind` on a working task, filed by whoever §4.3 says files it."""
        task = task or self.task()
        payload = PAYLOAD[kind] if payload is None else payload
        system = kind in BY_SYSTEM or (kind == "question" and payload.get("reason", "asked") != "asked")
        who = SYSTEM if system else Actor.employee(task["assignee_id"])
        return self.box.create(kind, who, title=f"{kind} 결재", payload=payload, task_id=task["id"])

    def review(self, task=None):
        task = task or self.task()
        return task, self.file("final_review", task)

    def hire(self, **over):
        hr, _ = self.hr()
        payload = {"action": "hire", "draft_id": "drf_1", "reason": "프로그래머가 모자란다", "draft": dict(DRAFT),
                   **over}
        return self.box.create("hire", Actor.employee(hr), title="프로그래머 채용", payload=payload)

    def decide(self, item, decision, **body):
        return self.owner.post(f"/studio/inbox/{item['id']}/decide", json={"decision": decision, **body})

    def item(self, item):
        return self.store.get_inbox_item(item["id"])

    def room(self, task_id):
        return self.store.list_rooms(kind="task", task_id=task_id)[0]["id"]

    def said(self, room_id):
        return self.store.list_messages(room_id)

    def types(self):
        return [event["type"] for event in self.store.list_stream_events(limit=10_000)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    fx, hk = Fx(), Hk()
    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(api, "_effects", fx)
    monkeypatch.setattr(api, "_hooks", hk)
    api.close_store()
    yield World(server, fx, hk)
    api.close_store()


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def refused(r, status, code):
    assert (r.status_code, r.json().get("code")) == (status, code), r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    return r.json()


def invalid(r):
    """FastAPI's own 422: a list in `detail`, no `code` (§6.3)."""
    assert r.status_code == 422 and isinstance(r.json()["detail"], list) and "code" not in r.json(), r.text


def doc_section(start, end):
    text = DOC.read_text(encoding="utf-8")
    at = text.index(start)
    return text[at:text.index(end, at)]


def table(section, width):
    """The rows of the tables in a piece of the document that have `width` cells."""
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines() if line.strip().startswith("|")]
    return [cells for cells in rows if len(cells) == width]


def ticks(cell):
    return re.findall(r"`([^`]+)`", cell)


# --- the contract (§5.2, §5.3, §5.4, §5.6, §6.4) ----------------------------------------------------------
def test_decision_values_are_the_table_of_5_2():
    seen = []
    for cells in table(doc_section("### 5.2 종류별 결정 값", "- `edits` (`task_proposal`)"), 6):
        kind = cells[0].strip("`")
        if kind not in inbox.KINDS:
            continue
        values = tuple(ticks(cells[2]))
        seen.append(kind)
        assert inbox.DECISIONS[kind] == values
        assert inbox.COMMENT_REQUIRED.get(kind, ()) == tuple(v for v in ticks(cells[3]) if v in values)
        assert (kind in inbox.NOTIFY_OK) == cells[5].startswith("`approve`")
    assert tuple(seen) == inbox.KINDS == get_args(api.InboxKind) and len(seen) == 9
    assert set(inbox.TARGET) == {(k, d) for k in inbox.KINDS if k != "hire" for d in inbox.DECISIONS[k]}
    assert {inbox.TARGET[(k, d)] for k in BLOCKING_KINDS for d in inbox.DECISIONS[k]} == {"working"}  # all T5


def test_required_payload_keys_are_the_tables_of_5_3_and_5_4():
    seen = set()
    for cells in table(doc_section("### 5.3 payload", "- `task_proposal` 의 본문 필드"), 4):
        kind = cells[0].strip("`")
        if kind in inbox.KINDS and kind != "final_review":
            keys = tuple(t.removesuffix("[]") for t in ticks(cells[2]) if re.fullmatch(r"[a-z_]+(\[\])?", t))
            assert inbox.PAYLOAD_REQUIRED[kind] == keys
            seen.add(kind)
    marked = tuple(cells[0].strip("`").removesuffix("[]")
                   for cells in table(doc_section("payload (저장):", "`package` (GET 때 계산):"), 3) if cells[1] == "●")
    assert inbox.PAYLOAD_REQUIRED["final_review"] == marked and len(seen) == 8
    actions = [cells[0].strip("`") for cells in table(doc_section("| `action` | 뜻 |", "- `source_inbox_id`"), 3)]
    assert actions[2:] == list(inbox.HIRE_BODY)  # after the header and the rule
    reasons = [cells[0].strip("`") for cells in table(doc_section("`question` 의 `reason`:", "M-7 의"), 4)]
    assert reasons[2:] == list(inbox.QUESTION_OPTIONS)


def test_decision_texts_are_the_sentences_of_5_6():
    doc = doc_section("### 5.6 결정문", "### 5.7")
    for key, sentence in inbox.SENTENCES.items():
        if key != ("budget", "reject"):
            for ours, theirs in (("{id}", "<id>"), ("{label}", "<label>"), ("{command}", "<command>")):
                sentence = sentence.replace(ours, theirs)
            assert sentence in doc, key
    for key, sentence in inbox.ANSWERS.items():
        assert sentence.replace("{label}", "<고른 선택지의 label>") in doc, key
    assert inbox.SENTENCES[("budget", "reject")] == "예산을 더 쓰지 않는다. " + inbox.ANSWERS["wrap_up"]
    assert set(inbox.SENTENCES) == {("final_review", "rework")} | {
        (k, d) for k in BLOCKING_KINDS if k != "question" for d in inbox.DECISIONS[k]}
    item = {"id": "inb_1a2b3c4d", "kind": "pre_approval", "decision": "approve", "decision_comment": " 키만 ",
            "decision_data": {}, "payload": {}}
    head = "[결재 inb_1a2b3c4d · 사전 승인 · 승인]\n올린 변경이 승인됐다. 올린 범위 안에서 진행하라."
    assert inbox.decision_text(item) == head + "\n코멘트: 키만" and head + "\n코멘트: " in doc
    assert inbox.decision_text({**item, "decision_comment": ""}) == head


def test_routes_are_the_s5_rows_of_the_inbox_table():
    doc = set()
    for cells in table(doc_section("**결재 · 집계**", "**런 · 사용량"), 5):
        row = re.fullmatch(r"`(GET|POST) (/studio/[^`]+)`", cells[0])
        if row and "S-5" in cells[4]:
            doc.add((row.group(1), row.group(2)))
    built = {(method, r.path) for r in api.router.routes for method in r.methods
             if r.path.startswith("/studio/inbox")}
    assert len(doc) == 3 and built == doc
    assert set(inbox.CODES) <= set(api.STATUS)


def test_install_hands_the_hooks_to_the_requests(world, monkeypatch):
    for name in ("_cfg", "_effects", "_liveness", "_hooks"):
        monkeypatch.setattr(api, name, getattr(api, name))  # install() sets them: put back afterwards
    hk = Hk()
    api.install(FastAPI(), api._cfg, world.fx, hooks=hk)
    box = api.get_inbox(api.get_machine(world.store))
    assert box.hooks is hk and box.machine.effects is world.fx and box.studio_dir == world.dir


# --- the six blocking kinds: filed (T4) -> decided (T5) -> the run is fed and waits for a slot --------------
DECIDE = [  # kind, payload (None = PAYLOAD[kind]), decision, comment, data, a piece of the decision text
    ("design_approval", None, "approve", "", None, "설계가 승인됐다"),
    ("design_approval", None, "revise", "입력 처리를 분리해라", None, "설계를 코멘트대로 고쳐서"),
    ("structure_choice", None, "choose", "", {"option_id": "a"}, "구조는 선택지 `a`(`컴포넌트`)로 정해졌다"),
    ("structure_choice", None, "revise", "셋째 안도 내라", None, "선택지를 다시 내라"),
    ("question", None, "answer", "", {"option_id": "hi"}, "질문의 답이다: `높게`."),
    ("question", None, "answer", "1.5배로", None, "코멘트: 1.5배로"),
    ("question", {"reason": "run_failed", "body": "런이 실패했다", "error": "boom"}, "answer", "",
     {"option_id": "retry"}, "멈췄던 자리에서 이어서 하라."),
    ("question", {"reason": "run_cancelled", "body": "런이 끊겼다"}, "answer", "", {"option_id": "wrap_up"},
     "새 일을 벌이지 말고"),
    ("question", {"reason": "exchange_limit", "body": "왕복 한도"}, "answer", "", {"option_id": "continue"},
     "왕복 수를 다시 셌다"),
    ("question", {"reason": "editor_unresponsive", "body": "에디터 무응답"}, "answer", "", {"option_id": "retry"},
     "멈췄던 자리에서 이어서 하라."),
    ("question", {"reason": "no_review_request", "body": "검수 요청 없이 끝났다"}, "answer", "마저 해라", None,
     "남은 일을 마치고"),
    ("pre_approval", None, "approve", "", None, "올린 변경이 승인됐다"),
    ("pre_approval", None, "reject", "", None, "올린 변경은 하지 않는다"),
    ("pre_approval", None, "revise", "키만 바꿔라", None, "올린 변경을 코멘트대로"),
    ("budget", None, "approve", "", None, "예산이 늘었다"),
    ("budget", None, "reject", "", None, "예산을 더 쓰지 않는다. 새 일을 벌이지 말고"),
    ("risky_command", None, "approve", "", None, "한 번 실행해도 된다: `git  push\n origin main`"),
    ("risky_command", None, "reject", "", None, "이 명령은 실행하지 않는다"),
]


@pytest.mark.parametrize("kind, payload, decision, comment, data, piece", DECIDE)
def test_blocking_item_is_filed_decided_and_fed_to_the_run(world, kind, payload, decision, comment, data, piece):
    task = world.task()
    world.store.update_task(task["id"], exchange_count=3)
    mark = len(world.types())
    item = world.file(kind, task, payload)
    assert (item["kind"], item["status"], item["task_id"], item["run_id"]) == (kind, "pending", task["id"], "run_001")
    assert item["requester_kind"] == ("system" if payload and payload["reason"] != "asked" or kind in BY_SYSTEM
                                      else "employee")
    assert world.store.get_task(task["id"])["status"] == "blocked"
    assert world.types()[mark:mark + 2] == ["inbox.created", "task.transitioned"]

    mark = len(world.types())
    out = ok(world.decide(item, decision, comment=comment, **({"data": data} if data else {})))
    assert out["task"]["status"] == "working" and out["task"]["queued_at"]  # T5: in the line for a slot
    assert (out["inbox"]["status"], out["inbox"]["decision"], out["inbox"]["decision_comment"]) == (
        "decided", decision, comment)
    assert out["inbox"]["delivery"] == "delivered" and out["inbox"]["delivered_at"]
    assert out["inbox"]["decision_data"]["via"] == "app"
    assert world.types()[mark:mark + 2] == ["inbox.decided", "task.transitioned"]
    assert world.types()[-1] == "message.created"                        # the system message comes after (§7.4)
    assert ("after", "T5") in world.fx.calls                             # where S-8 resumes the run

    (_, run_id, text), = world.hk.fed()
    assert run_id == "run_001" and piece in text
    assert text.startswith(f"[결재 {item['id']} · {inbox.KIND_LABEL[kind]} · {inbox.DECISION_LABEL[decision]}]")
    assert (f"코멘트: {comment}" in text) == bool(comment)
    if piece.startswith("코멘트"):
        assert text.count("\n") == 1  # a comment alone: head and comment, no instruction sentence
    note = world.said(world.room(task["id"]))[-1]
    assert (note["author_kind"], note["body"]) == ("system", text)
    assert note["meta"] == {"event": "inbox.decided", "inbox_id": item["id"], "task_id": task["id"]}

    assert out["task"]["exchange_count"] == (0 if (payload or {}).get("reason") == "exchange_limit" else 3)
    grant = out["inbox"]["decision_data"].get("grant")
    if (kind, decision) == ("risky_command", "approve"):
        key = hashlib.sha256("Bash\ngit push origin main".encode()).hexdigest()
        assert grant == {"key": key, "used_at": None, "used_run_id": None, "void": False}
    else:
        assert grant is None

    again = refused(world.decide(item, decision, comment=comment, **({"data": data} if data else {})),
                    409, "already_decided")  # the same value, too
    assert again["inbox"]["id"] == item["id"] and again["inbox"]["status"] == "decided"
    assert len(world.hk.fed()) == 1 and world.fx.calls.count(("after", "T5")) == 1


def test_system_questions_get_the_servers_options(world):
    for reason, ids in inbox.QUESTION_OPTIONS.items():
        if reason == "asked":
            continue
        item = world.file("question", payload={"reason": reason, "body": "…", "options": [{"id": "x", "label": "y"}]})
        assert [o["id"] for o in item["payload"].get("options", [])] == list(ids)
        assert all(o["label"] == inbox.OPTION_LABEL[o["id"]] for o in item["payload"].get("options", []))


def test_who_files_what(world):
    task = world.task()
    assignee = Actor.employee(task["assignee_id"])
    with pytest.raises(TransitionError) as e:  # the machine's rule (§4.3 T4): the guard hook's kind
        world.box.create("risky_command", assignee, payload=PAYLOAD["risky_command"], task_id=task["id"])
    assert e.value.code == "actor_not_allowed"
    with pytest.raises(InboxError) as e:       # `run_failed` is the server's reason
        world.box.create("question", assignee, payload={"reason": "run_failed", "body": "…"}, task_id=task["id"])
    assert (e.value.code, e.value.extra["fields"]) == ("missing_fields", ["reason"])
    with pytest.raises(ValueError):            # an item without a task: the second half of S-5 (§5.8 나)
        world.box.create("budget", SYSTEM, payload=PAYLOAD["budget"])
    with pytest.raises(ValueError):            # C1 files it
        world.box.create("task_proposal", world.me, payload={}, task_id=task["id"])
    assert world.store.get_task(task["id"])["status"] == "working" and world.store.list_inbox() == []

    # §5.7: a member of the task room who is not the assignee asks — the item names it and the task's run
    world.store.create_room_member(world.room(task["id"]), world.emp)
    item = world.box.create("question", world.me, title="이 값 맞나?\n둘째 줄", payload={"body": "점프 높이 확인"},
                            task_id=task["id"])
    assert (item["requester_id"], item["run_id"], item["title"]) == (world.emp, task["run_id"], "이 값 맞나?")
    assert item["payload"] == {"body": "점프 높이 확인", "reason": "asked"}
    with pytest.raises(TransitionError) as e:  # one pending item per task
        world.file("pre_approval", task)
    assert e.value.code == "illegal_transition" and len(world.store.list_inbox(task_id=task["id"])) == 1


BROKEN = [  # kind, payload, the keys named
    ("design_approval", {"stage": "design"}, ["stop_reason"]),
    ("structure_choice", {"question": "구조?", "options": OPTIONS[:1]}, ["options"]),
    ("structure_choice", {"question": "구조?", "options": [OPTIONS[0], OPTIONS[0]]}, ["options"]),
    ("structure_choice", {**PAYLOAD["structure_choice"], "recommended": "z"}, ["recommended"]),
    ("question", {"reason": "bored", "body": " "}, ["body", "reason"]),
    ("pre_approval", {"what": "입력 매핑"}, ["why", "if_not"]),
    ("budget", {**PAYLOAD["budget"], "scope": "day", "spent_usd": "5"}, ["spent_usd", "scope"]),
    ("risky_command", {"command": "rm -rf x", "tool": "Bash"}, ["rule_id", "reason"]),
    ("final_review", {"summary": "끝", "checklist": [{"criterion": "테스트", "status": "maybe"}]}, ["checklist"]),
    ("final_review", {"summary": "", "checklist": []}, ["summary", "checklist"]),
]


@pytest.mark.parametrize("kind, payload, fields", BROKEN)
def test_a_broken_payload_files_nothing(world, kind, payload, fields):
    task = world.task()
    marks = world.store.get_task(task["id"]), len(world.types())
    with pytest.raises(InboxError) as e:
        world.file(kind, task, payload)
    assert (e.value.code, e.value.as_dict()["fields"]) == ("missing_fields", fields)
    assert marks == (world.store.get_task(task["id"]), len(world.types())) and world.store.list_inbox() == []


def test_hire_payloads_are_checked_by_action():
    head = {"draft_id": "d", "reason": "r"}
    assert inbox.validate_payload("hire", {**head, "action": "fire"}) == ["action"]
    assert inbox.validate_payload("hire", {**head, "action": "hire", "draft": {"name": "서연"}}) == [
        "draft.department_id", "draft.persona_md", "draft.guide_md", "draft.employee_yaml"]
    amend = {**head, "action": "guide_amend", "target_employee_id": "emp_1", "file": "MEMORY.md", "new_text": "x",
             "diff": ""}
    assert inbox.validate_payload("hire", amend) == ["base_sha256", "file"]  # MEMORY.md is not amended (§9.1)
    assert inbox.validate_payload("hire", {**amend, "file": "GUIDE.md", "base_sha256": ""}) == []
    assert inbox.validate_payload("hire", {"action": "hire", "draft": dict(DRAFT)}) == ["draft_id", "reason"]
    assert inbox.validate_payload("question", ["reason"]) == ["payload"]
    with pytest.raises(ValueError):
        inbox.validate_payload("memo", {})


# --- what a decision may carry (§5.1 의 3, §5.2) -----------------------------------------------------------
def test_invalid_decisions_change_nothing(world):
    choice, question = world.file("structure_choice"), world.file("question")
    url = f"/studio/inbox/{choice['id']}/decide"
    pick = {"decision": "choose", "data": {"option_id": "a"}}
    for body in ({"decision": "approve"}, {"decision": "choose"}, {"decision": "choose", "data": {"option_id": "z"}},
                 {"decision": "revise"}, {"decision": "revise", "comment": " "},
                 {"decision": "choose", "data": {"option_id": "a", "grant": {}}}, {**pick, "via": "sms"},
                 {**pick, "via": "notification"}):
        assert refused(world.owner.post(url, json=body), 422, "invalid_decision")["allowed"] == ["choose", "revise"]
    assert refused(world.decide(question, "answer"), 422, "invalid_decision")["allowed"] == ["answer"]
    refused(world.decide(question, "answer", comment="x", data={"edits": {}}), 422, "invalid_decision")
    refused(world.decide(question, "answer", data={"option_id": "retry"}), 422, "invalid_decision")
    invalid(world.owner.post(url, json={}))
    invalid(world.owner.post(url, json={"decision": "choose", "extra": 1}))
    refused(world.owner.post("/studio/inbox/inb_none/decide", json={"decision": "approve"}), 404, "not_found")
    assert world.item(choice)["status"] == world.item(question)["status"] == "pending"
    assert world.hk.calls == [] and ("check", "T5") not in world.fx.calls
    assert ok(world.decide(choice, "choose", data={"option_id": "a", "edits": None}))["task"]["status"] == "working"


def test_a_notification_decides_only_a_plain_approve(world):
    for kind in BLOCKING_KINDS:
        item = world.file(kind)
        r = world.decide(item, "approve", via="notification")
        if kind in inbox.NOTIFY_OK:
            assert ok(r)["inbox"]["decision_data"] == {"via": "notification"}
        else:  # a choice or an answer is needed, or N-3 keeps it in the app
            refused(r, 422, "invalid_decision")
            assert world.item(item)["status"] == "pending"
    item = world.file("pre_approval")
    refused(world.decide(item, "approve", comment="좋다", via="notification"), 422, "invalid_decision")
    refused(world.decide(item, "reject", via="notification"), 422, "invalid_decision")
    assert ok(world.decide(item, "reject", via="dashboard"))["inbox"]["decision_data"] == {"via": "dashboard"}
    proposal, = world.store.list_inbox(task_id=world.task("proposed")["id"])
    assert ok(world.decide(proposal, "approve", via="notification"))["task"]["status"] == "backlog"
    _, review = world.review()
    refused(world.decide(review, "pass", via="notification"), 422, "invalid_decision")


# --- task_proposal: T1 · T2 -------------------------------------------------------------------------------
def test_a_proposal_approved_with_edits_becomes_a_backlog_task(world):
    first, task = world.task("backlog"), world.task("proposed")
    item, = world.store.list_inbox(task_id=task["id"], status="pending")
    assert inbox.validate_payload("task_proposal", item["payload"]) == []  # C1 files a whole §5.3 payload
    edits = {"title": "2단 점프 (공중)", "assignee_id": world.emp, "is_milestone": True, "depends_on": [first["id"]]}
    out = ok(world.decide(item, "approve", data={"edits": edits}))
    assert (out["task"]["status"], out["task"]["title"], out["task"]["assignee_id"], out["task"]["is_milestone"]) == (
        "backlog", "2단 점프 (공중)", world.emp, True)
    assert [d["depends_on_id"] for d in world.store.list_task_deps(task_id=task["id"])] == [first["id"]]
    assert out["inbox"]["delivery"] == "none" and out["inbox"]["decision_data"] == {"edits": edits, "via": "app"}
    assert world.room(task["id"])                                               # T1 opens the task room
    events = world.store.list_task_events(task["id"])
    assert [e["kind"] for e in events] == ["created", "transition", "edited", "assigned", "deps"]
    assert events[-1]["detail"] == {"from": [], "to": [first["id"]]}
    assert world.fx.calls.count(("after", "T1")) == 1 and ("after", "deps") not in world.fx.calls  # one transaction
    note = world.said(world.dm)[-1]                                             # the proposer's 1:1 room
    assert note["author_kind"] == "system" and "승인" in note["body"] and task["title"] in note["body"]
    assert note["meta"] == {"event": "inbox.decided", "inbox_id": item["id"], "task_id": task["id"]}
    assert world.hk.calls == []                                                 # no run to feed
    refused(world.decide(item, "reject", comment="늦었다"), 409, "already_decided")
    assert world.store.get_task(task["id"])["status"] == "backlog"


def test_a_proposal_is_rejected_with_a_comment(world):
    task = world.task("proposed")
    item, = world.store.list_inbox(task_id=task["id"])
    assert refused(world.decide(item, "reject"), 422, "invalid_decision")["allowed"] == ["approve", "reject"]
    refused(world.decide(item, "reject", comment="안 한다", data={"edits": {"title": "x"}}), 422, "invalid_decision")
    out = ok(world.decide(item, "reject", comment="지금은 안 한다"))
    assert out["task"]["status"] == "rejected" and out["task"]["finished_at"]
    assert world.store.list_task_events(task["id"])[-1]["reason"] == "지금은 안 한다"
    body = world.said(world.dm)[-1]["body"]
    assert "거절" in body and "코멘트: 지금은 안 한다" in body
    refused(world.decide(item, "approve"), 409, "already_decided")


def test_edits_of_a_proposal_are_checked_before_anything_is_written(world, monkeypatch):
    first, task = world.task("backlog"), world.task("proposed")
    item, = world.store.list_inbox(task_id=task["id"])
    marks = world.store.get_task(task["id"]), len(world.types())
    for edits in ({"status": "done"}, {"type": "epic"}, {"is_milestone": "yes"}, {"depends_on": "task_1"}, "title"):
        refused(world.decide(item, "approve", data={"edits": edits}), 422, "invalid_decision")
    blank = refused(world.decide(item, "approve", data={"edits": {"title": " ", "depends_on": [first["id"]]}}),
                    422, "missing_fields")
    assert blank["fields"] == ["title"]
    for edits in ({"depends_on": ["task_none"]}, {"depends_on": [task["id"]]},
                  {"depends_on": [first["id"]], "assignee_id": "emp_none"}):  # a good predecessor, a bad assignee
        assert refused(world.decide(item, "approve", data={"edits": edits}), 409, "precondition_failed")["failed"] == []
    refused(world.decide(item, "approve", data={"edits": {"assignee_id": "emp_none"}}), 409, "precondition_failed")
    assert marks == (world.store.get_task(task["id"]), len(world.types()))
    assert world.item(item)["status"] == "pending" and world.store.list_task_deps(task_id=task["id"]) == []

    def overtaken(*args, **kwargs):  # T1 is refused after the predecessors were written, in the same transaction
        raise TransitionError("stale_state", "다른 요청이 먼저 바꿨다")

    monkeypatch.setattr(world.m, "_write_inbox", overtaken)
    with pytest.raises(TransitionError):
        world.box.decide(item["id"], "approve", data={"edits": {"depends_on": [first["id"]]}})
    assert marks == (world.store.get_task(task["id"]), len(world.types()))
    assert world.item(item)["status"] == "pending" and world.store.list_task_deps(task_id=task["id"]) == []
    assert [e["kind"] for e in world.store.list_task_events(task["id"])] == ["created"]


# --- final_review: T6 files it, T7 · T8 · T9 decide it ----------------------------------------------------
def test_pass_applies_the_changes_and_closes_the_task(world):
    task = world.task()
    world.store.update_task(task["id"], exchange_count=2)
    mark = len(world.types())
    _, item = world.review(task)
    assert (item["kind"], item["run_id"], item["payload"]["exchange_count"]) == ("final_review", "run_001", 2)
    assert world.types()[mark:mark + 2] == ["inbox.created", "task.transitioned"]
    assert world.store.get_task(task["id"])["status"] == "review"
    out = ok(world.decide(item, "pass"))
    assert out["task"]["status"] == "done" and out["task"]["finished_at"] and out["inbox"]["delivery"] == "none"
    assert ("before", "T7") in world.fx.calls and ("after", "T7") in world.fx.calls  # apply + commit: the engine's
    assert world.hk.calls == [] and world.said(world.room(task["id"])) == []          # no run goes on
    refused(world.decide(item, "revert"), 409, "already_decided")
    assert world.store.get_task(task["id"])["status"] == "done" and world.fx.calls.count(("before", "T7")) == 1


def test_a_pass_the_engine_refuses_leaves_the_item_to_be_decided_again(world):
    task, item = world.review()
    world.fx.fail[("before", "T7")] = ValueError("다른 실행이 그 폴더를 쓰는 중이다")
    assert "다른 실행" in refused(world.decide(item, "pass"), 409, "engine_error")["detail"]
    del world.fx.fail[("before", "T7")]
    world.fx.fail[("check", "T7")] = TransitionError("precondition_failed", "런이 아직 돈다", failed=["run_running"])
    assert refused(world.decide(item, "pass"), 409, "precondition_failed")["failed"] == ["run_running"]
    assert world.item(item)["status"] == "pending" and world.store.get_task(task["id"])["status"] == "review"
    world.fx.fail.clear()
    assert ok(world.decide(item, "pass"))["task"]["status"] == "done"


def test_rework_feeds_the_comment_and_revert_rolls_back(world):
    task, item = world.review()
    refused(world.decide(item, "rework"), 422, "invalid_decision")  # the comment is the decision text
    out = ok(world.decide(item, "rework", comment="착지 판정이 틀렸다"))
    assert out["task"]["status"] == "working" and out["task"]["queued_at"] and out["inbox"]["delivery"] == "delivered"
    (_, run_id, text), = world.hk.fed()
    assert run_id == task["run_id"] and "검수에서 재작업이 나왔다" in text and "코멘트: 착지 판정이 틀렸다" in text
    assert world.said(world.room(task["id"]))[-1]["body"] == text and ("after", "T8") in world.fx.calls
    refused(world.decide(item, "pass"), 409, "already_decided")

    task, item = world.review()
    out = ok(world.decide(item, "revert"))
    assert (out["task"]["status"], out["task"]["run_id"], out["inbox"]["delivery"]) == ("backlog", None, "none")
    assert ("before", "T9") in world.fx.calls and len(world.hk.fed()) == 1  # rolled back; no text for that run
    refused(world.decide(item, "revert"), 409, "already_decided")


def test_judgment_marks_reach_hr_or_the_system_room(world):
    marks = [{"index": 0, "mark": "should_have_asked"}, {"index": 1, "mark": "ok"}]
    task, item = world.review()
    for bad in ([{"index": 2, "mark": "ok"}], [{"index": 0, "mark": "bad"}], [{"index": 0}], "x",
                [{"index": True, "mark": "ok"}], [{"index": 0, "mark": "ok"}, {"index": 0, "mark": "ok"}]):
        refused(world.decide(item, "pass", data={"judgment_marks": bad}), 422, "invalid_decision")
    assert ok(world.decide(item, "pass", data={"judgment_marks": marks}))["inbox"]["decision_data"] == {
        "judgment_marks": marks, "via": "app"}
    note = world.said("room_system")[-1]  # nobody in HR yet: the system room is told
    assert "점프 높이를 1.5배로" in note["body"] and "입력 버퍼" not in note["body"] and task["id"] in note["body"]
    assert note["meta"]["inbox_id"] == item["id"] and world.hk.calls == []

    hr, hr_dm = world.hr()
    _, item = world.review()
    ok(world.decide(item, "rework", comment="다시", data={"judgment_marks": marks}))
    assert "점프 높이를 1.5배로" in world.said(hr_dm)[-1]["body"] and ("chat_turn", hr, hr_dm) in world.hk.calls
    _, item = world.review()
    ok(world.decide(item, "pass", data={"judgment_marks": marks[1:]}))  # nothing to learn from an `ok`
    assert len(world.said(hr_dm)) == 1 and world.hk.calls.count(("chat_turn", hr, hr_dm)) == 1


# --- delivery, races, cancel (§3.13, §4.7, §5.1, §10.5) ---------------------------------------------------
def test_a_text_the_run_cannot_take_yet_waits(world):
    world.hk.stopped = False  # T4 just happened: the run is still going
    item = world.file("pre_approval")
    out = ok(world.decide(item, "approve"))
    assert (out["inbox"]["delivery"], out["inbox"]["delivered_at"]) == ("pending", None)
    assert world.box.deliver(item["id"]) is False
    world.hk.fail = ValueError("run is done")  # engine.interject refuses a finished run
    assert world.box.deliver(item["id"]) is False and world.item(item)["delivery"] == "pending"
    world.hk.fail, world.hk.stopped = None, True
    assert world.box.deliver(item["id"]) is True
    assert world.item(item)["delivery"] == "delivered" and world.item(item)["delivered_at"]
    assert world.box.deliver(item["id"]) is False and len(world.hk.fed()) == 4  # delivered once
    assert world.box.deliver("inb_none") is False


def test_a_failure_after_the_commit_keeps_the_decision_and_what_follows_it(world):
    item = world.file("pre_approval")
    world.fx.fail[("after", "T5")] = RuntimeError("queue is down")
    with pytest.raises(RuntimeError):
        world.box.decide(item["id"], "approve")
    assert (world.item(item)["status"], world.item(item)["delivery"]) == ("decided", "delivered")
    assert world.said(world.room(item["task_id"]))[-1]["meta"]["inbox_id"] == item["id"]


def test_of_two_decisions_at_once_one_wins(world):
    item = world.file("pre_approval")
    inner = []
    world.fx.on[("check", "T5")] = lambda: inner.append(world.box.decide(item["id"], "reject"))
    late = refused(world.decide(item, "approve"), 409, "already_decided")  # checked first, wrote second
    assert inner[0]["inbox"]["decision"] == late["inbox"]["decision"] == "reject"
    (_, _, text), = world.hk.fed()
    assert "올린 변경은 하지 않는다" in text and world.store.get_task(item["task_id"])["status"] == "working"

    item = world.file("budget")
    world.fx.on[("check", "T5")] = lambda: world.box.decide(item["id"], "approve")
    assert refused(world.decide(item, "approve"), 409, "already_decided")["inbox"]["decision"] == "approve"
    assert len(world.hk.fed()) == 2  # the same value loses too, and feeds nothing twice


@pytest.mark.parametrize("kind, decision, comment, rule", [
    ("pre_approval", "approve", "", "T5"), ("budget", "approve", "", "T5"), ("budget", "reject", "여기까지", "T5"),
    ("final_review", "rework", "다시", "T8")])
def test_the_run_is_fed_before_it_is_resumed(world, kind, decision, comment, rule):
    """§5.5: interject -> approve -> advance. `Effects.after` is where S-8 resumes the run."""
    item = world.file(kind)
    seen = []

    def resumed():
        said = world.said(world.room(item["task_id"]))
        seen.append((len(world.hk.fed()), world.item(item)["delivery"],
                     said[-1]["meta"].get("inbox_id") if said else None))

    world.fx.on[("after", rule)] = resumed
    ok(world.decide(item, decision, comment=comment))
    assert seen == [(1, "delivered", item["id"])]  # the text and the task room's message were there already
    assert len(world.hk.fed()) == 1 and world.fx.calls.count(("after", rule)) == 1


def test_a_run_that_is_still_going_is_resumed_owing_the_text(world):
    world.hk.stopped = False
    item = world.file("pre_approval")
    seen = []
    world.fx.on[("after", "T5")] = lambda: seen.append((len(world.hk.fed()), world.item(item)["delivery"]))
    ok(world.decide(item, "approve"))
    assert seen == [(1, "pending")]  # asked once, before the resume; S-8 calls deliver() again
    assert len(world.hk.fed()) == 1 and world.fx.calls.count(("after", "T5")) == 1


def test_the_machine_calls_committed_between_the_commit_and_after(world):
    first, task = world.task("backlog"), world.task("proposed")
    seen = []

    def committed(out):
        seen.append((out.rule, world.store.get_task(task["id"])["status"], ("after", "T1") in world.fx.calls))
        raise RuntimeError("what follows broke")

    with pytest.raises(RuntimeError):
        world.m.transition(task["id"], "backlog", OWNER, depends_on=[first["id"]], committed=committed)
    assert seen == [("T1", "backlog", False)] and world.fx.calls.count(("after", "T1")) == 1  # after ran all the same
    assert [d["depends_on_id"] for d in world.store.list_task_deps(task_id=task["id"])] == [first["id"]]
    with pytest.raises(TransitionError):  # refused: nothing was committed, so nothing follows
        world.m.transition(task["id"], "proposed", OWNER, committed=committed)
    with pytest.raises(ValueError):  # the caller's bug: only T1 takes predecessors
        world.m.transition(first["id"], "cancelled", OWNER, depends_on=[])
    assert len(seen) == 1 and world.store.get_task(first["id"])["status"] == "backlog"


def test_an_item_without_a_task_stays_pending_while_no_engine_is_wired(world):
    """§5.8 나: `Inbox.file_run` files these and the engine's Hooks decide them (tests/test_studio_runs.py).
    With the default Hooks nothing takes the decision: it is refused, not a 500, and the item stays pending."""
    with pytest.raises(ValueError):
        world.box.create("budget", SYSTEM, title="예산", payload=PAYLOAD["budget"])
    for kind in ("budget", "design_approval"):
        item = world.store.create_inbox_item(kind, f"{kind} 결재", "system", project_id=world.pid, run_id="run_x",
                                             payload=PAYLOAD[kind])
        assert refused(world.decide(item, "approve"), 409, "precondition_failed")["failed"] == []
        assert world.item(item)["status"] == "pending"
        view = ok(world.owner.get(f"/studio/inbox/{item['id']}"))
        assert view["task"] is None and view["allowed_decisions"] == list(inbox.DECISIONS[kind])
    assert world.hk.calls == [] and world.fx.calls == []


def test_cancelling_a_blocked_task_closes_its_item_and_what_the_run_was_owed(world):
    task = world.task()
    first = world.file("risky_command", task)
    world.hk.stopped = False  # the text of the first decision never reached the run
    out = ok(world.decide(first, "approve"))
    assert out["inbox"]["delivery"] == "pending" and out["inbox"]["decision_data"]["grant"]["void"] is False
    second = world.file("question", task, {"reason": "run_cancelled", "body": "런이 끊겼다"})
    mark = len(world.types())
    r = world.owner.post(f"/studio/tasks/{task['id']}/cancel", json={"reason": "안 하기로 했다"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and ("before", "T11") in world.fx.calls
    closed = world.item(second)
    assert (closed["status"], closed["decision"], closed["delivery"]) == ("cancelled", None, "none")
    assert closed["closed_at"] and world.types()[mark:mark + 2] == ["inbox.cancelled", "task.transitioned"]
    owed = world.item(first)
    assert owed["delivery"] == "none" and owed["decision_data"]["grant"]["void"] is True
    assert owed["decision_data"]["via"] == "app" and owed["decision"] == "approve"  # the rest of it stays
    late = refused(world.decide(second, "answer", comment="계속 해라"), 409, "already_decided")
    assert late["inbox"]["status"] == "cancelled" and world.store.get_task(task["id"])["status"] == "cancelled"


@pytest.mark.parametrize("decision, comment, dropped", [("pass", "", True), ("revert", "", True),
                                                       ("rework", "다시", False)])
def test_unused_grants_end_with_the_run_and_used_ones_stay(world, decision, comment, dropped):
    task = world.task()
    used = world.file("risky_command", task)
    ok(world.decide(used, "approve"))
    world.resume(task)
    data = world.item(used)["decision_data"]
    data["grant"].update(used_at=now(), used_run_id=task["run_id"])  # the guard let the command through once
    world.store.update_inbox_item(used["id"], decision_data=data)
    unused = world.file("risky_command", task, {**PAYLOAD["risky_command"], "command": "git push --force"})
    ok(world.decide(unused, "approve"))
    world.resume(task)
    _, item = world.review(task)
    ok(world.decide(item, decision, comment=comment))
    assert world.item(used)["decision_data"]["grant"] == data["grant"]
    assert world.item(unused)["decision_data"]["grant"]["void"] is dropped  # T8 keeps it (§12.6 의 2)


# --- hire: no task; an approval writes files and rows (§5.5) ----------------------------------------------
def test_an_approved_hire_makes_the_employee(world):
    hr, _ = world.hr()
    mark = len(world.types())
    item = world.hire()
    assert (item["kind"], item["status"], item["task_id"], item["run_id"], item["requester_id"]) == (
        "hire", "pending", None, None, hr)
    assert world.types()[mark:] == ["inbox.created"]
    count, mark = len(world.store.list_employees()), len(world.types())
    edits = {"draft": {"first_message": "잘 부탁드립니다"}}
    out = ok(world.decide(item, "approve", data={"edits": edits}))
    assert out["task"] is None and out["inbox"]["decision_data"] == {"edits": edits, "via": "app"}
    assert (out["inbox"]["status"], out["inbox"]["decision"], out["inbox"]["delivery"]) == ("decided", "approve", "none")

    employee, = [e for e in world.store.list_employees() if e["name"] == "서연"]
    folder = world.dir / "employees" / employee["id"]
    assert employee["department_id"] == "program" and employee["project_id"] is None
    assert {p.name: p.read_bytes().decode("utf-8") for p in folder.iterdir()} == {
        "PERSONA.md": "# 서연\n", "GUIDE.md": "# 지침\n", "MEMORY.md": "", "employee.yaml": "department: program\n"}
    dm, = world.store.list_rooms(kind="dm", employee_id=employee["id"])
    assert [m["employee_id"] for m in world.store.list_room_members(room_id=dm["id"])] == [employee["id"]]
    assert employee["id"] in [m["employee_id"] for m in world.store.list_room_members(room_id="room_dep_program")]
    hello, = world.said(dm["id"])
    assert (hello["author_kind"], hello["author_id"], hello["body"]) == ("employee", employee["id"], "잘 부탁드립니다")
    assert hello["meta"] == {"event": "greeting", "inbox_id": item["id"]}
    assert world.types()[mark:] == ["inbox.decided", "employee.updated", "room.updated", "room.updated",
                                    "message.created"]
    made = world.store.list_stream_events(limit=10_000)[mark + 1]["data"]
    assert made == {"employee": employee, "change": "created"}
    assert [call[:2] for call in world.hk.calls] == [("commit", folder)]  # the folder's first commit (H-3)
    assert world.owner.get(f"/studio/employees/{employee['id']}").json()["files"]["persona_md"] == "# 서연\n"

    refused(world.decide(item, "reject"), 409, "already_decided")
    assert len(world.store.list_employees()) == count + 1 and len(world.hk.calls) == 1


def test_a_hire_that_cannot_be_made_stays_pending_and_leaves_no_folder(world):
    taken = world.hire(draft={**DRAFT, "name": "민수"})
    assert refused(world.decide(taken, "approve"), 409, "conflict")["field"] == "name"
    nowhere = world.hire(draft_id="drf_2", draft={**DRAFT, "department_id": "dep_none"})
    assert refused(world.decide(nowhere, "approve"), 409, "precondition_failed")["failed"] == []
    second_hr = world.hire(draft_id="drf_3", draft={**DRAFT, "department_id": "hr"})
    refused(world.decide(second_hr, "approve"), 409, "precondition_failed")
    assert refused(world.decide(taken, "approve", data={"edits": {"draft": {"name": " "}}}),
                   422, "missing_fields")["fields"] == ["draft.name"]
    refused(world.decide(taken, "approve", data={"edits": {"draft": {"salary": 1}}}), 422, "invalid_decision")
    refused(world.decide(taken, "approve", data={"edits": {"new_text": "x"}}), 422, "invalid_decision")
    assert {world.item(i)["status"] for i in (taken, nowhere, second_hr)} == {"pending"}
    assert not (world.dir / "employees").exists() and world.hk.calls == []
    ok(world.decide(taken, "approve", data={"edits": {"draft": {"name": "서연"}}}))  # fixed while approving
    hello, = world.said(world.store.list_rooms(kind="dm")[-1]["id"])
    assert hello["body"] == inbox.GREETING.replace("{name}", "서연")


def test_a_hire_decided_in_between_is_already_decided(world, monkeypatch):
    item = world.hire()
    real = world.box._name_taken

    def racing(name, tx=None):
        world.box.decide(item["id"], "reject")  # another request gets there after the checks of this one
        return real(name, tx)

    monkeypatch.setattr(world.box, "_name_taken", racing)
    with pytest.raises(InboxError) as e:
        world.box.decide(item["id"], "approve")
    assert e.value.code == "already_decided" and e.value.extra["inbox"]["decision"] == "reject"
    assert [e["name"] for e in world.store.list_employees()] == ["민수", "인사팀장"]
    assert list((world.dir / "employees").iterdir()) == []  # the folder written for it is gone


def test_a_hire_overtaken_by_another_approval_is_refused_like_the_first_check(world, monkeypatch):
    hr, _ = world.hr()
    named = world.hire()
    successor = world.hire(draft_id="drf_hr", draft={**DRAFT, "name": "새 인사", "department_id": "hr"})
    world.store.update_employee(hr, retired_at=now())  # HR has left: the part takes one successor
    real, between = world.box._name_taken, []

    def racing(name, tx=None):
        taken = real(name, tx)
        if tx is None:  # after the checks of this request and before its transaction, another approval commits
            between.pop()()
        return taken

    monkeypatch.setattr(world.box, "_name_taken", racing)
    between.append(lambda: world.staff("서연"))
    with pytest.raises(InboxError) as e:
        world.box.decide(named["id"], "approve")
    assert (e.value.code, e.value.extra["field"]) == ("conflict", "name")
    between.append(lambda: world.staff("다른 인사", "hr", placed=False))
    with pytest.raises(InboxError) as e:
        world.box.decide(successor["id"], "approve")
    assert e.value.code == "precondition_failed"  # not a 500 of employees_one_hr
    assert {world.item(i)["status"] for i in (named, successor)} == {"pending"}
    assert list((world.dir / "employees").iterdir()) == [] and world.hk.calls == []
    assert sorted(e["name"] for e in world.store.list_employees()) == sorted(["민수", "서연", "다른 인사"])


def test_reject_and_revise_of_a_hire_tell_hr(world):
    hr, hr_dm = world.hr()
    item = world.hire()
    refused(world.decide(item, "revise"), 422, "invalid_decision")
    ok(world.decide(item, "revise", comment="말투를 부드럽게"))
    note = world.said(hr_dm)[-1]
    assert note["author_kind"] == "system" and "코멘트: 말투를 부드럽게" in note["body"]
    assert note["meta"] == {"event": "inbox.decided", "inbox_id": item["id"]}
    assert world.hk.calls == [("chat_turn", hr, hr_dm)]  # HR reworks the draft

    again = world.hire()  # … and hands it in again: it answers the revised one
    assert again["payload"]["supersedes_inbox_id"] == item["id"] and world.item(item)["status"] == "decided"
    third = world.hire()  # once more while that one is pending: it replaces it
    assert (world.item(again)["status"], world.item(again)["decision"]) == ("cancelled", None)
    assert third["payload"]["supersedes_inbox_id"] == again["id"]
    assert world.types()[-2:] == ["inbox.cancelled", "inbox.created"]
    assert refused(world.decide(again, "approve"), 409, "already_decided")["inbox"]["status"] == "cancelled"
    ok(world.decide(third, "reject"))
    assert "반려" in world.said(hr_dm)[-1]["body"] and len(world.hk.calls) == 1  # a rejection starts no turn
    assert [e["name"] for e in world.store.list_employees()] == ["민수", "인사팀장"]


def test_only_hr_files_a_hire_item(world):
    world.hr()
    payload = {"action": "hire", "draft_id": "drf_1", "reason": "모자란다", "draft": dict(DRAFT)}
    for who in (world.me, SYSTEM, OWNER):
        with pytest.raises(InboxError) as e:
            world.box.create("hire", who, title="채용", payload=payload)
        assert e.value.code == "actor_not_allowed"
    with pytest.raises(InboxError) as e:
        world.box.create("hire", Actor.employee(world.hr()[0]), title=" ", payload={**payload, "draft_id": ""})
    assert (e.value.code, e.value.extra["fields"]) == ("missing_fields", ["draft_id", "title"])
    assert world.store.list_inbox() == []


def test_guide_amend_and_policy_write_the_file_that_is_still_the_base(world):
    hr = Actor.employee(world.hr()[0])
    folder = world.dir / "employees" / world.emp
    folder.mkdir(parents=True)
    (folder / "GUIDE.md").write_bytes("옛 지침\n".encode("utf-8"))

    def amend(base, n):
        payload = {"action": "guide_amend", "draft_id": f"drf_g{n}", "reason": "검수 표시", "file": "GUIDE.md",
                   "target_employee_id": world.emp, "new_text": "새 지침\n", "diff": "-옛\n+새", "base_sha256": base}
        return world.box.create("hire", hr, title="지침 수정안", payload=payload)

    stale = amend(hashlib.sha256(b"something else").hexdigest(), 1)
    refused(world.decide(stale, "approve"), 409, "precondition_failed")  # the file changed after the draft
    assert (folder / "GUIDE.md").read_bytes() == "옛 지침\n".encode("utf-8") and world.item(stale)["status"] == "pending"
    item = amend(hashlib.sha256("옛 지침\n".encode("utf-8")).hexdigest(), 2)
    mark = len(world.types())
    assert ok(world.decide(item, "approve"))["inbox"]["status"] == "decided"
    assert (folder / "GUIDE.md").read_bytes() == "새 지침\n".encode("utf-8")
    assert world.types()[mark:] == ["inbox.decided", "employee.updated"]
    assert world.hk.calls[-1][:2] == ("commit", folder)
    refused(world.decide(item, "approve"), 409, "already_decided")

    draft = {"action": "policy", "draft_id": "drf_p", "reason": "정책이 없다", "new_text": "# 정책\n", "diff": "",
             "base_sha256": ""}
    with pytest.raises(InboxError) as e:  # which project?
        world.box.create("hire", hr, title="정책 초안", payload=draft)
    assert (e.value.code, e.value.extra["fields"]) == ("missing_fields", ["project_id"])
    policy = world.box.create("hire", hr, title="정책 초안", payload=draft, project_id=world.pid)
    ok(world.decide(policy, "approve"))
    assert (world.dir / "projects" / world.pid / "POLICY.md").read_bytes() == "# 정책\n".encode("utf-8")
    late = world.box.create("hire", hr, title="정책 수정안", payload={**draft, "draft_id": "drf_p2"},
                            project_id=world.pid)
    refused(world.decide(late, "approve"), 409, "precondition_failed")  # "" = no file yet, and now there is one


# --- REST: list and item (§6.4) ---------------------------------------------------------------------------
def test_the_list_orders_pages_and_counts(world):
    a, b, c = world.file("pre_approval"), world.file("budget"), world.file("design_approval")
    h = world.hire()

    def ids(**params):
        return [i["id"] for i in ok(world.owner.get("/studio/inbox", params=params))["items"]]

    page = ok(world.owner.get("/studio/inbox"))
    assert [i["id"] for i in page["items"]] == [a["id"], b["id"], c["id"], h["id"]]  # the longest waiting first
    assert (page["has_more"], page["pending_count"]) == (False, 4)
    assert ids(kind="budget") == [b["id"]] and ids(task_id=c["task_id"]) == [c["id"]]
    assert ids(project_id=world.pid) == [a["id"], b["id"], c["id"]] and ids(status="decided") == []
    page = ok(world.owner.get("/studio/inbox", params={"limit": 2}))
    assert [i["id"] for i in page["items"]] == [a["id"], b["id"]] and page["has_more"] is True

    for day, item in enumerate((a, b, c), start=1):
        ok(world.decide(item, "approve"))
        world.store.update_inbox_item(item["id"], closed_at=f"2026-01-0{day}T10:00:00")
    page = ok(world.owner.get("/studio/inbox", params={"status": "decided", "limit": 2}))
    assert [i["id"] for i in page["items"]] == [c["id"], b["id"]]  # the newest closed first
    assert (page["has_more"], page["pending_count"]) == (True, 1)
    page = ok(world.owner.get("/studio/inbox", params={"status": "decided", "limit": 2,
                                                       "before": page["items"][-1]["closed_at"]}))
    assert [i["id"] for i in page["items"]] == [a["id"]] and page["has_more"] is False
    assert ids(status="cancelled") == [] and ids(status="pending") == [h["id"]]
    invalid(world.owner.get("/studio/inbox", params={"status": "open"}))
    invalid(world.owner.get("/studio/inbox", params={"kind": "memo"}))
    invalid(world.owner.get("/studio/inbox", params={"limit": 0}))


def test_items_closed_in_the_same_second_come_on_one_page(world):
    """`before` is `closed_at` alone (§6.4), so a page never ends inside a second: nothing is skipped."""
    items = [world.file("pre_approval") for _ in range(6)]
    for item, day in zip(items, (3, 2, 2, 2, 1, 1)):
        ok(world.decide(item, "approve"))
        world.store.update_inbox_item(item["id"], closed_at=f"2026-01-0{day}T10:00:00")
    i0, i1, i2, i3, i4, i5 = (item["id"] for item in items)

    def page(**params):
        out = ok(world.owner.get("/studio/inbox", params={"status": "decided", **params}))
        return [i["id"] for i in out["items"]], out["has_more"]

    assert page(limit=1) == ([i0], True)                      # the page ends where a second ends: as it was
    assert page(limit=2) == ([i0, i3, i2, i1], True)          # … inside one: the rest of that second comes too
    assert page(limit=3) == ([i0, i3, i2, i1], True)
    assert page(limit=4) == ([i0, i3, i2, i1], True)
    assert page(limit=1, before="2026-01-03T10:00:00") == ([i3, i2, i1], True)
    assert page(limit=1, before="2026-01-02T10:00:00") == ([i5, i4], False)   # the last second: nothing is left
    seen, cursor = [], None
    while True:  # walking the pages with the cursor meets each item once
        out = ok(world.owner.get("/studio/inbox", params={"status": "decided", "limit": 2,
                                                           **({"before": cursor} if cursor else {})}))
        seen += [i["id"] for i in out["items"]]
        if not out["has_more"]:
            break
        cursor = out["items"][-1]["closed_at"]
    assert seen == [i0, i3, i2, i1, i5, i4]


def test_an_item_shows_its_task_and_what_can_be_decided(world):
    task, item = world.review()
    view = ok(world.owner.get(f"/studio/inbox/{item['id']}"))
    assert view["task"]["id"] == task["id"] and view["allowed_decisions"] == ["pass", "rework", "revert"]
    assert view["package"] is None and view["payload"]["summary"] == "2단 점프를 넣었다"  # package: S-12
    ok(world.decide(item, "pass"))
    view = ok(world.owner.get(f"/studio/inbox/{item['id']}"))
    assert view["allowed_decisions"] == [] and view["task"]["status"] == "done" and view["decision"] == "pass"
    view = ok(world.owner.get(f"/studio/inbox/{world.hire()['id']}"))
    assert view["task"] is None and "package" not in view
    assert view["allowed_decisions"] == ["approve", "reject", "revise"]
    refused(world.owner.get("/studio/inbox/inb_none"), 404, "not_found")
    assert TestClient(world.owner.app, client=("192.168.0.7", 50000)).get("/studio/inbox").status_code in (401, 403)
