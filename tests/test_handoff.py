"""Stop hand-over by a lightweight model, re-runs that read it, cleanup, and the folder's own context."""
import json

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine, StageSpec
from relay_agent.projctx import discover
from relay_agent.runners import ClaudeCliRunner, MockRunner, RunnerError, StageCall, Usage
from relay_agent.usage import UsageStore

TWO = """
name: r
stages:
  - {name: scout, provider: mock, prompt: role.md}
  - {name: build, provider: mock, prompt: role.md}
"""
WRITTEN = "### 요청\n- 평균 함수 고치기\n### 작업된 내역\n- calc.py 수정 시작\n### 남은 일\n- 테스트"


class FailOnce(MockRunner):
    """build edits a file, then fails the first time; records every prompt it gets."""

    def __init__(self):
        super().__init__()
        self.prompts: dict[str, list[str]] = {}
        self.failed = False

    def run(self, call):
        self.prompts.setdefault(call.stage, []).append(call.prompt)
        if call.stage == "build" and not self.failed:
            self.failed = True
            call.emit("tool_use", {"tool": "Read", "target": "notes.md"})
            call.emit("tool_use", {"tool": "Edit", "target": "calc.py"})
            call.emit("note", {"text": "평균 계산을 고치는 중"})
            raise RunnerError("boom")
        return super().run(call)


def make(tmp_path, runner, writer=None):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(TWO, encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner, handoff_writer=writer)
    return engine, relay


def test_lightweight_model_writes_the_handoff_without_tool_history(tmp_path):
    seen = []

    def writer(facts):
        seen.append(facts)
        return WRITTEN, Usage("claude", "haiku", input_tokens=900, output_tokens=200, cost_usd=0.002)

    engine, relay = make(tmp_path, FailOnce(), writer)
    run = engine.advance(engine.create(relay, "평균 함수 고치기", tmp_path).id)
    stop = run.baton.stop
    assert run.status == "failed" and stop.summary == WRITTEN and stop.writer == "haiku"
    facts = seen[0]
    assert "평균 함수 고치기" in facts and "calc.py" in facts and "평균 계산을 고치는 중" in facts
    assert "notes.md" not in facts  # reads are tool history, not work
    assert any(r["stage"] == "build:handoff" for r in engine.usage.summary(run.id))
    assert WRITTEN in (engine.runs_dir / run.id / "HANDOFF.md").read_text(encoding="utf-8")


def test_writer_failure_keeps_the_plain_handoff(tmp_path):
    def writer(facts):
        raise RuntimeError("no claude")

    engine, relay = make(tmp_path, FailOnce(), writer)
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert "### 작업된 내역" in run.baton.stop.summary and "calc.py" in run.baton.stop.summary
    assert any(e["kind"] == "handoff_failed" for e in engine.history.events(run.id))


def test_resume_reads_the_handoff_then_done_removes_it(tmp_path):
    runner = FailOnce()
    engine, relay = make(tmp_path, runner, lambda f: (WRITTEN, Usage("claude", "haiku")))
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert (engine.runs_dir / run.id / "HANDOFF.md").exists()

    done = engine.advance(run.id, resume=True)
    retry_prompt = runner.prompts["build"][-1]
    assert "HANDOFF" in retry_prompt and "calc.py 수정 시작" in retry_prompt
    assert done.status == "done" and done.baton.stop is None
    # a finished run leaves its own hand-over (written by the same model) until the user completes the work
    assert done.baton.handoff == WRITTEN and done.baton.handoff_by == "haiku"
    assert (engine.runs_dir / run.id / "HANDOFF.md").exists()


