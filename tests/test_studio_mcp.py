"""Studio MCP (S-6, 2/2): the five tools that are no task's behind `POST /studio/mcp/<tool>`, the process a
run talks through (`mcp_servers/studio.py`, with a fake HTTP) and the engine's env callback (§8.1–§8.5,
§6.6 '직원의 첨부', §11.2 · §11.4). No run and no AI is started."""
import asyncio
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from relay_agent import pipeline
from relay_agent.config import Config
from relay_agent.projctx import discover
from relay_agent.studio import api, drafts, files, runs, sat, tools
from test_mcp_attach import RELAY, setup as engine_setup
from test_studio_api_tasks import DOC, world  # noqa: F401 - `world` is that file's fixture, `desk` stands on it
from test_studio_tools import desk, failed  # noqa: F401 - `desk` is that file's fixture, used here too

ROOT = Path(__file__).resolve().parent.parent
YAML = "version: 1\ndepartment: program\n"
GUIDE = "# 지침\n\n## 결재 기준\n\n- 묻고 한다: 구조를 바꾸는 선택\n- 알아서 한다: 없음\n"
NEWER = "# 지침\n먼저 묻는다\n\n## 결재 기준\n\n- 묻고 한다: 구조를 바꾸는 선택\n- 알아서 한다: 없음\n"
DRAFT = dict(name="하늘", department_id="program", persona_md="# 하늘\n", guide_md=GUIDE, employee_yaml=YAML)
GONE = "2026-10-11T09:00:00"


def tools_of_8_4():
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("### 8.4 도구"):text.index("### 8.5 도구별 권한")]
    return set(re.findall(r"^\| `(\w+)` \|", section, re.M))


