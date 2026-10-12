"""MCP servers of a run: the project's own always, global / other-folder ones by name, loaded on demand."""
import json

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.projctx import discover
from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore

RELAY = "name: r\nworkspace: none\nstages:\n  - {name: build, provider: mock, prompt: role.md, tools: [Read, Grep]}\n"


class Recorder(MockRunner):
    def __init__(self):
        super().__init__()
        self.seen = []  # MockRunner keeps its own `calls`

    def run(self, call):
        self.seen.append(call)
        return super().run(call)


def setup(tmp_path, monkeypatch, repo_fields=None):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"unreal": {"command": "unrealmcp"}}}), encoding="utf-8")
    monkeypatch.setattr("relay_agent.projctx.Path.home", staticmethod(lambda: home))
    game = tmp_path / "Game"
    game.mkdir()
    (game / "CLAUDE.md").write_text("rules", encoding="utf-8")
    (game / ".mcp.json").write_text(json.dumps({"mcpServers": {"proj": {"command": "p"}}}), encoding="utf-8")
    other = tmp_path / "Tools"
    other.mkdir()
    (other / ".mcp.json").write_text(json.dumps({"mcpServers": {"pipeline": {"command": "x"}}}), encoding="utf-8")
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(RELAY, encoding="utf-8")
    runner = Recorder()
    repos = RepoRegistry({"game": {"path": str(game), "mcp_from": [str(other)], **(repo_fields or {})}})
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner, repos=repos)
    return engine, relay, runner, game


def test_discover_separates_project_and_optional_servers(tmp_path, monkeypatch):
    _, _, _, game = setup(tmp_path, monkeypatch)
    ctx = discover(game, mcp_from=[str(tmp_path / "Tools")])
    assert list(ctx.mcp) == ["proj"]
    assert ctx.optional_mcp["unreal"]["scope"] == "전역(사용자)" and "Tools" in ctx.optional_mcp["pipeline"]["scope"]


def test_only_the_projects_own_server_by_default(tmp_path, monkeypatch):
    engine, relay, runner, _ = setup(tmp_path, monkeypatch)
    engine.advance(engine.create(relay, "g", repo="game").id)
    call = runner.seen[0]
    assert call.mcp_servers == ["proj"] and "unreal" not in call.mcp_overrides
    assert "ToolSearch" in call.tools and "MCP 서버 proj" in call.prompt


def test_repo_default_and_run_pick_attach_global_and_other_folder_servers(tmp_path, monkeypatch):
    engine, relay, runner, game = setup(tmp_path, monkeypatch, {"mcp": ["unreal"]})
    assert {c["name"]: (c["always"], c["default"]) for c in engine.mcp_choices(game, engine.repos.get("game"))} == {
        "proj": (True, True), "pipeline": (False, False), "unreal": (False, True)}
    engine.advance(engine.create(relay, "g", repo="game").id)  # the repo's default
    assert runner.seen[0].mcp_servers == ["proj", "unreal"]
    assert runner.seen[0].mcp_overrides["unreal"] == {"command": "unrealmcp"}
    assert "unreal" in runner.seen[0].prompt and "grep/glob 하지 말고" in runner.seen[0].prompt

    run = engine.create(relay, "g", repo="game", mcp=["pipeline", "nope"])  # this run's own pick wins
    engine.advance(run.id)
    assert runner.seen[1].mcp_servers == ["proj", "pipeline"]
    assert any(e["kind"] == "mcp_missing" and e["detail"]["server"] == "nope" for e in engine.history.events(run.id))

    engine.advance(engine.create(relay, "g", repo="game", mcp=[]).id)  # explicitly none of the optional ones
    assert runner.seen[2].mcp_servers == ["proj"]


def test_the_projects_own_server_comes_before_the_registry_and_the_global_one_of_its_name(tmp_path, monkeypatch):
    """A new UE project's `unreal` (studio M-5: the repository is registered with `mcp: [unreal]`) is the one
    of its own `.mcp.json`, also when `mcp_registry` and the user's settings have an `unreal` — MNYS's."""
    engine, relay, _, game = setup(tmp_path, monkeypatch, {"mcp": ["unreal"]})  # the global `unreal` is there
    own = {"command": "uv", "args": ["run", "unreal_mcp_server.py"], "env": {"UNREAL_MCP_PORT": "55558"}}
    (game / ".mcp.json").write_text(json.dumps({"mcpServers": {"unreal": own}}), encoding="utf-8")
    engine.mcp_registry["unreal"] = {"command": "registry"}
    run = engine.create(relay, "g", repo="game")
    ctx = discover(game)
    assert ctx.mcp["unreal"] == own and "unreal" not in ctx.optional_mcp
    assert engine._attached_mcp(run, ctx, engine.repos.get("game"))["unreal"] == own
    assert not any(e["kind"] == "mcp_missing" for e in engine.history.events(run.id))
    # the one thing that comes before it: the entry of that folder in ~/.claude.json
    settings = tmp_path / "home" / ".claude.json"
    entry = {"command": "entry-of-the-folder"}
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["projects"] = {game.resolve().as_posix(): {"mcpServers": {"unreal": entry}}}
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert discover(game).mcp["unreal"] == entry
    assert engine._attached_mcp(run, discover(game), engine.repos.get("game"))["unreal"] == entry

