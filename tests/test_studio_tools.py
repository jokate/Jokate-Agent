"""Studio MCP on the server's side (S-6, 1/2): actor tokens (§8.2) and the seven task tools behind
`POST /studio/mcp/<tool>` (§8.3–§8.5, §5.7). The engine is a dict of run states — no run and no AI is started."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from relay_agent.studio import TransitionError, api, sat, tools
from test_studio_api_tasks import TEXT, refused, world  # noqa: F401 - `world` is that file's fixture, used here too

SEVEN = {"task_propose", "followup_propose", "task_get", "task_list", "task_progress", "task_request_review",
         "ask_owner"}
CHECKLIST = [{"criterion": "테스트 통과", "status": "verified_auto", "note": ""}]
REVIEW = dict(summary="2단 점프를 붙였다", checklist=CHECKLIST)
QUESTION = dict(kind="question", title="점프 높이는?", body="얼마나 높이 뛰나?")
OPTIONS = [{"id": "a", "label": "컴포넌트", "detail": "캐릭터에 붙인다", "figure": ""},
           {"id": "b", "label": "어빌리티", "detail": "GAS 로 만든다", "figure": {"mermaid": "graph TD; A-->B"}}]
ASKS = {  # the tool's kind -> (its arguments, the inbox kind it files: §1.5)
    "question": (dict(body="얼마나 높이 뛰나?", options=[{"id": "low", "label": "낮게"}, {"id": "high", "label": "높게"}]),
                 "question"),
    "choice": (dict(body="어디에 둘까?", options=OPTIONS, recommended="a", fix_points="입력 처리"), "structure_choice"),
    "approval": (dict(what="엔진 설정을 바꾼다", why="입력이 늦다", if_not="지연이 남는다", policy_ref="정책 3"),
                 "pre_approval"),
}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def forge(token, **over):
    """The claims of `token` with some of them changed, under the signature it had."""
    _, claims, signature = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    body.update(over)
    claims = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    return f"sat1.{claims}.{signature}"


def failed(out, code, end_turn=False):
    assert (out["ok"], out.get("code"), out["end_turn"]) == (False, code, end_turn), out
    return out


class Desk:
    """The runs the fake engine knows, and calls made with an employee's token."""

    def __init__(self, world, runs):
        self.w, self.runs, self.store = world, runs, world.store
        self.http = TestClient(world.owner.app)  # this PC, no token of its own
        self.tracked, self.dms = [], {}

    def working(self, state="working", **over):
        """A task taken to `state` whose run the engine still reports as running."""
        task = self.w.task(state, **over)
        self.runs[task["run_id"]] = "running"
        return task

    def token(self, task):
        return api.actor_token(task["run_id"])

    def room(self, task):
        return self.store.list_rooms(kind="task", task_id=task["id"])[0]

    def join(self, task, employee_id):
        self.store.create_room_member(self.room(task)["id"], employee_id)

    def turn(self, employee_id, *, task=None):
        """The token of a turn that is no task's run: a chat turn in the employee's 1:1 room, or (with
        `task`) a read turn in that task's room."""
        if task is None:
            if employee_id not in self.dms:
                self.dms[employee_id] = self.store.create_room("dm", title=f"1:1 {employee_id}",
                                                               employee_id=employee_id)["id"]
                self.store.create_room_member(self.dms[employee_id], employee_id)
            room_id = self.dms[employee_id]
        else:
            room_id = self.room(task)["id"]
        run_id = f"turn_{len(self.tracked) + 1}"
        api.track_run(run_id, mode="read" if task else "chat", employee_id=employee_id, room_id=room_id,
                      task_id=task["id"] if task else None)
        self.tracked.append(run_id)
        self.runs[run_id] = "running"
        return api.actor_token(run_id)

    def set_employee(self, employee_id, **cols):
        with self.store.transaction() as tx:
            for column, value in cols.items():
                tx.exec(f"UPDATE employees SET {column} = ? WHERE id = ?", (value, employee_id))

    def post(self, tool, token, **args):
        return self.http.post(f"/studio/mcp/{tool}", json=args, headers=bearer(token))

    def call(self, tool, token, **args):
        """A call that got past the authentication: always HTTP 200 and the three keys of §8.3."""
        r = self.post(tool, token, **args)
        assert r.status_code == 200, r.text
        out = r.json()
        assert isinstance(out["ok"], bool) and isinstance(out["end_turn"], bool) and out["message"], out
        assert out["message"].endswith(tools.END_TURN) == out["end_turn"], out
        return out

    def untouched(self, task):
        return self.store.get_task(task["id"])["status"] == "working" and not self.store.list_inbox(task_id=task["id"])