@pytest.fixture
def process(monkeypatch):
    """mcp_servers/studio.py loaded as a run would start it: its address and token come from the env."""
    monkeypatch.setenv("STUDIO_URL", "http://studio.test/")
    monkeypatch.setenv("STUDIO_ACTOR_TOKEN", "sat1.claims.signature")
    spec = importlib.util.spec_from_file_location("studio_mcp_process", ROOT / "mcp_servers" / "studio.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def studio_dir(tmp_path):
    return tmp_path / "runs" / "studio"  # where `world` puts cfg.runs_dir


def hr_turn(desk):  # noqa: F811
    hr = desk.store.create_employee("인사", "hr")["id"]
    return hr, desk.turn(hr)


def room_of(desk, kind, **fields):  # noqa: F811
    found = desk.store.list_rooms(kind=kind, **fields)
    return found[0] if found else desk.store.create_room(kind, title=kind, **fields)


# --- the fourteen tools (§8.4; the hiring pair of T5 made twelve fourteen) ---------------------------------------
def test_the_fourteen_tools_are_the_ones_of_8_4(process):
    twelve = tools_of_8_4()
    listed = asyncio.run(process.mcp.list_tools())
    assert len(twelve) == 16 and {tool.name for tool in listed} == twelve == set(tools.TOOLS)
    schema = api.openapi_schema()
    assert {path.rsplit("/", 1)[1] for path in schema["paths"] if path.startswith("/studio/mcp/")} == twelve
    models = schema["components"]["schemas"]
    for tool in listed:  # the process sends what the server's model of that tool takes, and nothing else
        body = schema["paths"][f"/studio/mcp/{tool.name}"]["post"]["requestBody"]["content"]["application/json"]
        model = models[body["schema"]["$ref"].rsplit("/", 1)[1]]
        assert set(tool.inputSchema.get("properties", {})) == set(model.get("properties", {})), tool.name
        assert "required" not in model, tool.name  # a missing argument is a result, not a 422
    assert process.END_TURN == tools.END_TURN  # the sentence is written twice: the process imports no studio code
    assert api.openapi_is_current()


# --- the process: HTTP into the result of §8.3 ------------------------------------------------------------------
def test_the_process_turns_http_errors_into_results(process, monkeypatch):
    sent = []

    def answer(status, body=None, text=None):
        def post(url, *, json, headers, timeout):  # noqa: A002 - httpx's own argument name
            sent.append((url, json, headers))
            request = httpx.Request("POST", url)
            if text is not None:
                return httpx.Response(status, text=text, request=request)
            return httpx.Response(status, json=body, request=request)
        monkeypatch.setattr(process.httpx, "post", post)

    refusal = {"ok": False, "code": "missing_fields", "message": "note 가 비었다", "end_turn": False, "fields": ["note"]}
    answer(200, refusal)
    assert process.task_progress(note="") == refusal  # a business error is the server's result, passed on whole
    assert sent == [("http://studio.test/studio/mcp/task_progress", {"note": "", "percent": None},
                     {"Authorization": "Bearer sat1.claims.signature"})]

    def ended(out, code):
        assert (out["ok"], out["code"], out["end_turn"]) == (False, code, True), out
        assert out["message"].endswith(tools.END_TURN) and set(out) == {"ok", "code", "message", "end_turn"}

    answer(401, {"detail": "행위자 토큰이 무효입니다", "code": "actor_invalid"})
    ended(process.task_get(), "actor_invalid")
    for status in (403, 404, 409, 500, 503):  # anything but 401 and 422 (§8.3)
        answer(status, {"detail": "x", "code": "unavailable"})
        ended(process.task_list(), "server_unreachable")

    def mistaken(out, *said):  # a 422: the turn goes on — the employee corrects the arguments and calls again
        assert (out["ok"], out["code"], out["end_turn"]) == (False, "invalid_arguments", False), out
        assert set(out) == {"ok", "code", "message", "end_turn"} and tools.END_TURN not in out["message"]
        assert all(part in out["message"] for part in said), out

    answer(422, {"detail": [  # FastAPI's own form (§6.3)
        {"type": "int_parsing", "loc": ["body", "limit"], "msg": "Input should be a valid integer", "input": "many"},
        {"type": "missing", "loc": ["body", "attachments", 0, "path"], "msg": "Field required"}]})
    mistaken(process.task_list(), "limit: Input should be a valid integer", "attachments.0.path: Field required")
    answer(422, {"detail": "limit 은 숫자다"})
    mistaken(process.task_list(), "limit 은 숫자다")
    for unread in ({"detail": []}, {"code": "x"}, ["not", "a", "detail"]):  # still a 422: the same code
        answer(422, unread)
        mistaken(process.task_list(), "HTTP 422")
    answer(422, text="<html>proxy</html>")
    mistaken(process.task_list(), "HTTP 422")
    answer(200, text="<html>proxy</html>")
    ended(process.employee_list(), "server_unreachable")
    answer(200, ["not", "a", "result"])
    ended(process.employee_list(), "server_unreachable")
    for error in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow")):
        def down(url, error=error, **kwargs):
            raise error
        monkeypatch.setattr(process.httpx, "post", down)
        ended(process.room_post(text="안녕"), "server_unreachable")


def test_a_call_through_the_process_is_the_employee_of_the_token(process, desk, monkeypatch):  # noqa: F811
    task = desk.working()
    monkeypatch.setattr(process.httpx, "post", lambda url, *, json, headers, timeout: desk.http.post(  # noqa: A002
        url.removeprefix(process.BASE), json=json, headers=headers))
    monkeypatch.setattr(process, "TOKEN", desk.token(task))
    assert process.task_get()["task"]["id"] == task["id"]
    out = process.room_post(text="빌드가 끝났다")
    assert out["ok"] and out["queued"] == []
    assert desk.store.get_message(out["message_id"])["author_id"] == task["assignee_id"]
    assert process.employee_draft(**DRAFT)["code"] == "actor_not_allowed"  # the server's refusal, as it is
    for tool, args, name in (("task_get", {"employee_id": desk.w.mate}, "employee_id"),  # the server's own 422
                             ("task_list", {"limit": "many"}, "limit")):
        out = process._call(tool, **args)
        assert (out["ok"], out["code"], out["end_turn"]) == (False, "invalid_arguments", False), out
        assert name in out["message"] and tools.END_TURN not in out["message"], out
    assert process.task_get()["ok"]  # corrected, the next call goes through
    monkeypatch.setattr(process, "TOKEN", "sat1.forged.token")
    assert (process.task_get()["code"], process.task_get()["end_turn"]) == ("actor_invalid", True)


# --- the engine: the env callback (§8.2 '주입', §11.2 · §11.4) ----------------------------------------------------
def test_the_engine_attaches_studio_only_to_the_runs_the_callback_knows(tmp_path, monkeypatch):
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    entry = {"command": "py", "args": ["studio.py"]}
    engine.mcp_registry["studio"] = dict(entry)
    engine.advance(engine.create(relay, "g", repo="game").id)  # no callback: as it was
    assert runner.seen[0].mcp_servers == ["proj"] and "studio" not in runner.seen[0].mcp_overrides

    ledger, asked = {}, []
    engine.mcp_env["studio"] = lambda run_id, stage: asked.append((run_id, stage)) or ledger.get(run_id)
    outside = engine.create(relay, "g", repo="game")  # a dashboard · CLI run
    engine.advance(outside.id)
    assert runner.seen[1].mcp_servers == ["proj"] and "studio" not in runner.seen[1].mcp_overrides

    inside = engine.create(relay, "g", repo="game")
    ledger[inside.id] = {"STUDIO_URL": "http://127.0.0.1:8020", "STUDIO_ACTOR_TOKEN": "sat1.a.b"}
    engine.advance(inside.id)
    assert runner.seen[2].mcp_servers == ["proj", "studio"]
    assert runner.seen[2].mcp_overrides["studio"] == {**entry, "env": ledger[inside.id]}
    assert engine.mcp_registry["studio"] == entry  # the token is the stage's: the registry keeps none
    assert asked == [(outside.id, "build"), (inside.id, "build")]

    listed = tmp_path / "listed.yaml"  # a stage that names the server itself
    listed.write_text(RELAY.replace("tools: [Read, Grep]", "tools: [Read, Grep], mcp: [studio]"), encoding="utf-8")
    engine.advance(engine.create(listed, "g", repo="game").id)
    assert runner.seen[3].mcp_servers == ["proj"]  # not in the ledger: not attached, whoever asks
    named = engine.create(listed, "g", repo="game")
    ledger[named.id] = ledger[inside.id]
    engine.advance(named.id)
    assert runner.seen[4].mcp_servers == ["studio", "proj"]

    engine.mcp_env["studio"] = lambda run_id, stage: 1 / 0  # a broken callback does not break the run
    broken = engine.create(relay, "g", repo="game")
    assert engine.advance(broken.id).status == "done" and runner.seen[5].mcp_servers == ["proj"]
    assert any(e["kind"] == "mcp_env_failed" and e["detail"]["server"] == "studio"
               for e in engine.history.events(broken.id))


def test_a_stage_without_tools_gets_studio_and_nothing_else(tmp_path, monkeypatch):
    engine, _, runner, _ = engine_setup(tmp_path, monkeypatch)
    entry = {"command": "py", "args": ["studio.py"]}
    engine.mcp_registry["studio"] = dict(entry)
    bare = tmp_path / "bare.yaml"  # no tools: the project's own servers are not attached to such a stage
    bare.write_text(RELAY.replace("tools: [Read, Grep]", "tools: []"), encoding="utf-8")
    ledger = {}
    engine.mcp_env["studio"] = lambda run_id, stage: ledger.get(run_id)
    engine.advance(engine.create(bare, "g", repo="game").id)  # a dashboard · CLI run
    assert runner.seen[0].mcp_servers == [] and "studio" not in runner.seen[0].mcp_overrides

    inside = engine.create(bare, "g", repo="game")
    ledger[inside.id] = {"STUDIO_URL": "http://127.0.0.1:8020", "STUDIO_ACTOR_TOKEN": "sat1.a.b"}
    engine.advance(inside.id)
    assert runner.seen[1].mcp_servers == ["studio"]  # what the callback gives is attached whatever the tools
    assert runner.seen[1].mcp_overrides["studio"] == {**entry, "env": ledger[inside.id]}


def test_a_run_that_picked_studio_keeps_its_servers_without_a_project_context(tmp_path, monkeypatch):
    """§9.5: the repo's `project_context: false`, or a folder with no marker file. What a run that picked studio
    picked from the registry is attached all the same; a run that did not pick it gets no MCP, as before."""
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch, {"project_context": False})
    entry, notes = {"command": "py", "args": ["studio.py"]}, {"command": "py", "args": ["notes.py"]}
    engine.mcp_registry.update(studio=dict(entry), notes=dict(notes))
    env = {"STUDIO_URL": "http://127.0.0.1:8020", "STUDIO_ACTOR_TOKEN": "sat1.a.b"}
    ledger = {}
    engine.mcp_env["studio"] = lambda run_id, stage: ledger.get(run_id)

    def turn(folder=None, inside=False, **picked):
        """One run to its end: (the run, the call its stage was made with, the kinds of its events)."""
        run = engine.create(relay, "g", folder, repo=None if folder else "game", **picked)
        if inside:
            ledger[run.id] = env
        assert engine.project_context(run) is None
        run = engine.advance(run.id)
        assert run.status == "done"
        return run, runner.seen[-1], [e["kind"] for e in engine.history.events(run.id)]

    for picked in ({}, {"mcp": []}, {"mcp": ["notes"]}):  # studio not picked — dashboard · CLI · campaign: as it was
        _, call, events = turn(**picked)
        assert call.mcp_servers == [] and "ToolSearch" not in call.tools, picked
        assert not {"studio", "notes"} & set(call.mcp_overrides) and "mcp_missing" not in events, picked
    _, call, _ = turn(inside=True)  # a run of the ledger that picked nothing: the callback alone gives it studio
    assert call.mcp_servers == ["studio"] and "ToolSearch" not in call.tools

    run, call, events = turn(inside=True, mcp=["notes", "studio"])  # an employee's turn or task run
    assert call.mcp_servers == run.mcp == ["notes", "studio"] and "ToolSearch" in call.tools
    assert call.mcp_overrides["studio"] == {**entry, "env": env} and call.mcp_overrides["notes"] == notes
    assert not call.project and "project_context" not in events and "mcp_missing" not in events
    assert engine.mcp_registry["studio"] == entry

    # the project's and the user's own servers are not looked for without a context: said, and not attached
    run, call, _ = turn(inside=True, mcp=["nope", "unreal", "studio"])
    assert call.mcp_servers == ["studio"]
    missing = {e["detail"]["server"] for e in engine.history.events(run.id) if e["kind"] == "mcp_missing"}
    assert missing == {"nope", "unreal"}
    _, call, _ = turn(inside=True, mcp=["notes", "studio"], mcp_only=["studio"])  # narrowed as with a context
    assert call.mcp_servers == ["studio"] and "notes" not in call.mcp_overrides
    _, call, _ = turn(mcp=["studio"])  # picked, but not a run of the ledger: no token, so no studio
    assert call.mcp_servers == []
    # picked with another, and not a run of the ledger: studio is left out, the other and ToolSearch stay (§9.5)
    _, call, events = turn(mcp=["notes", "studio"])
    assert call.mcp_servers == ["notes"] and "ToolSearch" in call.tools
    assert call.mcp_overrides["notes"] == notes and "env" not in call.mcp_overrides.get("studio", {})  # no token
    assert "mcp_missing" not in events

    nobody = tmp_path / "nobody"  # a PC whose user has no MCP server, and a folder with no marker file
    nobody.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr("relay_agent.projctx.Path.home", staticmethod(lambda: nobody))
    assert discover(plain) is None
    run, call, events = turn(plain, inside=True, mcp=["notes", "studio"])  # an employee placed nowhere
    assert call.mcp_servers == run.mcp == ["notes", "studio"] and "ToolSearch" in call.tools
    assert call.mcp_overrides["studio"] == {**entry, "env": env} and "project_context" not in events
    _, call, _ = turn(plain)
    assert call.mcp_servers == [] and "ToolSearch" not in call.tools


