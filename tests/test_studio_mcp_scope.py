"""T2: a studio run has every MCP server Claude Code has (no `--strict-mcp-config`), narrowed only by the
employee's own `mcp`; a server that does not answer is left out and told, not a failed run."""
import pytest

from relay_agent import mcpcheck
from relay_agent.projctx import CONNECTOR, mcp_sources
from relay_agent.runners import ClaudeCliRunner
from relay_agent.studio import api
from test_mcp_attach import setup as engine_setup  # ~/.claude.json has `unreal`, the game's .mcp.json has `proj`
from test_studio_api_tasks import world  # noqa: F401 - `world` is its fixture, `desk` stands on it
from test_studio_tools import desk  # noqa: F401


def allowed(args):
    return args[args.index("--allowedTools") + 1].split(",")


def plan(engine, run):
    return next(e["detail"] for e in engine.history.events(run.id) if e["kind"] == "mcp_plan")


def test_a_task_run_of_scope_all_has_the_users_and_the_projects_servers_and_no_strict_flag(
        desk, tmp_path, monkeypatch):  # noqa: F811
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    engine.guard_stage = api.guard_stage  # what server.py registers
    task, run = desk.working(), engine.create(relay, "g", repo="game", mcp=[], mcp_scope="all")
    with desk.store.transaction() as tx:
        tx.exec("UPDATE tasks SET run_id = ? WHERE id = ?", (run.id, task["id"]))
    engine.advance(run.id)
    call = runner.seen[-1]
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert "--strict-mcp-config" not in args and "--disallowedTools" not in args
    assert {"mcp__unreal", "mcp__proj"} <= set(allowed(args))  # pre-approved: a headless run cannot ask
    assert call.mcp_servers == [] and not call.isolate  # nothing goes into the config file: Claude Code loads them
    assert "unreal" in call.prompt and "proj" in call.prompt  # and the AI is told it has them
    servers = {(s["name"], s["source"]) for s in plan(engine, run)["servers"]}
    assert {("unreal", "user"), ("proj", "project"), (CONNECTOR, "connector")} <= servers  # recorded on the run


def test_a_scope_only_run_is_narrowed_to_the_names_and_stays_strict(tmp_path, monkeypatch):
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    run = engine.create(relay, "g", repo="game", mcp=["unreal"], mcp_only=["unreal"], mcp_scope="only")
    engine.advance(run.id)
    call = runner.seen[-1]
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert "--strict-mcp-config" in args and call.native_mcp is None
    assert call.mcp_servers == ["unreal"] and "mcp__proj" not in allowed(args)  # the project's `proj` is dropped
    assert [s["name"] for s in plan(engine, run)["servers"]] == ["unreal"]
    with pytest.raises(ValueError):
        engine.create(relay, "g", repo="game", mcp_scope="only")  # `only` without the names
    with pytest.raises(ValueError):
        engine.create(relay, "g", repo="game", mcp_scope="some")


def test_a_server_that_does_not_answer_is_left_out_and_told_and_the_run_goes_on(tmp_path, monkeypatch):
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    told = []
    engine.run_notice = lambda run_id, kind, detail: told.append((run_id, kind, detail))
    engine.mcp_probe = lambda config: "에디터가 꺼져 있음" if config.get("command") == "unrealmcp" else None
    run = engine.advance(engine.create(relay, "g", repo="game", mcp=[], mcp_scope="all").id)
    assert run.status == "done"  # not failed
    assert told == [(run.id, "mcp", {"failed": {"unreal": "에디터가 꺼져 있음"}})]
    call = runner.seen[-1]
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert "mcp__unreal" not in allowed(args) and "mcp__proj" in allowed(args)
    assert args[args.index("--disallowedTools") + 1] == "mcp__unreal"  # Claude Code would load it itself otherwise
    assert [s["name"] for s in plan(engine, run)["servers"] if s["source"] != "connector"] == ["proj"]
    assert mcpcheck.last()["unreal"]["ok"] is False and mcpcheck.last()["proj"]["ok"] is True


def test_a_run_outside_studio_still_fails_on_a_server_that_does_not_answer(tmp_path, monkeypatch):
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    engine.mcp_probe = lambda config: "down"
    run = engine.advance(engine.create(relay, "g", repo="game").id)  # no mcp_scope: as it was
    assert run.status == "failed" and "MCP 연결 안 됨" in run.error and not runner.seen


