"""T4: what a studio stage really got is recorded as an event and shown by `GET /studio/tasks/{id}`; a game
project without the editor's MCP server, and one whose editor does not answer, are told in different words."""
import json

from relay_agent.studio import api, notice, review
from test_mcp_attach import setup as engine_setup  # ~/.claude.json has `unreal`, the game's .mcp.json has `proj`
from test_studio_api_tasks import world  # noqa: F401 - `world` is its fixture, `desk` stands on it
from test_studio_tools import desk  # noqa: F401


def game_run(tmp_path, monkeypatch, *, unreal=True, scope="all"):
    engine, relay, runner, game = engine_setup(tmp_path, monkeypatch)
    (game / "Game.uproject").write_text("{}", encoding="utf-8")  # a game project (projctx.game_root)
    if not unreal:
        (tmp_path / "home" / ".claude.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    told = []
    engine.run_notice = lambda run_id, kind, detail: told.append((kind, detail))
    only = {"mcp_only": ["proj"]} if scope == "only" else {}
    return engine, relay, runner, told, engine.create(relay, "g", repo="game", mcp=[], mcp_scope=scope, **only)


def working_run(desk, engine, relay):  # noqa: F811
    task, run = desk.working(), engine.create(relay, "g", repo="game", mcp=[], mcp_scope="all")
    with desk.store.transaction() as tx:
        tx.exec("UPDATE tasks SET run_id = ? WHERE id = ?", (run.id, task["id"]))
    return task, run


def test_a_studio_stage_records_what_it_got_and_the_task_detail_shows_it(desk, tmp_path, monkeypatch):  # noqa: F811
    engine, relay, runner, game = engine_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    engine.guard_stage = api.guard_stage
    task, run = working_run(desk, engine, relay)
    engine.advance(run.id)
    events = [e for e in engine.history.events(run.id) if e["kind"] == "stage_capabilities"]
    assert events and events[0]["stage"] == runner.seen[0].stage
    detail, call = events[-1]["detail"], runner.seen[-1]
    assert detail["tools"] == list(call.tools) and "Bash" in detail["tools"] and "Skill" in detail["tools"]
    assert detail["allowed_tools"] == list(call.allowed_tools)
    servers = {(s["name"], s["source"], s["status"]) for s in detail["mcp"]}
    assert {("unreal", "user", "connected"), ("proj", "project", "connected")} <= servers
    assert isinstance(detail["skills"], list)
    got = review.capabilities(engine, run.id)
    assert got["stage"] == events[-1]["stage"] and got["tools"] == detail["tools"] and got["mcp"] == detail["mcp"]
    assert set(got) == {"stage", "tools", "allowed_tools", "mcp", "skills", "at"}
    monkeypatch.setattr(api, "_capabilities", lambda run_id: review.capabilities(engine, run_id))
    assert desk.w.owner.get(f"/studio/tasks/{task['id']}").json()["capabilities"] == got
    assert review.capabilities(engine, "run_none") is None and review.capabilities(engine, None) is None


def test_a_failed_server_is_in_the_capabilities_as_failed(desk, tmp_path, monkeypatch):  # noqa: F811
    engine, relay, runner, game = engine_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    engine.guard_stage = api.guard_stage
    engine.mcp_probe = lambda config: "에디터가 꺼져 있음" if config.get("command") == "unrealmcp" else None
    task, run = working_run(desk, engine, relay)
    engine.advance(run.id)
    mcp = {s["name"]: s for s in review.capabilities(engine, run.id)["mcp"]}
    assert mcp["unreal"]["status"] == "failed" and "꺼져" in mcp["unreal"]["error"]
    assert mcp["proj"]["status"] == "connected"


def test_a_run_outside_studio_records_no_capabilities(tmp_path, monkeypatch):
    engine, relay, runner, game = engine_setup(tmp_path, monkeypatch)
    run = engine.advance(engine.create(relay, "g", repo="game").id)
    assert run.status == "done" and review.capabilities(engine, run.id) is None
    assert not [e for e in engine.history.events(run.id) if e["kind"] == "stage_capabilities"]


def test_a_game_without_the_unreal_server_is_told_it_is_missing(tmp_path, monkeypatch):
    engine, relay, runner, told, run = game_run(tmp_path, monkeypatch, unreal=False)
    assert engine.advance(run.id).status == "done"
    assert ("unreal", {"state": "missing"}) in told and not [t for t in told if t[0] == "mcp"]


def test_a_game_whose_unreal_server_does_not_answer_is_told_it_is_down_not_missing(tmp_path, monkeypatch):
    engine, relay, runner, told, run = game_run(tmp_path, monkeypatch)
    engine.mcp_probe = lambda config: "에디터가 꺼져 있음" if config.get("command") == "unrealmcp" else None
    assert engine.advance(run.id).status == "done"
    assert told == [("unreal", {"state": "down", "name": "unreal", "why": "에디터가 꺼져 있음"})]  # once, no 'mcp' twin
    missing, down = (notice._text("unreal", d) for d in ({"state": "missing"}, {"state": "down", "why": "x"}))
    assert missing[0] == down[0] == notice.UNREAL_NOTICE_EVENT and missing[1] != down[1]
    assert "설정에 없" in missing[1] and "연결되지 않았" in down[1] and "x" in down[1]


def test_a_game_with_the_unreal_server_answering_is_told_nothing_of_it(tmp_path, monkeypatch):
    engine, relay, runner, told, run = game_run(tmp_path, monkeypatch)
    assert engine.advance(run.id).status == "done" and told == []


def test_a_run_narrowed_to_names_is_not_told_of_the_editor(tmp_path, monkeypatch):
    engine, relay, runner, told, run = game_run(tmp_path, monkeypatch, unreal=False, scope="only")
    engine.advance(run.id)
    assert not [t for t in told if t[0] == "unreal"]


def test_a_folder_that_is_no_game_is_not_told_of_the_editor(tmp_path, monkeypatch):
    (tmp_path / "other").mkdir()
    engine, relay, runner, game = engine_setup(tmp_path / "other", monkeypatch)  # no .uproject
    (tmp_path / "other" / "home" / ".claude.json").write_text("{}", encoding="utf-8")
    told = []
    engine.run_notice = lambda run_id, kind, detail: told.append(kind)
    engine.advance(engine.create(relay, "g", repo="game", mcp=[], mcp_scope="all").id)
    assert "unreal" not in told


def test_a_run_narrowed_to_names_is_told_a_failed_unreal_by_the_mcp_notice(tmp_path, monkeypatch):
    engine, relay, runner, told, _ = game_run(tmp_path, monkeypatch, scope="only")
    run = engine.create(relay, "g", repo="game", mcp=["unreal"], mcp_only=["unreal", "proj"], mcp_scope="only")
    engine.mcp_probe = lambda config: "에디터가 꺼져 있음" if config.get("command") == "unrealmcp" else None
    assert engine.advance(run.id).status == "done"
    assert ("mcp", {"failed": {"unreal": "에디터가 꺼져 있음"}}) in told and not [t for t in told if t[0] == "unreal"]


def test_the_continue_notice_is_worded():
    event, body = notice._text("continue", {})
    assert event == notice.CONTINUE_EVENT and "한 번 이어" in body