def test_a_runs_folder_is_its_workspace_or_its_workdir(tmp_path, monkeypatch):
    engine, relay, _, _ = engine_setup(tmp_path, monkeypatch)
    run = engine.create(relay, "g", repo="game")  # `workspace: none`: the stages work in the project folder
    assert runs.workdir(engine, run.id) == Path(run.workdir)
    assert runs.workdir(engine, "run_nobody_made") is None  # a run the engine does not have
    copy = tmp_path / "runs" / run.id / "workspace"
    monkeypatch.setattr(engine, "workspace", lambda state: SimpleNamespace(path=copy))  # a run with a workspace
    assert runs.workdir(engine, run.id) == copy
    monkeypatch.setattr(engine, "workspace", lambda state: 1 / 0)  # any error: no folder
    assert runs.workdir(engine, run.id) is None


def test_studios_callback_gives_a_token_to_the_runs_of_its_ledger(desk):  # noqa: F811
    import relay_agent.server as server

    task = desk.working()
    env = api.mcp_env(task["run_id"], "build")
    assert set(env) == {"STUDIO_URL", "STUDIO_ACTOR_TOKEN"} and env["STUDIO_URL"] == server.cfg.server_url
    claims = sat.verify(env["STUDIO_ACTOR_TOKEN"])
    assert (claims["run"], claims["emp"], claims["task"], claims["mode"]) == (
        task["run_id"], task["assignee_id"], task["id"], "work")
    desk.turn(desk.w.emp)  # a chat turn is in the ledger too
    assert sat.verify(api.mcp_env(desk.tracked[-1], "chat")["STUDIO_ACTOR_TOKEN"])["mode"] == "chat"
    assert api.mcp_env("run_of_the_dashboard", "build") is None

    assert server.engine.mcp_env["studio"] == api.mcp_env  # the three connection points in the old code
    assert "studio" in pipeline.READ_ONLY_MCP
    registered = Config().mcp_registry["studio"]
    assert Path(registered["args"][0]) == ROOT / "mcp_servers" / "studio.py" and "env" not in registered