def test_new_request_in_session_carries_the_unfinished_handoff(tmp_path):
    runner = FailOnce()
    engine, relay = make(tmp_path, runner, lambda f: (WRITTEN, Usage("claude", "haiku")))
    first = engine.advance(engine.create(relay, "g", tmp_path).id)

    second = engine.create(relay, "다시 해줘", session_id=first.session_id)
    assert first.id in second.baton.previous_handoff and "calc.py 수정 시작" in second.baton.previous_handoff
    assert "직전 실행의 인계서" in second.baton.to_markdown()
    done = engine.advance(second.id)
    assert done.status == "done"
    assert "calc.py 수정 시작" in runner.prompts["scout"][-1]  # the first stage of the re-request read it
    assert (engine.runs_dir / first.id / "HANDOFF.md").exists()  # a finished run does not close the work

    third = engine.create(relay, "다음 일", session_id=first.session_id)
    assert second.id in third.baton.previous_handoff  # the finished run's hand-over carries on too
    engine.advance(third.id)

    # only the user's "작업 완료" closes the session's hand-overs
    r = engine.complete_session(first.session_id)
    assert r["handoffs_removed"] == 3 and engine.history.get_session(first.session_id)["completed_at"]
    for run_id in (first.id, second.id, third.id):
        assert not (engine.runs_dir / run_id / "HANDOFF.md").exists()
    fourth = engine.create(relay, "새 작업", session_id=first.session_id)
    assert fourth.baton.previous_handoff == ""
    assert engine.history.get_session(first.session_id)["completed_at"] is None  # a new request reopens it


def project(tmp_path):
    root = tmp_path / "Game"
    (root / ".git").mkdir(parents=True)
    (root / "Source" / ".git").mkdir(parents=True)  # a nested repo (MNYS/Source) still belongs to the project
    (root / "CLAUDE.md").write_text("조회는 `python Tools/q.py find <질의>` 로.\n`python -m x` 는 무시", encoding="utf-8")
    skill = root / ".claude" / "skills" / "engine-src"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: engine-src\n---\n`python Tools/ue.py sym X`", encoding="utf-8")
    (root / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(make:*)"]}}))
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"editor": {"command": "ed"}}}))
    return root


def test_discover_finds_instructions_skills_mcp_and_commands_up_the_tree(tmp_path):
    root = project(tmp_path)
    ctx = discover(root / "Source", home=tmp_path / "home")
    assert ctx.root == root.resolve()
    assert [p.name for p in ctx.instructions] == ["CLAUDE.md"] and ctx.skills == ["engine-src"]
    assert ctx.commands == ["python Tools/q.py", "python Tools/ue.py"] and "editor" in ctx.mcp
    assert "Bash(python Tools/q.py:*)" in ctx.allowed_bash() and "Bash(make:*)" in ctx.allowed_bash()
    assert "Bash(cd:*)" in ctx.allowed_bash()  # `cd <root> && python Tools/q.py …` must not wait for approval
    assert "Skill" in ctx.allowed_other()
    copy_lines = "\n".join(ctx.prompt_lines(tmp_path / "copy"))
    assert f"cd {root.resolve().as_posix()}" in copy_lines and "복사본" in copy_lines
    assert "복사본" not in "\n".join(ctx.prompt_lines(root / "Source"))
    assert discover(tmp_path / "home" / "x", home=tmp_path / "home") is None  # never the user-level folder


def test_project_context_keeps_tools_skills_and_skips_safe_mode(tmp_path):
    ctx = discover(project(tmp_path) / "Source", home=tmp_path / "home")
    stage = StageSpec(name="scout", provider="claude", prompt="p.md", tools=["Read", "Grep", "Glob"], isolate=True)
    tools = RelayEngine._stage_tools(stage, None, ctx)
    assert "Bash" in tools and "Skill" in tools
    call = StageCall(stage="scout", model=None, effort=None, system="s", prompt="p", cwd=tmp_path, tools=tools,
                     isolate=True, project=True)
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert "--safe-mode" not in args and "--disable-slash-commands" not in args
    plain = ClaudeCliRunner(exe="claude").build_args(StageCall(stage="s", model=None, effort=None, system="s",
                                                               prompt="p", cwd=tmp_path, isolate=True), None)
    assert "--safe-mode" in plain
