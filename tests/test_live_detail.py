"""What the liveness bar shows while a stage runs, and run.json saves that survive a locked target."""
import os

from relay_agent import pipeline
from relay_agent.runners import READ_ONLY_BASH, ClaudeCliRunner, LiveDetail, StageCall


def chunk(kind, **fields):
    return {"type": "stream_event", "event": {"type": kind, **fields}}


def test_live_detail_follows_thinking_tool_and_result():
    d = LiveDetail()
    d.on_event(chunk("content_block_start", content_block={"type": "thinking"}))
    p = d.on_event(chunk("content_block_delta", delta={"type": "thinking_delta", "thinking": "쿨다운 위치를 찾자"}))
    assert p["doing"] == "thinking" and p["snippet"] == "쿨다운 위치를 찾자"
    d.on_event(chunk("content_block_start", content_block={"type": "tool_use", "name": "Bash"}))
    p = d.on_event(chunk("content_block_delta", delta={"type": "input_json_delta", "partial_json": '{"command": "py'}))
    assert p["doing"] == "tool_input" and p["tool"] == "Bash" and p["snippet"].endswith('"py')
    p = d.on_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "python Tools/mnys_q.py find Dash"}}]}})
    assert p["doing"] == "tool" and p["target"] == "python Tools/mnys_q.py find Dash" and p["tool_since"]
    p = d.on_event({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    assert p["doing"] == "tool_result" and p["tool"] is None and p["snippet"] == ""
    d.on_event(chunk("content_block_start", content_block={"type": "text"}))
    d.on_event(chunk("content_block_delta", delta={"type": "text_delta", "text": "x" * 500}))
    assert len(d.on_event(chunk("content_block_delta", delta={"type": "text_delta", "text": "끝"}))["snippet"]) == LiveDetail.TAIL


def test_cli_args_stream_partials_and_preapprove_read_only_shell(tmp_path):
    call = StageCall(stage="s", model=None, effort=None, system="s", prompt="p", cwd=tmp_path, tools=["Read", "Bash"],
                     allowed_tools=["Bash(uv run pytest:*)"])
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert "--include-partial-messages" in args
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "Bash(uv run pytest:*)" in allowed and all(r in allowed for r in READ_ONLY_BASH)
    no_bash = ClaudeCliRunner(exe="claude").build_args(StageCall(stage="s", model=None, effort=None, system="s",
                                                                 prompt="p", cwd=tmp_path, tools=["Read"]), None)
    assert "--allowedTools" not in no_bash


def test_save_replace_retries_then_falls_back(tmp_path, monkeypatch):
    tmp, dst = tmp_path / "run.json.tmp", tmp_path / "run.json"
    dst.write_text("old", encoding="utf-8")
    tmp.write_text("new", encoding="utf-8")
    calls = {"n": 0}
    real = os.replace

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "액세스가 거부되었습니다")
        real(a, b)

    monkeypatch.setattr(pipeline.os, "replace", flaky)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    pipeline._replace(tmp, dst)
    assert dst.read_text(encoding="utf-8") == "new" and not tmp.exists() and calls["n"] == 3

    tmp.write_text("newer", encoding="utf-8")
    monkeypatch.setattr(pipeline.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "locked")))
    pipeline._replace(tmp, dst)  # never succeeds: written in place instead of failing the run
    assert dst.read_text(encoding="utf-8") == "newer" and not tmp.exists()


def test_liveness_reads_the_pulse_file_of_a_run_owned_by_another_process(tmp_path):
    import json
    import os
    import time

    from relay_agent.history import HistoryStore
    from relay_agent.pipeline import RelayEngine
    from relay_agent.runners import MockRunner
    from relay_agent.usage import UsageStore

    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nstages:\n  - {name: build, provider: mock, prompt: role.md}\n", encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: MockRunner())
    run = engine.create(relay, "g", tmp_path)
    # as a `relay run` in a terminal would leave it: running, owned by a live process that is not this one
    run.status, run.owner_pid = "running", os.getppid()
    engine.save(run)
    engine._on_stage_event(run.id, "build")("pulse", {"doing": "tool", "tool": "Bash", "target": "pytest -q",
                                                      "tool_since": time.time() - 30, "snippet": "", "tools": 1})
    engine._pulse.clear()  # the other process's memory is not ours; only its pulse.json is
    assert json.loads((tmp_path / "runs" / run.id / "pulse.json").read_text(encoding="utf-8"))["tool"] == "Bash"
    live = engine.liveness(run.id)
    assert live["alive"] and live["precise"] and live["doing"] == "tool" and live["target"] == "pytest -q"
    assert 29 <= live["tool_seconds"] <= 35