# --- room_post (§8.4 · §8.5) ------------------------------------------------------------------------------------
SPEC = {"format": "spec_table", "source": "Docs/combat.md",
        "rows": [{"item": "기본 공격 피해", "value": "10", "done": "허수아비 HP 가 10 줄어든다", "note": ""}]}
ITEMS = {"format": "data_items", "items": [{"asset": "/Game/Data/DT_Weapon", "field": "Damage", "type": "float"}]}
BAD_HANDOFFS = {  # name: (handoff, the fields[] it gets, a word of the reason)
    "unknown_format": ({"format": "v1", "rows": SPEC["rows"]}, ["format"], "v1"),
    "no_format": ({"rows": SPEC["rows"]}, ["format"], "format"),
    "empty_object": ({}, ["format"], "format"),
    "format_is_a_list": ({"format": ["spec_table"], "rows": SPEC["rows"]}, ["format"], "format"),
    "format_is_an_object": ({"format": {}, "rows": SPEC["rows"]}, ["format"], "format"),
    "missing_done": ({"format": "spec_table", "rows": [{"item": "a", "value": "1"}]}, ["rows[0].done"], "done"),
    "blank_value": ({"format": "spec_table", "rows": [{"item": "a", "value": "  ", "done": "x"}]}, ["rows[0].value"],
                    "value"),
    "empty_table": ({"format": "spec_table", "rows": []}, ["rows"], "비어"),
    "no_table": ({"format": "data_items"}, ["items"], "items"),
    "wrong_table": ({"format": "data_items", "rows": SPEC["rows"]}, ["handoff", "items"], "rows"),
    "unknown_key": ({"format": "spec_table", "rows": SPEC["rows"], "to": "qa"}, ["handoff"], "to"),
    "unknown_column": ({"format": "data_items", "items": [{"asset": "a", "field": "b", "type": "c", "x": "1"}]},
                       ["items[0]"], "x"),
}


@pytest.mark.parametrize("name", list(BAD_HANDOFFS))
def test_room_post_refuses_a_handoff_that_is_no_format_and_writes_nothing(desk, name):  # noqa: F811
    task = desk.working()
    token, room = desk.token(task), desk.room(task)
    handoff, fields, word = BAD_HANDOFFS[name]
    count, events = len(desk.store.list_messages(room["id"])), len(desk.store.list_stream_events())
    out = failed(desk.call("room_post", token, text="넘긴다", handoff=handoff), "invalid_format")
    assert out["fields"] == fields and word in out["message"] and ("spec_table" in out["message"] or "data_items" in out["message"])
    assert len(desk.store.list_messages(room["id"])) == count and len(desk.store.list_stream_events()) == events
    assert desk.untouched(task)