@pytest.fixture
def desk(world, monkeypatch):  # noqa: F811
    runs = {}

    def liveness(run_id):
        if run_id not in runs:
            raise FileNotFoundError(run_id)  # RelayEngine.load: no such run
        return {"status": runs[run_id], "alive": runs[run_id] == "running"}

    monkeypatch.setattr(api, "_liveness", liveness)
    monkeypatch.setattr(api, "_run_view", None)
    made = Desk(world, runs)
    yield made
    for run_id in made.tracked:
        api.untrack_run(run_id)


# --- the token (§8.2 의 검사 1) ------------------------------------------------------------------------------
def test_a_token_carries_its_claims_and_nothing_else_verifies():
    token = sat.issue(run="run_1", emp="emp_a", room="room_1", task=None, mode="chat", iat=1700000000)
    _, claims, signature = token.split(".")
    assert token.startswith("sat1.") and "=" not in claims and len(signature) == 64
    assert sat.verify(token) == {"v": 1, "run": "run_1", "emp": "emp_a", "room": "room_1", "task": None,
                                 "mode": "chat", "iat": 1700000000}
    assert sat.verify(sat.issue(run="r", emp="e", room="m", task="tsk_1", mode="work"))["task"] == "tsk_1"
    for bad in (None, "", "sat1.", "sat1.c.s", "sat1.é.é", token + "x", token[:-1], f"sat2.{claims}.{signature}",
                f"sat1.{claims}", f"sat1.{claims}.{signature}.x", f"sat1.{claims}.{signature.upper()}",
                forge(token, emp="emp_b"), forge(token, mode="work"), forge(token)):
        assert sat.verify(bad) is None, bad
    with pytest.raises(ValueError):
        sat.issue(run="r", emp="e", room="m", task=None, mode="write")
    sat.reset_secret()  # a restart: the secret is only in memory
    assert sat.verify(token) is None


# --- authentication (§6.2, §8.2 의 검사 1–3) ------------------------------------------------------------------
def test_an_actor_token_is_the_employee_on_the_actor_paths_only(desk):
    task = desk.working()
    token = desk.token(task)
    claims = sat.verify(token)
    assert (claims["run"], claims["emp"], claims["room"], claims["task"], claims["mode"]) == (
        task["run_id"], task["assignee_id"], desk.room(task)["id"], task["id"], "work")
    assert desk.call("task_get", token)["task"]["id"] == task["id"]
    refused(desk.http.get("/studio/projects", headers=bearer(token)), 403, "actor_not_allowed")  # the owner's path
    refused(desk.w.owner.post("/studio/mcp/task_get", json={}), 403, "actor_not_allowed")       # the owner here
    refused(desk.http.post("/studio/mcp/task_get", json={}), 401, "actor_invalid")              # this PC, no token
    assert api.actor_token("run_nobody_started") is None  # not in the ledger: no token, no studio MCP


