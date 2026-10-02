from pathlib import Path

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore


def setup(tmp_path, repo_cfg):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nworkspace: copy\nstages:\n  - {name: build, provider: mock, prompt: role.md, "
                     "tools: [Read, Bash], mcp: [docs_read]}\n", encoding="utf-8")
    runner = MockRunner()
    registry = {"docs_read": {"command": "python", "args": ["docs_read.py", "GLOBAL_DOCS"]}}
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner, repos=RepoRegistry(repo_cfg), mcp_registry=registry)
    return engine, relay, runner


def test_run_by_repo_name_applies_path_mode_verify_notes_and_docs(tmp_path):
    repo_dir = tmp_path / "game"
    (repo_dir / "Docs").mkdir(parents=True)
    (repo_dir / "a.txt").write_text("a", encoding="utf-8")
    engine, relay, runner = setup(tmp_path, {"game": {
        "path": str(repo_dir), "workspace": "inplace", "verify": ["uv run pytest -q"],
        "docs": "Docs", "notes": "UE5 GAS 프로젝트", "excludes": ["Content"],
    }})
    run = engine.create(relay, "고쳐줘", repo="game")
    assert run.repo == "game" and Path(run.workdir) == repo_dir.resolve() and run.workspace_mode == "inplace"
    assert engine.history.get_session(run.session_id)["repo"] == "game"

    engine.advance(run.id)
    call = runner.calls[0]
    assert "Bash(uv run pytest -q)" in call.allowed_tools and "Bash(uv run pytest -q:*)" in call.allowed_tools
    assert "저장소: `game`" in call.prompt and "`uv run pytest -q`" in call.prompt and "UE5 GAS" in call.prompt
    assert call.mcp_overrides["docs_read"]["args"][-1] == str(repo_dir.resolve() / "Docs")

    # a follow-up in the same session inherits the repo without naming it again
    second = engine.create(relay, "이어서", session_id=run.session_id)
    assert second.repo == "game"


def test_missing_repo_path_is_a_clear_error(tmp_path):
    engine, relay, _ = setup(tmp_path, {"other": {"path": str(tmp_path / "nope")}})
    try:
        engine.create(relay, "g", repo="other")
    except ValueError as e:
        assert "경로가 이 머신에 없습니다" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_match_picks_deepest_registered_repo(tmp_path):
    (tmp_path / "mono" / "svc").mkdir(parents=True)
    reg = RepoRegistry({"mono": {"path": str(tmp_path / "mono")}, "svc": {"path": str(tmp_path / "mono" / "svc")}})
    assert reg.match(tmp_path / "mono" / "svc" / "src").name == "svc"
    assert reg.match(tmp_path / "mono" / "x").name == "mono"
    assert reg.match(tmp_path) is None


def test_remote_client_cannot_target_unregistered_folder(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import relay_agent.server as server

    registered = tmp_path / "ok"
    registered.mkdir()
    monkeypatch.setattr(server.engine, "repos", RepoRegistry({"ok": {"path": str(registered)}}))
    monkeypatch.setattr(server.engine, "history", HistoryStore(tmp_path / "h.sqlite"))  # never the real DB
    monkeypatch.setattr(server.cfg, "auth_token", "t")
    monkeypatch.setattr(server.cfg, "remote_networks", [])  # this machine's local config may limit to Tailscale
    monkeypatch.setattr(server, "LOOPBACK", set())  # pretend the test client is remote
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer t"}
    assert client.post("/sessions", json={"title": "x", "workdir": str(tmp_path)}, headers=headers).status_code == 403
    r = client.post("/sessions", json={"title": "x", "repo": "ok"}, headers=headers)
    assert r.status_code == 200 and r.json()["repo"] == "ok"


def test_session_follows_its_folder_when_its_repo_was_renamed(tmp_path):
    import pytest

    repo_dir = tmp_path / "game"
    repo_dir.mkdir()
    engine, relay, _ = setup(tmp_path, {"boolpyeon": {"path": str(repo_dir), "workspace": "inplace"}})
    # a session made while the repo was registered under another name (here: its path)
    session = engine.history.create_session("작업", str(repo_dir), repo=str(repo_dir))

    run = engine.create(relay, "로드맵대로 진행", session_id=session["id"], repo=session["repo"])

    assert run.repo == "boolpyeon" and Path(run.workdir) == repo_dir.resolve()
    assert engine.history.get_session(session["id"])["repo"] == "boolpyeon"  # fixed for the next request too
    with pytest.raises(ValueError, match="등록되지 않은 저장소"):  # a name asked for explicitly is still checked
        engine.create(relay, "x", session_id=session["id"], repo="nope")


def test_repo_add_sets_and_clears_the_default_mcp(tmp_path, monkeypatch):
    import argparse

    import yaml

    from relay_agent import cli, config

    monkeypatch.setattr(config, "ROOT", tmp_path)
    game = tmp_path / "Game"
    game.mkdir()
    base = dict(action="add", name="game", path=str(game), url=None, workspace=None, relay=None, verify=None,
                docs=None, notes=None, mcp_from=None)
    cfg = argparse.Namespace(repos={}, repos_root="")
    cli.repo_command(argparse.Namespace(**base, mcp=["unreal"]), cfg)
    local = tmp_path / "relay.config.local.yaml"
    assert yaml.safe_load(local.read_text(encoding="utf-8"))["repos"]["game"]["mcp"] == ["unreal"]
    cli.repo_command(argparse.Namespace(**base, mcp=None), cfg)  # not given: kept
    assert yaml.safe_load(local.read_text(encoding="utf-8"))["repos"]["game"]["mcp"] == ["unreal"]
    cli.repo_command(argparse.Namespace(**base, mcp=[""]), cfg)  # --mcp "": cleared
    assert yaml.safe_load(local.read_text(encoding="utf-8"))["repos"]["game"]["mcp"] == []


def test_a_repo_default_mcp_is_attached_to_its_runs(tmp_path, monkeypatch):
    import json

    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"unreal": {"command": "ue-mcp"}}}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    game = tmp_path / "Game"
    game.mkdir()
    engine, relay, runner = setup(tmp_path, {"game": {"path": str(game), "workspace": "copy", "mcp": ["unreal"]}})
    run = engine.create(relay, "레벨에 액터 배치", repo="game")
    assert run.workspace_mode == "inplace"  # the repo's editor MCP forces in place
    engine.advance(run.id)
    assert "unreal" in runner.calls[0].mcp_servers and runner.calls[0].mcp_overrides["unreal"] == {"command": "ue-mcp"}
