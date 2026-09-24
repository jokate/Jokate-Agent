from datetime import datetime, timezone
from types import SimpleNamespace

from relay_agent.runners import (AnthropicApiRunner, ClaudeCliRunner, StageCall, Usage, estimate_cost,
                                 refusal_fallback)


def listed(*ids_by_date):
    return [SimpleNamespace(id=i, created_at=datetime(2026, 1, 1 + n, tzinfo=timezone.utc))
            for n, i in enumerate(ids_by_date)]


class FakeModels:
    def __init__(self, models=None, error=None):
        self.models, self.error, self.calls = models or [], error, 0

    def list(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(reversed(self.models))  # order from the API is not relied on


def runner(models=None, error=None, model_map=None):
    client = SimpleNamespace(models=FakeModels(models, error))
    return AnthropicApiRunner(client=client, model_map=model_map), client.models


def test_api_alias_resolves_to_the_newest_model_of_its_tier():
    r, models = runner(listed("claude-opus-5", "claude-sonnet-5", "claude-mythos-5-1", "claude-opus-5-5", "claude-opus-6"))
    assert r.resolve_model("opus") == "claude-opus-6"  # a release after this code still gets picked up
    assert r.resolve_model("sonnet") == "claude-sonnet-5"
    assert r.resolve_model(None) == "claude-opus-6"
    assert r.resolve_model("claude-opus-5") == "claude-opus-5"  # a full id is used as given
    assert models.calls == 1  # listed once per runner


def test_api_alias_falls_back_when_models_cannot_be_listed_and_model_map_pins():
    r, _ = runner(error=RuntimeError("offline"))
    assert r.resolve_model("opus") == "claude-opus-5-5"
    assert r.resolve_model("fable") == "claude-fable-5-1"
    pinned, models = runner(listed("claude-opus-6"), model_map={"opus": "claude-opus-5-5"})
    assert pinned.resolve_model("opus") == "claude-opus-5-5" and models.calls == 0


def test_cli_passes_aliases_through_unless_pinned(tmp_path):
    call = StageCall(stage="s", model="opus", effort=None, system="", prompt="", cwd=tmp_path, fallback_model="sonnet")
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert args[args.index("--model") + 1] == "opus"  # Claude Code maps the alias to its newest Opus
    pinned = ClaudeCliRunner(exe="claude", model_map={"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5"})
    args = pinned.build_args(call, None)
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--fallback-model") + 1] == "claude-sonnet-5"


def test_cost_and_refusal_fallback_for_known_and_newer_models():
    assert round(estimate_cost(Usage("anthropic_api", "claude-opus-5-5", input_tokens=1_000_000)), 2) == 4.0
    assert round(estimate_cost(Usage("anthropic_api", "claude-opus-6", input_tokens=1_000_000)), 2) == 4.0  # tier price
    assert refusal_fallback("claude-opus-5-5") and refusal_fallback("claude-fable-5-1")
    assert not refusal_fallback("claude-opus-4-8") and not refusal_fallback("claude-sonnet-5")