def test_forged_ended_and_retired_tokens_are_401(desk):
    task = desk.working()
    token = desk.token(task)
    for fake in (forge(token, emp=desk.w.mate),   # another employee under the same signature
                 forge(token, task=None), token[:-4] + ("0000" if not token.endswith("0000") else "1111"),
                 sat.issue(run="run_x", emp=task["assignee_id"], room="room_x", task=None, mode="chat")):  # no such run
        refused(desk.post("task_get", fake), 401, "actor_invalid")
        refused(desk.http.get("/studio/projects", headers=bearer(fake)), 401, "unauthorized")
    for ended in ("done", "failed", "cancelled", "awaiting_approval", "pending"):
        desk.runs[task["run_id"]] = ended
        refused(desk.post("task_get", token), 401, "actor_invalid")
        refused(desk.http.get("/studio/projects", headers=bearer(token)), 401, "unauthorized")
    del desk.runs[task["run_id"]]  # the engine has no such run
    refused(desk.post("task_get", token), 401, "actor_invalid")
    desk.runs[task["run_id"]] = "running"
    assert desk.call("task_get", token)["ok"]
    desk.set_employee(task["assignee_id"], retired_at="2026-10-11T09:00:00")
    refused(desk.post("task_get", token), 401, "actor_invalid")


def test_check_2_follows_the_run_view_and_a_restart_voids_every_token(desk, monkeypatch):
    task = desk.working()
    token = desk.token(task)
    monkeypatch.setattr(api, "_liveness", None)  # no engine connected: cannot say, which is not "ended"
    refused(desk.post("task_get", token), 503, "unavailable")
    status = {"now": "running"}
    monkeypatch.setattr(api, "_run_view", lambda run_id: status["now"] and {
        "id": run_id, "status": status["now"], "changes_status": None})  # what S-8 will set
    assert desk.call("task_get", token)["ok"]
    for gone in ("cancelled", None):
        status["now"] = gone
        refused(desk.post("task_get", token), 401, "actor_invalid")
    status["now"] = "running"
    sat.reset_secret()
    refused(desk.post("task_get", token), 401, "actor_invalid")
    assert desk.call("task_get", desk.token(task))["ok"]  # the token of the next stage


def test_a_missing_argument_is_a_result_and_a_wrong_type_the_422_of_the_request(desk):
    token = desk.token(desk.working())
    for tool in sorted(SEVEN - {"task_get", "task_list"}):
        failed(desk.call(tool, token), "missing_fields")
    r = desk.post("task_list", token, limit="many")
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert desk.post("task_get", token, employee_id=desk.w.mate).status_code == 422  # no such argument: nobody else's id


def test_the_seven_tools_are_in_the_contract():
    schema = api.openapi_schema()
    served = {path.rsplit("/", 1)[1] for path in schema["paths"] if path.startswith("/studio/mcp/")}
    assert served == set(tools.TOOLS) and SEVEN <= served
    assert all(list(schema["paths"][f"/studio/mcp/{name}"]) == ["post"] for name in served)
    models = schema["components"]["schemas"]
    assert set(models["McpAskOwner"]["properties"]) == {
        "kind", "title", "body", "options", "recommended", "fix_points", "what", "why", "if_not", "policy_ref",
        "attachments"}
    assert set(models["McpFollowupPropose"]["properties"]) == set(models["McpTaskPropose"]["properties"]) | {
        "parent_task_id"}
    assert all("required" not in models[name] for name in models if name.startswith("Mcp"))  # all have defaults
    with pytest.raises(ValueError):
        tools.call("room_delete", None, {}, machine=None, inbox=None)


