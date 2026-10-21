"""T1.1: the stage-budget boost has a ceiling, a relay without a run limit is told too, and a chat run keeps its
studio mark across a server restart. Same Lab and `studio` fixture as test_studio_spend."""
import pytest

from relay_agent.runners import MockRunner, RunnerError
from relay_agent.studio import notice, work
from test_studio_runs import Pricey, demo, lab  # noqa: F401 - lab is a fixture
from test_studio_spend import advance, studio  # noqa: F401 - studio is a fixture


def test_a_stage_budget_boost_stops_doubling_at_the_ceiling_and_the_run_goes_on(studio, tmp_path):
    caps = []

    class Greedy(MockRunner):
        def run(self, call):
            caps.append(call.max_budget_usd)
            if len(caps) <= 5:
                error = RunnerError("단계 예산 도달", "budget")
                error.cost_usd = 0.01
                raise error
            return super().run(call)

    studio.runner = Greedy()
    run, out = advance(studio, demo(tmp_path, scout={"max_budget_usd": 0.4}))
    assert out.status == "done" and caps[:6] == pytest.approx([0.4, 0.8, 1.6, 3.2, 3.2, 3.2])
    assert out.stage_budget_boost == {"scout": 8.0}
    assert [i for i in studio.pending() if i["kind"] == "budget"] == []
    extended = [e["detail"] for e in studio.engine.history.events(run.id) if e["kind"] == "budget_auto_extended"]
    assert [d["capped"] for d in extended] == [False, False, True, True, True]


def test_a_relay_without_a_run_limit_tells_from_the_first_step_on(studio, tmp_path):
    studio.runner, studio.engine.spend_notice_step_usd = Pricey(), 0.03
    run, out = advance(studio, demo(tmp_path, max_run_cost_usd=0))
    told = studio.said()
    assert out.status == "done" and told and all(m["meta"]["spent_usd"] >= 0.03 for m in told)
    spent = [m["meta"]["spent_usd"] for m in told]
    assert all(b - a >= 0.03 - 1e-9 for a, b in zip(spent, spent[1:]))
    assert [i for i in studio.pending() if i["kind"] == "budget"] == []


def test_a_relay_without_a_run_limit_is_quiet_below_the_step(studio, tmp_path):
    studio.runner = Pricey()  # $0.08 in all, the step is $5
    advance(studio, demo(tmp_path, max_run_cost_usd=0))
    assert studio.said() == []


def test_the_studio_mode_is_stored_in_the_run(studio):
    run, out = advance(studio, mode="chat")
    assert studio.engine.load(run.id).studio_mode == "chat"


def test_a_run_outside_studio_stores_no_mode(studio):
    run, out = advance(studio, mode=None)
    assert studio.engine.load(run.id).studio_mode is None


def test_after_a_restart_a_stored_chat_run_gets_its_budget_item_closed_and_goes_on(studio, tmp_path):
    studio.runner = Pricey()
    run = studio.run(demo(tmp_path, max_run_cost_usd=0.01))  # a budget stop with a task-less item, never tracked
    [item] = studio.pending()
    saved = studio.engine.load(run.id)
    saved.studio_mode = "chat"  # what a tracked chat turn left behind before the restart
    studio.engine.save(saved)
    assert notice.mode(run.id) is None
    out = work.recover(studio.engine, lambda: studio.world.box)
    assert out["released"] == [item["id"]]
    assert studio.world.item(item)["decision"] == "approve"
    after = studio.engine.load(run.id)
    # past the budget stop: it now waits only at the chat turn's human gate (a chat turn does not skip it)
    assert after.baton.stop.kind == "awaiting_approval" and after.baton.stop.stage == "plan"


def test_after_a_restart_a_run_outside_studio_keeps_its_budget_item(studio, tmp_path):
    studio.runner = Pricey()
    run = studio.run(demo(tmp_path, max_run_cost_usd=0.01))
    [item] = studio.pending()
    out = work.recover(studio.engine, lambda: studio.world.box)
    assert out["released"] == [] and studio.world.item(item)["status"] == "pending"
    assert studio.engine.load(run.id).status == "awaiting_approval"


def test_an_auto_approval_studio_run_is_still_told_at_the_limit(studio, tmp_path):
    studio.runner = Pricey()
    run, out = advance(studio, demo(tmp_path, max_run_cost_usd=0.01), approval="auto")
    assert out.status == "done" and [m["meta"]["stage"] for m in studio.said()][:1] == ["plan"]
    assert studio.engine.load(run.id).budget_extra_usd == 0.0
