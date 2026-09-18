"""A silent AI process is cut and restarted; a resume without a saved conversation starts fresh at once."""
import sys
import time
from pathlib import Path

from relay_agent.runners import ClaudeCliRunner, LiveDetail, RunnerError, StageCall, run_process

SILENT = "import time, sys; sys.stderr.write('Retrying in 5s (529 overloaded)\\n'); sys.stderr.flush(); time.sleep(30)"


def test_run_process_kills_a_silent_process_after_stall_s(tmp_path):
    events = []
    call = StageCall(stage="s", model=None, effort=None, system="s", prompt="p", cwd=tmp_path, timeout_s=60, stall_s=2,
                     on_event=lambda k, d: events.append((k, d)))
    seen = []
    t0 = time.monotonic()
    try:
        run_process([sys.executable, "-c", SILENT], call, None, lambda line: None, on_stderr=seen.append)
        raise AssertionError("expected a stall")
    except RunnerError as e:
        assert e.kind == "stalled" and "아무 출력이 없어" in str(e) and "529" in str(e)
    assert time.monotonic() - t0 < 25  # not the 30 s the process wanted
    assert seen and "529" in seen[0]  # the retry notice reached the liveness callback
    stopped = next(d for k, d in events if k == "process_stopped")
    assert stopped["why"] == "stalled" and stopped["confirmed"]


def test_stderr_retry_notice_becomes_a_retry_pulse():
    d = LiveDetail()
    p = d.on_stderr("Retrying in 12s (attempt 3/10, 529 overloaded)")
    assert p["doing"] == "retry" and "529" in p["snippet"]
    p = d.on_event({"type": "system", "subtype": "api_retry", "attempt": 2, "retry_delay_ms": 8000, "error_status": 529})
    assert p["doing"] == "retry" and "attempt=2" in p["snippet"] and "529" in p["snippet"]


def test_stalled_stage_is_started_once_more(tmp_path, monkeypatch):
    attempts = []

    def fake_once(self, call, model):
        attempts.append(call.resume_session)
        if len(attempts) == 1:
            raise RunnerError("AI 프로세스가 8분간 아무 출력이 없어 중단", "stalled")
        return "ok", None

    events = []
    monkeypatch.setattr(ClaudeCliRunner, "_run_once", fake_once)
    call = StageCall(stage="s", model="haiku", effort=None, system="s", prompt="p", cwd=tmp_path,
                     on_event=lambda k, d: events.append((k, d)))
    assert ClaudeCliRunner(exe="claude").run(call) == ("ok", None)
    assert attempts == [None, None] and [k for k, _ in events] == ["stall_restart"]


def test_resume_without_a_saved_conversation_starts_fresh_without_calling(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ClaudeCliRunner, "_run_once", lambda self, call, model: calls.append(call) or ("ok", None))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    events = []
    call = StageCall(stage="s", model="haiku", effort=None, system="s", prompt="[재개]", fresh_prompt="full", cwd=tmp_path,
                     resume_session="deadbeef-0000", on_event=lambda k, d: events.append((k, d)))
    ClaudeCliRunner(exe="claude").run(call)
    assert calls[0].resume_session is None and calls[0].prompt == "full"
    assert events[0][0] == "resume_fallback" and "저장되지 않았음" in events[0][1]["reason"]

    saved = ClaudeCliRunner.session_file(tmp_path, "deadbeef-0000")
    saved.parent.mkdir(parents=True)
    saved.write_text("{}", encoding="utf-8")
    calls.clear()
    ClaudeCliRunner(exe="claude").run(call)
    assert calls[0].resume_session == "deadbeef-0000" and calls[0].prompt == "[재개]"


def test_a_running_tool_is_not_a_stall(tmp_path):

    call = StageCall(stage="s", model=None, effort=None, system="s", prompt="p", cwd=tmp_path, timeout_s=60, stall_s=1)
    code, _ = run_process([sys.executable, "-c", "import time; time.sleep(3); print('done')"], call, None,
                          lambda line: None, busy=lambda: True)  # a long Bash command: silent but legitimate
    assert code == 0


def test_a_cli_that_never_reads_the_prompt_is_a_stall_not_a_broken_pipe(tmp_path):
    # the prompt is larger than a pipe buffer, the "CLI" never reads stdin and prints nothing
    events = []
    call = StageCall(stage="s", model=None, effort=None, system="s", prompt="p", cwd=tmp_path, timeout_s=60, stall_s=2,
                     on_event=lambda k, d: events.append((k, d)))
    t0 = time.monotonic()
    try:
        run_process([sys.executable, "-c", "import time; time.sleep(30)"], call, "x" * 300_000, lambda line: None)
        raise AssertionError("expected a stall")
    except RunnerError as e:
        assert e.kind == "stalled"
    assert time.monotonic() - t0 < 25
    assert any(k == "process_stopped" and d["why"] == "stalled" for k, d in events)


def test_a_cli_that_exits_before_reading_the_prompt_is_explained(tmp_path):
    call = StageCall(stage="s", model=None, effort=None, system="s", prompt="p", cwd=tmp_path, timeout_s=60)
    try:
        run_process([sys.executable, "-c", "import sys; sys.stderr.write('bad flag\n'); sys.exit(2)"], call,
                    "x" * 300_000, lambda line: None)
        raise AssertionError("expected an error")
    except RunnerError as e:
        assert e.kind == "error" and "프롬프트를 읽기 전에 끝남" in str(e) and "bad flag" in str(e)
