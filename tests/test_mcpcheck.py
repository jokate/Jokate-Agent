"""Before a run: the MCP servers it attaches must answer, and an editor-MCP relay needs one."""
import json
import socket
import sys
from pathlib import Path

from relay_agent import mcpcheck
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore

SERVER = """
import json, sys
print("starting...", flush=True)            # a log line before the answer is fine
req = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2025-06-18",
      "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}}), flush=True)
sys.stdin.read()
"""


def script(tmp_path, body, name="srv.py"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return {"command": sys.executable, "args": [str(path)]}


def test_a_stdio_server_that_answers_initialize_is_up(tmp_path):
    assert mcpcheck.probe(script(tmp_path, SERVER)) is None


def test_a_stdio_server_that_dies_or_hangs_is_down(tmp_path):
    died = mcpcheck.probe(script(tmp_path, "import sys; sys.stderr.write('editor not found'); sys.exit(1)", "a.py"))
    assert "종료" in died and "editor not found" in died
    hung = mcpcheck.probe(script(tmp_path, "import time; time.sleep(30)", "b.py"), timeout_s=1)
    assert "응답 없음" in hung
    assert "실행할 수 없음" in mcpcheck.probe({"command": "no-such-mcp-server-xyz"})


def test_an_http_server_is_checked_by_connecting_to_its_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        assert mcpcheck.probe({"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}) is None
    finally:
        listener.close()
    assert "연결할 수 없음" in mcpcheck.probe({"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}, timeout_s=1)


def engine(tmp_path, relay_yaml, probe, repos=None):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(relay_yaml, encoding="utf-8")
    runner = MockRunner()
    eng = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                      runner_factory=lambda _: runner, repos=RepoRegistry(repos or {}), mcp_probe=probe)
    return eng, relay, runner


def with_unreal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"unreal": {"command": "ue-mcp"}}}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    game = tmp_path / "Game"
    game.mkdir()
    return game


RELAY = "name: r\nworkspace: inplace\nstages:\n  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}\n"


def test_a_down_mcp_stops_the_run_before_any_stage_and_resume_checks_again(tmp_path, monkeypatch):
    game = with_unreal(tmp_path, monkeypatch)
    state = {"up": False}
    eng, relay, runner = engine(tmp_path, RELAY, lambda cfg: None if state["up"] else "55557 에 연결할 수 없음")
    run = eng.advance(eng.create(relay, "액터 배치", game, mcp=["unreal"]).id)
    assert run.status == "failed" and "unreal: 55557 에 연결할 수 없음" in run.error and not runner.calls
    check = next(e for e in eng.history.events(run.id) if e["kind"] == "mcp_check")
    assert check["detail"]["failed"] == {"unreal": "55557 에 연결할 수 없음"}

    state["up"] = True  # the editor was started
    run = eng.advance(run.id, resume=True)
    assert run.status == "done" and len(runner.calls) == 1


def test_no_probe_and_no_attached_mcp_leave_runs_alone(tmp_path, monkeypatch):
    game = with_unreal(tmp_path, monkeypatch)
    eng, relay, runner = engine(tmp_path, RELAY, lambda cfg: "down")
    assert eng.advance(eng.create(relay, "코드만", game).id).status == "done"  # unreal not picked: not checked


def test_an_editor_mcp_relay_needs_an_attached_mcp(tmp_path, monkeypatch):
    game = with_unreal(tmp_path, monkeypatch)
    eng, relay, runner = engine(tmp_path, "name: r\nworkspace: inplace\nstages:\n  - {name: edit, provider: mock, prompt: role.md, "
                                          "tools: [Read], edits_via_mcp: true}\n", None)
    run = eng.create(relay, "액터 배치", game)
    assert run.workspace_mode == "inplace"  # an MCP-editing stage is a writing stage: changes are tracked
    run = eng.advance(run.id)
    assert run.status == "failed" and "연결된 MCP 가 없습니다" in run.error and not runner.calls
    assert eng.advance(eng.create(relay, "액터 배치", game, mcp=["unreal"]).id).status == "done"
