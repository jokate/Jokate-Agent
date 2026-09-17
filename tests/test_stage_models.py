"""Per-stage model picks: only usable AIs, write stages need file tools, the pick wins over retry escalation."""
import pytest

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.providers import ProviderRegistry
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore

RELAY = """
name: r
workspace: none
stages:
  - {name: plan, provider: mock, model: fable, prompt: role.md}
  - {name: build, provider: mock, model: sonnet, retry_model: fable, prompt: role.md, tools: [Edit]}
"""


class Recorder(MockRunner):
    def __init__(self, calls):
        super().__init__()
        self.seen = calls

    def run(self, call):
        self.seen.append((call.stage, call.model))
        return super().run(call)


def make(tmp_path, providers):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(RELAY, encoding="utf-8")
    calls = []
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Recorder(calls), providers=providers)
    return engine, relay, calls


def registry(tmp_path):
    return ProviderRegistry({"mock": {"kind": "mock", "tools": True},
                             "chat": {"kind": "mock", "tools": False},
                             "ghost": {"kind": "cli", "command": ["no-such-cli-xyz"], "tools": True}},
                            [], history=HistoryStore(tmp_path / "p.sqlite"))


def test_pick_overrides_relay_model(tmp_path):
    engine, relay, calls = make(tmp_path, registry(tmp_path))
    run = engine.create(relay, "g", tmp_path, stage_models={"plan": {"model": "opus"}, "build": {}})
    assert run.stage_models == {"plan": {"provider": "mock", "model": "opus"}}
    engine.advance(run.id)
    assert calls == [("plan", "opus"), ("build", "sonnet")]


def test_unknown_stage_unavailable_ai_and_toolless_writer_are_rejected(tmp_path):
    engine, relay, _ = make(tmp_path, registry(tmp_path))
    with pytest.raises(ValueError, match="없는 단계"):
        engine.create(relay, "g", tmp_path, stage_models={"nope": {"model": "opus"}})
    with pytest.raises(ValueError, match="ghost"):
        engine.create(relay, "g", tmp_path, stage_models={"plan": {"provider": "ghost"}})
    with pytest.raises(ValueError, match="파일을 수정할 수 없어"):
        engine.create(relay, "g", tmp_path, stage_models={"build": {"provider": "chat"}})


def test_catalog_lists_only_usable_providers(tmp_path, monkeypatch):
    from relay_agent.providers import ProviderSpec

    monkeypatch.setattr(ProviderSpec, "availability", lambda self: (self.name == "claude", "no"))
    reg = ProviderRegistry({}, [], history=HistoryStore(tmp_path / "p.sqlite"))
    reg.mark_model_exhausted("claude", "fable", None, "limit")
    cat = {c["provider"]: c for c in reg.catalog()}
    assert list(cat) == ["claude"]
    models = {m["model"]: m for m in cat["claude"]["models"]}
    assert list(models) == ["fable", "opus", "sonnet", "haiku"]
    assert models["fable"]["exhausted_until"] and not models["opus"]["exhausted_until"]
    assert models["opus"]["tier"] == "고급"
