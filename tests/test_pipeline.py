from pathlib import Path

import pytest

from relay_agent.baton import Baton, LogEntry, StageResult, result_schema
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import ClaudeCliRunner, MockRunner, StageCall
from relay_agent.usage import UsageStore

RELAY = """
name: t
stages:
  - {name: scout, runner: mock, model: haiku, prompt: role.md}
  - {name: plan, runner: mock, model: opus, prompt: role.md, reads_outputs: [scout], gate: human}
  - {name: build, runner: mock, model: sonnet, prompt: role.md, reads_outputs: [plan]}
  - {name: review, runner: mock, model: sonnet, prompt: role.md, reads_outputs: [build], on_retry: build, max_retries: 1}
"""


def result(stage, verdict="pass", issues=()):
    return {"summary": f"{stage} ok", "state": stage, "open_issues": list(issues), "next_steps": [],
            "output": f"{stage} out", "verdict": verdict}


@pytest.fixture
def setup(tmp_path: Path):
    (tmp_path / "role.md").write_text("role", encoding="utf-8")
    relay = tmp_path / "t.yaml"
    relay.write_text(RELAY, encoding="utf-8")

    def make(script):
        runner = MockRunner(script)
        engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"),
                             HistoryStore(tmp_path / "h.sqlite"), runner_factory=lambda _: runner)
        make.relay = relay
        return engine, runner, engine.create(relay, "목표", tmp_path)

    return make


def test_session_keeps_question_history_and_context(setup):
    engine, runner, first = setup({})
    engine.advance(first.id)
    engine.approve(first.id)
    engine.advance(first.id)

    second = engine.create(setup.relay, "두 번째 질문", Path(first.workdir), session_id=first.session_id)
    turns = engine.history.turns(first.session_id)
    assert [t["question"] for t in turns] == ["목표", "두 번째 질문"]
    assert turns[0]["status"] == "done" and "review mock 산출물" in turns[0]["result"]
    # the new run inherits a one-line summary of the earlier turn, not its transcript
    assert len(second.baton.session_context) == 1 and first.id in second.baton.session_context[0]
    assert "이 세션의 이전 요청" in second.baton.to_markdown()
    assert engine.history.search_turns("두 번째")[0]["run_id"] == second.id


def test_activity_events_show_actual_work(setup):
    engine, _, run = setup({})
    engine.advance(run.id)
    kinds = [(e["stage"], e["kind"]) for e in engine.history.events(run.id) if e["kind"] != "prompt_breakdown"]
    assert kinds[:4] == [("-", "run_created"), ("scout", "stage_started"), ("scout", "tool_use"), ("scout", "stage_finished")]
    assert kinds[-1] == ("plan", "awaiting_approval")
    tool = next(e for e in engine.history.events(run.id) if e["kind"] == "tool_use")
    assert tool["detail"] == {"tool": "Read", "target": "mock/scout.txt"}


def test_cli_stream_parsing_emits_tool_calls(tmp_path):
    seen = []
    call = StageCall(stage="s", model="haiku", effort=None, system="", prompt="", cwd=tmp_path,
                     on_event=lambda k, d: seen.append((k, d)))
    ClaudeCliRunner._emit_activity(call, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "Cooldown", "path": "Source"}},
        {"type": "tool_use", "name": "StructuredOutput", "input": {}},
    ]}})
    ClaudeCliRunner._emit_activity(call, {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "denied"},
    ]}})
    assert seen == [("tool_use", {"tool": "Grep", "target": "Cooldown  @ Source"}),
                    ("stage_answer", {"summary": "", "verdict": "pass", "open_issues": []}),
                    ("tool_error", {"message": "denied"})]


def test_gate_then_done(setup):
    engine, runner, run = setup({})
    run = engine.advance(run.id)
    assert run.status == "awaiting_approval" and run.index == 2
    engine.approve(run.id)
    run = engine.advance(run.id)
    assert run.status == "done"
    assert [h.stage for h in run.history] == ["scout", "plan", "build", "review"]
    assert (engine.runs_dir / run.id / "HANDOFF.md").exists()  # kept until the user marks the work complete


def test_stage_sees_only_requested_outputs(setup):
    engine, runner, run = setup({})
    engine.advance(run.id)
    plan_prompt = runner.calls[1].prompt
    assert "산출물: scout" in plan_prompt
    engine.approve(run.id)
    engine.advance(run.id)
    build_prompt = runner.calls[2].prompt
    assert "산출물: plan" in build_prompt and "산출물: scout" not in build_prompt


def test_review_retry_loops_back_then_exhausts(setup):
    engine, runner, run = setup({"review": [result("review", "retry", ["x 고칠 것"]), result("review", "retry")]})
    engine.advance(run.id)
    engine.approve(run.id)
    run = engine.advance(run.id)
    assert [h.stage for h in run.history] == ["scout", "plan", "build", "review", "build", "review"]
    assert run.status == "failed" and "retries exhausted" in run.error
    assert "x 고칠 것" in runner.calls[4].prompt  # second build saw the review's issue


def test_usage_recorded(setup):
    engine, _, run = setup({})
    engine.advance(run.id)
    rows = engine.usage.summary(run.id)
    assert {r["stage"] for r in rows} == {"scout", "plan"}


def test_result_schema_is_strict():
    schema = result_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"summary", "state", "open_issues", "next_steps"}  # the rest defaults
    assert "default" not in str(schema)
    assert set(result_schema(all_required=True)["required"]) == set(StageResult.model_fields)  # OpenAI strict


