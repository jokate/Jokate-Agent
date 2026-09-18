"""Result presentation: stop handoff, MCP recording, at-a-glance summary."""
import threading
import time

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import ClaudeCliRunner, MockRunner, StageCall
from relay_agent.usage import UsageStore


def make(tmp_path, yaml_text, runner):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(yaml_text, encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    return engine, relay


THREE = """
name: r
stages:
  - {name: scout, provider: mock, prompt: role.md}
  - {name: build, provider: mock, prompt: role.md}
  - {name: review, provider: mock, prompt: role.md}
"""


def test_cancel_writes_handoff_up_to_the_stop(tmp_path):
    class SlowBuild(MockRunner):
        def run(self, call):
            if call.stage == "build":
                call.emit("tool_use", {"tool": "Edit", "target": "calc.py"})
                call.emit("mcp_call", {"server": "docs_read", "tool": "search", "target": "average"})
                self.delay_s = 5
            else:
                self.delay_s = 0
            return super().run(call)

    engine, relay = make(tmp_path, THREE, SlowBuild())
    run = engine.create(relay, "g", tmp_path)
    t = threading.Thread(target=engine.advance, args=(run.id,))
    t.start()
    for _ in range(100):
        if any(e["kind"] == "mcp_call" for e in engine.history.events(run.id)):
            break
        time.sleep(0.05)
    engine.cancel(run.id)
    t.join(5)

    stopped = engine.load(run.id)
    stop = stopped.baton.stop
    assert stopped.status == "cancelled" and stop.kind == "cancelled" and stop.stage == "build"
    assert stop.done_stages == ["scout"] and stop.remaining_stages == ["build", "review"]
    # request / work done / what is left — changed files are work, the tool log is not
    assert "### 요청" in stop.summary and "### 작업된 내역" in stop.summary and "### 남은 일" in stop.summary
    assert "calc.py" in stop.summary and "docs_read" not in stop.summary and not stop.partial_actions
    handoff = (engine.runs_dir / run.id / "HANDOFF.md").read_text(encoding="utf-8")
    assert "## 중단 지점" in handoff and "relay resume" in handoff

    finished = engine.advance(run.id, resume=True)
    assert finished.status == "done" and finished.baton.stop is None


def test_gate_pause_note_and_result_fields_in_summary(tmp_path):
    script = {"build": [{
        "summary": "평균 함수 수정", "state": "완료", "open_issues": [], "next_steps": [],
        "highlights": ["빈 리스트면 0.0 반환", "테스트 통과"], "user_checks": ["에디터에서 결과 확인"],
        "diagram": "```mermaid\nflowchart LR\n  A-->B\n```",
    }]}
    engine, relay = make(tmp_path, """
name: r
stages:
  - {name: build, provider: mock, prompt: role.md, gate: human}
  - {name: review, provider: mock, prompt: role.md}
""", MockRunner(script))
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "awaiting_approval" and run.baton.stop.kind == "awaiting_approval"
    assert run.baton.stop.remaining_stages == ["review"]

    s = engine.summary(run.id)
    assert s["highlights"] == ["[build] 빈 리스트면 0.0 반환", "[build] 테스트 통과"]
    assert s["user_checks"] == ["에디터에서 결과 확인"]
    assert s["diagram"] == "flowchart LR\n  A-->B" and s["diagram_stage"] == "build"
    full = (engine.runs_dir / run.id / "HANDOFF.md").read_text(encoding="utf-8")
    assert "```mermaid" in full and "- [ ] 에디터에서 결과 확인" in full
    # stage prompts don't pay for diagrams or highlight lists
    prompt_copy = run.baton.to_markdown(include_outputs=[])
    assert "mermaid" not in prompt_copy and "## 핵심 작업" not in prompt_copy


def test_mcp_calls_and_result_sizes_are_recorded():
    seen = []
    call = StageCall(stage="s", model="haiku", effort=None, system="", prompt="", cwd=".",
                     on_event=lambda k, d: seen.append((k, d)))
    pending = {}
    ClaudeCliRunner._emit_activity(call, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__docs_read__read_section", "input": {"doc": "A.md", "heading": "x"}},
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "big.txt"}},
    ]}}, pending)
    ClaudeCliRunner._emit_activity(call, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "섹션 본문" * 10},
        {"type": "tool_result", "tool_use_id": "t2", "content": "x" * 9000},
    ]}}, pending)
    kinds = [k for k, _ in seen]
    assert kinds == ["mcp_call", "tool_use", "mcp_result", "tool_result_large"]
    assert seen[0][1]["server"] == "docs_read" and seen[0][1]["tool"] == "read_section"
    assert seen[2][1]["chars"] == 50 and seen[3][1]["chars"] == 9000


def test_summary_counts_tools_mcp_and_clean_verify_commands(tmp_path):
    engine, relay = make(tmp_path, "name: r\nstages:\n  - {name: a, provider: mock, prompt: role.md}\n", MockRunner())
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    for kind, detail in [
        ("tool_use", {"tool": "Bash", "target": 'cd "C:\\x" && ls'}),
        ("tool_use", {"tool": "Bash", "target": 'cd "C:\\x" && python test_calc.py'}),
        ("mcp_call", {"server": "docs_read", "tool": "search", "target": "q"}),
        ("mcp_result", {"server": "docs_read", "tool": "search", "chars": 1200, "is_error": False}),
    ]:
        engine.history.add_event(run.id, "a", kind, detail)
    s = engine.summary(run.id)
    assert s["verification"] == ["python test_calc.py"]
    assert s["mcp"] == {"docs_read.search": {"calls": 1, "result_chars": 1200, "errors": 0}}
    assert s["tools"]["Bash"] == 2
