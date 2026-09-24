import json
from pathlib import Path

from relay_agent import router
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine, RelaySpec
from relay_agent.runners import Usage
from relay_agent.usage import UsageStore

RELAYS = Path(__file__).resolve().parent.parent / "relays"


def make_engine(tmp_path, ask=None):
    return RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                       router=ask)


def project(tmp_path):
    proj = tmp_path / "game"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# 미니게임 모음\nUnity 2D. 씬은 MCP 로만 고친다.\n", encoding="utf-8")
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"unity-mcp": {"command": "relay.exe"}}}), encoding="utf-8")
    return proj


def test_auto_picks_the_relay_the_router_names_and_records_why(tmp_path):
    seen = {}

    def ask(text, model):
        seen["text"], seen["model"] = text, model
        return ('{"relay": "game-cycle", "reason": "새 미니게임 제작 요청"}',
                Usage("claude", "claude-haiku-4-5", input_tokens=600, output_tokens=30, cost_usd=0.001))

    engine = make_engine(tmp_path, ask)
    run = engine.create(RELAYS / "auto.yaml", "계란 뒤집기 미니게임 만들어줘", project(tmp_path),
                        stage_models={"plan": {"model": "opus"}})  # a pick for a stage the chosen relay lacks is dropped

    assert Path(run.relay).name == "game-cycle.yaml" and run.workspace_mode == "inplace"
    assert seen["model"] == "haiku"
    # the router sees the request, the target's own harness and every candidate — never itself or the mock relay
    assert "계란 뒤집기" in seen["text"] and "미니게임 모음" in seen["text"] and "unity-mcp" in seen["text"]
    assert "- game-cycle:" in seen["text"] and "- quick:" in seen["text"]
    assert "- auto:" not in seen["text"] and "- demo:" not in seen["text"]
    events = engine.history.events(run.id)
    routed = next(e for e in events if e["kind"] == "relay_routed")
    assert routed["detail"]["relay"] == "game-cycle" and routed["detail"]["reason"] == "새 미니게임 제작 요청"
    assert next(e for e in events if e["kind"] == "run_created")["detail"]["relay"] == "game-cycle"
    assert engine.history.turns(run.session_id)[0]["relay"] == "game-cycle"


def test_auto_falls_back_without_failing_the_run(tmp_path):
    proj = project(tmp_path)
    no_ai = make_engine(tmp_path / "a").create(RELAYS / "auto.yaml", "버튼 색 바꿔줘", proj)
    assert Path(no_ai.relay).name == "quick.yaml"

    def broken(text, model):
        raise RuntimeError("claude -p timed out")

    run = make_engine(tmp_path / "b", broken).create(RELAYS / "auto.yaml", "버튼 색 바꿔줘", proj)
    assert Path(run.relay).name == "quick.yaml"
    reason = next(e for e in HistoryStore(tmp_path / "b" / "h.sqlite").events(run.id) if e["kind"] == "relay_routed")
    assert "timed out" in reason["detail"]["reason"]


def test_parse_accepts_json_in_prose_or_a_bare_name_and_rejects_unknown():
    names = ["quick", "default", "docs-qa"]
    assert router.parse('골랐습니다: {"relay": "default", "reason": "여러 파일"}', names) == ("default", "여러 파일")
    assert router.parse("docs-qa", names) == ("docs-qa", "")
    assert router.parse('{"relay": "deploy"}', names) is None
    usage = Usage("claude", "haiku")
    choice = router.route(lambda t, m: ('{"relay": "deploy"}', usage), "haiku", "", [{"name": n} for n in names], "quick")
    assert choice["relay"] == "quick" and "찾지 못해" in choice["reason"]


def test_shipped_auto_relay_offers_every_real_relay():
    spec, _ = RelaySpec.load(RELAYS / "auto.yaml")
    assert spec.router is not None and not spec.stages
    names = [o["name"] for o in router.candidates(RELAYS, spec.router.candidates, spec.router.exclude + ["auto"],
                                                  RelaySpec.load)]
    assert {"quick", "default", "docs-qa", "game-cycle", "quick-fable"} <= set(names)
    assert "demo" not in names and "auto" not in names