def test_the_sources_and_the_servers_endpoint(desk, tmp_path, monkeypatch):  # noqa: F811
    _, _, _, game = engine_setup(tmp_path, monkeypatch)
    plugin = tmp_path / "plug"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text('{"mcpServers": {"bridge": {"command": "b"}}}', encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        '{"version": 2, "plugins": {"ue@market": [{"installPath": %s}]}}' % __import__("json").dumps(str(plugin)),
        encoding="utf-8")
    found = {s["name"]: s for s in mcp_sources(None, {"docs_read": {"command": "d"}})}
    assert found["unreal"]["source"] == "user" and found["plugin_ue_bridge"]["source"] == "plugin"
    assert found["docs_read"]["source"] == "registry" and found[CONNECTOR]["config"] is None
    mcpcheck.record("unreal", "down")
    body = desk.w.owner.get("/studio/mcp-servers")  # not /studio/mcp/: that prefix takes the employees' tokens only
    assert body.status_code == 200, body.text
    by_name = {(item["name"], item["source"]): item for item in body.json()}
    assert by_name[("unreal", "user")]["last"]["ok"] is False and by_name[("unreal", "user")]["last"]["why"] == "down"
    assert ("plugin_ue_bridge", "plugin") in by_name and (CONNECTOR, "connector") in by_name
    assert by_name[(CONNECTOR, "connector")]["last"] is None


# --- T2.1: the user's `unreal` counts for copy vs in place on a scope 'all' run --------------------------------
def test_a_scope_all_run_with_the_users_unreal_is_not_made_in_a_copy(tmp_path, monkeypatch):
    engine, relay, _, _ = engine_setup(tmp_path, monkeypatch)
    run = engine.create(relay, "g", repo="game", workspace="copy", mcp=[], mcp_scope="all")
    assert run.workspace_mode == "inplace"
    forced = [e["detail"] for e in engine.history.events(run.id) if e["kind"] == "workspace_forced"]
    assert forced and "unreal" in forced[0]["reason"]


def test_a_pinned_copy_scope_all_run_with_the_users_unreal_is_refused(tmp_path, monkeypatch):
    engine, relay, _, _ = engine_setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="unreal"):
        engine.create(relay, "g", repo="game", workspace="copy", mcp=[], mcp_scope="all", pin_copy=True)


def test_a_scope_only_run_without_unreal_keeps_the_copy(tmp_path, monkeypatch):
    engine, relay, _, _ = engine_setup(tmp_path, monkeypatch)
    run = engine.create(relay, "g", repo="game", workspace="copy", mcp=["studio"], mcp_only=["studio"], mcp_scope="only")
    assert run.workspace_mode == "copy"


def test_the_native_live_server_is_the_editors_name():
    from relay_agent import pipeline
    from relay_agent.studio import plugins
    assert pipeline.NATIVE_LIVE_MCP == {plugins.DEFAULT_SERVER}


def test_uses_editor_counts_the_users_unreal_on_scope_all(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from relay_agent.studio import work
    engine_setup(tmp_path, monkeypatch)
    run = dict(mcp=["studio"], relay="없는.yaml", index=0)
    assert work.uses_editor(SimpleNamespace(**run, mcp_scope="all")) is True
    assert work.uses_editor(SimpleNamespace(**run, mcp_scope=None)) is False


def test_a_chat_turn_has_the_read_tools_and_the_same_mcp_rule_as_a_task_run(desk, tmp_path, monkeypatch):  # noqa: F811
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    engine.guard_stage = api.guard_stage
    desk.turn(desk.w.emp)
    cases = (("all", dict(mcp=[])), ("only", dict(mcp=["unreal"], mcp_only=["unreal"])))
    for scope, kw in cases:
        run = engine.create(relay, "g", repo="game", mcp_scope=scope, **kw)
        api.track_run(run.id, mode="chat", employee_id=desk.w.emp, room_id=desk.dms[desk.w.emp])
        desk.tracked.append(run.id)
        engine.advance(run.id)
        args = ClaudeCliRunner(exe="claude").build_args(runner.seen[-1], None)
        tools = args[args.index("--tools") + 1].split(",")
        assert not {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"} & set(tools) and "WebSearch" in tools
        if scope == "all":
            assert "--strict-mcp-config" not in args and {"mcp__unreal", "mcp__proj"} <= set(allowed(args))
        else:
            assert "--strict-mcp-config" in args and "mcp__unreal" in allowed(args) and "mcp__proj" not in allowed(args)