# --- task_propose · followup_propose (C1) -----------------------------------------------------------------
def test_task_propose_registers_a_proposal_in_the_callers_project(desk):
    w = desk.w
    task, before = desk.working(), desk.w.task("backlog")
    token = desk.token(task)
    out = desk.call("task_propose", token, **TEXT, suggested_assignee_id=w.mate, depends_on=[before["id"]])
    assert out["ok"] and not out["end_turn"] and set(out) == {"ok", "end_turn", "message", "task_id", "inbox_id"}
    made, item = w.store.get_task(out["task_id"]), w.store.get_inbox_item(out["inbox_id"])
    assert (made["status"], made["project_id"], made["proposer_id"], made["assignee_id"], made["parent_id"]) == (
        "proposed", w.pid, task["assignee_id"], None, None)
    assert (item["kind"], item["status"], item["task_id"], item["requester_id"]) == (
        "task_proposal", "pending", made["id"], task["assignee_id"])
    assert item["payload"]["suggested_assignee_id"] == w.mate and item["payload"]["depends_on"] == [before["id"]]
    assert (item["payload"]["source_task_id"], item["payload"]["source_run_id"]) == (task["id"], task["run_id"])
    assert [dep["depends_on_id"] for dep in w.store.list_task_deps(task_id=made["id"])] == [before["id"]]
    chat = desk.call("task_propose", desk.turn(w.mate), **TEXT)  # a chat turn: the project the caller is placed in
    assert w.store.get_task(chat["task_id"])["project_id"] == w.pid
    assert "source_task_id" not in w.store.get_inbox_item(chat["inbox_id"])["payload"]


def test_task_propose_refusals_write_nothing(desk):
    w = desk.w
    task = desk.working()
    token = desk.token(task)
    count = len(w.store.list_tasks())
    assert failed(desk.call("task_propose", token), "missing_fields")["fields"] == [
        "title", "reason", "spec", "done_criteria", "type"]
    assert failed(desk.call("task_propose", token, **{**TEXT, "type": "epic", "spec": "  "}),
                  "missing_fields")["fields"] == ["spec", "type"]
    failed(desk.call("task_propose", token, **TEXT, project_id=w.elsewhere), "actor_not_allowed")  # not placed there
    assert failed(desk.call("task_propose", token, **TEXT, campaign_id="cmp_none"), "precondition_failed")["failed"] == []
    desk.set_employee(task["assignee_id"], status="on_leave")
    failed(desk.call("task_propose", token, **TEXT), "actor_not_allowed")
    assert len(w.store.list_tasks()) == count


def test_hr_proposes_anywhere_and_names_the_project(desk):
    w = desk.w
    hr = (w.store.list_employees(department_id="hr") or [w.store.create_employee("인사", "hr")])[0]["id"]
    token = desk.turn(hr)
    assert failed(desk.call("task_propose", token, **TEXT), "missing_fields")["fields"] == ["project_id"]
    out = desk.call("task_propose", token, **TEXT, project_id=w.elsewhere)
    assert w.store.get_task(out["task_id"])["project_id"] == w.elsewhere
    loose = w.store.create_employee("무소속", "program")["id"]  # placed in no project, and not HR
    failed(desk.call("task_propose", desk.turn(loose), **TEXT), "actor_not_allowed")


def test_followup_propose_links_the_parent(desk):
    w = desk.w
    task = desk.working()
    token = desk.token(task)
    out = desk.call("followup_propose", token, **TEXT)  # the parent is the task of this turn
    assert set(out) == {"ok", "end_turn", "message", "task_id", "inbox_id", "parent_id"} and not out["end_turn"]
    assert out["parent_id"] == task["id"] == w.store.get_task(out["task_id"])["parent_id"]
    assert w.store.get_inbox_item(out["inbox_id"])["payload"]["parent_id"] == task["id"]
    chat, other = desk.turn(w.mate), w.task("done")
    assert desk.call("followup_propose", chat, **TEXT, parent_task_id=other["id"])["parent_id"] == other["id"]
    failed(desk.call("followup_propose", chat, **TEXT), "no_task_context")
    failed(desk.call("followup_propose", chat, **TEXT, parent_task_id="tsk_none"), "not_found")
    away = w.task("backlog", project_id=w.elsewhere)
    failed(desk.call("followup_propose", chat, **TEXT, parent_task_id=away["id"]), "not_found")  # cannot see it
    assert failed(desk.call("followup_propose", token, title="x"), "missing_fields")["fields"] == [
        "reason", "spec", "done_criteria", "type"]


