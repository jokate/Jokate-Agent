"""A stage that hits its own cost cap pauses for approval (not a failure); CLI error results are explained."""
import pytest

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import ClaudeCliRunner, MockRunner, RunnerError, StageCall
from relay_agent.usage import UsageStore


def test_stage_budget_pauses_then_approval_doubles_the_cap(tmp_path):
    caps = []

    class Capped(MockRunner):
        def run(self, call):
            caps.append(call.max_budget_usd)
            if len(caps) == 1:
                err = RunnerError("단계 예산 $0.4 도달", "budget")
                err.cost_usd = 0.41
                raise err
            return super().run(call)

    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nstages:\n  - {name: scout, provider: mock, prompt: role.md, max_budget_usd: 0.4}\n"
                     "  - {name: build, provider: mock, prompt: role.md}\n", encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Capped())
    run = engine.advance(engine.create(relay, "big project", tmp_path).id)
    assert run.status == "awaiting_approval" and run.pending_budget_stage == "scout"
    assert run.baton.stop.kind == "stage_budget" and "2배" in run.baton.stop.resume_hint
    assert engine.usage.summary(run.id)[0]["cost_usd"] == 0.41  # the spend is still accounted for

    engine.approve(run.id)
    done = engine.advance(run.id)
    assert done.status == "done" and caps == [0.4, 0.8, None]


@pytest.mark.parametrize("result,expected_kind,expected_text", [
    ({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "terminal_reason": "budget_exhausted",
      "errors": ["Reached maximum budget ($0.2)"], "total_cost_usd": 0.21, "num_turns": 9}, "budget", "단계 예산"),
    ({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 30}, "error", "최대 턴 수"),
    ({"type": "result", "subtype": "error_during_execution", "is_error": True}, "error", "error_during_execution"),
    ({"type": "result", "subtype": "success", "is_error": False, "result": None, "stop_reason": "end_turn", "num_turns": 3},
     "error", "결과 없음"),
])
def test_cli_error_results_are_explained_not_none(tmp_path, monkeypatch, result, expected_kind, expected_text):
    import json

    import relay_agent.runners as runners

    def fake_run_process(args, call, stdin_text, on_line, live=None):
        on_line(json.dumps(result) + "\n")
        return 1, ""

    monkeypatch.setattr(runners, "run_process", fake_run_process)
    call = StageCall(stage="scout", model="haiku", effort=None, system="", prompt="", cwd=tmp_path, max_budget_usd=0.2)
    with pytest.raises(RunnerError) as err:
        ClaudeCliRunner(exe="claude")._run_once(call, "haiku")
    assert err.value.kind == expected_kind and expected_text in str(err.value)
    assert "None" not in str(err.value)