def test_room_post_checks_the_handoff_before_the_mention_does_anything(desk):  # noqa: F811
    task = desk.working()
    token, room = desk.token(task), desk.room(task)
    desk.join(task, desk.w.mate)
    count, events = len(desk.store.list_messages(room["id"])), len(desk.store.list_stream_events())
    out = failed(desk.call("room_post", token, text="봐 달라", mentions=[desk.w.mate],
                           handoff={"format": "spec_table", "rows": []}), "invalid_format")
    assert out["fields"] == ["rows"]
    assert desk.store.get_task(task["id"])["exchange_count"] == task["exchange_count"] and desk.untouched(task)
    assert len(desk.store.list_messages(room["id"])) == count and len(desk.store.list_stream_events()) == events


def test_room_post_stores_a_good_handoff_of_either_format_as_it_came(desk):  # noqa: F811
    task = desk.working()
    token = desk.token(task)
    for handoff in (ITEMS, SPEC, {**ITEMS, "source": "Docs/x.md"}):
        out = desk.call("room_post", token, text="넘긴다", handoff=handoff)
        assert out["ok"] and desk.store.get_message(out["message_id"])["meta"] == {"handoff": handoff}
    out = desk.call("room_post", token, text="인계 아님")  # no handoff: no meta
    assert desk.store.get_message(out["message_id"])["meta"] == {}

def test_room_post_writes_as_the_employee_and_announces_it(desk):  # noqa: F811
    task = desk.working()
    token, room = desk.token(task), desk.room(task)
    before = len(desk.store.list_stream_events())
    out = desk.call("room_post", token, text="빌드가 끝났다", handoff=SPEC)
    message = desk.store.get_message(out["message_id"])
    assert out["queued"] == [] and not out["end_turn"]
    assert (message["room_id"], message["author_kind"], message["author_id"], message["body"], message["run_id"]) == (
        room["id"], "employee", task["assignee_id"], "빌드가 끝났다", task["run_id"])
    assert message["meta"] == {"handoff": SPEC} and message["attachments"] == []
    events = desk.store.list_stream_events()[before:]
    assert [(e["type"], e["room_id"], e["task_id"]) for e in events] == [("message.created", room["id"], task["id"])]
    assert desk.store.get_task(task["id"])["exchange_count"] == task["exchange_count"]  # S-9 counts, not S-6

    count = len(desk.store.list_messages(room["id"]))
    assert failed(desk.call("room_post", token, text="  "), "missing_fields")["fields"] == ["text"]
    assert failed(desk.call("room_post", token, text="x", attachments=[{"name": "a.png"}]),
                  "missing_fields")["fields"] == ["attachments"]
    failed(desk.call("room_post", token, text="x", room="room_nowhere"), "not_found")
    assert failed(desk.call("room_post", token, text="x", mentions=[task["assignee_id"]]),
                  "precondition_failed")["failed"] == []  # oneself
    failed(desk.call("room_post", token, text="x", mentions=[desk.w.mate]), "precondition_failed")  # not a member
    assert len(desk.store.list_messages(room["id"])) == count

    desk.join(task, desk.w.mate)
    out = desk.call("room_post", token, text="봐 달라", mentions=[desk.w.mate, desk.w.mate])
    assert desk.store.get_message(out["message_id"])["mentions"] == [desk.w.mate] and out["queued"] == []


def test_room_post_is_for_the_rooms_of_8_5(desk):  # noqa: F811
    w, store = desk.w, desk.store
    task = desk.working()
    room, assignee = desk.room(task), desk.token(task)
    mate = desk.turn(w.mate)  # a chat turn in mate's own 1:1 room
    hr, hr_token = hr_turn(desk)
    failed(desk.call("room_post", mate, text="끼어든다", room=room["id"]), "actor_not_allowed")  # task: members
    failed(desk.call("room_post", hr_token, text="인사다", room=room["id"]), "actor_not_allowed")
    desk.join(task, w.mate)
    assert desk.call("room_post", mate, text="이제 구성원이다", room=room["id"])["ok"]
    store.update_room_member(room["id"], w.mate, left_at=GONE)
    failed(desk.call("room_post", mate, text="나간 뒤", room=room["id"]), "actor_not_allowed")

    assert desk.call("room_post", mate, text="내 방")["ok"]  # dm: one's own (the room of this turn)
    failed(desk.call("room_post", assignee, text="남의 방", room=desk.dms[w.mate]), "actor_not_allowed")

    program = room_of(desk, "department", department_id="program")  # department: its members, as a group room
    failed(desk.call("room_post", mate, text="행이 없다", room=program["id"]), "actor_not_allowed")
    if store.get_room_member(program["id"], w.mate) is None:
        store.create_room_member(program["id"], w.mate)
    assert desk.call("room_post", mate, text="파트에 알린다", room=program["id"])["ok"]
    failed(desk.call("room_post", hr_token, text="인사다", room=program["id"]), "actor_not_allowed")

    system = room_of(desk, "system")  # system: HR alone
    assert desk.call("room_post", hr_token, text="일일 보고", room=system["id"])["ok"]
    failed(desk.call("room_post", mate, text="공지", room=system["id"]), "actor_not_allowed")

    store.update_room(desk.dms[w.mate], archived_at=GONE)
    assert failed(desk.call("room_post", mate, text="닫힌 방"), "precondition_failed")["failed"] == []


