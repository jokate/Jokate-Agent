"""T1 of the 'owner decides less' campaign: a studio run is told of its spending, not stopped by it, and does not
wait at a human gate of the relay. The demo relay on the mock runner (test_studio_runs.Lab): no AI is called.
A run is a studio run when `engine.studio_mode` says so; the tests register `studio/notice.py` the way server.py does."""
import pytest

from relay_agent.runners import MockRunner, RunnerError
from relay_agent.studio import api, notice, push, work
from test_studio_runs import DEMO, Pricey, demo, lab, ok  # noqa: F401 - lab is a fixture


@pytest.fixture
def studio(lab):
    """The Lab's engine wired as server.py wires it; `lab.track(run, mode)` makes a run a studio run in the 1:1 room."""
    lab.engine.studio_mode, lab.engine.run_notice = notice.mode, notice.tell
    lab.engine.spend_notice_step_usd = 5.0
    tracked = []

    def track(run, mode="work"):
        api.track_run(run.id, mode=mode, employee_id=lab.world.emp, room_id=lab.world.dm)
        tracked.append(run.id)

    lab.track = track
    lab.said = lambda event="spend_notice": [
        m for m in lab.store.list_messages(lab.world.dm, limit=200)
        if m["author_kind"] == "system" and (m["meta"] or {}).get("event") == event]
    yield lab
    for run_id in tracked:
        api.untrack_run(run_id)


def advance(lab, relay=DEMO, mode="work", **create):
    run = lab.engine.create(relay, "로그인 폼 고치기", lab.tmp / "work", workspace="none", **create) \
        if (lab.tmp / "work").mkdir(exist_ok=True) is None else None
    if mode:
        lab.track(run, mode)
    return run, lab.engine.advance(run.id)


def test_a_studio_run_is_not_stopped_by_the_run_budget_and_files_no_budget_item(studio, tmp_path):
    studio.runner = Pricey()
    run, out = advance(studio, demo(tmp_path, max_run_cost_usd=0.01))
    assert out.status == "done" and studio.stages() == ["scout", "plan", "build", "review"]
    assert [i for i in studio.pending() if i["kind"] == "budget"] == []
    assert "budget_exceeded" not in studio.events(run) and "stage_budget_exceeded" not in studio.events(run)


def test_the_room_is_told_at_the_first_budget_and_at_every_step_and_never_twice(studio, tmp_path):
    studio.runner, studio.engine.spend_notice_step_usd = Pricey(), 0.03  # $0.02 a stage, a limit of $0.01
    run, out = advance(studio, demo(tmp_path, max_run_cost_usd=0.01))
    told = studio.said()
    # first budget: before plan ($0.02); the next $0.03 is reached after build ($0.06); review ends at $0.08
    assert [(m["meta"]["event"], m["meta"]["stage"], m["meta"]["spent_usd"]) for m in told] == [
        ("spend_notice", "plan", 0.02), ("spend_notice", "build", 0.06)]
    assert told[0]["body"] == "이 작업이 지금까지 $0.02 을 썼습니다(단계 plan)"
    assert studio.events(run).count("spend_notice") == 2
    saved = studio.engine.load(run.id)
    assert saved.spend_noticed_usd == 0.06
    studio.engine._spend_check(saved, "review", True)  # a run loaded again (server restart) does not tell again
    studio.engine._spend_check(saved, "review", False)
    assert len(studio.said()) == 2


def test_no_notice_without_a_budget_hit_or_a_step(studio, tmp_path):
    studio.runner = Pricey()
    advance(studio, demo(tmp_path, max_run_cost_usd=10))  # $0.08 in all: the default step is $5
    assert studio.said() == []


