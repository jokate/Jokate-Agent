"""Cutting in on a running relay, and recovering a result the model didn't return in the required shape."""
import json
from pathlib import Path

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

    def fake(args, call, stdin_text, on_line, live=None, **kw):
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


def test_delete_session_removes_runs_but_not_active_or_undecided_ones(tmp_path):
    engine, relay = make(tmp_path, MockRunner())
    done = engine.advance(engine.create(relay, "끝난 일", tmp_path).id)
    sid = engine.history.find_session_by_run_prefix(done.id)
    assert (engine.runs_dir / done.id).exists()
    assert engine.delete_session(sid) == {"deleted": sid, "runs": 1}
    assert engine.history.get_session(sid) is None and not (engine.runs_dir / done.id).exists()
    assert engine.history.events(done.id) == []

    pending = engine.create(relay, "대기 중", tmp_path)
    sid2 = engine.history.find_session_by_run_prefix(pending.id)
    with pytest.raises(ValueError, match="진행 중"):
        engine.delete_session(sid2)
    pending = engine.advance(pending.id)
    pending.changes_status, pending.workspace_mode = "ready", "inplace"
    engine.save(pending)
    assert engine.delete_check(sid2)["rollback_only"] == [pending.id]  # already in the original: no block
    pending.workspace_mode = "copy"
    engine.save(pending)
    with pytest.raises(ValueError, match="복사본에만"):
        engine.delete_session(sid2)
    assert engine.delete_session(sid2, force=True)["runs"] == 1


def test_attachments_are_copied_listed_and_readable(tmp_path):
    shot = tmp_path / "crash.png"
    shot.write_bytes(b"\x89PNG fake")
    runner = MockRunner()
    engine, relay = make(tmp_path, runner)
    run = engine.create(relay, "이 크래시 봐줘", tmp_path, attachments=[shot, shot])
    copies = [Path(a) for a in run.baton.attachments]
    assert [c.name for c in copies] == ["crash.png", "crash-1.png"] and all(c.read_bytes() == shot.read_bytes() for c in copies)
    engine.advance(run.id)
    call = runner.calls[0]
    assert call.add_dirs == [str(engine.runs_dir / run.id / "attachments")] and "crash.png" in call.prompt


def test_latest_stage_checks_replace_older_ones():
    from relay_agent.baton import Baton, StageResult

    b = StageResult(summary="s", state="s", open_issues=[], next_steps=[], user_checks=["누수 확인", "누수 확인"]).apply(Baton(goal="g"), "scout")
    assert b.user_checks == ["누수 확인"]
    b = StageResult(summary="s", state="s", open_issues=[], next_steps=[], user_checks=["빌드 확인"]).apply(b, "review")
    assert b.user_checks == ["빌드 확인"]
    b = StageResult(summary="s", state="s", open_issues=[], next_steps=[]).apply(b, "x")
    assert b.user_checks == ["빌드 확인"]


def test_auto_approve_runs_through_design_gates(tmp_path):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "g.yaml"
    relay.write_text("name: g\nworkspace: none\nstages:\n  - {name: plan, provider: mock, prompt: role.md, gate: human}\n"
                     "  - {name: build, provider: mock, prompt: role.md}\n", encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: MockRunner())
    assert engine.advance(engine.create(relay, "g", tmp_path, approval="always").id).status == "awaiting_approval"
    run = engine.advance(engine.create(relay, "g", tmp_path, approval="never").id)
    assert run.status == "done" and "gate_skipped" in [e["kind"] for e in engine.history.events(run.id)]
    # ai (the product default): continues unless the design itself asks for a decision
    assert engine.advance(engine.create(relay, "g", tmp_path, approval="ai").id).status == "done"
    asking = MockRunner({"plan": [{"summary": "s", "state": "s", "open_issues": [], "next_steps": [],
                                   "needs_approval": True, "approval_reason": "A안/B안 중 선택 필요"}]})
    engine = RelayEngine(tmp_path / "runs2", UsageStore(tmp_path / "u2.sqlite"), HistoryStore(tmp_path / "h2.sqlite"),
                         runner_factory=lambda _: asking)
    paused = engine.advance(engine.create(relay, "g", tmp_path, approval="ai").id)
    assert paused.status == "awaiting_approval" and "A안/B안" in paused.baton.stop.reason