def test_room_post_copies_files_from_the_runs_folder_and_the_project_only(desk, tmp_path, studio_dir,  # noqa: F811
                                                                           monkeypatch):
    work, project, elsewhere = tmp_path / "work", tmp_path / "game", tmp_path / "elsewhere"
    for folder in (work, project, elsewhere):
        folder.mkdir()
    (work / "shot.png").write_bytes(b"png")
    (project / "log.txt").write_bytes(b"editor log")
    (elsewhere / "secret.txt").write_bytes(b"secret")
    monkeypatch.setattr(api, "_workdir", lambda run_id: work)
    with desk.store.transaction() as tx:
        tx.exec("UPDATE projects SET path = ? WHERE id = ?", (str(project), desk.w.pid))
    task = desk.working()
    token, room = desk.token(task), desk.room(task)

    out = desk.call("room_post", token, text="", attachments=[
        {"path": "shot.png"}, {"path": str(project / "log.txt"), "name": "shot.png"}])
    message = desk.store.get_message(out["message_id"])
    folder = studio_dir / "attachments" / str(message["id"])
    assert message["attachments"] == [
        {"name": "shot.png", "path": f"{message['id']}/shot.png", "mime": files.mime("shot.png"), "size": 3},
        {"name": "shot-2.png", "path": f"{message['id']}/shot-2.png", "mime": files.mime("shot.png"), "size": 10}]
    assert (folder / "shot.png").read_bytes() == b"png" and (folder / "shot-2.png").read_bytes() == b"editor log"
    assert (work / "shot.png").exists() and (project / "log.txt").exists()  # copied, not moved
    assert desk.w.owner.get(f"/studio/attachments/{message['id']}/shot.png").content == b"png"

    count, folders = len(desk.store.list_messages(room["id"])), sorted((studio_dir / "attachments").iterdir())
    for spec, code in (({"path": "missing.png"}, "attachment_not_found"),
                       ({"path": str(work)}, "attachment_not_found"),  # a folder is not a file
                       ({"path": str(elsewhere / "secret.txt")}, "attachment_outside_workspace"),
                       ({"path": str(elsewhere / "nothing.txt")}, "attachment_outside_workspace"),  # not told apart
                       ({"path": "../elsewhere/secret.txt"}, "attachment_outside_workspace")):
        out = failed(desk.call("room_post", token, text="본다", attachments=[{"path": "shot.png"}, spec]),
                     "precondition_failed")
        assert out["failed"] == [code], spec
    monkeypatch.setattr(files, "MAX_ATTACHMENT_MB", 0)
    assert failed(desk.call("room_post", token, text="본다", attachments=[{"path": "shot.png"}]),
                  "precondition_failed")["failed"] == ["attachment_too_large"]
    monkeypatch.setattr(files, "MAX_ATTACHMENT_MB", 25)
    monkeypatch.setattr(api, "_workdir", lambda run_id: None)  # the engine has no folder for that run
    assert failed(desk.call("room_post", token, text="본다", attachments=[{"path": "shot.png"}]),
                  "precondition_failed")["failed"] == ["attachment_outside_workspace"]
    assert desk.call("room_post", token, text="본다", attachments=[{"path": str(project / "log.txt")}])["ok"]
    assert len(desk.store.list_messages(room["id"])) == count + 1  # nothing was written by the refused ones
    assert len(list((studio_dir / "attachments").iterdir())) == len(folders) + 1


# --- employee_list ------------------------------------------------------------------------------------------
def test_employee_list_shows_the_callers_project_and_hr_everyone(desk):  # noqa: F811
    w, store = desk.w, desk.store
    far, gone = w.hire(w.elsewhere), w.hire(w.pid)
    desk.set_employee(gone, retired_at=GONE)
    loose = store.create_employee("대기", "program")["id"]  # placed nowhere
    hr, hr_token = hr_turn(desk)
    mine = desk.call("employee_list", desk.turn(w.emp))["items"]
    assert {e["id"] for e in mine} == {w.emp, w.mate}
    assert set(mine[0]) == {"id", "name", "department_id", "project_id", "status"}
    assert desk.call("employee_list", desk.turn(w.emp), project_id=w.elsewhere)["items"] == []
    assert desk.call("employee_list", desk.turn(w.emp), department_id="hr")["items"] == []
    assert desk.call("employee_list", desk.turn(loose))["items"] == []
    assert {e["id"] for e in desk.call("employee_list", hr_token)["items"]} == {w.emp, w.mate, far, loose, hr}
    assert [e["id"] for e in desk.call("employee_list", hr_token, project_id=w.elsewhere)["items"]] == [far]
    assert [e["id"] for e in desk.call("employee_list", hr_token, department_id="hr")["items"]] == [hr]


