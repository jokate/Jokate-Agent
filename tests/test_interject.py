"""Cutting in on a running relay, and recovering a result the model didn't return in the required shape."""
import json

import pytest

from relay_agent.baton import StageResult
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import ClaudeCliRunner, LiveChannel, MockRunner, StageCall
from relay_agent.usage import UsageStore


def make(tmp_path, runner):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nworkspace: none\nstages:\n  - {name: scout, provider: mock, prompt: role.md}\n"
                     "  - {name: build, provider: mock, prompt: role.md}\n", encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    return engine, relay


def test_note_goes_into_the_running_session_and_later_stages(tmp_path):
    answers = []

    class CutIn(MockRunner):
        def run(self, call):
            if call.stage == "scout":
                call.live.attach(self.live_messages.append)
                answers.append(engine.interject(run_id, "Hero.cpp 는 건드리지 마"))
            else:
                self.prompts = call.prompt
            return super().run(call)

    runner = CutIn()
    engine, relay = make(tmp_path, runner)
    run_id = engine.create(relay, "체력 수정", tmp_path).id
    done = engine.advance(run_id)
    assert answers == [{"status": "running", "applied": "live"}]
    assert "Hero.cpp 는 건드리지 마" in runner.live_messages[0]          # delivered live, no restart
    assert "Hero.cpp 는 건드리지 마" in runner.prompts                    # and the next stage sees it
    assert done.baton.user_notes == ["Hero.cpp 는 건드리지 마"] and done.status == "done"
    kinds = [e["kind"] for e in engine.history.events(run_id)]
    assert "user_interject" in kinds and "user_notes_applied" in kinds


def test_note_on_paused_run_applies_on_resume_and_done_run_refuses(tmp_path):
    engine, relay = make(tmp_path, MockRunner())
    run = engine.create(relay, "g", tmp_path)
    assert engine.interject(run.id, "테스트도 돌려")["applied"] == "on_resume"
    done = engine.advance(run.id)
    assert done.baton.user_notes == ["테스트도 돌려"]
    with pytest.raises(ValueError, match="끝난"):
        engine.interject(run.id, "늦은 지시")


def test_live_channel_refuses_when_nothing_listens():
    ch = LiveChannel()
    assert ch.send("x") is False
    got = []
    ch.attach(got.append)
    assert ch.send("y") and got == ["y"]
    ch.detach()
    assert ch.send("z") is False


def test_lenient_result_repairs_small_model_slips():
    r = StageResult.lenient({"summary": "정찰 완료", "state": None, "open_issues": "- A 확인\n- B 확인",
                             "next_steps": None, "verdict": "ok", "decisions_added": [{"decision": "x"}]})
    assert r.open_issues == ["A 확인", "B 확인"] and r.next_steps == [] and r.verdict == "pass"
    assert r.state == "정찰 완료" and r.decisions_added == []


def run_cli(tmp_path, monkeypatch, lines):
    import relay_agent.runners as runners

    def fake(args, call, stdin_text, on_line, live=None):
        for line in lines:
            on_line(json.dumps(line))
        return 0, ""

    monkeypatch.setattr(runners, "run_process", fake)
    seen = []
    call = StageCall(stage="scout", model="haiku", effort=None, system="s", prompt="p", cwd=tmp_path,
                     on_event=lambda k, d: seen.append(k))
    return ClaudeCliRunner(exe="claude").run(call)[0], seen


def test_prose_answer_without_structured_output_is_recovered(tmp_path, monkeypatch):
    prose = {"type": "assistant", "message": {"content": [{"type": "text", "text": "## 정찰 결과\nSource/Hero.cpp 에 체력 로직"}]}}
    result, seen = run_cli(tmp_path, monkeypatch, [prose, {"type": "result", "subtype": "success", "is_error": False,
                                                          "result": "## 정찰 결과\nSource/Hero.cpp 에 체력 로직"}])
    assert result.summary == "정찰 결과" and "Hero.cpp" in result.output and "result_repaired" in seen


def test_schema_retry_exhaustion_is_recovered_from_json_in_text(tmp_path, monkeypatch):
    text = '결과: {"summary": "완료", "state": "정찰 끝", "open_issues": "없음", "next_steps": ["구현"]}'
    result, _ = run_cli(tmp_path, monkeypatch, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "error_max_structured_output_retries", "is_error": True}])
    assert result.summary == "완료" and result.open_issues == ["없음"] and result.next_steps == ["구현"]


def test_token_watch_counts_rereads_and_big_results():
    from relay_agent.runners import TokenWatch

    w = TokenWatch()
    pending = {"t1": ("Read", "Content/Map.json")}
    for i, ctx in enumerate((5000, 45000)):
        w.on_event({"type": "assistant", "message": {"id": f"m{i}", "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 10}}}, pending)
    w.on_event({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1}}}, pending)  # same turn
    w.on_event({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                          "content": "x" * 30000}]}}, pending)
    rep = w.report()
    assert rep["turns"] == 2 and rep["reread_tokens"] == 50000 and rep["peak_context"] == 45000
    assert rep["top_results"][0] == {"tool": "Read", "target": "Content/Map.json", "chars": 30000}


def test_prompt_breakdown_names_the_biggest_part():
    from relay_agent.pipeline import prompt_breakdown

    b = prompt_breakdown("sys", "# HANDOFF\n## 목표\n짧음\n## 산출물: scout\n" + "가" * 5000 + "\n")
    assert next(iter(b["sections"])) == "산출물: scout" and b["prompt_chars"] > 5000


def test_recovery_leaves_runs_of_other_live_processes_alone(tmp_path):
    import os
    import subprocess
    import sys

    engine, relay = make(tmp_path, MockRunner())
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        live, dead = engine.create(relay, "a", tmp_path), engine.create(relay, "b", tmp_path)
        for run, pid in ((live, other.pid), (dead, 999999)):
            run.status, run.owner_pid = "running", pid
            engine.save(run)
        assert engine.recover_interrupted() == [dead.id]
        assert engine.load(live.id).status == "running" and os.getpid() != other.pid
    finally:
        other.kill()