# --- task_get · task_list ---------------------------------------------------------------------------------
def test_task_get_reads_the_task_and_what_it_waits_for(desk):
    w = desk.w
    task = desk.working()
    token = desk.token(task)
    first = w.task("backlog")
    later = w.task("backlog", depends_on=[first["id"]])
    out = desk.call("task_get", token)  # the task of this turn
    assert set(out) == {"ok", "end_turn", "message", "task", "depends_on", "dependents", "pending_inbox",
                        "recent_events"} and not out["end_turn"]
    assert (out["task"]["id"], out["task"]["status"]) == (task["id"], "working")
    assert (out["depends_on"], out["dependents"], out["pending_inbox"]) == ([], [], None)
    assert out["recent_events"] and out["recent_events"][-1]["kind"] == "transition"
    assert desk.call("task_get", token, task_id=later["id"])["depends_on"] == [first["id"]]
    assert desk.call("task_get", token, task_id=first["id"])["dependents"] == [later["id"]]
    asked = desk.call("ask_owner", token, **QUESTION)
    pending = desk.call("task_get", token)["pending_inbox"]
    assert set(pending) == {"id", "kind", "title", "created_at"}
    assert (pending["id"], pending["kind"], pending["title"]) == (asked["inbox_id"], "question", QUESTION["title"])
    for n in range(12):
        desk.call("task_progress", token, note=f"걸음 {n}")
    events = desk.call("task_get", token)["recent_events"]
    assert len(events) == 10 and events[-1]["reason"] == "걸음 11"
    chat = desk.turn(w.mate)
    assert desk.call("task_get", chat, task_id=task["id"])["task"]["id"] == task["id"]  # the same project
    failed(desk.call("task_get", chat), "no_task_context")
    failed(desk.call("task_get", chat, task_id="tsk_none"), "not_found")
    away = w.task("backlog", project_id=w.elsewhere)
    failed(desk.call("task_get", chat, task_id=away["id"]), "not_found")


def test_task_list_shows_the_callers_project(desk):
    w = desk.w
    task = desk.working()
    token = desk.token(task)
    mine, away = w.task("backlog"), w.task("backlog", project_id=w.elsewhere)

    def ids(caller=token, **args):
        return [item["id"] for item in desk.call("task_list", caller, **args)["items"]]

    out = desk.call("task_list", token)
    assert not out["end_turn"] and ids() == [task["id"], mine["id"]]
    assert all(set(item) == {"id", "title", "status", "type", "assignee_id", "is_milestone"} for item in out["items"])
    assert ids(status="working") == [task["id"]] and ids(status="done") == []
    assert ids(assignee="me") == [task["id"]] and ids(assignee=mine["assignee_id"]) == [mine["id"]]
    assert ids(limit=1) == ids(limit=0) == [task["id"]] and len(ids(limit=9999)) == 2
    assert failed(desk.call("task_list", token, status="doing"), "missing_fields")["fields"] == ["status"]
    hr = (w.store.list_employees(department_id="hr") or [w.store.create_employee("인사", "hr")])[0]["id"]
    assert set(ids(desk.turn(hr))) == {task["id"], mine["id"], away["id"]}  # HR: every project
    loose = w.store.create_employee("무소속", "program")["id"]
    assert ids(desk.turn(loose)) == []


# --- task_progress ----------------------------------------------------------------------------------------
def test_task_progress_is_the_assignees_in_the_task_run(desk):
    w = desk.w
    task = desk.working()
    token = desk.token(task)
    marks = len(w.events(task["id"]))
    out = desk.call("task_progress", token, note="점프 입력을 붙였다", percent=40)
    event = w.events(task["id"])[-1]
    assert set(out) == {"ok", "end_turn", "message", "event_id"} and out["ok"] and not out["end_turn"]
    assert (out["event_id"], event["kind"], event["reason"]) == (event["id"], "progress", "점프 입력을 붙였다")
    assert "percent" in json.dumps(event) and w.store.list_stream_events()[-1]["type"] == "task.updated"
    failed(desk.call("task_progress", desk.turn(w.mate), note="x"), "no_task_context")
    desk.join(task, w.mate)
    failed(desk.call("task_progress", desk.turn(w.mate, task=task), note="x"), "actor_not_allowed")  # B on A's task
    own_read_turn = desk.turn(task["assignee_id"], task=task)  # the assignee, but not from the task run
    failed(desk.call("task_progress", own_read_turn, note="x"), "actor_not_allowed")
    assert failed(desk.call("task_progress", token), "missing_fields")["fields"] == ["note"]
    assert failed(desk.call("task_progress", token, note="x", percent=101), "missing_fields")["fields"] == ["percent"]
    assert len(w.events(task["id"])) == marks + 1
    closed = desk.working("done")
    assert failed(desk.call("task_progress", desk.token(closed), note="x"), "precondition_failed")["failed"] == []