def test_a_stage_budget_is_doubled_without_asking_and_the_stage_goes_on(studio, tmp_path):
    caps = []

    class Capped(MockRunner):
        def run(self, call):
            caps.append(call.max_budget_usd)
            if len(caps) == 1:
                error = RunnerError("단계 예산 $0.4 도달", "budget")
                error.cost_usd = 0.41
                raise error
            return super().run(call)

    studio.runner = Capped()
    run, out = advance(studio, demo(tmp_path, scout={"max_budget_usd": 0.4}))
    assert out.status == "done" and caps[:2] == [0.4, 0.8]
    assert out.stage_budget_boost == {"scout": 2.0} and out.pending_budget_stage is None
    assert [i for i in studio.pending() if i["kind"] == "budget"] == []
    extended = [e for e in studio.engine.history.events(run.id) if e["kind"] == "budget_auto_extended"]
    assert extended and extended[0]["detail"]["studio"] is True
    assert [m["meta"]["stage"] for m in studio.said()] == ["scout"]  # the first budget: told once


def test_a_task_run_passes_a_human_gate_and_leaves_the_stage_summary(studio):
    run, out = advance(studio, approval="always")
    assert out.status == "done" and "awaiting_approval" not in studio.events(run)
    skipped = [e for e in studio.engine.history.events(run.id) if e["kind"] == "gate_skipped"]
    assert skipped and skipped[0]["detail"]["reason"] == "스튜디오 작업: 설계 승인 넘김"
    [passed] = studio.said("gate_passed")
    assert passed["meta"]["stage"] == "plan" and passed["body"] == "`plan` 단계를 승인 없이 넘겼습니다 — plan 완료(mock)"
    assert studio.pending() == []


def test_a_chat_turn_does_not_skip_the_gate(studio):
    run, out = advance(studio, mode="chat", approval="always")
    assert out.status == "awaiting_approval" and out.baton.stop.kind == "awaiting_approval"


def test_a_run_outside_studio_keeps_its_budget_stop_and_its_gate(studio, tmp_path):
    studio.runner = Pricey()
    run, out = advance(studio, demo(tmp_path, max_run_cost_usd=0.01), mode=None)  # never tracked: not studio's
    assert out.status == "awaiting_approval" and out.baton.stop.kind == "budget"
    assert [i["kind"] for i in studio.pending()] == ["budget"] and studio.said() == []
    studio.runner = MockRunner()
    other = studio.engine.create(DEMO, "다른 일", studio.tmp / "work2", workspace="none", approval="always") \
        if (studio.tmp / "work2").mkdir() is None else None
    assert studio.engine.advance(other.id).baton.stop.kind == "awaiting_approval"


def test_the_spend_notice_is_pushed_quietly_and_the_gate_notice_not_at_all(studio, tmp_path):
    studio.runner = Pricey()
    advance(studio, demo(tmp_path, max_run_cost_usd=0.01), approval="always")
    spend = studio.said()[0]
    grade, title, line, link = push._of_message({"message": spend}, studio.store)
    assert (grade, title) == (push.QUIET, "예산 안내") and "을 썼습니다" in line and link == {"type": "room", "id": studio.world.dm}
    [passed] = studio.said("gate_passed")
    assert push._of_message({"message": passed}, studio.store) is None


def test_the_start_closes_an_open_budget_item_and_the_run_goes_on(studio, tmp_path):
    studio.runner = Pricey()
    run = studio.run(demo(tmp_path, max_run_cost_usd=0.01))  # before this change: a budget stop with its item
    [item] = studio.pending()
    hold = studio.store.create_inbox_item("budget", "사용량 보류", "system", payload={
        "scope": "run", "stage": "", "spent_usd": 0, "limit_usd": 0, "stop_reason": "usage_hold", "key": "k"})
    studio.track(run)  # the server now knows it as a studio run, and the engine too
    out = work.recover(studio.engine, lambda: studio.world.box)
    assert out["released"] == [item["id"]]
    assert studio.world.item(item)["status"] == "decided" and studio.world.item(item)["decision"] == "approve"
    assert studio.world.item(hold)["status"] == "pending"  # V-4's hold is not a run's stop
    assert studio.engine.load(run.id).status == "done"
    assert [i["id"] for i in studio.pending()] == [hold["id"]]
