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
    monkeypatch.setattr(server, "LOOPBACK", set())  # pretend the test client is remote
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer t"}
    assert client.post("/sessions", json={"title": "x", "workdir": str(tmp_path)}, headers=headers).status_code == 403
    r = client.post("/sessions", json={"title": "x", "repo": "ok"}, headers=headers)
    assert r.status_code == 200 and r.json()["repo"] == "ok"