# --- task_request_review (T6) -----------------------------------------------------------------------------
def test_task_request_review_files_the_final_review_and_ends_the_turn(desk):
    w = desk.w
    task = desk.working()
    token = desk.token(task)
    out = desk.call("task_request_review", token, **REVIEW,
                    evidence=[{"kind": "url", "ref": "https://example.com/ci/41", "caption": "CI"}],
                    judgment_calls=[{"text": "높이는 1.5배로 했다", "why": "기획서에 없다"}])
    assert out["ok"] and out["end_turn"] and set(out) == {"ok", "end_turn", "message", "inbox_id"}
    item = w.store.get_inbox_item(out["inbox_id"])
    assert (item["kind"], item["status"], item["task_id"], item["run_id"], item["requester_id"]) == (
        "final_review", "pending", task["id"], task["run_id"], task["assignee_id"])
    assert item["payload"]["summary"] == REVIEW["summary"] and item["payload"]["checklist"] == CHECKLIST
    assert item["payload"]["exchange_count"] == 0 and item["payload"]["evidence"][0]["ref"].startswith("https://")
    assert w.store.get_task(task["id"])["status"] == "review"
    again = failed(desk.call("task_request_review", token, **REVIEW), "illegal_transition", end_turn=True)
    assert (again["from"], again["to"]) == ("review", "review")
    asked = failed(desk.call("ask_owner", token, **QUESTION), "illegal_transition", end_turn=True)
    assert (asked["from"], asked["to"]) == ("review", "blocked")
    assert failed(desk.call("ask_owner", token), "illegal_transition", end_turn=True)["from"] == "review"


def test_task_request_review_refusals_leave_the_task_working(desk):
    w = desk.w
    task = desk.working()
    token = desk.token(task)
    desk.join(task, w.mate)
    failed(desk.call("task_request_review", desk.turn(w.mate, task=task), **REVIEW), "actor_not_allowed")
    failed(desk.call("task_request_review", desk.turn(task["assignee_id"], task=task), **REVIEW), "actor_not_allowed")
    failed(desk.call("task_request_review", desk.turn(w.mate), **REVIEW), "no_task_context")
    assert failed(desk.call("task_request_review", token), "missing_fields")["fields"] == ["summary", "checklist"]
    wrong = [{"criterion": "테스트", "status": "maybe"}]
    assert failed(desk.call("task_request_review", token, summary="끝", checklist=wrong),
                  "missing_fields")["fields"] == ["checklist"]
    assert failed(desk.call("task_request_review", token, **REVIEW, evidence=[{"kind": "url"}]),
                  "missing_fields")["fields"] == ["evidence"]
    shot = [{"kind": "screenshot", "ref": "shots/jump.png"}]  # a file: checked (§6.6); this run has no folder for it
    assert failed(desk.call("task_request_review", token, **REVIEW, evidence=shot),
                  "precondition_failed")["failed"] == ["attachment_outside_workspace"]
    w.fx.fail[("check", "T6")] = TransitionError("precondition_failed", "완료 기준마다 표기가 없다",
                                                 failed=["checklist_incomplete"])  # the harness gate (S-11)
    assert failed(desk.call("task_request_review", token, **REVIEW), "precondition_failed")["failed"] == [
        "checklist_incomplete"]
    assert desk.untouched(task)
    w.fx.fail.clear()
    assert desk.call("task_request_review", token, **REVIEW)["end_turn"]