def test_user_is_alerted_when_a_run_needs_them(tmp_path):
    from relay_agent.notify import toast_xml

    seen = []
    engine, relay = make(tmp_path, MockRunner())
    engine.notifier = lambda run_id, kind, title, detail: seen.append((kind, title))
    run = engine.advance(engine.create(relay, "체력 로직 고쳐줘\n자세한 설명", tmp_path).id)
    assert seen == [("run_done", "체력 로직 고쳐줘")] and run.status == "done"
    feed = engine.history.events_of_kinds(["run_done"])
    assert feed[-1]["run_id"] == run.id and feed[-1]["question"].startswith("체력")
    xml = toast_xml('A&B <x>', '"q"', "http://h/?run=1&x=2")
    assert "A&amp;B &lt;x&gt;" in xml and 'launch="http://h/?run=1&amp;x=2"' in xml


def test_liveness_reports_what_the_ai_is_doing(tmp_path):
    seen = {}

    class Busy(MockRunner):
        def run(self, call):
            call.emit("pulse", {"doing": "thinking", "turn": 2})
            seen["live"] = engine.liveness(run_id)
            return super().run(call)

    engine, relay = make(tmp_path, Busy())
    run_id = engine.create(relay, "g", tmp_path).id
    engine.advance(run_id)
    live = seen["live"]
    assert live["alive"] and live["doing"] == "thinking" and live["turn"] == 2 and live["stage"] == "build"
    assert live["quiet_seconds"] < 5 and live["precise"]
    assert "pulse" not in [e["kind"] for e in engine.history.events(run_id)]  # never written to the log
    assert engine.liveness(run_id)["alive"] is False  # done


def test_interrupted_stage_continues_its_conversation_on_resume(tmp_path):
    from relay_agent.runners import RunnerError

    calls = []

    class Flaky(MockRunner):
        def run(self, call):
            calls.append((call.stage, call.session_id, call.resume_session, call.prompt[:4]))
            if call.stage == "scout" and len(calls) == 1:
                raise RunnerError("network", "error")
            return super().run(call)

    engine, relay = make(tmp_path, Flaky())
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "failed" and run.stage_sessions["scout"] == calls[0][1]
    run = engine.advance(run.id, resume=True)
    stage, sid, resume, head = calls[1]
    assert resume == calls[0][1] and sid is None and head == "[재개]"  # same conversation, short prompt
    assert run.status == "done" and run.stage_sessions == {}
    assert calls[2][2] is None and calls[2][1]  # next stage: its own fresh conversation


def test_auto_mode_never_pauses_and_tells_every_stage_it_is_preapproved(tmp_path):
    from relay_agent.history import HistoryStore
    from relay_agent.pipeline import RelayEngine
    from relay_agent.runners import MockRunner
    from relay_agent.usage import UsageStore

    class Rec(MockRunner):
        prompts = []

        def run(self, call):
            Rec.prompts.append(call.prompt)
            return super().run(call)

    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nworkspace: none\nstages:\n  - {name: plan, provider: mock, prompt: role.md, gate: human}\n"
                     "  - {name: build, provider: mock, prompt: role.md}\n", encoding="utf-8")
    script = {"plan": [{"summary": "s", "state": "s", "open_issues": [], "next_steps": [], "needs_approval": True,
                        "approval_reason": "대안 선택"}]}
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Rec(script=script))
    run = engine.advance(engine.create(relay, "g", tmp_path, approval="auto").id)
    assert run.status == "done"  # even though the plan asked for approval
    assert all("자동 진행 모드" in p for p in Rec.prompts) and len(Rec.prompts) == 2
    assert RelayEngine.__dict__["DEFAULT_APPROVAL"] in ("auto", "always")  # auto in production (tests pin "always")