# --- HR's three (§8.4 인사 전용) ---------------------------------------------------------------------------------
def test_the_hr_tools_refuse_an_employee_who_is_not_hr(desk, studio_dir):  # noqa: F811
    hr, hr_token = hr_turn(desk)
    made = desk.call("employee_draft", hr_token, **DRAFT)["draft_id"]
    for token in (desk.turn(desk.w.emp), desk.token(desk.working())):  # a chat turn and a task run
        for tool, args in (("employee_draft", DRAFT), ("employee_draft", dict(draft_id=made, name="바다")),
                           ("employee_submit", dict(draft_id=made, reason="사람이 모자라다")),
                           ("policy_draft", dict(project_id=desk.w.pid, policy_md="# 정책\n", reason="r", submit=True))):
            failed(desk.call(tool, token, **args), "actor_not_allowed")
    assert not desk.store.list_inbox(kind="hire")
    assert [path.name for path in (studio_dir / "drafts").iterdir()] == [f"{made}.json"]
    assert drafts.load(studio_dir, made)["draft"]["name"] == "하늘"  # nobody else changed it


def test_employee_draft_keeps_its_problems_and_submit_refuses_them(desk, studio_dir):  # noqa: F811
    hr, token = hr_turn(desk)
    assert failed(desk.call("employee_draft", token, name="하늘"), "missing_fields")["fields"] == [
        "department_id", "persona_md", "guide_md", "employee_yaml"]
    assert not (studio_dir / "drafts").exists()
    out = desk.call("employee_draft", token, **{**DRAFT, "name": "민수", "department_id": "nowhere",
                                                 "project_id": "prj_none", "employee_yaml": "- a\n- b\n"})
    codes = ["name_taken", "department_not_found", "project_not_found", "yaml_invalid"]
    assert out["action"] == "hire" and [p["code"] for p in out["problems"]] == codes
    assert all(p["message"] for p in out["problems"]) and not out["end_turn"]
    draft_id = out["draft_id"]
    saved = json.loads((studio_dir / "drafts" / f"{draft_id}.json").read_text(encoding="utf-8"))
    assert drafts.DRAFT_ID.fullmatch(draft_id) and (saved["kind"], saved["action"], saved["author_id"]) == (
        "employee", "hire", hr)
    assert failed(desk.call("employee_submit", token, draft_id=draft_id, reason="사람이 모자라다"),
                  "precondition_failed")["failed"] == codes
    assert [p["code"] for p in desk.call("employee_draft", token, **{**DRAFT, "department_id": "hr",
                                                                    "project_id": desk.w.pid})["problems"]] == [
        "hr_exists", "hr_has_project", "department_mismatch"]  # the yaml of DRAFT names another part (§9.4 의 4)
    assert [p["code"] for p in desk.call("employee_draft", token, **{
        **DRAFT, "name": "바다", "employee_yaml": "version: 2\ndepartment: program\nrelay: nowhere\n"})["problems"]] == [
        "version_unsupported", "relay_unknown"]
    assert not desk.store.list_inbox(kind="hire")

    fixed = desk.call("employee_draft", token, draft_id=draft_id, name="하늘", department_id="program",
                      project_id=desk.w.pid, employee_yaml=YAML)  # the other arguments keep what was stored
    assert (fixed["draft_id"], fixed["problems"]) == (draft_id, [])
    assert drafts.load(studio_dir, draft_id)["created_at"] == saved["created_at"]
    out = desk.call("employee_submit", token, draft_id=draft_id, reason="사람이 모자라다", source_inbox_id="inb_0badc0de")
    item = desk.store.get_inbox_item(out["inbox_id"])
    assert (item["kind"], item["status"], item["requester_id"], item["project_id"]) == (
        "hire", "pending", hr, desk.w.pid)
    payload = item["payload"]
    assert (payload["action"], payload["draft_id"], payload["reason"], payload["source_inbox_id"]) == (
        "hire", draft_id, "사람이 모자라다", "inb_0badc0de")
    assert payload["draft"] == {**DRAFT, "project_id": desk.w.pid, "first_message": "", "part_name": "", "part_job": "",
                                "part_relay": "", "part_models": ""}

    again = desk.call("employee_submit", token, draft_id=draft_id, reason="다시 올린다")  # replaces the pending one
    assert desk.store.get_inbox_item(out["inbox_id"])["status"] == "cancelled"
    assert desk.store.get_inbox_item(again["inbox_id"])["payload"]["supersedes_inbox_id"] == out["inbox_id"]
    assert len(desk.store.list_inbox(kind="hire", status="pending")) == 1

    for unknown in ("drf_00000000", "drf_0000000z", "../studio", "inb_0badc0de"):
        failed(desk.call("employee_submit", token, draft_id=unknown, reason="r"), "not_found")
        failed(desk.call("employee_draft", token, draft_id=unknown, **DRAFT), "not_found")
    assert failed(desk.call("employee_submit", token, draft_id=draft_id), "missing_fields")["fields"] == ["reason"]


