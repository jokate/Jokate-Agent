import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay_agent import cc_import
from relay_agent.baton import StageResult
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.providers import ProviderRegistry, ProviderSpec
from relay_agent.runners import ExternalCliRunner, MockRunner, RunnerError, StageCall, Usage, extract_result
from relay_agent.usage import UsageStore

OK = {"summary": "ok", "state": "s", "open_issues": [], "next_steps": [], "output": "out"}


def engine_with(tmp_path: Path, relay_yaml: str, runners: dict, providers: dict | None = None, chain=None):
    (tmp_path / "role.md").write_text("role", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(relay_yaml, encoding="utf-8")
    usage, history = UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite")
    registry = ProviderRegistry(providers or {}, chain, usage=usage, history=history)
    engine = RelayEngine(tmp_path / "runs", usage, history, runner_factory=lambda name: runners[name],
                         providers=registry)
    return engine, relay


class Named(MockRunner):
    def __init__(self, name, script=None, delay_s=0.0):
        super().__init__(script, delay_s)
        self.name = name

    def run(self, call):
        res, usage = super().run(call)
        usage.runner = self.name
        return res, usage


MOCKISH = {"kind": "mock", "tools": True}


def test_quota_error_switches_provider_and_marks_exhausted(tmp_path):
    claude = Named("claude", {"s": [RunnerError("You've hit your usage limit", "quota")]})
    codex = Named("codex")
    engine, relay = engine_with(tmp_path, """
name: r
stages:
  - {name: s, provider: claude, model: sonnet, prompt: role.md, alternates: [{provider: codex, model: gpt-x}]}
""", {"claude": claude, "codex": codex}, {"claude": MOCKISH, "codex": MOCKISH})
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "done" and run.history[0].runner == "codex"
    assert codex.calls[0].model == "gpt-x" and codex.calls[0].fallback_model is None
    kinds = [e["kind"] for e in engine.history.events(run.id)]
    assert "provider_exhausted" in kinds and "provider_switch" in kinds
    assert engine.providers.status("claude")["available"] is False  # cooling down


def test_reported_usage_window_switches_before_calling(tmp_path):
    claude, codex = Named("claude"), Named("codex")
    engine, relay = engine_with(tmp_path, """
name: r
stages:
  - {name: s, provider: claude, prompt: role.md, alternates: [{provider: codex}]}
""", {"claude": claude, "codex": codex}, {"claude": MOCKISH, "codex": MOCKISH})
    reset = (datetime.now(timezone.utc) + timedelta(hours=2)).timestamp()
    engine.history.record_limits("claude", "allowed", [{"window": "five_hour", "utilization": 0.97, "resets_at": reset}])
    status = engine.providers.status("claude")
    assert not status["available"] and "five_hour" in status["reason"] and status["limits"][0]["utilization"] == 0.97

    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "done" and not claude.calls and codex.calls


def test_model_specific_or_overage_windows_do_not_bench_the_provider(tmp_path):
    engine, _ = engine_with(tmp_path, "name: r\nstages: []\n", {}, {"claude": MOCKISH})
    reset = (datetime.now(timezone.utc) + timedelta(days=3)).timestamp()
    engine.history.record_limits("claude", "allowed", [
        {"window": "five_hour", "utilization": 0.30, "resets_at": reset},
        {"window": "seven_day", "utilization": 0.60, "resets_at": reset},
        {"window": "seven_day_overage", "utilization": 1.0, "resets_at": reset},
        {"window": "seven_day_fable", "utilization": 0.99, "resets_at": reset},
    ])
    status = engine.providers.status("claude")
    assert status["available"] is True
    assert {w["window"]: w["gating"] for w in status["limits"]} == {
        "five_hour": True, "seven_day": True, "seven_day_overage": False, "seven_day_fable": False}

    engine.history.record_limits("claude", "allowed", [{"window": "seven_day", "utilization": 0.97, "resets_at": reset}])
    assert engine.providers.status("claude")["available"] is False  # overall usage still switches


def _quota(model, limit_type):
    err = RunnerError(f"{model} limit reached", "quota")
    err.model, err.limit_type, err.resets_at = model, limit_type, None
    return err


class ModelLimited(Named):
    """Claude where only some models are out of usage."""

    def __init__(self, out: set[str], limit_type="seven_day_fable"):
        super().__init__("claude")
        self.out, self.limit_type, self.models = out, limit_type, []

    def run(self, call):
        self.models.append(call.model)
        if call.model in self.out:
            raise _quota(call.model, self.limit_type)
        return super().run(call)


def test_fable_only_limit_steps_down_on_claude_and_is_remembered(tmp_path):
    claude, codex = ModelLimited({"fable"}), Named("codex")
    engine, relay = engine_with(tmp_path, """
name: r
stages:
  - {name: plan, provider: claude, model: fable, fallback_model: opus, prompt: role.md, alternates: [{provider: codex}]}
  - {name: review, provider: claude, model: fable, prompt: role.md, alternates: [{provider: codex}]}
""", {"claude": claude, "codex": codex}, {"claude": MOCKISH, "codex": MOCKISH})
    reset = (datetime.now(timezone.utc) + timedelta(days=2)).timestamp()
    engine.history.record_limits("claude", "allowed", [{"window": "five_hour", "utilization": 0.05, "resets_at": reset},
                                                       {"window": "seven_day", "utilization": 0.08, "resets_at": reset}])
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "done" and not codex.calls  # never left Claude
    assert claude.models == ["fable", "opus", "opus"]  # second stage skips Fable up front
    kinds = [e["kind"] for e in engine.history.events(run.id)]
    assert "model_exhausted" in kinds and "model_substituted" in kinds and "provider_exhausted" not in kinds
    status = engine.providers.status("claude")
    assert status["available"] and status["exhausted_models"][0]["model"] == "fable"


def test_overall_limit_still_switches_to_another_ai(tmp_path):
    claude, codex = ModelLimited({"fable", "opus", "sonnet"}, limit_type="seven_day"), Named("codex")
    engine, relay = engine_with(tmp_path, """
name: r
stages:
  - {name: plan, provider: claude, model: fable, prompt: role.md, alternates: [{provider: codex}]}
""", {"claude": claude, "codex": codex}, {"claude": MOCKISH, "codex": MOCKISH})
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "done" and claude.models == ["fable"] and codex.calls
    assert "provider_exhausted" in [e["kind"] for e in engine.history.events(run.id)]


def test_every_ladder_model_out_then_switches(tmp_path):
    claude, codex = ModelLimited({"fable", "opus", "sonnet"}), Named("codex")
    engine, relay = engine_with(tmp_path, """
name: r
stages:
  - {name: plan, provider: claude, model: fable, prompt: role.md, alternates: [{provider: codex}]}
""", {"claude": claude, "codex": codex}, {"claude": MOCKISH, "codex": MOCKISH})
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "done" and claude.models == ["fable", "opus", "sonnet"] and codex.calls


def test_expired_window_no_longer_blocks(tmp_path):
    engine, _ = engine_with(tmp_path, "name: r\nstages: []\n", {}, {"claude": MOCKISH})
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
    engine.history.record_limits("claude", "allowed", [{"window": "five_hour", "utilization": 1.0, "resets_at": past}])
    assert engine.providers.status("claude")["available"] is True


def test_daily_budget_and_write_tools_filter_candidates(tmp_path):
    engine, _ = engine_with(tmp_path, "name: r\nstages: []\n", {}, {
        "claude": {**MOCKISH, "daily_usd": 1.0},
        "reader": {"kind": "openai", "base_url": "http://localhost:1/v1"},  # text-only API: can't edit files
        "codex": MOCKISH,
    })
    engine.usage.record("x", "s", Usage("claude", "m", cost_usd=1.5))
    usable, skipped = engine.providers.candidates("claude", [{"provider": "reader"}, {"provider": "codex"}], True)
    assert [u["provider"] for u in usable] == ["codex"]
    assert {s["provider"] for s in skipped} == {"claude", "reader"}


def test_optional_stage_skipped_when_no_provider(tmp_path):
    engine, relay = engine_with(tmp_path, """
name: r
stages:
  - {name: a, provider: claude, prompt: role.md}
  - {name: cross, provider: nothere, prompt: role.md, optional: true}
""", {"claude": Named("claude")}, {"claude": MOCKISH, "nothere": {"kind": "cli", "command": ["definitely-not-installed-xyz"]}})
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "done"
    assert "stage_skipped" in [e["kind"] for e in engine.history.events(run.id)]


def test_cancel_running_relay_then_resume(tmp_path):
    slow = Named("claude", delay_s=5)
    engine, relay = engine_with(tmp_path, "name: r\nstages:\n  - {name: a, provider: claude, prompt: role.md}\n",
                                {"claude": slow}, {"claude": MOCKISH})
    run = engine.create(relay, "g", tmp_path)
    t = threading.Thread(target=engine.advance, args=(run.id,))
    t.start()
    for _ in range(50):
        if engine.load(run.id).status == "running":
            break
        time.sleep(0.05)
    started = time.monotonic()
    engine.cancel(run.id)
    t.join(5)
    assert time.monotonic() - started < 2
    assert engine.load(run.id).status == "cancelled"
    slow.delay_s = 0
    assert engine.advance(run.id, resume=True).status == "done"


def test_recover_interrupted_runs(tmp_path):
    engine, relay = engine_with(tmp_path, "name: r\nstages:\n  - {name: a, provider: claude, prompt: role.md}\n",
                                {"claude": Named("claude")}, {"claude": MOCKISH})
    run = engine.create(relay, "g", tmp_path)
    run.status = "running"
    engine.save(run)
    assert engine.recover_interrupted() == [run.id]
    assert engine.load(run.id).status == "failed"


def test_copy_workspace_run_returns_patch(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("old\n", encoding="utf-8")

    class Editor(Named):
        def run(self, call):
            (call.cwd / "a.txt").write_text("new\n", encoding="utf-8")
            return super().run(call)

    engine, relay = engine_with(tmp_path, """
name: r
workspace: copy
stages:
  - {name: build, provider: claude, prompt: role.md, tools: [Edit]}
""", {"claude": Editor("claude")}, {"claude": MOCKISH})
    run = engine.advance(engine.create(relay, "g", project).id)
    assert run.status == "done" and run.changes_status == "ready" and run.changes["files"] == 1
    assert (project / "a.txt").read_text(encoding="utf-8") == "old\n"
    assert "+new" in engine.patch_text(run.id)
    engine.apply_changes(run.id)
    assert (project / "a.txt").read_text(encoding="utf-8") == "new\n"
    assert engine.load(run.id).changes_status == "applied"


def test_external_cli_runner_with_fake_cli(tmp_path):
    fake = tmp_path / "fakecli.py"
    fake.write_text(
        "import sys, json\n"
        "prompt = sys.stdin.read()\n"
        "assert 'JSON' in prompt\n"
        "print(json.dumps({'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 42.0, 'window_minutes': 300, 'resets_in_seconds': 60}}}))\n"
        "open(sys.argv[sys.argv.index('--out') + 1], 'w', encoding='utf-8').write('done. ' + json.dumps("
        "{'summary': 'fake ok', 'state': 's', 'open_issues': [], 'next_steps': [], 'output': 'x'}))\n",
        encoding="utf-8",
    )
    spec = ProviderSpec(name="fake", kind="cli", command=[sys.executable, str(fake), "--out", "{out_file}"],
                        model_args=["--model", "{model}"], prices={"m1": [1.0, 2.0]})
    seen = []
    call = StageCall(stage="s", model="m1", effort=None, system="ROLE", prompt="BATON", cwd=tmp_path,
                     on_event=lambda k, d: seen.append((k, d)))
    result, usage = ExternalCliRunner(spec).run(call)
    assert result.summary == "fake ok" and usage.runner == "fake" and usage.estimated and usage.cost_usd > 0
    limits = [d for k, d in seen if k == "rate_limit"][0]
    assert limits["windows"][0]["window"] == "5h" and limits["windows"][0]["utilization"] == 0.42


def test_extract_result_takes_last_valid_json():
    text = 'thinking {"a": 1} then {"summary": "first", "state": "s", "open_issues": [], "next_steps": []} ' \
           'final {"summary": "last", "state": "s", "open_issues": [], "next_steps": [], "output": "o"}'
    assert extract_result(text).summary == "last"


def test_claude_code_import_keeps_prompts_and_short_answers(tmp_path, monkeypatch):
    project = tmp_path / "C--work"
    project.mkdir()
    lines = [
        {"type": "custom-title", "customTitle": "작업 A"},
        {"type": "user", "cwd": "C:\\work", "timestamp": "2026-09-17T01:00:00Z", "message": {"content": "첫 질문"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file body"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "답변 " + "x" * 600}]}},
        {"type": "user", "isMeta": True, "message": {"content": "<system-reminder>noise"}},
        {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "user", "timestamp": "2026-09-17T01:05:00Z", "message": {"content": "두 번째 질문"}},
    ]
    (project / "abc12345-0000.jsonl").write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines), encoding="utf-8")
    monkeypatch.setattr(cc_import, "PROJECTS_DIR", tmp_path)

    history = HistoryStore(tmp_path / "h.sqlite")
    result = cc_import.import_session(history, "C--work", "abc12345-0000")
    assert result["imported"] == 2
    turns = history.turns(result["session_id"])
    assert [t["question"] for t in turns] == ["첫 질문", "두 번째 질문"]
    assert turns[0]["status"] == "imported" and turns[0]["result"].startswith("답변") and len(turns[0]["result"]) <= 401
    assert history.get_session(result["session_id"])["title"] == "[Claude Code] 작업 A"
    # re-import adds nothing new; the session context now carries the conversation in two lines
    assert cc_import.import_session(history, "C--work", "abc12345-0000")["imported"] == 0
    assert len(history.session_context(result["session_id"])) == 2
    assert cc_import.latest_session_for_cwd("C:\\work") == ("C--work", "abc12345-0000")