def test_cli_args_disable_unlisted_mcp(tmp_path):
    runner = ClaudeCliRunner(exe="claude", mcp_registry={"docs_read": {"command": "x"}})
    call = StageCall(stage="s", model="haiku", effort=None, system="r", prompt="p", cwd=tmp_path,
                     tools=["Read"], mcp_servers=["docs_read"])
    args = runner.build_args(call, tmp_path / "m.json")
    assert "--strict-mcp-config" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__docs_read"
    assert args[args.index("--tools") + 1] == "Read"


def test_baton_apply_dedupes_pointers():
    b = Baton(goal="g")
    r = StageResult.model_validate({**result("s"), "pointers_added": [{"path": "a.py", "anchor": "f"}] * 2})
    assert len(r.apply(b, "s").pointers) == 1


def test_retry_effort_only_on_redo(tmp_path):
    (tmp_path / "role.md").write_text("role", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("""
name: r
stages:
  - {name: build, runner: mock, model: sonnet, effort: low, retry_effort: high, prompt: role.md}
  - {name: review, runner: mock, model: sonnet, prompt: role.md, on_retry: build, max_retries: 1}
""", encoding="utf-8")
    runner = MockRunner({"review": [result("review", "retry"), result("review")]})
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "done"
    assert [(c.stage, c.effort) for c in runner.calls] == [("build", "low"), ("review", None), ("build", "high"), ("review", None)]


def test_budget_guard_pauses_then_approve_continues(tmp_path):
    (tmp_path / "role.md").write_text("role", encoding="utf-8")
    relay = tmp_path / "b.yaml"
    relay.write_text("""
name: b
max_run_cost_usd: 0.01
stages:
  - {name: a, runner: mock, model: haiku, prompt: role.md}
  - {name: b, runner: mock, model: haiku, prompt: role.md}
""", encoding="utf-8")

    class Pricey(MockRunner):
        def run(self, call):
            res, usage = super().run(call)
            usage.cost_usd = 0.02
            return res, usage

    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Pricey())
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "awaiting_approval" and run.index == 1
    assert engine.history.events(run.id)[-1]["kind"] == "budget_exceeded"
    engine.approve(run.id)
    assert engine.advance(run.id).status == "done"


def test_baton_caps_outputs_and_log():
    b = Baton(goal="g", outputs={"plan": "x" * 5000})
    b.log = [LogEntry(stage="s", summary=str(i)) for i in range(20)]
    text = b.to_markdown(include_outputs=["plan"], output_ref="runs/1/outputs/{stage}.md")
    assert "잘림 2000자" in text and "runs/1/outputs/plan.md" in text
    assert "[s] 19" in text and "[s] 7" not in text
    full = b.to_markdown(max_output_chars=None)
    assert "x" * 5000 in full and "[s] 0" in full


def test_cli_system_prompt_modes(tmp_path):
    runner = ClaudeCliRunner(exe="claude")
    base = dict(stage="s", model="haiku", effort=None, system="ROLE", prompt="p", cwd=tmp_path)
    replace = runner.build_args(StageCall(**base, system_mode="replace", max_budget_usd=0.3, isolate=True), None)
    assert "ROLE" in replace[replace.index("--system-prompt") + 1] and "--append-system-prompt" not in replace
    assert "--safe-mode" in replace and replace[replace.index("--max-budget-usd") + 1] == "0.3"
    append = runner.build_args(StageCall(**base, system_mode="append", isolate=True, mcp_servers=["docs_read"]), None)
    assert "--exclude-dynamic-system-prompt-sections" in append and "--system-prompt" not in append
    assert "--disable-slash-commands" in append and "--safe-mode" not in append  # safe-mode would kill MCP


def test_cli_falls_back_from_fable_to_opus(tmp_path):
    from relay_agent.runners import RunnerError, Usage

    class Flaky(ClaudeCliRunner):
        def __init__(self):
            super().__init__(exe="claude")
            self.models = []

        def _run_once(self, call, model):
            self.models.append(model)
            if model == "fable":
                raise RunnerError("model unavailable")
            return StageResult.model_validate(result("plan")), Usage("claude_cli", "claude-opus-5")

    seen = []
    runner = Flaky()
    call = StageCall(stage="plan", model="fable", effort=None, system="", prompt="", cwd=tmp_path,
                     fallback_model="opus", on_event=lambda k, d: seen.append((k, d)))
    _, usage = runner.run(call)
    assert runner.models == ["fable", "opus"] and usage.model == "claude-opus-5"
    assert seen[0][0] == "model_fallback" and seen[0][1]["to"] == "opus"


def test_retry_model_escalates_only_on_redo(tmp_path):
    (tmp_path / "role.md").write_text("role", encoding="utf-8")
    relay = tmp_path / "m.yaml"
    relay.write_text("""
name: m
stages:
  - {name: build, runner: mock, model: sonnet, retry_model: fable, prompt: role.md}
  - {name: review, runner: mock, model: fable, prompt: role.md, on_retry: build, max_retries: 1}
""", encoding="utf-8")
    runner = MockRunner({"review": [result("review", "retry"), result("review")]})
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    engine.advance(engine.create(relay, "g", tmp_path).id)
    assert [(c.stage, c.model) for c in runner.calls] == [("build", "sonnet"), ("review", "fable"), ("build", "fable"), ("review", "fable")]