def test_an_amendment_is_drafted_against_the_file_of_now(desk, studio_dir):  # noqa: F811
    hr, token = hr_turn(desk)
    target = desk.w.emp
    guide = studio_dir / "employees" / target / "GUIDE.md"
    guide.parent.mkdir(parents=True)
    guide.write_bytes("# 지침\n묻지 않는다\n".encode("utf-8"))
    amend = dict(target_employee_id=target, file="GUIDE.md", guide_md=NEWER)
    failed(desk.call("employee_draft", token, **{**amend, "target_employee_id": "emp_nobody"}), "not_found")
    assert failed(desk.call("employee_draft", token, **{**amend, "file": "MEMORY.md"}),
                  "missing_fields")["fields"] == ["file"]
    assert failed(desk.call("employee_draft", token, target_employee_id=target, file="GUIDE.md", persona_md="x"),
                  "missing_fields")["fields"] == ["guide_md"]
    out = desk.call("employee_draft", token, **amend)
    assert (out["action"], out["problems"]) == ("guide_amend", [])
    saved = drafts.load(studio_dir, out["draft_id"])
    assert saved["base_sha256"] == hashlib.sha256(guide.read_bytes()).hexdigest()
    assert "-묻지 않는다" in saved["diff"] and "+먼저 묻는다" in saved["diff"]
    failed(desk.call("employee_draft", token, draft_id=out["draft_id"], **{**amend, "target_employee_id": desk.w.mate}),
           "precondition_failed")  # a draft does not become another employee's
    hire = desk.call("employee_draft", token, **DRAFT)["draft_id"]
    failed(desk.call("employee_draft", token, draft_id=hire, **amend), "precondition_failed")  # nor another kind

    guide.write_bytes(b"# changed by the owner\n")  # after the draft
    assert failed(desk.call("employee_submit", token, draft_id=out["draft_id"], reason="물어야 했다"),
                  "precondition_failed")["failed"] == ["file_changed"]
    assert desk.call("employee_draft", token, draft_id=out["draft_id"])["draft_id"] == out["draft_id"]  # same text
    item = desk.store.get_inbox_item(
        desk.call("employee_submit", token, draft_id=out["draft_id"], reason="물어야 했다")["inbox_id"])
    payload = item["payload"]
    assert (payload["action"], payload["target_employee_id"], payload["file"], payload["new_text"]) == (
        "guide_amend", target, "GUIDE.md", NEWER)
    assert payload["base_sha256"] == hashlib.sha256(b"# changed by the owner\n").hexdigest()
    assert item["project_id"] == desk.w.pid and "+먼저 묻는다" in payload["diff"]

    bad = desk.call("employee_draft", token, target_employee_id=target, file="employee.yaml", employee_yaml="a: [1")
    assert [p["code"] for p in bad["problems"]] == ["yaml_invalid"]
    assert failed(desk.call("employee_submit", token, draft_id=bad["draft_id"], reason="r"),
                  "precondition_failed")["failed"] == ["yaml_invalid"]
    moved = desk.call("employee_draft", token, target_employee_id=target, file="employee.yaml",
                      employee_yaml="version: 1\ndepartment: hr\n")  # the part does not change after the hire (§9.6)
    assert [p["code"] for p in moved["problems"]] == ["department_mismatch"]


def test_policy_draft_is_one_per_project_and_files_on_submit(desk, studio_dir):  # noqa: F811
    hr, token = hr_turn(desk)
    pid = desk.w.pid
    assert failed(desk.call("policy_draft", token, project_id=pid), "missing_fields")["fields"] == [
        "policy_md", "reason"]
    failed(desk.call("policy_draft", token, project_id="prj_none", policy_md="x", reason="r"), "not_found")
    first = desk.call("policy_draft", token, project_id=pid, policy_md="# 정책\n커밋 전에 묻는다\n", reason="되돌린 일이 있었다")
    assert first["inbox_id"] is None and "+커밋 전에 묻는다" in first["diff"] and not first["end_turn"]
    assert not desk.store.list_inbox(kind="hire")
    assert failed(desk.call("employee_submit", token, draft_id=first["draft_id"], reason="r"),
                  "precondition_failed")["failed"] == []  # a policy draft goes up through policy_draft
    second = desk.call("policy_draft", token, project_id=pid, policy_md="# 정책\n푸시 전에 묻는다\n", reason="되돌린 일이 있었다",
                       submit=True, source_inbox_id="inb_0badc0de")
    assert second["draft_id"] == first["draft_id"] and len(list((studio_dir / "drafts").iterdir())) == 1
    item = desk.store.get_inbox_item(second["inbox_id"])
    payload = item["payload"]
    assert (item["kind"], item["status"], item["requester_id"], item["project_id"]) == ("hire", "pending", hr, pid)
    assert (payload["action"], payload["draft_id"], payload["new_text"], payload["base_sha256"]) == (
        "policy", first["draft_id"], "# 정책\n푸시 전에 묻는다\n", "")  # no POLICY.md yet
    assert payload["source_inbox_id"] == "inb_0badc0de" and payload["diff"] == second["diff"]
    third = desk.call("policy_draft", token, project_id=pid, policy_md="# 정책\n다시\n", reason="고쳤다", submit=True)
    assert desk.store.get_inbox_item(second["inbox_id"])["status"] == "cancelled"
    assert desk.store.get_inbox_item(third["inbox_id"])["payload"]["supersedes_inbox_id"] == second["inbox_id"]
    other = desk.call("policy_draft", token, project_id=desk.w.elsewhere, policy_md="# 정책\n", reason="r")
    assert other["draft_id"] != first["draft_id"] and drafts.DRAFT_ID.fullmatch(other["draft_id"])