# --- ask_owner (T4, §5.7) ---------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", list(ASKS))
def test_ask_owner_files_the_blocking_item_and_ends_the_turn(desk, kind):
    w = desk.w
    args, filed = ASKS[kind]
    task = desk.working()
    token = desk.token(task)
    out = desk.call("ask_owner", token, kind=kind, title="점프를 어떻게 할까?", **args)
    assert out["ok"] and out["end_turn"] and set(out) == {"ok", "end_turn", "message", "inbox_id"}
    item = w.store.get_inbox_item(out["inbox_id"])
    assert (item["kind"], item["status"], item["title"], item["task_id"], item["run_id"], item["requester_id"]) == (
        filed, "pending", "점프를 어떻게 할까?", task["id"], task["run_id"], task["assignee_id"])
    payload = item["payload"]
    if kind == "question":
        assert payload == {"reason": "asked", **args}
    elif kind == "choice":
        assert payload["question"] == args["body"] and "body" not in payload
        assert [option["figure"] for option in payload["options"]] == [None, {"mermaid": "graph TD; A-->B"}]
        assert (payload["recommended"], payload["fix_points"]) == ("a", "입력 처리")
    else:
        assert payload == args
    assert w.store.get_task(task["id"])["status"] == "blocked"
    again = failed(desk.call("ask_owner", token, kind=kind, title="또 묻는다", **args), "illegal_transition", end_turn=True)
    assert (again["from"], again["to"]) == ("blocked", "blocked")
    review = failed(desk.call("task_request_review", token, **REVIEW), "illegal_transition", end_turn=True)
    assert (review["from"], review["to"]) == ("blocked", "review")
    assert len(w.store.list_inbox(task_id=task["id"])) == 1


def test_ask_owner_is_for_the_members_of_the_task_room(desk):
    w = desk.w
    task = desk.working()
    failed(desk.call("ask_owner", desk.turn(w.mate), **QUESTION), "no_task_context")  # a chat turn: ask in the room
    reader = desk.turn(w.mate, task=task)  # a token of that room; the DB says who is a member
    failed(desk.call("ask_owner", reader, **QUESTION), "actor_not_allowed")
    assert desk.untouched(task)
    desk.join(task, w.mate)
    out = desk.call("ask_owner", reader, **QUESTION)  # §5.7: a member who is not the assignee
    item = w.store.get_inbox_item(out["inbox_id"])
    assert out["end_turn"] and (item["requester_id"], item["run_id"]) == (w.mate, task["run_id"])
    assert w.store.get_task(task["id"])["status"] == "blocked"


def test_ask_owner_says_which_argument_is_missing(desk):
    task = desk.working()
    token = desk.token(task)

    def fields(**args):
        return failed(desk.call("ask_owner", token, **args), "missing_fields")["fields"]

    assert fields() == ["kind", "title"] and fields(kind="poll", title="?") == ["kind"]
    assert fields(kind="question", title="?") == ["body"]
    assert fields(kind="question", title="?", body="?", options=[{"id": "a"}]) == ["options"]
    assert fields(kind="choice", title="?", options=[]) == ["body", "options"]
    assert fields(kind="choice", title="?", body="?", options=OPTIONS, recommended="z") == ["recommended"]
    drawn = [OPTIONS[0], {**OPTIONS[1], "figure": {"svg": "<svg/>"}}]
    assert fields(kind="choice", title="?", body="?", options=drawn) == ["options"]
    assert fields(kind="approval", title="?", what="x", why="y") == ["if_not"]
    filed = [OPTIONS[0], {**OPTIONS[1], "figure": {"path": "figs/a.png"}}]  # a file: checked (§6.6), none is there
    assert failed(desk.call("ask_owner", token, kind="choice", title="?", body="?", options=filed),
                  "precondition_failed")["failed"] == ["attachment_outside_workspace"]
    assert desk.untouched(task)
